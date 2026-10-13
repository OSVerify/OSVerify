"""English reader delivery, exact feedback, partial outcomes and offline recovery."""
from html.parser import HTMLParser

import pytest
from test_paper_quota_report import fake_compile
from test_workflow import request

from osverifier.storage import read_json
from osverifier_reports import automatic, paper, reader
from osverifier_reports.progress import PaperProgress


def source(run):
    report = paper.PaperReport.model_validate({
        "request_id": "Example", "field": "ai", "run_status": "incomplete",
        "backend_kind": "fixture", "coverage_complete": False, "classification_status": "completed",
        "issues": [{"issue_id": f"i{n}", "severity": severity, "rationale": f"Audited impact {n}."}
                   for n, severity in enumerate(["minor", "major", "unknown", "major"])],
        "finding_details": [{"issue_id": f"i{n}", "assessment": "unresolved" if n == 3 else "confirmed", "kind": "invalid_inference"} for n in range(4)],
        "feedback": [{"issue_id": f"i{n}", "feedback_id": f"f{n}", "kind": "clarification_request" if n == 3 else "diagnosis",
                      "text": f"Exact feedback {n}. <script>not executable</script>\nSecond sentence.",
                      "source_spans": [{"document": "candidate_solution", "start_line": n+1, "end_line": n+1}],
                      "audit_artifact": f".verification/jobs/i{n}/result.json"} for n in range(4)],
    })
    paper.save(run, report)
    (run / "diagnostics.json").write_text("PRIVATE_SENTINEL")
    return report


class FeedbackParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.texts, self.current = [], None

    def handle_starttag(self, tag, attrs):
        if tag == "p" and dict(attrs).get("class") == "feedback":
            self.current = ""

    def handle_data(self, data):
        if self.current is not None:
            self.current += data

    def handle_endtag(self, tag):
        if tag == "p" and self.current is not None:
            self.texts.append(self.current)
            self.current = None


def test_reader_groups_assessment_and_impact_without_rewriting_or_leaking(tmp_path):
    report = source(tmp_path / "run")
    content, md, tex, index = reader.render_reader(report)
    parsed = FeedbackParser()
    parsed.feed(content)
    assert sorted(parsed.texts) == sorted(f.text for f in report.feedback)
    assert len(parsed.texts) == len(report.feedback)
    assert '<html lang="en">' in content and '<script>' not in content
    assert "PRIVATE_SENTINEL" not in content + md + tex
    assert content.index('id="major"') < content.index('id="minor"') < content.index('id="unknown"') < content.index('id="questions"')
    assert "Exact feedback 3" not in content.split('id="questions"')[0]
    assert "Audited impact 2." in content  # Preserve approved unknown-impact rationale.
    assert all(f.text in md for f in report.feedback)
    assert {r["issue_id"] for r in index} == {i.issue_id for i in report.issues}
    assert [r["reader_number"] for r in index] == [1, 2, 3, 4]
    assert [r["issue_id"] for r in index] == ["i1", "i0", "i2", "i3"]
    assert tex.count("Correctness Review") == 1
    assert "Review incomplete" in content and "Why this is major" in content
    assert all(s not in content for s in ("严重", "已确认", "需要澄清"))


def test_publish_reader_is_cached_and_keeps_html_when_pdf_unavailable(tmp_path, monkeypatch):
    run = tmp_path / "run"
    source(run)
    before = {p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}
    calls = []

    def missing(tex):
        calls.append(tex)
        raise RuntimeError("no TeX installation")

    monkeypatch.setattr(automatic, "compile_pdf", missing)
    output = reader.publish_reader(run)
    assert output.is_file() and output.with_suffix(".md").is_file()
    # The sidecar is the public projection, identical here because report.json is already public.
    assert read_json(output.parent / "source-report.json") == read_json(run / "report.json")
    assert read_json(output.parent / "status.json")["pdf_status"] == "failed"
    monkeypatch.setattr(automatic, "compile_pdf", lambda tex: (calls.append(tex), fake_compile(tex))[1])
    assert reader.publish_reader(run) == output and len(calls) == 2
    assert reader.publish_reader(run) == output and len(calls) == 2
    output.write_text("tampered")
    assert reader.publish_reader(run) == output and len(calls) == 3
    assert '<html lang="en">' in output.read_text()
    assert before == {p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}
    assert read_json(output.parent / "provenance.json")["model_calls"] == 0


