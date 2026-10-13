from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from osverifier.demo import (
    BAD_SUGGESTION,
    DIAGNOSIS,
    REPAIR_REQUEST,
    demo_audit,
    demo_proposal,
    demo_request,
    demo_review,
    feedback_decision,
)
from osverifier.models import (
    FeedbackAudit,
    FeedbackDecision,
    IssueReview,
    ProposalBatch,
    ReviewDiagnostics,
    ReviewReport,
    ReviewRequest,
)
from osverifier.pipeline import RunConfig, Verifier
from osverifier.storage import FrozenRunMismatch, read_json


class ScriptedBackend:
    identity = {"kind": "test_fixture"}

    def __init__(self):
        self.outputs = {
            "proposer": demo_proposal(),
            "issue_validator": demo_review(),
            "feedback_auditor": demo_audit(),
        }
        self.calls = []

    def generate(self, role, payload, schema, attempt_dir):
        self.calls.append(role)
        value = self.outputs[role]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value()
        return deepcopy(value)


@pytest.fixture
def backend():
    return ScriptedBackend()


def run(backend, tmp_path):
    return Verifier(backend, RunConfig(workers=1, max_attempts=1)).verify(demo_request(), tmp_path)


def diagnostics(tmp_path):
    return ReviewDiagnostics.model_validate(read_json(tmp_path / "diagnostics.json"))


def blocking_error(backend):
    review = backend.outputs["issue_validator"]
    review.update(kind="invalid_inference", rejection_supported=True, completion_effect="none")
    review["feedback_candidates"][0]["text"] = (
        "The challenged inference is invalid under the stated assumptions."
    )


def test_real_omission_keeps_audited_feedback_but_withholds_problem_changing_remedy(
    backend, tmp_path
):
    report = run(backend, tmp_path)
    assert report.decision == "REVISE"
    assert report.run_status == "completed"
    assert [item.text for item in report.feedback] == [DIAGNOSIS, REPAIR_REQUEST]
    assert diagnostics(tmp_path).findings[0].withheld_feedback_count == 1
    for name in ("report.json", "report.md"):
        assert BAD_SUGGESTION not in (tmp_path / name).read_text()
    assert BAD_SUGGESTION in (tmp_path / "jobs/issue-0000-validation/result.json").read_text()


def test_no_internal_rationale_or_search_prose_leaks_to_report(backend, tmp_path):
    backend.outputs["proposer"]["search_limitations"] = ["UNREVIEWED PROPOSER ADVICE"]
    backend.outputs["issue_validator"]["rationale"] = "UNREVIEWED INTERNAL CLAIM"
    backend.outputs["feedback_auditor"]["rationale"] = "UNREVIEWED AUDITOR REWRITE"
    run(backend, tmp_path)
    for name in ("report.json", "report.md"):
        assert "UNREVIEWED" not in (tmp_path / name).read_text()


def test_confirmed_blocking_error_can_reject_only_after_diagnosis_audit(backend, tmp_path):
    blocking_error(backend)
    report = run(backend, tmp_path)
    assert report.decision == "REJECT"
    assert diagnostics(tmp_path).findings[0].rejection_supported


@pytest.mark.parametrize("assessment", ["disputed", "uncertain"])
def test_auditor_veto_suppresses_rejection_and_all_feedback(backend, tmp_path, assessment):
    blocking_error(backend)
    audit = backend.outputs["feedback_auditor"]
    audit["issue_review_assessment"] = assessment
    audit["decisions"] = [feedback_decision(f"F{i}", False) for i in (1, 2, 3)]
    report = run(backend, tmp_path)
    assert report.decision is None
    assert report.feedback == []
    assert diagnostics(tmp_path).findings[0].status == "audit_blocked"
    assert report.run_status == "incomplete"


def test_withheld_diagnosis_blocks_otherwise_approved_remedy(backend, tmp_path):
    blocking_error(backend)
    backend.outputs["feedback_auditor"]["decisions"][0] = feedback_decision("F1", False)
    report = run(backend, tmp_path)
    assert report.decision is None
    assert report.feedback == []
    assert report.run_status == "incomplete"


