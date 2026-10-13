from copy import deepcopy

import pytest
from pydantic import ValidationError
from test_workflow import FixtureBackend, request

from osverifier import cli
from osverifier.exporting import export_report
from osverifier.models import PartialIssueReview, ReviewReport
from osverifier.pipeline import RunConfig, Verifier
from osverifier.quota import QuotaExhausted
from osverifier.reporting import render_markdown
from osverifier.storage import FrozenRunMismatch, read_json
from osverifier.workflow import DecomposingVerifier, WorkflowConfig
from osverifier.workflow_models import WorkflowReport


def partial_request():
    return request().model_copy(update={"mode": "partial"})


class PartialBackend(FixtureBackend):
    def __init__(
        self, *, finding=False, reject=False, audit_reject=False, quota=False, disputed=False
    ):
        super().__init__(
            {"root": ["issue"], "whole_solution": ["issue"]} if finding else {},
            reject=["issue"] if reject else [],
        )
        self.audit_reject, self.quota, self.disputed = audit_reject, quota, disputed
        self.inputs = []

    def generate(self, role, payload, schema, attempt_dir):
        self.inputs.append((role, deepcopy(payload), schema))
        if role == "feedback_auditor" and self.quota:
            raise QuotaExhausted("fixture quota")
        value = super().generate(role, payload, schema, attempt_dir)
        if role == "issue_validator":
            value["route_obstruction"] = (
                "Fixture: indispensable premise contradicts the original assumptions; local replacement cannot retain this route."
                if value["rejection_supported"]
                else ""
            )
        if role == "feedback_auditor":
            value.update(
                route_rejection_upheld=self.audit_reject,
                route_rationale="Fixture independent route audit",
            )
            if self.disputed:
                value["issue_review_assessment"] = "disputed"
                for d in value["decisions"]:
                    d["decision"] = "withhold"
        return value


def run(backend, directory, decomposed):
    verifier = (
        DecomposingVerifier(backend, WorkflowConfig(workers=1, max_attempts=1))
        if decomposed
        else Verifier(backend, RunConfig(workers=1, max_attempts=1))
    )
    return verifier.verify(partial_request(), directory)


@pytest.mark.parametrize("decomposed", [False, True])
def test_unfinished_progress_can_pass_and_keeps_original_question(tmp_path, decomposed):
    backend = PartialBackend()
    result = run(backend, tmp_path, decomposed)
    assert (
        result.mode == "partial" and result.decision == "PASS" and result.run_status == "completed"
    )
    for _, payload, _ in backend.inputs:
        assert payload["request"]["mode"] == "partial"
        assert payload["request"]["question"] == request().question
        assert payload["request"]["candidate_solution"] == request().candidate_solution
    if decomposed:
        assert result.coverage_complete
        assert any(p.get("scope", {}).get("kind") == "segment" for _, p, _ in backend.inputs)
    assert "not a correctness or completion certificate" in render_markdown(result)
    assert "Partial progress" in export_report(tmp_path, "latex")[0]


@pytest.mark.parametrize("decomposed", [False, True])
@pytest.mark.parametrize(
    "reject,audit_reject,expected",
    [(False, False, "REVISE"), (True, False, "REVISE"), (True, True, "REJECT")],
)
def test_reject_requires_both_route_gates_revision_needs_no_repair(
    tmp_path, decomposed, reject, audit_reject, expected
):
    backend = PartialBackend(finding=True, reject=reject, audit_reject=audit_reject)
    result = run(backend, tmp_path, decomposed)
    assert result.decision == expected and result.run_status == "completed"
    assert len(result.feedback) == 1 and result.feedback[0].kind == "diagnosis"
    assert result.feedback[0].text == "Audited fixture: issue"
    if decomposed:
        assert result.coverage_complete == (expected != "REJECT")
    schemas = {role: schema for role, _, schema in backend.inputs}
    assert "route_obstruction" in schemas["issue_validator"]["required"]
    assert "route_rejection_upheld" in schemas["feedback_auditor"]["required"]