def test_reader_new_snapshot_does_not_replace_previous_artifacts(tmp_path, monkeypatch):
    run = tmp_path / "run"
    report = source(run)
    monkeypatch.setattr(automatic, "compile_pdf", fake_compile)
    original = reader.publish_reader(run)
    before = original.read_bytes()
    report.classification_status = "failed"
    report.issues[0].severity = "unknown"
    paper.save(run, report)
    new = reader.publish_reader(run)
    assert new != original and original.read_bytes() == before
    assert "Severity assessment could not be completed" in new.read_text()


def test_reader_separates_user_content_from_internal_audit_metadata(tmp_path):
    report = source(tmp_path / "run")
    report.progress = PaperProgress(execution="failed", search="completed", scopes_searched=32,
        scopes_total=32, failed_findings=1, withheld_findings=2, image_count=16,
        image_review_calls=109, images="partial", evidence_limits=["supplement", "code"])
    report.coverage_complete = True
    content, md, tex, index = reader.render_reader(report, paper_title="A < B & C")
    for output in (content, md, tex):
        assert "Example" not in output
        for internal in ("Source index", ".verification/", "candidate_solution", "audit-index.json", "Execution:", "32/32", "109", "Assessment:"):
            assert internal not in output
        assert "1 additional potential issue" in output
        assert "supplementary material" in output
    assert "Correctness Review for A &lt; B &amp; C" in content
    assert len(index) == 4 and all(row["feedback"][0]["audit_artifact"] for row in index)
    assert content.index("Exact feedback 1") < content.index("Audited impact 1.")


@pytest.mark.parametrize("body,title", [
    ("# A Paper Title\n\n## Abstract", "A Paper Title"),
    (r"\title{A {Nested} Title}\begin{document}", "A Nested Title"),
    ("% \\title{Old title}\n\\title{New title}", "New title"),
    ("Unmarked text without a title", None),
    ("# Introduction\nSome text", None),
])
def test_automatic_title_uses_paper_content_without_falling_back_to_internal_id(tmp_path, monkeypatch, body, title):
    run = tmp_path / "run"
    source(run)
    req = request().model_copy(update={"request_id": "Example", "candidate_solution": body, "question": "Review this paper."})
    (run / "request.json").write_text(req.model_dump_json())
    monkeypatch.setattr(automatic, "compile_pdf", fake_compile)
    output = reader.publish_reader(run)
    assert read_json(output.parent / "display-metadata.json")["paper_title"] == title
    assert "Example" not in output.read_text()
    if title:
        assert "Correctness Review for " + title in output.read_text()


def test_changed_display_title_creates_new_snapshot_without_modifying_old_report(tmp_path, monkeypatch):
    run = tmp_path / "run"
    source(run)
    before = (run / "report.json").read_bytes()
    monkeypatch.setattr(automatic, "compile_pdf", fake_compile)
    first = reader.publish_reader(run, paper_title="Original title")
    second = reader.publish_reader(run, paper_title="Corrected title")
    assert first != second and "Original title" in first.read_text()
    assert "Corrected title" in second.read_text()
    assert (run / "report.json").read_bytes() == before


def test_reader_rejects_math_report(tmp_path):
    report = source(tmp_path / "run")
    report.field = "math"
    paper.save(tmp_path / "run", report)
    with pytest.raises(ValueError, match="AI paper"):
        reader.publish_reader(tmp_path / "run")
