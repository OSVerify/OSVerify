"""Versioned contracts. Model output is validated again locally, including links."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewRequest(StrictModel):
    request_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    candidate_solution: str = Field(min_length=1)
    mode: Literal["full_solution", "partial"] = "full_solution"

    @model_validator(mode="after")
    def nonblank(self) -> Self:
        if not all(
            value.strip() for value in (self.request_id, self.question, self.candidate_solution)
        ):
            raise ValueError(
                "request ID, question, and solution must contain non-whitespace content"
            )
        return self


class SourceSpan(StrictModel):
    """One-based, inclusive references into the immutable input, never a summary."""

    document: Literal["question", "candidate_solution"]
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("source span end precedes start")
        return self


class ReviewScope(StrictModel):
    """Extension point for a future decomposer; currently one whole-solution scope."""

    scope_id: Identifier
    objective: str
    source_spans: list[SourceSpan]


IssueKind = Literal[
    "false_claim", "invalid_inference", "calculation_error", "omission", "presentation", "other"
]
FeedbackKind = Literal["diagnosis", "repair_request", "repair_suggestion", "clarification_request"]


class FeedbackDraft(StrictModel):
    feedback_id: Identifier
    kind: FeedbackKind
    text: str = Field(min_length=1)


class ProposedIssue(StrictModel):
    issue_id: Identifier
    scope_id: Identifier
    kind: IssueKind
    target: str = Field(min_length=1)
    source_spans: list[SourceSpan] = Field(min_length=1)
    allegation: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    proposed_feedback: list[FeedbackDraft]

    @model_validator(mode="after")
    def feedback_ids(self) -> Self:
        unique_ids(self.proposed_feedback, "feedback_id")
        return self


class ProposalBatch(StrictModel):
    scope_id: Identifier
    search_status: Literal["completed", "incomplete"]
    search_limitations: list[str]
    issues: list[ProposedIssue]

    @model_validator(mode="after")
    def issue_ids(self) -> Self:
        unique_ids(self.issues, "issue_id")
        if any(issue.scope_id != self.scope_id for issue in self.issues):
            raise ValueError("issue belongs to another scope")
        return self


class IssueReview(StrictModel):
    issue_id: Identifier
    assessment: Literal["confirmed", "not_real", "unresolved"]
    kind: IssueKind
    impact: Literal["blocking", "nonblocking", "unknown"]
    rejection_supported: bool
    completion_effect: Literal["none", "missing_required_content", "uncertain"]
    source_spans: list[SourceSpan]
    rationale: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    feedback_candidates: list[FeedbackDraft]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        unique_ids(self.feedback_candidates, "feedback_id")
        if self.rejection_supported and (
            self.assessment != "confirmed"
            or self.impact != "blocking"
            or self.kind == "presentation"
        ):
            raise ValueError(
                "rejection needs a confirmed blocking defect, not presentation"
            )
        if (
            self.rejection_supported
            and self.kind == "omission"
            and self.completion_effect != "missing_required_content"
        ):
            raise ValueError("omission-based rejection must establish missing required content")
        if self.completion_effect == "missing_required_content" and self.assessment != "confirmed":
            raise ValueError("unconfirmed finding cannot establish incompleteness")
        if self.assessment == "confirmed":
            if not self.source_spans or not any(
                item.kind == "diagnosis" for item in self.feedback_candidates
            ):
                raise ValueError("confirmed issue requires source spans and an auditable diagnosis")
        if self.assessment == "not_real" and self.feedback_candidates:
            raise ValueError("disproved allegations must not generate external feedback")
        if self.assessment == "unresolved" and any(
            item.kind != "clarification_request" for item in self.feedback_candidates
        ):
            raise ValueError("unresolved issues permit only cautious clarification requests")
        return self


class FeedbackDecision(StrictModel):
    feedback_id: Identifier
    decision: Literal["approve", "withhold"]
    grounded_in_source: bool
    addresses_validated_issue: bool
    preserves_problem: bool
    mathematically_supported: bool
    uncertainty_calibrated: bool
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def approval_checks(self) -> Self:
        if self.decision == "approve" and not all(
            (
                self.grounded_in_source,
                self.addresses_validated_issue,
                self.preserves_problem,
                self.mathematically_supported,
                self.uncertainty_calibrated,
            )
        ):
            raise ValueError("feedback approval requires all five checks")
        return self


class FeedbackAudit(StrictModel):
    issue_id: Identifier
    issue_review_assessment: Literal["upheld", "disputed", "uncertain"]
    rationale: str = Field(min_length=1)
    decisions: list[FeedbackDecision]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        unique_ids(self.decisions, "feedback_id")
        if self.issue_review_assessment != "upheld" and any(
            item.decision == "approve" for item in self.decisions
        ):
            raise ValueError("disputed or uncertain issue review cannot release feedback")
        return self


class PartialIssueReview(IssueReview):
    """A local error and evidence against continuing the route are separate claims."""

    route_obstruction: str

    @model_validator(mode="after")
    def route_basis(self) -> Self:
        if self.rejection_supported and not self.route_obstruction.strip():
            raise ValueError("partial rejection requires explicit evidence against the current route")
        return self


class PartialFeedbackAudit(FeedbackAudit):
    route_rejection_upheld: bool
    route_rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def route_gate(self) -> Self:
        if self.route_rejection_upheld and self.issue_review_assessment != "upheld":
            raise ValueError("route rejection requires an upheld finding")
        return self


def review_contracts(mode: str):
    return (PartialIssueReview, PartialFeedbackAudit) if mode == "partial" else (IssueReview, FeedbackAudit)


def validate_route_audit(review, audit):
    if isinstance(audit, PartialFeedbackAudit) and audit.route_rejection_upheld and not review.rejection_supported:
        raise ValueError("route audit cannot introduce a rejection absent from the issue review")


class ReleasedFeedback(StrictModel):
    issue_id: str
    feedback_id: str
    kind: FeedbackKind
    text: str
    source_spans: list[SourceSpan]
    audit_artifact: str


class FindingStatus(StrictModel):
    """Only status metadata; unapproved mathematical prose stays in internal artifacts."""

    issue_id: str
    status: Literal["confirmed", "not_real", "unresolved", "pending", "failed", "audit_blocked"]
    kind: IssueKind | None
    impact: Literal["blocking", "nonblocking", "unknown"]
    rejection_supported: bool
    missing_required_content: bool
    withheld_feedback_count: int


class ReviewReport(StrictModel):
    """Three workflow decisions. None means this review has not produced a decision."""

    schema_version: Literal["2.0"] = "2.0"
    request_id: str
    mode: Literal["full_solution", "partial"] = "full_solution"
    decision: Literal["READY_FOR_HUMAN", "PASS", "REVISE", "REJECT"] | None
    run_status: Literal["running", "completed", "incomplete"]
    backend_kind: str
    feedback: list[ReleasedFeedback]
    diagnostics_artifact: str = "diagnostics.json"

    @model_validator(mode="after")
    def decision_consistency(self) -> Self:
        if self.run_status == "completed" and self.decision is None:
            raise ValueError("completed review requires a workflow decision")
        if (self.decision == "PASS" and self.mode != "partial") or (self.decision == "READY_FOR_HUMAN" and self.mode == "partial"):
            raise ValueError("clean decision must match the review mode")
        if self.decision in ("READY_FOR_HUMAN", "PASS") and (self.run_status != "completed" or self.feedback):
            raise ValueError("human handoff requires completed review and no outstanding feedback")
        if self.decision in ("REVISE", "REJECT") and not self.feedback:
            raise ValueError("revision or rejection requires audited feedback")
        return self


class ReviewDiagnostics(StrictModel):
    """Internal accounting; these fields do not create additional workflow decisions."""

    schema_version: Literal["2.0"] = "2.0"
    request_id: str
    proposer_status: Literal["pending", "completed", "incomplete", "failed"]
    decomposition: Literal["not_implemented"] = "not_implemented"
    issues_proposed: int
    issues_processed: int
    findings: list[FindingStatus]
    unresolved_issue_ids: list[str]
    execution_errors: list[str]
    limits: list[str]
    provenance_artifact: str = "manifest.json"


def unique_ids(items: list, field: str) -> None:
    values = [getattr(item, field) for item in items]
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {field}")


def validate_spans(spans: list[SourceSpan], request: ReviewRequest) -> None:
    for span in spans:
        if span.end_line > len(getattr(request, span.document).splitlines()):
            raise ValueError(f"source span exceeds {span.document}")
