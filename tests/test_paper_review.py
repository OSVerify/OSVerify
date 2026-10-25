from types import SimpleNamespace

import pytest
from test_workflow import FixtureBackend, request

from osverifier.exporting import export_report
from osverifier.storage import read_json
from osverifier_reports import paper
from osverifier_reports.writer import Draft, Entry, Fidelity, Placement, render, source_snapshot


class Classifier:
    """Scripted classifier: minor iff a released repair suggestion exists, unless ``by_kind`` is off."""

    def __init__(self, approve=True, fail=False, by_kind=True):
        self.approve, self.fail, self.by_kind, self.calls = approve, fail, by_kind, []

    def call(self, role, payload, schema, directory):
        self.calls.append(role)
        assert "decision" not in payload
        assert payload["paper"] == request().candidate_solution
        if self.fail:
            raise RuntimeError("classification failed")
        if schema is Fidelity:
            return Fidelity(approved=self.approve, reasons=[])
        ids = list(dict.fromkeys(f["issue_id"] for f in payload["feedback"]))
        suggested = {f["issue_id"] for f in payload["feedback"] if f["kind"] == "repair_suggestion"}
        # With by_kind, the internally rejected first finding is minor because its fix was released.
        minor = (lambda n, i: i in suggested) if self.by_kind else (lambda n, i: n == 0)
        return paper.Ratings(issues=[paper.Rating(issue_id=i, severity="minor" if minor(n, i) else "major", rationale="Fixture impact basis") for n, i in enumerate(ids)])


class RepairingBackend(FixtureBackend):
    """Fixture verifier that also releases a repair suggestion for the first proposed issue."""

    def generate(self, role, payload, schema, attempt_dir):
        result = super().generate(role, payload, schema, attempt_dir)
        if role == "issue_validator" and payload["proposed_issue"]["target"] == "reject":
            result["feedback_candidates"].append(
                {"feedback_id": "fix", "kind": "repair_suggestion", "text": "Fixture repair"}
            )
        return result


def args(tmp_path, early_stop=False):
    return SimpleNamespace(run_dir=tmp_path, decompose=True, early_stop=early_stop,
                           workers=1, max_issues=None, max_attempts=1, timeout_retries=0, model="test",
                           codex_bin="codex", codex_home=None)


def review(tmp_path, classifier=None, early_stop=False, backend_cls=None):
    backend = (backend_cls or RepairingBackend)({"root": ["reject"], "node-0001": ["local"]}, reject=["reject"])
    result = paper.run_paper(args(tmp_path, early_stop), request(), backend, classifier or Classifier())
    return result, backend


def test_paper_continues_after_rejection_and_reclassifies(tmp_path):
    classifier = Classifier()
    report, backend = review(tmp_path, classifier)
    assert report.coverage_complete and report.run_status == "completed"
    assert "decomposer" in [r for r, _ in backend.calls]
    assert "decision" not in report.model_dump()
    assert [i.severity for i in report.issues] == ["minor", "major"]
    assert read_json(tmp_path / ".verification/report.json")["decision"] == "REJECT"
    md = (tmp_path / "report.md").read_text()
    assert md.index("## Major issues") < md.index("## Minor issues")
    assert all(word not in md for word in ("REJECT", "REVISE", "READY_FOR_HUMAN"))
    original = read_json(tmp_path / ".verification/report.json")["feedback"]
    assert [f.text for f in report.feedback] == [f["text"] for f in original]
    assert all((tmp_path / f.audit_artifact).exists() for f in report.feedback)
    resumed = FixtureBackend()
    same = paper.run_paper(args(tmp_path), request(), resumed, classifier)
    assert same == report and not resumed.calls and len(classifier.calls) == 2


def test_explicit_paper_early_stop_is_partial(tmp_path):
    report, backend = review(tmp_path, early_stop=True)
    assert not report.coverage_complete
    assert report.termination_reason == "stopped_after_confirmed_issue"
    assert "decomposer" not in [r for r, _ in backend.calls]
    with pytest.raises(ValueError, match="settings changed"):
        paper.run_paper(args(tmp_path), request(), FixtureBackend(), Classifier())


def test_minor_without_complete_fix_is_withheld_by_auditor(tmp_path):
    report, backend = review(tmp_path, Classifier(approve=False, by_kind=False), backend_cls=FixtureBackend)
    assert report.classification_status == "withheld"
    assert [f.kind for f in report.feedback] == ["diagnosis", "diagnosis"]
    assert all(i.severity == "unknown" for i in report.issues)


@pytest.mark.parametrize("kind", ["repair_request", "diagnosis", "repair_suggestion"])
def test_complete_year_correction_is_not_blocked_by_feedback_kind(tmp_path, kind):
    report, _ = review(tmp_path / "initial")
    report.feedback = [report.feedback[0].model_copy(update={
        "kind": kind, "text": "Correct the publication year in reference 4 from 1957 to 1952."
    })]
    report.issues = [report.issues[0]]
    original = report.feedback[0].model_dump()
    classifier = Classifier(by_kind=False)
    ratings = paper.classify(report, request(), classifier, tmp_path / "classification")
    assert ratings[0].severity == "minor"
    assert classifier.calls == ["classify-paper-issues", "audit-paper-severity"]
    assert report.feedback[0].model_dump() == original