@pytest.mark.parametrize(
    "field",
    [
        "grounded_in_source",
        "addresses_validated_issue",
        "preserves_problem",
        "mathematically_supported",
        "uncertainty_calibrated",
    ],
)
def test_approval_requires_every_feedback_check(field):
    decision = feedback_decision("F1")
    decision[field] = False
    with pytest.raises(ValidationError):
        FeedbackDecision.model_validate(decision)


def test_schema_prohibits_rejecting_presentation():
    review = demo_review()
    review.update(kind="presentation", rejection_supported=True)
    with pytest.raises(ValidationError):
        IssueReview.model_validate(review)


@pytest.mark.parametrize("completion_effect", ["none", "uncertain"])
def test_omission_rejection_requires_confirmed_missing_content(completion_effect):
    review = demo_review()
    review.update(rejection_supported=True, completion_effect=completion_effect)
    with pytest.raises(ValidationError):
        IssueReview.model_validate(review)


@pytest.mark.parametrize("upheld", [True, False])
def test_core_omission_rejection_still_requires_feedback_audit(backend, tmp_path, upheld):
    # Scripted contract check; the model roles establish core scope and mathematics.
    backend.outputs["issue_validator"]["rejection_supported"] = True
    if not upheld:
        audit = backend.outputs["feedback_auditor"]
        audit["issue_review_assessment"] = "disputed"
        audit["decisions"] = [feedback_decision(f"F{i}", False) for i in (1, 2, 3)]
    report = run(backend, tmp_path)
    assert report.decision == ("REJECT" if upheld else None)
    assert bool(report.feedback) == upheld
    assert diagnostics(tmp_path).findings[0].rejection_supported == upheld


def test_auditor_cannot_smuggle_replacement_text():
    audit = demo_audit()
    audit["decisions"][0]["replacement_text"] = "Unaudited new advice"
    with pytest.raises(ValidationError):
        FeedbackAudit.model_validate(audit)


@pytest.mark.parametrize("mutation", ["missing", "invented", "duplicate", "wrong_issue"])
def test_bad_audit_links_fail_closed(backend, tmp_path, mutation):
    audit = backend.outputs["feedback_auditor"]
    if mutation == "missing":
        audit["decisions"].pop()
    elif mutation == "invented":
        audit["decisions"][-1]["feedback_id"] = "not-in-review"
    elif mutation == "duplicate":
        audit["decisions"][-1]["feedback_id"] = "F1"
    else:
        audit["issue_id"] = "another-issue"
    report = run(backend, tmp_path)
    assert report.run_status == "incomplete"
    assert report.decision is None
    assert report.feedback == []
    assert diagnostics(tmp_path).findings[0].status == "failed"


def test_partial_empty_search_can_pass(backend, tmp_path):
    request = demo_request().model_copy(update={"mode": "partial"})
    backend.outputs["proposer"]["issues"] = []
    report = Verifier(backend).verify(request, tmp_path)
    assert report.decision == "PASS" and report.mode == "partial"


def test_finished_empty_search_is_ready_for_human(backend, tmp_path):
    backend.outputs["proposer"]["issues"] = []
    report = run(backend, tmp_path)
    assert report.decision == "READY_FOR_HUMAN"
    assert report.run_status == "completed"
    assert report.feedback == []
    assert backend.calls == ["proposer"]


def test_incomplete_search_is_not_a_clean_human_handoff(backend, tmp_path):
    backend.outputs["proposer"].update(issues=[], search_status="incomplete")
    report = run(backend, tmp_path)
    assert report.run_status == "incomplete"
    assert report.decision is None


def test_unresolved_issue_only_releases_cautious_clarification(backend, tmp_path):
    review = backend.outputs["issue_validator"]
    review.update(
        assessment="unresolved",
        impact="unknown",
        completion_effect="uncertain",
        feedback_candidates=[
            {
                "feedback_id": "Q1",
                "kind": "clarification_request",
                "text": "I could not determine whether the converse is supplied implicitly; please clarify that step.",
            }
        ],
    )
    backend.outputs["feedback_auditor"]["decisions"] = [feedback_decision("Q1")]
    report = run(backend, tmp_path)
    assert report.decision == "REVISE"
    assert report.run_status == "completed"
    assert diagnostics(tmp_path).unresolved_issue_ids == ["I1"]
    assert report.feedback[0].kind == "clarification_request"


