from __future__ import annotations

import hashlib
import json

import pytest

from osverifier_reports.writer import (
    Draft,
    Fidelity,
    render,
    source_snapshot,
    validate_draft,
    write_report,
)


def make_source(path):
    path.mkdir(parents=True)
    source = {
        "request_id": "Trial",
        "decision": "REVISE",
        "run_status": "completed",
        "backend_kind": "scripted_demo",
        "feedback": [
            {
                "issue_id": "i1",
                "feedback_id": "f1",
                "kind": "diagnosis",
                "text": "The converse is missing.",
                "source_spans": [
                    {"document": "candidate_solution", "start_line": 1, "end_line": 2}
                ],
                "audit_artifact": "jobs/audit.json",
            },
            {
                "issue_id": "i1",
                "feedback_id": "f2",
                "kind": "repair_request",
                "text": "Supply the converse.",
                "source_spans": [],
                "audit_artifact": "jobs/audit.json",
            },
        ],
    }
    (path / "report.json").write_text(json.dumps(source))
    (path / "diagnostics.json").write_text("PRIVATE_SENTINEL")
    return source


def plan():
    return Draft.model_validate(
        {
            "overview": "The review asks for the missing converse.",
            "entries": [
                {
                    "issue_id": "i1",
                    "title": "Missing converse",
                    "issue_type": "Omission",
                    "feedback": [
                        {"ref": 0, "section": "Concern"},
                        {"ref": 1, "section": "Suggestion"},
                    ],
                }
            ],
        }
    )


class Fake:
    def __init__(self, approve=True):
        self.approve = approve
        self.calls = []

    def call(self, role, payload, schema, directory):
        self.calls.append(role)
        assert "PRIVATE_SENTINEL" not in json.dumps(payload)
        return plan() if schema is Draft else Fidelity(approved=self.approve, reasons=[])


def test_separate_workflow_preserves_verifier_artifacts(tmp_path):
    run = tmp_path / "run"
    make_source(run)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in run.iterdir()}
    output = tmp_path / "draft"
    fake = Fake()
    result = write_report(run, output, fake)
    assert len(fake.calls) == 2
    assert result.is_file()
    assert "Missing converse" in result.read_text()
    assert "Unclassified Imprecisions" in result.read_text()
    assert r"\paragraph{Check}" in result.read_text()
    assert "No separate check" in result.read_text()
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in run.iterdir()}
    assert json.loads((output / "status.json").read_text())["status"] == "completed"
    with pytest.raises(ValueError, match="fresh"):
        write_report(run, output, fake)
    with pytest.raises(ValueError, match="separate"):
        write_report(run, run / "draft", fake)


def test_withheld_draft_cannot_be_downloaded_as_final(tmp_path):
    run = tmp_path / "run"
    make_source(run)
    output = tmp_path / "draft"
    fake = Fake(False)
    with pytest.raises(ValueError, match="withheld the report after 2 drafts"):
        write_report(run, output, fake)
    assert len(fake.calls) == 4  # one retry, never more
    assert (output / "draft.json").exists()
    assert not (output / "report.tex").exists()
    assert json.loads((output / "status.json").read_text())["status"] == "failed"


def test_no_omissions_duplication_or_repair_as_check(tmp_path):
    run = tmp_path / "run"
    make_source(run)
    source = source_snapshot(run / "report.json")
    draft = plan()
    draft.entries[0].feedback.pop()
    with pytest.raises(ValueError):
        validate_draft(source, draft)
    draft = plan()
    draft.entries[0].feedback[1].section = "Check"
    with pytest.raises(ValueError):
        validate_draft(source, draft)
    draft = plan()
    draft.entries.append(draft.entries[0])
    with pytest.raises(ValueError):
        validate_draft(source, draft)
    source["run_status"] = "incomplete"
    source["coverage_complete"] = False
    tex = render(source, plan())
    assert "Review is unfinished" in tex and "scope coverage: False" in tex






def test_automatic_pdf_retry_reuses_approved_text(tmp_path, monkeypatch):
    from osverifier_reports import automatic

    run = tmp_path / "run"
    make_source(run)
    before = (run / "report.json").read_bytes()
    backend = Fake()

    def fail(tex):
        raise automatic.CompilerUnavailable("missing compiler")

    monkeypatch.setattr(automatic, "compile_pdf", fail)
    with pytest.raises(RuntimeError, match="missing compiler"):
        automatic.publish(run, backend)
    directory = automatic.report_directory(run)
    assert json.loads((directory / "status.json").read_text())["status"] == "failed"
    # Approved text waits for a compiler; it is not yet the published artifact.
    assert (directory / "pending-report.tex").exists() and not (directory / "report.tex").exists()

    def compile_fake(tex):
        pdf = tex.with_suffix(".pdf")
        pdf.write_bytes(b"%PDF-test")
        return pdf

    monkeypatch.setattr(automatic, "compile_pdf", compile_fake)
    assert automatic.publish(run, backend).read_bytes() == b"%PDF-test"
    assert automatic.publish(run, backend).is_file()
    assert len(backend.calls) == 2
    assert (run / "report.json").read_bytes() == before


