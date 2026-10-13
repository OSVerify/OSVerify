"""Deterministic release gate. No LLM-written summary is added after the final audit."""

from __future__ import annotations

from .models import (
    FeedbackAudit,
    FindingStatus,
    IssueReview,
    ProposalBatch,
    ReleasedFeedback,
    ReviewDiagnostics,
    ReviewReport,
    ReviewRequest,
)


def assemble_report(
    request: ReviewRequest,
    backend_kind: str,
    batch: ProposalBatch | None,
    outcomes: dict[str, tuple[IssueReview, FeedbackAudit, str] | None],
    errors: list[str],
    *,
    finished: bool,
) -> tuple[ReviewReport, ReviewDiagnostics]:
    findings: list[FindingStatus] = []
    feedback: list[ReleasedFeedback] = []
    unresolved_issue_ids: list[str] = []
    blocked_issue_ids: list[str] = []
    rejected = False
    if batch:
        for issue in batch.issues:
            if issue.issue_id not in outcomes or outcomes[issue.issue_id] is None:
                findings.append(
                    FindingStatus(
                        issue_id=issue.issue_id,
                        status="failed" if issue.issue_id in outcomes else "pending",
                        kind=None,
                        impact="unknown",
                        rejection_supported=False,
                        missing_required_content=False,
                        withheld_feedback_count=0,
                    )
                )
                continue
            review, audit, audit_artifact = outcomes[issue.issue_id]
            approved_ids = {
                decision.feedback_id
                for decision in audit.decisions
                if decision.decision == "approve"
            }
            diagnosis_ids = {
                item.feedback_id for item in review.feedback_candidates if item.kind == "diagnosis"
            }
            upheld = audit.issue_review_assessment == "upheld"
            if review.assessment == "confirmed":
                upheld = upheld and bool(diagnosis_ids) and diagnosis_ids <= approved_ids
            released = (
                [item for item in review.feedback_candidates if item.feedback_id in approved_ids]
                if upheld
                else []
            )
            withheld = len(review.feedback_candidates) - len(released)
            if not upheld or review.assessment == "unresolved":
                unresolved_issue_ids.append(issue.issue_id)
            # Approved clarification is usable revision feedback. Unresolved reviews
            # without such feedback must not be promoted to a clean human handoff.
            if not upheld or (review.assessment == "unresolved" and not released):
                blocked_issue_ids.append(issue.issue_id)
            if upheld:
                for item in released:
                    feedback.append(
                        ReleasedFeedback(
                            issue_id=issue.issue_id,
                            feedback_id=item.feedback_id,
                            kind=item.kind,
                            text=item.text,
                            source_spans=review.source_spans,
                            audit_artifact=audit_artifact,
                        )
                    )
            confirmed = upheld and review.assessment == "confirmed"
            missing = confirmed and review.completion_effect == "missing_required_content"
            reject_issue = confirmed and review.rejection_supported
            if request.mode == "partial":
                reject_issue = reject_issue and bool(getattr(review, "route_obstruction", "").strip()) and getattr(audit, "route_rejection_upheld", False)
            rejected |= reject_issue
            findings.append(
                FindingStatus(
                    issue_id=issue.issue_id,
                    status=review.assessment if upheld else "audit_blocked",
                    kind=review.kind if upheld else None,
                    impact=review.impact if upheld else "unknown",
                    rejection_supported=reject_issue,
                    missing_required_content=missing,
                    withheld_feedback_count=withheld,
                )
            )
    processed = sum(outcome is not None for outcome in outcomes.values())
    work_complete = (
        batch is not None
        and batch.search_status == "completed"
        and processed == len(batch.issues)
        and not errors
    )
    review_complete = finished and work_complete and not blocked_issue_ids
    decision = None
    if rejected:
        decision = "REJECT"
    elif feedback:
        decision = "REVISE"
    elif review_complete:
        decision = "PASS" if request.mode == "partial" else "READY_FOR_HUMAN"
    limits = [
        "READY_FOR_HUMAN means automated review identified no outstanding issue or revision request; a human makes the final correctness judgment.",
        "Only the whole-solution proposer is implemented; decomposition is deferred.",
    ]
    if request.mode == "partial":
        limits[0] = "PASS permits continued work on partial progress; it does not certify correctness or completion. REVISE does not guarantee repairability; REJECT concerns the current route."
    if backend_kind == "scripted_demo":
        limits.append(
            "SCRIPTED DEMO: model judgments are fixtures, not a live mathematical evaluation."
        )
    if batch and batch.search_limitations:
        # The proposer's prose has not been feedback-audited. Keep it internal.
        limits.append(
            "The proposer recorded search limitations; inspect the internal proposal artifact."
        )
    if not work_complete or blocked_issue_ids:
        limits.append(
            "Some search, validation, or audit remains unfinished; this run cannot authorize a clean human handoff."
        )
    report = ReviewReport(
        request_id=request.request_id,
        mode=request.mode,
        decision=decision,
        run_status=("completed" if review_complete else "incomplete") if finished else "running",
        backend_kind=backend_kind,
        feedback=feedback,
    )
    diagnostics = ReviewDiagnostics(
        request_id=request.request_id,
        proposer_status=batch.search_status if batch else ("failed" if finished else "pending"),
        issues_proposed=len(batch.issues) if batch else 0,
        issues_processed=processed,
        findings=findings,
        unresolved_issue_ids=unresolved_issue_ids,
        execution_errors=errors,
        limits=limits,
    )
    return report, diagnostics


def render_markdown(report: ReviewReport) -> str:
    lines = [
        "# Verifier report",
        "",
        f"- Decision: **{report.decision or 'Pending'}**",
        f"- Run: {report.run_status}",
        "",
    ]
    if report.mode == "partial":
        lines += ["Partial-progress review against the original question. PASS permits continued work, not a correctness or completion certificate. REVISE requires correction but does not guarantee repairability. REJECT concerns the current route, not the original problem.", ""]
    if report.backend_kind == "scripted_demo":
        lines.extend(["**SCRIPTED DEMO — no live model judgments.**", ""])
    if report.decision == "READY_FOR_HUMAN":
        lines.extend(
            [
                "No outstanding issue or revision request was identified. Ready for final human confirmation.",
                "",
            ]
        )
    if report.run_status != "completed":
        lines.extend(
            [
                "Review is unfinished. Any released feedback is partial; this report does not authorize a clean human handoff.",
                "",
            ]
        )
    lines.extend(["## Audited feedback", ""])
    if not report.feedback:
        lines.extend(["No feedback text was released.", ""])
    for item in report.feedback:
        lines.extend(
            [
                f"### {item.issue_id} / {item.feedback_id} ({item.kind})",
                "",
                item.text,
                "",
                "Source: "
                + ", ".join(
                    f"{span.document} lines {span.start_line}–{span.end_line}"
                    for span in item.source_spans
                ),
                f"Audit: `{item.audit_artifact}`",
                "",
            ]
        )
    lines.extend(
        [
            f"Internal audit and execution details: [{report.diagnostics_artifact}]({report.diagnostics_artifact})",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
