"""Paper-facing results and separately audited impact classification."""
from __future__ import annotations

import hashlib
import json
import sys
import uuid
from typing import Literal

from pydantic import Field

from osverifier.models import ReleasedFeedback, StrictModel
from osverifier.pipeline import RunConfig, Verifier
from osverifier.quota import QuotaExhausted, record_quota
from osverifier.storage import FrozenRunMismatch, atomic_json, atomic_text, read_json, run_lock
from osverifier.workflow import DecomposingVerifier, WorkflowConfig

from .writer import ROOT, CodexWriter, Fidelity


class Rating(StrictModel):
    issue_id: str
    severity: Literal["major", "minor", "unknown"]
    rationale: str = Field(min_length=1)


class Ratings(StrictModel):
    issues: list[Rating]


class PaperReport(StrictModel):
    schema_version: Literal["paper-1.0"] = "paper-1.0"
    review_kind: Literal["paper"] = "paper"
    request_id: str
    run_status: Literal["running", "completed", "incomplete"]
    backend_kind: str
    coverage_complete: bool
    termination_reason: str | None = None
    feedback: list[ReleasedFeedback]
    issues: list[Rating]
    classification_status: Literal["pending", "completed", "withheld", "failed"] = "pending"
    classification_error: str | None = None
    interruption_reason: Literal["quota_exhausted"] | None = None
    interruption_phase: str | None = None
    diagnostics_artifact: str = ".verification/diagnostics.json"


def markdown(report: PaperReport) -> str:
    lines = [f"# Paper review: {report.request_id}", "", "## Review summary", "",
             f"Review status: **{report.run_status}**.",
             f"Full selected-scope coverage: **{report.coverage_complete}**.",
             f"Severity classification: **{report.classification_status}**.", "",
             f"Released findings concern {len(report.issues)} issue(s).", ""]
    if report.interruption_reason:
        lines += ["**PARTIAL REPORT — quota exhausted.**", f"Paused during {report.interruption_phase}. Only already audited feedback and classifications are included. Resume with the same inputs and run directory after quota recovers.", ""]
    if report.backend_kind == "scripted_demo":
        lines += ["**SCRIPTED DEMO: no live mathematical judgments.**", ""]
    if report.run_status != "completed":
        lines += ["The review is unfinished; feedback and coverage are partial.", ""]
    if not report.issues:
        lines += ["No confirmed issue was reported within the reviewed scope. This is not a correctness certificate.", ""]
    for severity, heading in (("major", "Major issues"), ("minor", "Minor issues"), ("unknown", "Unclassified issues")):
        items = [i for i in report.issues if i.severity == severity]
        if not items:
            continue
        lines += [f"## {heading}", ""]
        for issue in items:
            lines += [f"### {issue.issue_id}", "", f"Classification basis: {issue.rationale}", ""]
            for f in report.feedback:
                if f.issue_id != issue.issue_id:
                    continue
                spans = "; ".join(f"{s.document} lines {s.start_line}–{s.end_line}" for s in f.source_spans)
                lines += [f"**{f.kind.replace('_', ' ')}** ({f.feedback_id})", "", f"Location: {spans or 'Not supplied'}", "", f.text, "", f"Audit: `{f.audit_artifact}`", ""]
    return "\n".join(lines)


def save(directory, report):
    atomic_json(directory / "report.json", report.model_dump(mode="json"))
    atomic_text(directory / "report.md", markdown(report))


def project(core: dict) -> PaperReport:
    feedback = [ReleasedFeedback.model_validate({**f, "audit_artifact": ".verification/" + f["audit_artifact"]}) for f in core.get("feedback", [])]
    ids = list(dict.fromkeys(f.issue_id for f in feedback))
    return PaperReport(
        request_id=core["request_id"], run_status=core["run_status"],
        backend_kind=core.get("backend_kind", "codex"),
        coverage_complete=core.get("coverage_complete", core["run_status"] == "completed"),
        termination_reason={"audited_rejection": "stopped_after_confirmed_issue"}.get(core.get("termination_reason"), core.get("termination_reason")),
        feedback=feedback,
        issues=[Rating(issue_id=i, severity="unknown", rationale="Impact classification has not been approved.") for i in ids],
    )