@pytest.mark.parametrize("kind", ["repair_request", "repair_suggestion"])
def test_incomplete_correction_still_requires_severity_audit(tmp_path, kind):
    report, _ = review(tmp_path / "initial")
    report.feedback = [report.feedback[0].model_copy(update={
        "kind": kind, "text": "Correct the publication year in reference 4."
    })]
    report.issues = [report.issues[0]]
    classifier = Classifier(approve=False, by_kind=False)
    assert paper.classify(report, request(), classifier, tmp_path / "classification") is None
    assert classifier.calls == ["classify-paper-issues", "audit-paper-severity"]


@pytest.mark.parametrize("classifier,status", [(Classifier(approve=False), "withheld"), (Classifier(fail=True), "failed")])
def test_failed_or_withheld_classification_preserves_findings(tmp_path, classifier, status):
    report, _ = review(tmp_path, classifier)
    assert report.classification_status == status
    assert report.run_status == "completed" and len(report.issues) == 2
    assert all(i.severity == "unknown" for i in report.issues)
    assert "Fixture impact basis" not in (tmp_path / "report.md").read_text()


def test_all_report_channels_use_same_groups_without_verdict(tmp_path):
    report, _ = review(tmp_path)
    md, _, _ = export_report(tmp_path, "markdown")
    tex, _, _ = export_report(tmp_path, "latex")
    source = source_snapshot(tmp_path / "report.json")
    assert "decision" not in source
    draft = Draft(overview="Two findings were reported.", entries=[
        Entry(issue_id=i.issue_id, title=i.issue_id, issue_type="Issue", feedback=[
            Placement(ref=n, section="Concern" if f["kind"] == "diagnosis" else "Suggestion")
            for n, f in enumerate(source["feedback"]) if f["issue_id"] == i.issue_id
        ]) for i in report.issues
    ])
    drafted = render(source, draft)
    for output in (md, tex, drafted):
        assert all(word not in output for word in ("REJECT", "REVISE", "READY_FOR_HUMAN"))
        assert output.index("Major") < output.index("Minor")
    assert all("Fixture impact basis" in output for output in (md, tex))
    assert "Fixture impact basis" not in drafted
    assert all(i["rationale"] for i in source["issues"])


def test_paper_grouping_stable_within_each_class():
    core = {"request_id": "empty", "run_status": "completed", "feedback": []}
    report = paper.project(core)
    report.issues = [paper.Rating(issue_id=i, severity=s, rationale="basis") for i,s in
                     [("A", "minor"), ("B", "major"), ("C", "major"), ("D", "minor"), ("E", "unknown")]]
    md = paper.markdown(report)
    assert [md.index("### " + i) for i in ("B", "C", "A", "D", "E")] == sorted(md.index("### " + i) for i in ("A", "B", "C", "D", "E"))




def test_resume_config_mismatch_does_not_overwrite_public_report(tmp_path):
    from osverifier.storage import FrozenRunMismatch

    review(tmp_path)
    before = (tmp_path / "report.json").read_bytes()
    changed = FixtureBackend()
    changed.identity = {"kind": "different-backend"}
    with pytest.raises(FrozenRunMismatch):
        paper.run_paper(args(tmp_path), request(), changed, Classifier())
    assert (tmp_path / "report.json").read_bytes() == before


def test_incomplete_paper_without_released_findings_skips_classification(tmp_path):
    backend = FixtureBackend()
    backend.bad_partition = True
    classifier = Classifier()
    result = paper.run_paper(args(tmp_path), request(), backend, classifier)
    assert result.run_status == "incomplete" and not result.coverage_complete
    assert not classifier.calls
    assert result.classification_status == "skipped"
    assert result.classification_skip_reason == "no_released_findings"
    assert "decision" not in read_json(tmp_path / "report.json")


def test_cli_paper_entry_returns_public_schema(tmp_path, monkeypatch, capsys):
    import json

    from osverifier import cli

    req = tmp_path / "input.json"
    req.write_text(request().model_dump_json())
    run = tmp_path / "run"
    backend = RepairingBackend({"root": ["reject"]}, reject=["reject"])
    monkeypatch.setattr(cli, "CodexBackend", lambda **kw: backend)
    monkeypatch.setattr(paper, "report_writer", lambda args: Classifier())
    argv = ["verify", "--review-type", "paper", "--request", str(req),
            "--run-dir", str(run), "--model", "test", "--decompose", "--no-report"]
    assert cli.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == "paper-1.2" and result["field"] == "math"
    assert result["coverage_complete"] and "decision" not in result
    assert result["classification_status"] == "completed"
    argv[argv.index("paper")] = "solution"
    assert cli.main(argv) == 1
    assert "paper run" in capsys.readouterr().err
