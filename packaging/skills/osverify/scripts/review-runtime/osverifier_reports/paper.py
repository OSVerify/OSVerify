"""Paper-facing results and separately audited impact classification."""
from __future__ import annotations

import hashlib
import json
import sys
import uuid
from typing import Literal

from pydantic import Field

from osverifier.models import IssueKind, ReleasedFeedback, StrictModel
from osverifier.pipeline import RunConfig, Verifier
from osverifier.quota import QuotaExhausted, record_quota
from osverifier.storage import FrozenRunMismatch, atomic_json, atomic_text, read_json, run_lock
from osverifier.workflow import DecomposingVerifier, WorkflowConfig

from .evidence import freeze_images
from .fields import AI_REVIEW_SCOPE, FIELD_LABELS, AIPaperBackend, validate_field
from .progress import PaperProgress, paper_progress, progress_lines
from .references import (
    INTERNAL_REFERENCE_LIMITS,
    REFERENCE_NOTICE,
    ReferenceBackend,
    ReferenceStore,
    dependency_key,
    public_references,
    reconcile_references,
    reference_limits,
    reference_observations,
)
from .writer import ROOT, Fidelity, report_writer


class Rating(StrictModel):
    issue_id: str
    severity: Literal["major", "minor", "unknown"]
    rationale: str = Field(min_length=1)


class Ratings(StrictModel):
    issues: list[Rating]


class FindingDetail(StrictModel):
    issue_id: str
    assessment: Literal["confirmed", "unresolved"]
    kind: IssueKind


class PaperReport(StrictModel):
    schema_version: Literal["paper-1.0", "paper-1.1", "paper-1.2"] = "paper-1.1"
    review_kind: Literal["paper"] = "paper"
    field: Literal["math", "ai"] = "math"
    request_id: str
    run_status: Literal["running", "completed", "incomplete"]
    backend_kind: str
    coverage_complete: bool
    termination_reason: str | None = None
    feedback: list[ReleasedFeedback]
    issues: list[Rating]
    finding_details: list[FindingDetail] = Field(default_factory=list)
    reference_limitations: list[dict] = Field(default_factory=list)
    supplemental_reviews: list[dict] = Field(default_factory=list)
    progress: PaperProgress | None = None
    classification_status: Literal["pending", "completed", "withheld", "failed", "skipped"] = "pending"
    classification_error: str | None = None
    classification_skip_reason: Literal[
        "quota_exhausted", "verification_interrupted", "verification_failed", "no_released_findings"
    ] | None = None
    interruption_reason: Literal["quota_exhausted"] | None = None
    interruption_phase: str | None = None
    diagnostics_artifact: str = ".verification/diagnostics.json"