def test_text_rejected_by_the_compiler_is_never_reused(tmp_path, monkeypatch):
    from osverifier_reports import automatic

    run = tmp_path / "run"
    make_source(run)
    backend = Fake()
    compiled = []

    def reject(tex):
        compiled.append(tex)
        raise RuntimeError(f"PDF compilation failed; see {tex.parent / 'latex.log'}")

    monkeypatch.setattr(automatic, "compile_pdf", reject)
    with pytest.raises(RuntimeError, match="compilation failed"):
        automatic.publish(run, backend)
    directory = automatic.report_directory(run)
    assert not (directory / "report.tex").exists() and not (directory / "pending-report.tex").exists()
    assert compiled[0].parent.name.startswith("attempt-") and compiled[0].is_file()  # Kept as a diagnostic.

    def compile_fake(tex):
        compiled.append(tex)
        pdf = tex.with_suffix(".pdf")
        pdf.write_bytes(b"%PDF-test")
        return pdf

    monkeypatch.setattr(automatic, "compile_pdf", compile_fake)
    assert automatic.publish(run, backend).read_bytes() == b"%PDF-test"
    assert len(backend.calls) == 4 and compiled[1].parent != compiled[0].parent  # A fresh draft and audit.
    assert (directory / "report.tex").read_text() == compiled[1].read_text()


def test_automatic_withheld_report_never_compiles(tmp_path, monkeypatch):
    from osverifier_reports import automatic

    run = tmp_path / "run"
    make_source(run)
    monkeypatch.setattr(automatic, "compile_pdf", lambda _: pytest.fail("withheld text compiled"))
    with pytest.raises(ValueError, match="withheld"):
        automatic.publish(run, Fake(approve=False))
    assert not (automatic.report_directory(run) / "report.tex").exists()


@pytest.mark.parametrize("status,skip,expected", [("completed", False, 1), ("incomplete", False, 0), ("completed", True, 0)])
def test_verify_cli_automatic_report_hook(tmp_path, monkeypatch, status, skip, expected):
    from types import SimpleNamespace

    from osverifier import cli
    from osverifier.demo import demo_request
    from osverifier_reports import automatic

    request = tmp_path / "request.json"
    request.write_text(demo_request().model_dump_json())
    report = SimpleNamespace(run_status=status, model_dump=lambda **_: {"run_status": status})
    monkeypatch.setattr(cli, "CodexBackend", lambda **_: None)
    monkeypatch.setattr(cli.Verifier, "verify", lambda *_, **kw: report)
    calls = []
    monkeypatch.setattr(automatic, "after_verify", lambda args: calls.append(args))
    argv = ["verify", "--request", str(request), "--run-dir", str(tmp_path / "run"), "--model", "test"]
    if skip:
        argv += ["--no-report"]
    assert cli.main(argv) == (0 if status == "completed" else 2)
    assert len(calls) == expected


def test_pdf_failure_keeps_verification_success(monkeypatch, capsys):
    from types import SimpleNamespace

    from osverifier_reports import automatic

    monkeypatch.setattr(automatic, "report_writer", lambda args: None)
    monkeypatch.setattr(automatic, "publish", lambda *args: (_ for _ in ()).throw(RuntimeError("compiler missing")))
    automatic.after_verify(SimpleNamespace(run_dir="run", model="test", codex_bin="test", codex_home=None))
    assert "Verification is saved" in capsys.readouterr().err






def test_solution_report_typesets_math_and_ignores_paper_locators(tmp_path):
    run = tmp_path / "run"
    make_source(run)

    class Typesetting(Fake):
        def call(self, role, payload, schema, directory):
            if schema is Draft:
                self.calls.append(role)
                draft = plan().model_dump()
                draft["entries"][0]["feedback"][0]["presentation"] = [
                    {"original": "converse", "occurrence": 0, "replacement": "$converse$", "kind": "math"}]
                draft["entries"][0]["feedback"][1]["presentation"] = [
                    {"original": "converse", "occurrence": 0, "replacement": "Section 2", "kind": "location"}]
                return Draft.model_validate(draft)
            return super().call(role, payload, schema, directory)

    text = write_report(run, tmp_path / "draft", Typesetting()).read_text()
    assert r"The \(converse\) is missing." in text
    assert "Supply the converse." in text and "Section 2" not in text
    assert json.loads((tmp_path / "draft/status.json").read_text())["status"] == "completed"


