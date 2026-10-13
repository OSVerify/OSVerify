"""Contracts for parallel, single-layer decomposition review."""

from typing import Literal, Self

from pydantic import Field, model_validator

from .models import Identifier, ReviewReport, ReviewScope, StrictModel, unique_ids


class ScopedReview(ReviewScope):
    kind: Literal["whole_solution", "segment"] = "whole_solution"
    parent_scope_id: str | None = None
    context_before: str = ""
    context_after: str = ""


class TextSegment(StrictModel):
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    context_before: str
    context_after: str


class TextDecomposition(StrictModel):
    segments: list[TextSegment] = Field(min_length=1)


class DuplicateMatch(StrictModel):
    issue_id: Identifier
    duplicate_of: Identifier
    rationale: str = Field(min_length=1)


class DuplicatePlan(StrictModel):
    matches: list[DuplicateMatch]

    @model_validator(mode="after")
    def distinct(self) -> Self:
        unique_ids(self.matches, "issue_id")
        return self


class DuplicateCheck(DuplicateMatch):
    confirmed: bool


class DuplicateAudit(StrictModel):
    checks: list[DuplicateCheck]

    @model_validator(mode="after")
    def distinct(self) -> Self:
        unique_ids(self.checks, "issue_id")
        return self


class WorkflowReport(ReviewReport):
    schema_version: Literal["3.0"] = "3.0"
    termination_reason: (
        Literal["all_nodes_reviewed", "audited_rejection", "execution_incomplete"] | None
    ) = None
    coverage_complete: bool = False
    trajectory_artifact: str = "trajectory.jsonl"

    @model_validator(mode="after")
    def coverage_gate(self) -> Self:
        if self.decision in ("READY_FOR_HUMAN", "PASS") and not self.coverage_complete:
            raise ValueError("human readiness requires all planned scopes to finish")
        if self.termination_reason == "audited_rejection" and self.decision != "REJECT":
            raise ValueError("rejection early stop requires an audited rejection")
        return self