def classify(report, request, backend, directory):
    payload = {"paper": request.candidate_solution, "selected_scope": request.question,
               "feedback": [f.model_dump(mode="json") for f in report.feedback]}
    assets = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in ROOT.rglob("*") if p.is_file() and p.suffix in (".py", ".md", ".tex")}
    digest = hashlib.sha256(json.dumps({"input": payload, "assets": assets}, sort_keys=True).encode()).hexdigest()
    directory = directory / digest
    cached = directory / "approved.json"
    if cached.exists():
        return Ratings.model_validate(read_json(cached)).issues
    attempt = directory / uuid.uuid4().hex
    atomic_json(attempt / "provenance.json", {"sha256": digest, "assets": assets})
    ratings = backend.call("classify-paper-issues", payload, Ratings, attempt / "classify")
    if [i.issue_id for i in ratings.issues] != [i.issue_id for i in report.issues]:
        raise ValueError("Classification must include each released issue exactly once in source order")
    # Completeness is semantic: the independent severity auditor checks released
    # texts, including concrete repairs tagged as requests or diagnoses.
    atomic_json(attempt / "ratings.json", ratings.model_dump())
    audit = backend.call("audit-paper-severity", {**payload, "ratings": ratings.model_dump()}, Fidelity, attempt / "audit")
    atomic_json(attempt / "audit.json", audit.model_dump())
    if not audit.approved:
        return None
    atomic_json(cached, ratings.model_dump())
    return ratings.issues


def run_paper(args, request, backend, classifier=None):
    directory = args.run_dir.resolve()
    with run_lock(directory):
        task = {"review_kind": "paper", "request": request.model_dump(mode="json"),
                "decompose": args.decompose, "early_stop": args.early_stop}
        marker = directory / "paper-task.json"
        if marker.exists():
            if read_json(marker) != task:
                raise ValueError("Paper inputs or review settings changed; use a new run directory")
        elif (directory / "report.json").exists() or (directory / "manifest.json").exists():
            raise ValueError("Existing solution run; use a new directory for paper review")
        atomic_json(marker, task)
        atomic_json(directory / "request.json", request.model_dump(mode="json"))
        core_dir = directory / ".verification"
        report = project({"request_id": request.request_id, "run_status": "running"})
        if not (directory / "report.json").exists():
            save(directory, report)
        try:
            if args.decompose:
                core = DecomposingVerifier(backend, WorkflowConfig(
                    workers=args.workers, max_issues=args.max_issues, max_attempts=args.max_attempts,
                    early_stop_on_reject=args.early_stop,
                )).verify(request, core_dir)
            else:
                if args.early_stop:
                    raise ValueError("--early-stop requires --decompose")
                core = Verifier(backend, RunConfig(workers=args.workers, max_issues=args.max_issues, max_attempts=args.max_attempts)).verify(request, core_dir)
            report = project(core.model_dump(mode="json"))
        except FrozenRunMismatch:
            raise
        except BaseException:
            if (core_dir / "report.json").exists():
                report = project(read_json(core_dir / "report.json"))
            report.run_status = "incomplete"
            report.coverage_complete = False
            save(directory, report)
            raise
        save(directory, report)
        if report.run_status == "completed":
            (directory / "interruption.json").unlink(missing_ok=True)
            try:
                if report.issues:
                    classifier = classifier or CodexWriter(args.model, args.codex_bin, args.codex_home)
                    ratings = classify(report, request, classifier, directory / "paper-classification")
                    if ratings is None:
                        report.classification_status = "withheld"
                    else:
                        report.issues = ratings
                        report.classification_status = "completed"
                else:
                    report.classification_status = "completed"
            except BaseException as exc:
                if isinstance(exc, QuotaExhausted):
                    record_quota(directory, exc, phase="classification")
                    report.interruption_reason = "quota_exhausted"
                    report.interruption_phase = "classification"
                report.classification_status = "failed"
                report.classification_error = f"{type(exc).__name__}: {exc}"
                save(directory, report)
                if not isinstance(exc, Exception):
                    raise
                print(f"[paper] Severity classification failed; findings preserved: {exc}", file=sys.stderr)
            save(directory, report)
        core_pause = core_dir / "interruption.json"
        if report.run_status == "incomplete" and core_pause.exists() and read_json(core_pause).get("reason") == "quota_exhausted":
            report.interruption_reason = "quota_exhausted"
            report.interruption_phase = "verification"
        if report.interruption_reason:
            save(directory, report)
            if not getattr(args, "no_report", False):
                from .automatic import partial_after_quota

                partial_after_quota(directory)
        return report
