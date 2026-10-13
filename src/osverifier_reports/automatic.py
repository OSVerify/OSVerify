"""Post-verification publishing; never changes verification inputs or decisions."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from osverifier.backend import stop_process
from osverifier.quota import QuotaExhausted, record_quota
from osverifier.storage import atomic_json, atomic_text, read_json, run_lock

from .writer import REPORT_FORMAT_VERSION, report_writer, write_report


def report_directory(run_dir: Path) -> Path:
    run_dir = run_dir.resolve()
    digest = hashlib.sha256((run_dir / "report.json").read_bytes()).hexdigest()
    base = run_dir.parent / (run_dir.name + "-reports") / digest
    # AI reader exports manage their own reader-version subdirectory.
    if read_json(run_dir / "report.json").get("field") == "ai":
        return base
    return base / REPORT_FORMAT_VERSION


class CompilerUnavailable(RuntimeError):
    """No TeX engine ran, so the approved text is not known to be uncompilable."""


def compile_pdf(tex: Path) -> Path:
    """Compile in an isolated working directory with restricted TeX file access."""
    engine = shutil.which("xelatex") or shutil.which("lualatex")
    if not engine and Path("/Library/TeX/texbin/xelatex").is_file():
        engine = "/Library/TeX/texbin/xelatex"
    if not engine:
        raise CompilerUnavailable("PDF requires XeLaTeX or LuaLaTeX (TeX Live/MacTeX). Install it and rerun; the approved .tex is preserved.")
    with tempfile.TemporaryDirectory(prefix="osverifier-pdf-") as work:
        cwd = Path(work)
        shutil.copyfile(tex, cwd / "report.tex")
        env = {**os.environ, "openin_any": "p", "openout_any": "p", "TEXMFOUTPUT": work}
        with (tex.parent / "latex.log").open("w") as log:
            for _ in range(2):
                process = subprocess.Popen(
                    [engine, "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "report.tex"],
                    cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, start_new_session=True,
                )
                try:
                    code = process.wait(timeout=90)
                except BaseException:
                    stop_process(process)
                    raise
                if code:
                    raise RuntimeError(f"PDF compilation failed; see {tex.parent / 'latex.log'}")
        pdf = cwd / "report.pdf"
        if not pdf.is_file() or not pdf.read_bytes().startswith(b"%PDF-"):
            raise RuntimeError("LaTeX did not produce a PDF")
        destination = tex.with_suffix(".pdf")
        shutil.copyfile(pdf, destination)
        return destination


def publish(run_dir: Path, backend) -> Path:
    """Publish only text that compiled, and retain failed editorial/compile attempts.

    report.tex is the published artifact, written only after it compiles. Approved
    text waits in pending-report.tex only while no TeX engine is available. Text
    that a compiler rejected stays in its attempt directory as a diagnostic and is
    never reused: the next retry drafts and audits a new report.
    """
    source = read_json(Path(run_dir) / "report.json")
    if source.get("review_kind") == "paper" and source.get("classification_status") == "pending":
        raise ValueError("Wait for paper severity classification to finish")
    directory = report_directory(run_dir)
    with run_lock(directory):
        status_file = directory / "status.json"
        if status_file.exists() and read_json(status_file).get("status") == "completed" and (directory / "report.pdf").is_file():
            return directory / "report.pdf"
        published, pending = directory / "report.tex", directory / "pending-report.tex"
        try:
            atomic_json(status_file, {"status": "running", "phase": "writing"})
            if published.exists():
                tex = published  # Already compiled once; only the PDF needs rebuilding.
            elif pending.exists():
                tex = pending
            else:
                tex = write_report(run_dir, directory / ("attempt-" + uuid.uuid4().hex), backend)
            atomic_json(status_file, {"status": "running", "phase": "compiling"})
            try:
                pdf = compile_pdf(tex)
            except CompilerUnavailable:
                if tex != pending and tex != published:
                    atomic_text(pending, tex.read_text())
                raise
            except Exception:
                if tex == pending:
                    pending.unlink()  # A compiler rejected it; keep no reusable copy.
                raise
            if tex != published:
                atomic_text(published, tex.read_text())
                if tex == pending:
                    pdf.replace(directory / "report.pdf")
                    pending.unlink()
                else:
                    shutil.copyfile(pdf, directory / "report.pdf")
            result = directory / "report.pdf"
            atomic_json(status_file, {"status": "completed", "phase": "published"})
            return result
        except BaseException as exc:
            atomic_json(status_file, {"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            raise


def after_verify(args) -> None:
    # Reporting is a separate outcome: failure must not invalidate a completed review.
    print("[report] Preparing template PDF (2 additional Codex calls; up to 4 with one retry).", file=sys.stderr)
    try:
        pdf = publish(args.run_dir, report_writer(args))
        print(f"[report] PDF: {pdf}", file=sys.stderr)
    except QuotaExhausted as exc:
        source = read_json(args.run_dir / "report.json")
        if source.get("review_kind") == "paper":
            from .paper import PaperReport, save

            report = PaperReport.model_validate(source)
            report.interruption_reason = "quota_exhausted"
            report.interruption_phase = "report_writing"
            record_quota(args.run_dir, exc, phase="report_writing")
            save(args.run_dir, report)
            partial_after_quota(args.run_dir)
        else:
            print(f"[report] Verification is saved; template PDF failed: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"[report] Verification is saved; template PDF failed: {exc}", file=sys.stderr)


def publish_partial(run_dir: Path) -> Path:
    """Offline, explicitly partial snapshot; no classifier, writer, or audit calls."""
    from .paper import PaperReport, markdown, public_report
    from .references import REFERENCE_NOTICE
    from .writer import Draft, render, source_snapshot

    source = PaperReport.model_validate(read_json(run_dir / "report.json"))
    if source.interruption_reason != "quota_exhausted":
        raise ValueError("Partial quota export requires an interrupted paper report")
    directory = report_directory(run_dir)
    with run_lock(directory):
        status = directory / "status.json"
        output = directory / "partial"
        state = {"partial": True, "reason": "quota_exhausted", "phase": source.interruption_phase}
        atomic_json(status, {**state, "status": "running"})
        try:
            # Reader-facing snapshot files hold reference titles/URLs only.
            public = public_report(source)
            text = markdown(public)
            atomic_json(output / "report.json", public.model_dump(mode="json"))
            atomic_text(output / "report.md", text)
            # No new model calls after quota exhaustion. Keep exact released text
            # in the same reader layout, marking unfinished editorial work explicitly.
            snapshot = source_snapshot(run_dir / "report.json")
            snapshot["editorial_status"] = "interrupted"
            snapshot["reference_limitations"] = public.reference_limitations
            if snapshot.get("progress"):
                snapshot["progress"] = public.progress.model_dump(mode="json")
            kinds = {i.issue_id: i.kind for i in source.finding_details}
            sections = {"diagnosis": "Concern", "title": "Concern", "check": "Check",
                        "repair_request": "Suggestion", "repair_suggestion": "Suggestion",
                        "clarification_request": "Clarification"}
            draft = Draft(
                overview="This review is partial: quota exhausted. The findings below contain only previously released feedback. Report editing, quotation selection and reference summarization remain unfinished; no additional review was performed for this snapshot.",
                entries=[dict(issue_id=issue["issue_id"], title=f"Finding {n}",
                              issue_type=kinds.get(issue["issue_id"], "Not classified").replace("_", " "),
                              feedback=[dict(ref=i, section=sections[item["kind"]])
                                        for i, item in enumerate(snapshot["feedback"])
                                        if item["issue_id"] == issue["issue_id"]])
                         for n, issue in enumerate(snapshot["issues"], 1)],
                # Fixed orchestrator text; the renderer prints title, URL and this notice only.
                reference_limits=[dict(refs=[i], not_checked=REFERENCE_NOTICE,
                                       reason="Reference summarization was not performed for this partial snapshot.")
                                  for i in range(len(snapshot["reference_limitations"]))],
            )
            atomic_json(output / "source.json", snapshot)
            atomic_json(output / "draft.json", draft.model_dump())
            atomic_text(output / "report.tex", render(snapshot, draft))
            pdf = compile_pdf(output / "report.tex")
            atomic_json(status, {**state, "status": "completed"})
            return pdf
        except BaseException as exc:
            atomic_json(status, {**state, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            raise


def partial_after_quota(run_dir: Path) -> None:
    source = read_json(Path(run_dir) / "report.json")
    if source.get("field") == "ai":
        after_ai_review(run_dir)
        return
    try:
        pdf = publish_partial(run_dir)
        print(f"[report] Partial PDF (no model calls): {pdf}. Resume the same command after quota recovers.", file=sys.stderr)
    except Exception as exc:
        print(f"[report] Partial Markdown/LaTeX preserved; PDF failed: {exc}", file=sys.stderr)


def after_ai_review(run_dir: Path) -> None:
    """Readable output also for failed/interrupted AI runs, without more model calls."""
    from .reader import publish_reader

    try:
        html = publish_reader(run_dir)
        print(f"[report] English report: {html}", file=sys.stderr)
        status = read_json(html.parent / "status.json")
        if status["pdf_status"] == "completed":
            print(f"[report] PDF: {html.with_suffix('.pdf')}", file=sys.stderr)
        else:
            print(f"[report] HTML/Markdown ready; PDF unavailable: {status['pdf_error']}", file=sys.stderr)
    except Exception as exc:
        print(f"[report] Verification is saved; reader report failed: {exc}", file=sys.stderr)
