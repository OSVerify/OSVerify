"""Small, explicitly scripted fixture demonstrating feedback release, not model quality."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .models import ReviewRequest


def demo_request() -> ReviewRequest:
    return ReviewRequest(
        request_id="scripted-omitted-converse",
        question="Prove that for a real number x, x^2 = 1 if and only if x = 1 or x = -1.",
        candidate_solution=(
            "Suppose x^2 = 1. Then (x - 1)(x + 1) = 0.\n"
            "Therefore x = 1 or x = -1. This proves the equivalence."
        ),
    )


SPANS = [
    {"document": "question", "start_line": 1, "end_line": 1},
    {"document": "candidate_solution", "start_line": 2, "end_line": 2},
]
DIAGNOSIS = "The written argument proves only the forward implication; the converse has not been explicitly checked."
REPAIR_REQUEST = "Add the converse by checking that both x = 1 and x = -1 satisfy x^2 = 1."
BAD_SUGGESTION = "Restrict the problem to nonnegative x and drop the x = -1 case."


def demo_proposal() -> dict:
    return {
        "scope_id": "whole_solution",
        "search_status": "completed",
        "search_limitations": [],
        "issues": [
            {
                "issue_id": "I1",
                "scope_id": "whole_solution",
                "kind": "omission",
                "target": "The claimed equivalence",
                "source_spans": deepcopy(SPANS),
                "allegation": "The reverse implication is omitted.",
                "evidence": "Only an argument starting from x^2 = 1 is written.",
                "proposed_feedback": [
                    {"feedback_id": "P1", "kind": "repair_request", "text": REPAIR_REQUEST},
                    {"feedback_id": "P2", "kind": "repair_suggestion", "text": BAD_SUGGESTION},
                ],
            }
        ],
    }


def demo_review() -> dict:
    return {
        "issue_id": "I1",
        "assessment": "confirmed",
        "kind": "omission",
        "impact": "blocking",
        "rejection_supported": False,
        "completion_effect": "missing_required_content",
        "source_spans": deepcopy(SPANS),
        "rationale": "The requested iff has two directions, while the supplied proof writes only one.",
        "evidence": "The written proof assumes x^2 = 1 and concludes x = ±1; it does not substitute the two values.",
        "feedback_candidates": [
            {"feedback_id": "F1", "kind": "diagnosis", "text": DIAGNOSIS},
            {"feedback_id": "F2", "kind": "repair_request", "text": REPAIR_REQUEST},
            # Deliberately model a validator that lets bad advice through. The next gate must block it.
            {"feedback_id": "F3", "kind": "repair_suggestion", "text": BAD_SUGGESTION},
        ],
    }


def feedback_decision(feedback_id: str, approve: bool = True) -> dict:
    return {
        "feedback_id": feedback_id,
        "decision": "approve" if approve else "withhold",
        "grounded_in_source": approve,
        "addresses_validated_issue": True,
        "preserves_problem": approve,
        "mathematically_supported": approve,
        "uncertainty_calibrated": True,
        "rationale": "The exact text is supported."
        if approve
        else "The advice changes the problem and discards a required real solution.",
    }


def demo_audit() -> dict:
    return {
        "issue_id": "I1",
        "issue_review_assessment": "upheld",
        "rationale": "An omitted converse warrants repair, not rejection; one proposed remedy changes the question.",
        "decisions": [
            feedback_decision("F1"),
            feedback_decision("F2"),
            feedback_decision("F3", False),
        ],
    }


class DemoBackend:
    identity = {"kind": "scripted_demo", "fixture_version": 1}

    def generate(self, role: str, payload: dict, schema: dict, attempt_dir: Path) -> dict:
        if payload["request"] != demo_request().model_dump(mode="json"):
            raise ValueError("demo backend accepts only its fixed demonstration request")
        return {
            "proposer": demo_proposal,
            "issue_validator": demo_review,
            "feedback_auditor": demo_audit,
        }[role]()
