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

from .writer import CodexWriter, write_report


def report_directory(run_dir: Path) -> Path:
    run_dir = run_dir.resolve()
    digest = hashlib.sha256((run_dir / "report.json").read_bytes()).hexdigest()
    return run_dir.parent / (run_dir.name + "-reports") / digest


def compile_pdf(tex: Path) -> Path:
    """Compile in an isolated working directory with restricted TeX file access."""
    engine = shutil.which("xelatex") or shutil.which("lualatex")
    if not engine and Path("/Library/TeX/texbin/xelatex").is_file():
        engine = "/Library/TeX/texbin/xelatex"
    if not engine:
        raise RuntimeError("PDF requires XeLaTeX or LuaLaTeX (TeX Live/MacTeX). Install it and rerun; the approved .tex is preserved.")
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
    """Reuse approved text on PDF retries, and retain failed editorial attempts."""
    source = read_json(Path(run_dir) / "report.json")
    if source.get("review_kind") == "paper" and source.get("classification_status") == "pending":
        raise ValueError("Wait for paper severity classification to finish")
    directory = report_directory(run_dir)
    with run_lock(directory):
        status_file = directory / "status.json"
        if status_file.exists() and read_json(status_file).get("status") == "completed" and (directory / "report.pdf").is_file():
            return directory / "report.pdf"
        try:
            atomic_json(status_file, {"status": "running", "phase": "writing"})
            if not (directory / "report.tex").exists():
                tex = write_report(run_dir, directory / ("attempt-" + uuid.uuid4().hex), backend)
                atomic_text(directory / "report.tex", tex.read_text())
            atomic_json(status_file, {"status": "running", "phase": "compiling"})
            result = compile_pdf(directory / "report.tex")
            atomic_json(status_file, {"status": "completed", "phase": "published"})
            return result
        except BaseException as exc:
            atomic_json(status_file, {"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            raise


def after_verify(args) -> None:
    # Reporting is a separate outcome: failure must not invalidate a completed review.
    print("[report] Preparing template PDF (up to 2 additional Codex calls).", file=sys.stderr)
    try:
        pdf = publish(args.run_dir, CodexWriter(args.model, args.codex_bin, args.codex_home))
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
    from osverifier.exporting import render_latex

    from .paper import PaperReport, markdown

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
            text = markdown(source)
            atomic_json(output / "report.json", source.model_dump(mode="json"))
            atomic_text(output / "report.md", text)
            atomic_text(output / "report.tex", render_latex(text, title=f"Partial Referee Report on {source.request_id}"))
            pdf = compile_pdf(output / "report.tex")
            atomic_json(status, {**state, "status": "completed"})
            return pdf
        except BaseException as exc:
            atomic_json(status, {**state, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            raise


def partial_after_quota(run_dir: Path) -> None:
    try:
        pdf = publish_partial(run_dir)
        print(f"[report] Partial PDF (no model calls): {pdf}. Resume the same command after quota recovers.", file=sys.stderr)
    except Exception as exc:
        print(f"[report] Partial Markdown/LaTeX preserved; PDF failed: {exc}", file=sys.stderr)
