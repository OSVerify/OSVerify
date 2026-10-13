"""Input/source-fidelity contracts; fixtures do not establish OCR or math accuracy."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image
from test_paper_review import Classifier, args
from test_workflow import FixtureBackend, request

from osverifier.models import FeedbackAudit, IssueReview
from osverifier.storage import FrozenRunMismatch, atomic_json, read_json
from osverifier_reports import ocr, paper


class Calls:
    def __init__(self, *, correction=False, unreadable=False, mismatch=False):
        self.calls = []
        self.correction, self.unreadable, self.mismatch = correction, unreadable, mismatch

    def call(self, role, payload, output, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.calls.append((role, payload))
        if role == "transcribe-pdf-page":
            return output(page_number=payload["page_number"], text="Every even integer is divisible by two.", unreadable=[])
        if role == "check-pdf-transcription":
            corrected = self.correction and payload["transcription"].startswith("Every")
            return output(page_number=payload["page_number"], status="unreadable" if self.unreadable else "corrected" if corrected else "approved",
                          text="Each even integer is divisible by two." if corrected else payload["transcription"],
                          corrections=["Original says Each, not Every"] if corrected else [],
                          unreadable=["symbol"] if self.unreadable else [], rationale="Compared all original page content.")
        spans = payload["required_spans"]
        checks = []
        for span in spans:
            numbers = [p["page_number"] for p in payload["pages"]
                       if p["start_line"] <= span["end_line"] and span["start_line"] <= p["end_line"]]
            checks.append(dict(start_line=span["start_line"], end_line=span["end_line"], page_numbers=numbers,
                               status="ocr_error" if self.mismatch else "faithful", original_quote="Each even integer is divisible by two.", rationale="Original-page comparison."))
        result = output(status="ocr_error" if self.mismatch else "faithful", checks=checks,
                        dependencies_checked=checks[:1], rationale="Checked claim and surrounding assumptions.")
        return result


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    def freeze(paths, directory, pdf):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "source.pdf").write_bytes(pdf.read_bytes())
        image = directory / "page.png"
        Image.new("RGB", (50, 50), "white").save(image)
        return [{"image_id": "image-0001", "page_number": 1, "path": str(image),
                 "sha256": hashlib.sha256(image.read_bytes()).hexdigest(), "views": []}]
    monkeypatch.setattr(ocr, "freeze_images", freeze)
    pdf = tmp_path / "original.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    def make(caller=None, **kwargs):
        caller = caller or Calls()
        manifest = ocr.prepare_pdf(pdf, tmp_path / "prepared", caller, identity={"kind": "fixture"}, log=lambda _: None, **kwargs)
        return tmp_path / "prepared/ocr-manifest.json", manifest, caller
    return make


def test_page_correction_requires_another_independent_approval_and_resume(preparation):
    path, m, calls = preparation(Calls(correction=True))
    assert [role for role, _ in calls.calls] == ["transcribe-pdf-page", "check-pdf-transcription", "check-pdf-transcription"]
    assert "Each even" in Path(m["paper"]).read_text()
    assert Path(m["pages"][0]["approval"]).parent.name == "check-0002"
    preparation(calls)
    assert len(calls.calls) == 3
    assert ocr.load_preparation(path) == m


def test_unreadable_source_blocks_formal_input(preparation):
    with pytest.raises(ocr.OcrSourceBlocked, match="unreadable"):
        preparation(Calls(unreadable=True))


def test_corrections_are_not_self_approved(preparation):
    with pytest.raises(ocr.OcrSourceBlocked, match="no independent approval"):
        preparation(Calls(correction=True), max_checks=1)


@pytest.mark.parametrize("artifact", ["paper", "pdf", "image", "approval"])
def test_frozen_input_and_approval_integrity(preparation, artifact):
    path, m, _ = preparation()
    target = {"paper": m["paper"], "pdf": m["pdf"], "image": m["images"][0]["path"], "approval": m["pages"][0]["approval"]}[artifact]
    with Path(target).open("ab") as f:
        f.write(b" changed")
    with pytest.raises((FrozenRunMismatch, ValueError)):
        ocr.load_preparation(path)


def test_page_mapping_cannot_omit_lines(preparation):
    path, m, _ = preparation()
    m["pages"][0]["end_line"] -= 1
    atomic_json(path, m)
    with pytest.raises(FrozenRunMismatch):
        ocr.load_preparation(path)


def _payload(m):
    return {"request": {"candidate_solution": Path(m["paper"]).read_text()},
            "proposed_issue": {"source_spans": [{"document": "candidate_solution", "start_line": 2, "end_line": 2}]}}


def test_ocr_mismatch_blocks_before_mathematical_validator(preparation, tmp_path):
    path, m, _ = preparation()
    base = FixtureBackend()
    backend = ocr.OcrPaperBackend(base, path, Calls(mismatch=True))
    with pytest.raises(ocr.OcrSourceBlocked, match="ocr_error"):
        backend.generate("issue_validator", _payload(m), IssueReview.model_json_schema(), tmp_path / "attempt")
    assert base.calls == []
    assert read_json(tmp_path / "attempt/ocr-source/source-check.json")["status"] == "ocr_error"


def test_auditor_rechecks_new_validator_locations(preparation, tmp_path):
    path, m, _ = preparation()
    payload = _payload(m)
    payload["issue_review"] = {"source_spans": [{"document": "candidate_solution", "start_line": 1, "end_line": 2}]}
    calls = Calls(mismatch=True)
    backend = ocr.OcrPaperBackend(FixtureBackend(), path, calls)
    with pytest.raises(ocr.OcrSourceBlocked):
        backend.generate("feedback_auditor", payload, FeedbackAudit.model_json_schema(), tmp_path / "attempt")
    assert len(calls.calls[0][1]["required_spans"]) == 2


def test_source_checker_cannot_omit_required_span(preparation, tmp_path):
    path, m, _ = preparation()
    class Omit(Calls):
        def call(self, *args):
            result = super().call(*args)
            result.checks[0].start_line = 1
            return result
    backend = ocr.OcrPaperBackend(FixtureBackend(), path, Omit())
    with pytest.raises(ValueError, match="each exact"):
        backend._source_check(_payload(m), tmp_path / "attempt")


def test_faithful_source_does_not_itself_confirm_math(preparation, tmp_path):
    path, m, _ = preparation()
    backend = ocr.OcrPaperBackend(FixtureBackend(), path, Calls())
    result = backend._source_check(_payload(m), tmp_path / "attempt")
    assert result.status == "faithful"
    assert set(backend.attempt_artifacts(tmp_path / "attempt")) == {"source-check.json"}


def test_mismatched_review_text_never_enters_source_call(preparation, tmp_path):
    path, m, _ = preparation()
    calls = Calls()
    backend = ocr.OcrPaperBackend(FixtureBackend(), path, calls)
    payload = _payload(m)
    payload["request"]["candidate_solution"] += "Injected claim."
    with pytest.raises(FrozenRunMismatch):
        backend._source_check(payload, tmp_path / "attempt")
    assert calls.calls == []


def test_legacy_math_paper_never_dispatches_ocr(tmp_path, monkeypatch):
    def fail(*a, **kw):
        raise AssertionError("Native source must not enter OCR")
    monkeypatch.setattr(ocr, "prepare_pdf", fail)
    monkeypatch.setattr(ocr.OcrPaperBackend, "generate", fail)
    report = paper.run_paper(args(tmp_path), request(), FixtureBackend(), Classifier())
    assert report.run_status == "completed"


def test_failed_source_check_cannot_release_paper_error(preparation, tmp_path):
    path, m, _ = preparation()
    req = request().model_copy(update={"candidate_solution": Path(m["paper"]).read_text()})
    # Use the core's real error handling/release path with scripted model outputs.
    class SourceBackend(FixtureBackend):
        def generate(self, role, payload, schema, attempt):
            output = super().generate(role, payload, schema, attempt)
            if role == "proposer":
                for issue in output["issues"]:
                    issue["source_spans"] = [{"document": "candidate_solution", "start_line": 2, "end_line": 2}]
                output["visual_inspections"] = [{"image_id": "image-0001", "status": "inspected", "observation": "Fixture only."}]
            return output
    options = args(tmp_path / "review")
    options.decompose = False
    backend = ocr.OcrPaperBackend(SourceBackend({"whole_solution": ["defect"]}), path, Calls(mismatch=True))
    report = paper.run_paper(options, req, backend, Classifier())
    assert report.run_status == "incomplete"
    assert not report.feedback
    assert not report.coverage_complete


def test_long_pdf_renders_by_page_and_render_timeouts_are_reported(tmp_path, monkeypatch, capsys):
    import subprocess

    from osverifier_reports import evidence

    real_run, rendered = subprocess.run, []
    stall_page = []

    def run(cmd, *args, **kwargs):
        name = Path(cmd[0]).name
        if name == "pdfinfo":
            return subprocess.CompletedProcess(cmd, 0, b"Title: x\nPages:           3\n", b"")
        if name == "pdftoppm":
            page = int(cmd[cmd.index("-f") + 1])
            assert cmd[cmd.index("-l") + 1] == str(page) and kwargs["timeout"] == evidence.PAGE_RENDER_SECONDS
            if page in stall_page:
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            Image.new("RGB", (40, 40), "white").save(f"{cmd[-1]}-{page}.png")
            rendered.append(page)
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(evidence.shutil, "which", lambda tool: None if tool == "pdftotext" else f"/poppler/{tool}")
    pdf = tmp_path / "long.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    assert [p.name for p in evidence.pdf_pages(pdf, tmp_path / "pages")] == ["pdf-page-1.png", "pdf-page-2.png", "pdf-page-3.png"]
    assert rendered == [1, 2, 3]

    stall_page.append(2)
    with pytest.raises(ValueError, match="page 2 exceeded 120 seconds"):
        evidence.pdf_pages(pdf, tmp_path / "stalled")

    # The CLI reports a blocked input instead of crashing with a traceback.
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\necho codex-cli 0.162.0\n")
    codex.chmod(0o755)
    monkeypatch.setattr(evidence.shutil, "which", lambda tool: str(codex) if tool == str(codex) else None if tool == "pdftotext" else f"/poppler/{tool}")
    code = ocr.main(["prepare-pdf", "--pdf", str(pdf), "--run-dir", str(tmp_path / "prepared"), "--model", "m",
                     "--codex-bin", str(codex), "--codex-home", str(tmp_path / "account")])
    assert code == 2 and "page 2 exceeded 120 seconds" in capsys.readouterr().err
    assert read_json(tmp_path / "prepared/status.json")["status"] == "blocked"