def test_presentation_edits_are_repaired_rather_than_failing_the_report(tmp_path):
    run = tmp_path / "run"
    source = make_source(run)
    source["feedback"][0]["text"] = "Use 2(n+1) and then the bound 2; the converse is missing."
    (run / "report.json").write_text(json.dumps(source))

    class Miscounted(Fake):
        def call(self, role, payload, schema, directory):
            if schema is Draft:
                self.calls.append(role)
                draft = plan().model_dump()
                draft["entries"][0]["feedback"][0]["presentation"] = [
                    {"original": "2(n+1)", "occurrence": 0, "replacement": "$2(n+1)$", "kind": "math"},
                    # occurrence 0 of "2" lies inside 2(n+1): moved to the free "bound 2"
                    {"original": "2", "occurrence": 0, "replacement": "$2$", "kind": "math"},
                    {"original": "converse", "occurrence": 3, "replacement": "$converse$", "kind": "math"}]
                return Draft.model_validate(draft)
            return super().call(role, payload, schema, directory)

    output = tmp_path / "draft"
    text = write_report(run, output, Miscounted()).read_text()
    assert r"Use \(2(n+1)\) and then the bound \(2\); the \(converse\) is missing." in text
    changes = json.loads((output / "presentation-fallbacks.json").read_text())
    assert [c["reason"] for c in changes] == ["moved from occurrence 3 to 0", "moved from occurrence 0 to 1"]


def test_editorial_checker_sees_the_printed_feedback(tmp_path):
    run = tmp_path / "run"
    make_source(run)
    seen = {}

    class Recording(Fake):
        def call(self, role, payload, schema, directory):
            if schema is Draft:
                self.calls.append(role)
                draft = plan().model_dump()
                draft["entries"][0]["feedback"][0]["presentation"] = [
                    {"original": "converse", "occurrence": 0, "replacement": "$converse$", "kind": "math"}]
                return Draft.model_validate(draft)
            seen.update(payload)
            return super().call(role, payload, schema, directory)

    write_report(run, tmp_path / "draft", Recording())
    printed = {item["ref"]: item for item in seen["printed_feedback"]}
    assert printed[0]["printed"] == "The $converse$ is missing."
    assert printed[1]["printed"] == printed[1]["original"] == "Supply the converse."


class Sequenced(Fake):
    """Scripted writer/checker: drafts and verdicts are consumed in order."""

    def __init__(self, drafts, verdicts):
        super().__init__()
        self.drafts, self.verdicts, self.payloads = list(drafts), list(verdicts), []

    def call(self, role, payload, schema, directory):
        self.calls.append(role)
        self.payloads.append(payload)
        if schema is Draft:
            return self.drafts.pop(0)
        approved, reasons = self.verdicts.pop(0)
        return Fidelity(approved=approved, reasons=reasons)


def test_withheld_draft_gets_one_retry_with_the_checker_reasons(tmp_path):
    run = tmp_path / "run"
    make_source(run)
    backend = Sequenced([plan(), plan()], [(False, ["Typeset the formula."]), (True, ["Faithful."])])
    output = tmp_path / "draft"
    assert write_report(run, output, backend).is_file()
    assert backend.calls == ["write-referee-report", "check-referee-report"] * 2
    retry = backend.payloads[2]
    assert retry["editorial_reasons"] == ["Typeset the formula."] and "entries" in retry["previous_draft"]
    assert json.loads((output / "editorial-check-attempt-1.json").read_text())["approved"] is False
    assert json.loads((output / "editorial-check.json").read_text())["approved"] is True
    assert json.loads((output / "status.json").read_text())["status"] == "completed"


def test_invalid_first_draft_is_retried_before_any_editorial_check(tmp_path):
    run = tmp_path / "run"
    make_source(run)
    incomplete = plan().model_dump()
    incomplete["entries"][0]["feedback"] = incomplete["entries"][0]["feedback"][:1]
    backend = Sequenced([Draft.model_validate(incomplete), plan()], [(True, ["Faithful."])])
    assert write_report(run, tmp_path / "draft", backend).is_file()
    assert backend.calls == ["write-referee-report", "write-referee-report", "check-referee-report"]
    assert "format checks" in backend.payloads[1]["editorial_reasons"][0]