def markdown(report: PaperReport) -> str:
    lines = [f"# Paper review: {report.request_id}", "", "## Review summary", "",
             f"Field: **{FIELD_LABELS[report.field]}**. Correctness review only.",
             f"Review status: **{report.run_status}**.",
             f"Full selected-scope coverage: **{report.coverage_complete}**.",
             f"Severity classification: **{report.classification_status}**.", "",
             f"Released findings concern {len(report.issues)} issue(s).", ""]
    if report.progress:
        lines[lines.index(f"Review status: **{report.run_status}**."):lines.index(f"Severity classification: **{report.classification_status}**.")] = progress_lines(report.progress)
    if report.field == "ai":
        lines += [AI_REVIEW_SCOPE, "",
                  "A confirmed evidence gap does not establish that the scientific conclusion is false. Clarification requests preserve unresolved questions.", ""]
    if report.reference_limitations:
        lines += ["## Unresolved external references", "", "These are access/checking limitations, not established defects. Downloading a source alone does not verify its use.", ""]
        for ref in public_references(report.reference_limitations):
            lines += [f"- {ref['title']}" + (f" — {ref['url']}" if ref['url'] else "") + f". {REFERENCE_NOTICE}"]
        lines += [""]
    if report.classification_skip_reason:
        lines += [f"Classification skipped: {report.classification_skip_reason.replace('_', ' ')}.", ""]
    if report.interruption_reason:
        lines += ["**PARTIAL REPORT — quota exhausted.**", f"Paused during {report.interruption_phase}. Only already audited feedback and classifications are included. Resume with the same inputs and run directory after quota recovers.", ""]
    if report.backend_kind == "scripted_demo":
        lines += ["**SCRIPTED DEMO: no live mathematical judgments.**", ""]
    if report.run_status != "completed":
        lines += ["Execution did not finish successfully; only already audited feedback is included." if report.progress else "The review is unfinished; feedback and coverage are partial.", ""]
    if not report.issues:
        lines += ["No confirmed issue was reported within the reviewed scope. This is not a correctness certificate.", ""]
    for severity, heading in (("major", "Major issues"), ("minor", "Minor issues"), ("unknown", "Unclassified issues")):
        items = [i for i in report.issues if i.severity == severity]
        if not items:
            continue
        lines += [f"## {heading}", ""]
        for issue in items:
            lines += [f"### {issue.issue_id}", "", f"Classification basis: {issue.rationale}", ""]
            detail = next((d for d in report.finding_details if d.issue_id == issue.issue_id), None)
            if detail:
                lines += [f"Assessment: **{detail.assessment}**. Issue kind: **{detail.kind.replace('_', ' ')}**.", ""]
            for f in report.feedback:
                if f.issue_id != issue.issue_id:
                    continue
                spans = "; ".join(f"{s.document} lines {s.start_line}–{s.end_line}" for s in f.source_spans)
                lines += [f"**{f.kind.replace('_', ' ')}** ({f.feedback_id})", "", f"Source evidence (not a list of error locations): {spans or 'Not supplied'}", "", f.text, "", f"Audit: `{f.audit_artifact}`", ""]
    return "\n".join(lines)


def refresh_reference_progress(report):
    """Keep displayed evidence limits consistent after supplementary reference checks."""
    if report.progress is None:
        return
    other = [limit for limit in report.progress.evidence_limits
             if not limit.startswith('Unresolved reference: ')]
    report.progress.evidence_limits = sorted(set(other + [
        f"Unresolved reference: {c['title']}"
        for c in public_references(report.reference_limitations)]))
    if report.progress.evidence_limits:
        report.progress.evidence = 'limited'
    elif report.progress.evidence != 'not_assessed':
        report.progress.evidence = 'no_reported_limits'


def public_report(report: PaperReport) -> PaperReport:
    """Reader-facing copy: unchecked references keep only title and URL."""
    public = report.model_copy(deep=True, update={"reference_limitations": public_references(report.reference_limitations)})
    refresh_reference_progress(public)
    return public


def save(directory, report):
    refresh_reference_progress(report)
    records = report.reference_limitations
    # Full model-written records are internal diagnostics. A report reloaded from
    # report.json already holds only the public projection of the same records.
    if not records or any(set(r) - {"title", "url"} for r in records):
        atomic_json(directory / INTERNAL_REFERENCE_LIMITS, {"schema_version": 1, "records": records})
    public = public_report(report)
    atomic_json(directory / "report.json", public.model_dump(mode="json"))
    atomic_text(directory / "report.md", markdown(public))


def project(core: dict, field: str = "math", diagnostics: dict | None = None, *, directory=None, images=()) -> PaperReport:
    validate_field(field)
    feedback = [ReleasedFeedback.model_validate({**f, "audit_artifact": ".verification/" + f["audit_artifact"]}) for f in core.get("feedback", [])]
    ids = list(dict.fromkeys(f.issue_id for f in feedback))
    progress = paper_progress(core, diagnostics or {}, directory=directory, images=images) if diagnostics or field == "ai" else None
    run_status = ("running" if progress.execution == "running" else "completed" if progress.execution == "completed" else "incomplete") if progress else core["run_status"]
    termination = core.get("termination_reason")
    if progress and termination in ("execution_incomplete", "review_inconclusive"):
        termination = "all_tasks_finished" if progress.execution == "completed" else "execution_" + progress.execution
    return PaperReport(
        schema_version="paper-1.2" if progress else "paper-1.1",
        request_id=core["request_id"], run_status=run_status, progress=progress,
        field=field,
        reference_limitations=reference_limits(directory) if directory else [],
        backend_kind=core.get("backend_kind", "codex"),
        coverage_complete=(progress.search == "completed" and (field == "ai" or progress.execution == "completed")) if progress else core.get("coverage_complete", core["run_status"] == "completed"),
        termination_reason={"audited_rejection": "stopped_after_confirmed_issue"}.get(termination, termination),
        feedback=feedback,
        issues=[Rating(issue_id=i, severity="unknown", rationale="Impact classification has not been approved.") for i in ids],
        finding_details=[FindingDetail(issue_id=f["issue_id"], assessment=f["status"], kind=f["kind"])
                         for f in (diagnostics or {}).get("findings", [])
                         if f["issue_id"] in ids and f.get("status") in ("confirmed", "unresolved") and f.get("kind")],
    )