def test_minor_issue_uses_the_same_revise_decision(backend, tmp_path):
    backend.outputs["issue_validator"].update(
        kind="presentation", impact="nonblocking", completion_effect="none"
    )
    backend.outputs["issue_validator"]["feedback_candidates"] = [
        {
            "feedback_id": "F1",
            "kind": "diagnosis",
            "text": "Please make the notation consistent in line 2.",
        }
    ]
    backend.outputs["feedback_auditor"]["decisions"] = [feedback_decision("F1")]
    report = run(backend, tmp_path)
    assert report.decision == "REVISE"


def test_completed_results_resume_without_new_calls(backend, tmp_path):
    first = run(backend, tmp_path)
    backend.calls.clear()
    second = run(backend, tmp_path)
    assert first == second
    assert backend.calls == []


def test_failed_audit_resumes_without_repeating_proposal_or_issue_review(backend, tmp_path):
    backend.outputs["feedback_auditor"] = RuntimeError("temporary unavailable")
    first = run(backend, tmp_path)
    assert first.run_status == "incomplete"
    assert first.feedback == []
    backend.calls.clear()
    backend.outputs["feedback_auditor"] = demo_audit()
    second = run(backend, tmp_path)
    assert second.run_status == "completed"
    assert backend.calls == ["feedback_auditor"]
    assert (tmp_path / "jobs/issue-0000-feedback/attempt-0002/response.json").is_file()


def test_changed_request_refuses_stale_cache_and_preserves_old_report(backend, tmp_path):
    run(backend, tmp_path)
    original = (tmp_path / "report.json").read_bytes()
    request = demo_request().model_copy(update={"candidate_solution": "Different argument"})
    with pytest.raises(FrozenRunMismatch):
        Verifier(backend, RunConfig(workers=1, max_attempts=1)).verify(request, tmp_path)
    assert (tmp_path / "report.json").read_bytes() == original


def test_modified_cached_feedback_is_not_released(backend, tmp_path):
    run(backend, tmp_path)
    path = tmp_path / "jobs/issue-0000-validation/result.json"
    payload = read_json(path)
    payload["output"]["feedback_candidates"][0]["text"] = "TAMPERED AND NOT AUDITED"
    import json

    path.write_text(json.dumps(payload))
    report = run(backend, tmp_path)
    assert report.run_status == "incomplete"
    assert report.feedback == []
    assert "TAMPERED" not in (tmp_path / "report.md").read_text()


def test_existing_proposer_can_use_same_validator_interface(backend, tmp_path):
    report = Verifier(backend).verify(
        demo_request(), tmp_path, proposals=ProposalBatch.model_validate(demo_proposal())
    )
    assert backend.calls == ["issue_validator", "feedback_auditor"]
    assert [item.text for item in report.feedback] == [DIAGNOSIS, REPAIR_REQUEST]


def test_out_of_range_source_span_is_not_silently_accepted(backend, tmp_path):
    backend.outputs["issue_validator"]["source_spans"][0]["end_line"] = 999
    report = run(backend, tmp_path)
    assert report.run_status == "incomplete"
    assert report.feedback == []


def test_schema_invalid_attempt_is_not_cached_and_retry_can_recover(backend, tmp_path):
    calls = 0

    def response():
        nonlocal calls
        calls += 1
        return {"malformed": True} if calls == 1 else demo_review()

    backend.outputs["issue_validator"] = response
    report = Verifier(backend, RunConfig(workers=1, max_attempts=2)).verify(
        demo_request(), tmp_path
    )
    assert report.run_status == "completed"
    assert calls == 2
    assert (tmp_path / "jobs/issue-0000-validation/attempt-0001/failure.json").is_file()


def test_model_ids_never_become_paths(backend, tmp_path):
    malicious = "../../escape"
    backend.outputs["proposer"]["issues"][0]["issue_id"] = malicious
    backend.outputs["issue_validator"]["issue_id"] = malicious
    backend.outputs["feedback_auditor"]["issue_id"] = malicious
    report = run(backend, tmp_path)
    assert report.run_status == "incomplete"
    assert report.feedback == []
    assert not (tmp_path / "jobs/issue-0000-feedback").exists()