@pytest.mark.parametrize("decomposed", [False, True])
def test_auditor_cannot_introduce_reject_and_failures_never_pass(tmp_path, decomposed):
    result = run(PartialBackend(finding=True, audit_reject=True), tmp_path, decomposed)
    assert result.run_status == "incomplete" and result.decision is None and not result.feedback


@pytest.mark.parametrize("decomposed", [False, True])
def test_disputed_finding_cannot_pass_or_release(tmp_path, decomposed):
    result = run(PartialBackend(finding=True, disputed=True), tmp_path, decomposed)
    assert result.run_status == "incomplete" and result.decision is None and not result.feedback


@pytest.mark.parametrize("decomposed", [False, True])
def test_quota_resume_reuses_partial_validator_and_mode_is_frozen(tmp_path, decomposed):
    result = run(PartialBackend(finding=True, quota=True), tmp_path, decomposed)
    assert result.run_status == "incomplete" and result.decision is None
    resumed = PartialBackend(finding=True)
    result = run(resumed, tmp_path, decomposed)
    assert result.decision == "REVISE"
    assert not any(role == "issue_validator" for role, _, _ in resumed.inputs)
    verifier = (
        DecomposingVerifier(PartialBackend(), WorkflowConfig(workers=1, max_attempts=1))
        if decomposed
        else Verifier(PartialBackend(), RunConfig(workers=1, max_attempts=1))
    )
    with pytest.raises(FrozenRunMismatch):
        verifier.verify(request(), tmp_path)


def test_partial_route_evidence_and_clean_decision_schema_guards():
    backend = PartialBackend(finding=True, reject=True)
    from pathlib import Path

    issue = {
        "issue_id": "i",
        "scope_id": "root",
        "target": "issue",
        "source_spans": [{"document": "candidate_solution", "start_line": 1, "end_line": 1}],
    }
    raw = backend.generate("issue_validator", {"proposed_issue": issue}, {}, Path("."))
    raw["route_obstruction"] = " "
    with pytest.raises(ValidationError):
        PartialIssueReview.model_validate(raw)
    base = dict(request_id="r", run_status="completed", backend_kind="fixture", feedback=[])
    for mode, decision in [("full_solution", "PASS"), ("partial", "READY_FOR_HUMAN")]:
        with pytest.raises(ValidationError):
            ReviewReport(**base, mode=mode, decision=decision)
    with pytest.raises(ValidationError):
        WorkflowReport(**base, mode="partial", decision="PASS", coverage_complete=False)


def test_cli_partial_and_paper_exclusion(tmp_path, monkeypatch, capsys):
    question = tmp_path / "q.md"
    question.write_text("Original question")
    progress = tmp_path / "s.md"
    progress.write_text("Partial progress")
    monkeypatch.setattr(cli, "CodexBackend", lambda **kw: PartialBackend())
    argv = [
        "verify",
        "--mode",
        "partial",
        "--question",
        str(question),
        "--solution",
        str(progress),
        "--model",
        "fixture",
        "--no-report",
        "--run-dir",
        str(tmp_path / "run"),
    ]
    assert cli.main(argv) == 0
    assert read_json(tmp_path / "run/report.json")["decision"] == "PASS"
    assert '"mode": "partial"' in capsys.readouterr().out
    assert cli.main(argv + ["--review-type", "paper"]) == 1
    assert "only with --review-type solution" in capsys.readouterr().err


def test_partial_report_writer_keeps_mode_and_decision_meaning(tmp_path):
    from osverifier_reports.writer import Draft, render, source_snapshot

    run(PartialBackend(), tmp_path, False)
    source = source_snapshot(tmp_path / "report.json")
    assert source["mode"] == "partial"
    tex = render(
        source,
        Draft(overview="No confirmed problem was found in the reviewed progress.", entries=[]),
    )
    assert "PASS permits continuing" in tex and "REVISE does not guarantee repairability" in tex