def classify(report, request, backend, directory, images=()):
    payload = {"paper": request.candidate_solution, "selected_scope": request.question,
               "field": report.field,
               "finding_details": [d.model_dump() for d in report.finding_details],
               "feedback": [f.model_dump(mode="json") for f in report.feedback]}
    if images:
        payload["paper_images"] = list(images)
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
    field = validate_field(getattr(args, "field", None) or "math")
    directory = args.run_dir.resolve()
    image_paths = getattr(args, "paper_image", []) or []
    pdf = getattr(args, "paper_pdf", None)
    if (image_paths or pdf) and field != "ai":
        raise ValueError("--paper-image is available only for AI paper review")
    if field != "ai" and isinstance(backend, AIPaperBackend):
        raise ValueError("AI paper backend cannot review a Math paper")
    with run_lock(directory):
        images = freeze_images(image_paths, directory / "paper-images", pdf) if field == "ai" else []
        if field == "ai" and not isinstance(backend, AIPaperBackend):
            backend = AIPaperBackend(backend, images)
        elif field == "ai" and backend.images != images:
            raise ValueError("AI backend images differ from paper inputs")
        reference_store = ReferenceStore(directory / "references")
        reference_source_backend = backend
        backend = ReferenceBackend(backend, reference_store)
        task = {"review_kind": "paper", "request": request.model_dump(mode="json"),
                "field": field,
                "decompose": args.decompose, "early_stop": args.early_stop}
        marker = directory / "paper-task.json"
        if marker.exists():
            saved_task = read_json(marker)
            saved_task.setdefault("field", "math")
            if saved_task != task:
                raise ValueError("Paper inputs or review settings changed; use a new run directory")
        elif (directory / "report.json").exists() or (directory / "manifest.json").exists():
            raise ValueError("Existing solution run; use a new directory for paper review")
        atomic_json(marker, task)
        atomic_json(directory / "request.json", request.model_dump(mode="json"))
        core_dir = directory / ".verification"
        def projected(core):
            diagnostics = read_json(core_dir / "diagnostics.json") if (core_dir / "diagnostics.json").exists() else {}
            return project(core, field, diagnostics, directory=core_dir, images=images)

        report = project({"request_id": request.request_id, "run_status": "running"}, field, images=images)
        if not (directory / "report.json").exists():
            save(directory, report)
        try:
            if args.decompose:
                core = DecomposingVerifier(backend, WorkflowConfig(
                    workers=args.workers, max_issues=args.max_issues, max_attempts=args.max_attempts, timeout_retries=args.timeout_retries,
                    early_stop_on_reject=args.early_stop,
                )).verify(request, core_dir)
            else:
                if args.early_stop:
                    raise ValueError("--early-stop requires --decompose")
                core = Verifier(backend, RunConfig(workers=args.workers, max_issues=args.max_issues, max_attempts=args.max_attempts, timeout_retries=args.timeout_retries)).verify(request, core_dir)
            report = projected(core.model_dump(mode="json"))
            if not (core_dir / 'interruption.json').exists() and getattr(core, 'termination_reason', None) != 'audited_rejection':
                supplements = reconcile_references(request, reference_source_backend, reference_store, core_dir, directory, RunConfig(workers=args.workers, max_issues=args.max_issues, max_attempts=args.max_attempts, timeout_retries=args.timeout_retries))
                for index, (destination, extra, dependencies) in enumerate(supplements):
                    report.supplemental_reviews.append({'artifact': str(destination.relative_to(directory)), 'run_status': extra.run_status})
                    checked = {(row.get('role', 'proposer'), (row.get('scope') or {}).get('scope_id', 'unknown'), dependency_key(c)) for row in reference_observations(destination) for c in row['checks'] if c['status'] == 'verified'}
                    if extra.run_status == 'completed':
                        report.reference_limitations = [c for c in report.reference_limitations if (c.get('role', 'proposer'), c.get('scope_id', 'unknown'), dependency_key(c)) not in checked]
                    # Same status metadata as core findings, so readers group them by severity.
                    extra_findings = read_json(destination / 'diagnostics.json').get('findings', []) if (destination / 'diagnostics.json').exists() else []
                    for feedback in extra.feedback:
                        key = f"reference-{index + 1}-{feedback.issue_id}"
                        report.feedback.append(feedback.model_copy(update={'issue_id': key, 'audit_artifact': str(destination.relative_to(directory)) + '/' + feedback.audit_artifact}))
                        if not any(i.issue_id == key for i in report.issues):
                            report.issues.append(Rating(issue_id=key, severity='unknown', rationale='Supplementary finding awaiting classification.'))
                        finding = next((f for f in extra_findings if f['issue_id'] == feedback.issue_id
                                        and f.get('status') in ('confirmed', 'unresolved') and f.get('kind')), None)
                        if finding and not any(d.issue_id == key for d in report.finding_details):
                            report.finding_details.append(FindingDetail(issue_id=key, assessment=finding['status'], kind=finding['kind']))
                    for limit in reference_limits(destination):
                        if not any(c['url'] == limit['url'] and c['dependency'] == limit['dependency'] for c in report.reference_limitations):
                            report.reference_limitations.append(limit)
                    if extra.run_status != 'completed' and report.progress:
                        report.progress.search = 'partial'
                        report.coverage_complete = False
            save(directory, report)
        except FrozenRunMismatch:
            raise
        except BaseException as exc:
            if (core_dir / "report.json").exists():
                report = projected(read_json(core_dir / "report.json"))
            report.run_status = "incomplete"
            if report.progress:
                report.progress.execution = "failed" if isinstance(exc, Exception) and not isinstance(exc, QuotaExhausted) else "interrupted"
            else:
                report.coverage_complete = False
            report.classification_status = "skipped"
            report.classification_skip_reason = (
                "quota_exhausted" if isinstance(exc, QuotaExhausted)
                else "verification_failed" if isinstance(exc, Exception)
                else "verification_interrupted"
            )
            save(directory, report)
            if isinstance(exc, QuotaExhausted):
                record_quota(directory, exc, phase='reference_recheck')
                report.interruption_reason = 'quota_exhausted'
                report.interruption_phase = 'reference_recheck'
                save(directory, report)
                if not getattr(args, 'no_report', False):
                    from .automatic import partial_after_quota
                    partial_after_quota(directory)
            if field == "ai" and not isinstance(exc, QuotaExhausted) and not getattr(args, "no_report", False):
                from .automatic import after_ai_review

                after_ai_review(directory)
            raise
        # A quota pause returns an incomplete core report. Detect it before any
        # classifier dispatch; ordinary per-job failures do not block released findings.
        core_pause = core_dir / "interruption.json"
        if core_pause.exists() and read_json(core_pause).get("reason") == "quota_exhausted":
            report.interruption_reason = "quota_exhausted"
            report.interruption_phase = "verification"
            report.classification_status = "skipped"
            report.classification_skip_reason = "quota_exhausted"
            if report.progress:
                report.progress.execution = "interrupted"
        else:
            (directory / "interruption.json").unlink(missing_ok=True)
            if not report.issues and report.run_status != "completed":
                report.classification_status = "skipped"
                report.classification_skip_reason = "no_released_findings"
        save(directory, report)
        if report.classification_status != "skipped":
            try:
                if report.issues:
                    classifier = classifier or report_writer(args)
                    ratings = classify(report, request, classifier, directory / "paper-classification", images)
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
                    if field == "ai" and not getattr(args, "no_report", False):
                        from .automatic import after_ai_review

                        after_ai_review(directory)
                    raise
                print(f"[paper] Severity classification failed; findings preserved: {exc}", file=sys.stderr)
            save(directory, report)
        if report.interruption_reason:
            save(directory, report)
            if not getattr(args, "no_report", False):
                from .automatic import partial_after_quota

                partial_after_quota(directory)
        return report