def test_blank_solution_is_invalid():
    with pytest.raises(ValidationError):
        ReviewRequest(request_id="id", question="A question", candidate_solution=" \n ")


def test_codex_response_schemas_require_every_object_field():
    def check(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert set(value["required"]) == set(value["properties"])
                assert value["additionalProperties"] is False
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    for model in (ProposalBatch, IssueReview, FeedbackAudit):
        check(model.model_json_schema())


def test_interrupted_proposer_persists_an_incomplete_report(backend, tmp_path):
    def interrupted():
        raise KeyboardInterrupt()

    backend.outputs["proposer"] = interrupted
    with pytest.raises(KeyboardInterrupt):
        run(backend, tmp_path)
    report = read_json(tmp_path / "report.json")
    assert report["run_status"] == "incomplete"
    assert report["feedback"] == []


def test_over_budget_proposal_is_not_silently_truncated(backend, tmp_path):
    issue = deepcopy(backend.outputs["proposer"]["issues"][0])
    issue["issue_id"] = "I2"
    backend.outputs["proposer"]["issues"].append(issue)
    report = Verifier(backend, RunConfig(max_issues=1, max_attempts=1)).verify(
        demo_request(), tmp_path
    )
    assert report.run_status == "incomplete"
    assert diagnostics(tmp_path).issues_processed == 0
    assert backend.calls == ["proposer"]


def test_parallel_failure_preserves_other_audited_findings(tmp_path):
    class MixedBackend(ScriptedBackend):
        def generate(self, role, payload, schema, attempt_dir):
            if role == "proposer":
                batch = demo_proposal()
                other = deepcopy(batch["issues"][0])
                other["issue_id"] = "I2"
                batch["issues"].append(other)
                return batch
            if payload["proposed_issue"]["issue_id"] == "I2":
                raise RuntimeError("second issue unavailable")
            if role == "issue_validator":
                review = demo_review()
                review.update(
                    kind="invalid_inference", rejection_supported=True, completion_effect="none"
                )
                return review
            return demo_audit()

    report = Verifier(MixedBackend(), RunConfig(workers=2, max_attempts=1)).verify(
        demo_request(), tmp_path
    )
    assert report.run_status == "incomplete"
    assert report.decision == "REJECT"
    assert diagnostics(tmp_path).issues_proposed == 2
    assert diagnostics(tmp_path).issues_processed == 1
    assert [item.status for item in diagnostics(tmp_path).findings] == ["confirmed", "failed"]
    assert [item.issue_id for item in report.feedback] == ["I1", "I1"]


def test_package_change_during_a_call_cannot_promote_its_output(backend, tmp_path, monkeypatch):
    from osverifier.storage import package_fingerprint

    original = package_fingerprint()
    current = [original]
    monkeypatch.setattr("osverifier.jobs.package_fingerprint", lambda: current[0])

    def changed():
        current[0] = "changed-skills"
        return demo_proposal()

    backend.outputs["proposer"] = changed
    report = run(backend, tmp_path)
    assert report.run_status == "incomplete"
    assert not (tmp_path / "jobs/proposer/result.json").exists()


def test_run_directory_rejects_concurrent_writers(backend, tmp_path):
    from osverifier.storage import run_lock

    with run_lock(tmp_path):
        with pytest.raises(RuntimeError, match="another verifier"):
            run(backend, tmp_path)


def test_demo_cli_labels_fixture_results_and_writes_artifacts(tmp_path, capsys):
    import json

    from osverifier.cli import main

    assert main(["demo", "--run-dir", str(tmp_path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["backend_kind"] == "scripted_demo"
    assert "SCRIPTED DEMO" in (tmp_path / "report.md").read_text()
    assert any("SCRIPTED DEMO" in limit for limit in diagnostics(tmp_path).limits)
    assert (tmp_path / "report.md").is_file()


def test_public_report_has_three_decisions_without_internal_action_taxonomy(backend, tmp_path):
    report = run(backend, tmp_path)
    exported = report.model_dump(mode="json")
    assert exported["schema_version"] == "2.0"
    assert exported["decision"] == "REVISE"
    assert not (
        {"verdict", "next_action", "completion", "feedback_review_status", "findings", "limits"}
        & exported.keys()
    )
    assert (tmp_path / exported["diagnostics_artifact"]).is_file()


def test_no_audited_clarification_cannot_become_human_readiness(backend, tmp_path):
    backend.outputs["issue_validator"].update(
        assessment="unresolved",
        impact="unknown",
        completion_effect="uncertain",
        feedback_candidates=[],
    )
    backend.outputs["feedback_auditor"]["decisions"] = []
    report = run(backend, tmp_path)
    assert report.decision is None
    assert report.run_status == "incomplete"
    assert diagnostics(tmp_path).unresolved_issue_ids == ["I1"]


def test_disproved_issue_with_upheld_audit_is_ready_for_human(backend, tmp_path):
    backend.outputs["issue_validator"].update(
        assessment="not_real",
        impact="nonblocking",
        completion_effect="none",
        feedback_candidates=[],
    )
    backend.outputs["feedback_auditor"]["decisions"] = []
    report = run(backend, tmp_path)
    assert report.decision == "READY_FOR_HUMAN"
    assert report.run_status == "completed"
    assert diagnostics(tmp_path).findings[0].status == "not_real"


def test_report_schema_rejects_false_handoff_with_pending_execution(backend, tmp_path):
    backend.outputs["proposer"]["issues"] = []
    ready = run(backend, tmp_path).model_dump()
    ready["run_status"] = "incomplete"
    with pytest.raises(ValidationError):
        ReviewReport.model_validate(ready)


@pytest.mark.parametrize("workers", [3, 16])
def test_uncapped_proposals_and_parallel_codex_call_limit(tmp_path, workers):
    import threading

    class ManyIssuesBackend:
        identity = {"kind": "test_fixture"}

        def __init__(self):
            self.lock = threading.Lock()
            self.barrier = threading.Barrier(workers, timeout=5)
            self.active = 0
            self.peak = 0
            self.calls = 0

        def generate(self, role, payload, schema, attempt_dir):
            if role == "proposer":
                assert payload["max_issues"] is None
                batch = demo_proposal()
                batch["issues"] = []
                for index in range(20):
                    issue = deepcopy(demo_proposal()["issues"][0])
                    issue["issue_id"] = f"I{index}"
                    batch["issues"].append(issue)
                return batch
            issue_id = payload["proposed_issue"]["issue_id"]
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.calls += 1
            try:
                if role == "issue_validator":
                    if int(issue_id[1:]) < workers:
                        self.barrier.wait()
                    review = demo_review()
                    review.update(
                        issue_id=issue_id,
                        assessment="not_real",
                        impact="nonblocking",
                        completion_effect="none",
                        feedback_candidates=[],
                    )
                    return review
                audit = demo_audit()
                audit.update(issue_id=issue_id, decisions=[])
                return audit
            finally:
                with self.lock:
                    self.active -= 1

    backend = ManyIssuesBackend()
    config = (
        RunConfig(max_attempts=1) if workers == 16 else RunConfig(workers=workers, max_attempts=1)
    )
    report = Verifier(backend, config).verify(demo_request(), tmp_path)
    assert report.run_status == "completed"
    assert report.decision == "READY_FOR_HUMAN"
    assert diagnostics(tmp_path).issues_proposed == diagnostics(tmp_path).issues_processed == 20
    assert backend.calls == 40
    assert backend.peak == workers


def test_cli_defaults_match_python_defaults():
    from osverifier.cli import parser

    args = parser().parse_args(
        ["verify", "--request", "input.json", "--run-dir", "output", "--model", "fixture"]
    )
    assert args.max_issues is RunConfig().max_issues is None
    assert args.workers == RunConfig().workers == 16
    assert args.reasoning_effort == "xhigh"
    assert args.web_search == "live"


@pytest.mark.parametrize("max_issues", [0, -1])
def test_explicit_issue_count_limit_must_be_positive(max_issues):
    with pytest.raises(ValueError):
        RunConfig(max_issues=max_issues)
