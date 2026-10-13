"""Classify exact released feedback without hiding failed verification work."""
import json

import pytest
from test_paper_review import Classifier, args
from test_workflow import FixtureBackend, request

from osverifier.quota import QuotaExhausted
from osverifier.storage import read_json
from osverifier_reports import paper
from osverifier_reports.writer import source_snapshot


class FailedAudit(FixtureBackend):
    def generate(self, role, payload, schema, directory):
        if role == "feedback_auditor" and payload["proposed_issue"]["target"] == "unreleased":
            raise RuntimeError("fixture feedback audit failed")
        return super().generate(role, payload, schema, directory)


def backend(decompose=True):
    scope = "root" if decompose else "whole_solution"
    return FailedAudit({scope: ["released", "unreleased"]})


@pytest.mark.parametrize("field", ["ai", "math"])
@pytest.mark.parametrize("decompose", [True, False])
def test_failed_audit_still_classifies_only_released_feedback(tmp_path, field, decompose):
    options = args(tmp_path)
    options.field, options.decompose = field, decompose

    class ReleasedOnly(Classifier):
        core_before_classification = None

        def call(self, role, payload, schema, directory):
            assert [f["text"] for f in payload["feedback"]] == ["Audited fixture: released"]
            checkpoint = (tmp_path / ".verification/report.json").read_bytes()
            if self.core_before_classification is None:
                self.core_before_classification = checkpoint
            assert checkpoint == self.core_before_classification
            return super().call(role, payload, schema, directory)

    classifier = ReleasedOnly()
    report = paper.run_paper(options, request(), backend(decompose), classifier)
    assert classifier.calls == ["classify-paper-issues", "audit-paper-severity"]
    assert report.run_status == "incomplete"
    assert report.classification_status == "completed"
    assert report.classification_skip_reason is None
    assert [i.severity for i in report.issues] == ["major"]  # No released complete repair.
    assert [f.text for f in report.feedback] == ["Audited fixture: released"]
    core = read_json(tmp_path / ".verification/report.json")
    assert core["run_status"] == "incomplete"
    assert (tmp_path / ".verification/report.json").read_bytes() == classifier.core_before_classification
    if field == "ai":
        assert report.progress.execution == "failed" and report.progress.failed_findings == 1
        assert report.progress.audit == "failed"
    snapshot = source_snapshot(tmp_path / "report.json")
    assert snapshot["run_status"] == "incomplete" and snapshot["classification_status"] == "completed"


def test_partial_classification_cache_follows_released_snapshot_on_resume(tmp_path):
    options = args(tmp_path)
    options.field = "ai"
    first = Classifier()
    paper.run_paper(options, request(), backend(), first)
    original_audit = tmp_path / ".verification/jobs/root-issue-000001-feedback/result.json"
    audit_bytes = original_audit.read_bytes()

    # The same failed task is retried, but unchanged released feedback reuses its rating.
    cached = Classifier()
    partial = paper.run_paper(options, request(), backend(), cached)
    assert partial.run_status == "incomplete" and partial.classification_status == "completed"
    assert not cached.calls

    # When the second issue finally passes audit, classify the new feedback snapshot.
    changed = Classifier()
    recovered = FixtureBackend()
    final = paper.run_paper(options, request(), recovered, changed)
    assert final.run_status == "completed" and final.classification_status == "completed"
    assert len(final.issues) == 2 and len(changed.calls) == 2
    assert original_audit.read_bytes() == audit_bytes
    assert [role for role, _ in recovered.calls] == ["feedback_auditor"]


@pytest.mark.parametrize("approve,fail,status", [(False, False, "withheld"), (True, True, "failed")])
def test_partial_classification_failure_preserves_both_failure_and_feedback(tmp_path, approve, fail, status):
    options = args(tmp_path)
    options.field = "ai"
    report = paper.run_paper(options, request(), backend(), Classifier(approve=approve, fail=fail))
    assert report.run_status == "incomplete" and report.progress.execution == "failed"
    assert report.classification_status == status
    assert report.classification_skip_reason is None
    assert len(report.issues) == 1 and report.issues[0].severity == "unknown"
    assert [f.text for f in report.feedback] == ["Audited fixture: released"]


def test_partial_classification_quota_stops_calls_and_clears_on_manual_resume(tmp_path):
    class Exhausted(Classifier):
        def call(self, role, *a):
            self.calls.append(role)
            raise QuotaExhausted("fixture")

    options = args(tmp_path)
    options.field, options.no_report = "ai", True
    exhausted = Exhausted()
    paused = paper.run_paper(options, request(), backend(), exhausted)
    assert exhausted.calls == ["classify-paper-issues"]
    assert paused.run_status == "incomplete" and paused.classification_status == "failed"
    assert paused.interruption_phase == "classification"
    assert paused.interruption_reason == "quota_exhausted"
    assert (tmp_path / "interruption.json").exists()

    classifier = Classifier()
    resumed = paper.run_paper(options, request(), backend(), classifier)
    assert len(classifier.calls) == 2 and resumed.classification_status == "completed"
    assert resumed.run_status == "incomplete" and resumed.progress.failed_findings == 1
    assert resumed.interruption_reason is None
    assert not (tmp_path / "interruption.json").exists()


@pytest.mark.parametrize("error,reason", [(KeyboardInterrupt, "verification_interrupted"), (RuntimeError, "verification_failed")])
def test_unhandled_verification_exit_skips_new_calls_with_explicit_reason(tmp_path, monkeypatch, error, reason):
    original = paper.DecomposingVerifier.verify

    def stopped(verifier, *a, **kw):
        original(verifier, *a, **kw)
        raise error("fixture stop")

    monkeypatch.setattr(paper.DecomposingVerifier, "verify", stopped)
    options = args(tmp_path)
    options.field = "ai"
    options.no_report = True
    classifier = Classifier()
    with pytest.raises(error):
        paper.run_paper(options, request(), FixtureBackend({"root": ["released"]}), classifier)
    report = paper.PaperReport.model_validate(read_json(tmp_path / "report.json"))
    assert report.feedback and not classifier.calls
    assert report.run_status == "incomplete" and report.classification_status == "skipped"
    assert report.classification_skip_reason == reason
    assert reason.replace("_", " ") in (tmp_path / "report.md").read_text()
    assert source_snapshot(tmp_path / "report.json")["classification_skip_reason"] == reason


def test_cli_keeps_failure_exit_code_after_successful_partial_classification(tmp_path, monkeypatch, capsys):
    from osverifier import cli

    req = tmp_path / "input.json"
    req.write_text(request().model_dump_json())
    classifier = Classifier()
    monkeypatch.setattr(cli, "CodexBackend", lambda **kw: backend())
    monkeypatch.setattr(paper, "report_writer", lambda args: classifier)
    code = cli.main(["verify", "--review-type", "paper", "--request", str(req),
                     "--run-dir", str(tmp_path / "run"), "--model", "test", "--decompose", "--no-report"])
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report["run_status"] == "incomplete"
    assert report["classification_status"] == "completed" and len(classifier.calls) == 2
