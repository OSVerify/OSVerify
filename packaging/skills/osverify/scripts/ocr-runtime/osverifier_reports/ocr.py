"""Opt-in, image-grounded PDF transcription and issue source checks.

Native text review and the core's request/schema contracts are deliberately untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from osverifier.backend import DEFAULT_MODEL, SKILLS_ROOT, CodexBackend
from osverifier.cli_options import ExplicitOption
from osverifier.models import ReviewRequest, SourceSpan, StrictModel
from osverifier.quota import QuotaExhausted, record_quota
from osverifier.storage import (
    FrozenRunMismatch,
    atomic_json,
    atomic_text,
    digest,
    read_json,
    run_lock,
)

from .evidence import (
    attachment_paths,
    attachment_prompt,
    check_images,
    freeze_images,
    observation_schema,
    take_observations,
)
from .fields import AI_SKILLS, AIPaperBackend, code_sha256
from .writer import CodexWriter

ROOT = Path(__file__).parent


class Transcription(StrictModel):
    page_number: int = Field(ge=1)
    text: str = Field(min_length=1)
    unreadable: list[str]


class PageCheck(StrictModel):
    page_number: int = Field(ge=1)
    status: Literal["approved", "corrected", "unreadable"]
    text: str = Field(min_length=1)
    corrections: list[str]
    unreadable: list[str]
    rationale: str = Field(min_length=1)


class SpanCheck(StrictModel):
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    page_numbers: list[int] = Field(min_length=1)
    status: Literal["faithful", "ocr_error", "unreadable"]
    original_quote: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class SourceCheck(StrictModel):
    status: Literal["faithful", "ocr_error", "unreadable"]
    checks: list[SpanCheck] = Field(min_length=1)
    # The checker must inspect supporting assumptions and context, not only target lines.
    dependencies_checked: list[SpanCheck] = Field(min_length=1)
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "faithful" and any(c.status != "faithful" for c in self.checks + self.dependencies_checked):
            raise ValueError("faithful source check cannot contain a mismatch or unreadable dependency")
        return self


class OcrSourceBlocked(ValueError):
    """Input fidelity failure, never a scientific rejection or paper feedback."""


def assets():
    return {"code": code_sha256(Path(__file__)), **{
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((ROOT / "skills").glob("*pdf*/SKILL.md"))},
        "evidence": code_sha256(ROOT / "evidence.py"),
        "writer": code_sha256(ROOT / "writer.py")}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _check_file(path, expected):
    if not Path(path).is_file() or _sha(path) != expected:
        raise FrozenRunMismatch(f"Frozen OCR artifact changed: {Path(path).name}")


def _call(caller, role, payload, output_type, directory):
    """Fresh, retained attempts. Successful preparation calls can be resumed exactly."""
    key = digest({"role": role, "payload": payload, "schema": output_type.model_json_schema(), "assets": assets()})
    cached = directory / "approved.json"
    if cached.exists():
        saved = read_json(cached)
        if saved["input_sha256"] != key or digest(saved["output"]) != saved["output_sha256"]:
            raise FrozenRunMismatch("OCR call input or cached result changed")
        return output_type.model_validate(saved["output"])
    attempt = directory / uuid.uuid4().hex
    atomic_json(attempt / "provenance.json", {"input_sha256": key, "assets": assets()})
    value = caller.call(role, payload, output_type, attempt)
    observations = attempt / "paper-observations.json"
    if observations.exists() and getattr(value, "status", None) == "approved":
        if any(c["status"] != "inspected" for c in read_json(observations)["visual_inspections"]):
            raise OcrSourceBlocked("A page with unreadable visual evidence cannot be approved")
    atomic_json(cached, {"input_sha256": key, "output": value.model_dump(mode="json"),
                         "output_sha256": digest(value.model_dump(mode="json"))})
    return value


def prepare_pdf(pdf, directory, caller, *, identity, max_checks=3, log=print):
    """Transcribe each physical page, independently check, then freeze approved text.

    A corrected check is a new draft, never approval of its own correction. Another
    independent call must approve that exact draft. Unreadable pages block review.
    """
    if max_checks < 1:
        raise ValueError("max_checks must be positive")
    directory, pdf = Path(directory).resolve(), Path(pdf).resolve()
    with run_lock(directory):
        frozen = {"pdf_sha256": _sha(pdf), "backend": identity, "assets": assets(), "max_checks": max_checks}
        provenance = directory / "preparation.json"
        if provenance.exists() and read_json(provenance) != frozen:
            raise FrozenRunMismatch("PDF preparation inputs, backend or assets changed; use a new directory")
        atomic_json(provenance, frozen)
        if (directory / "ocr-manifest.json").exists():
            return load_preparation(directory / "ocr-manifest.json")
        images, text_lines, pages = [], [], []
        try:
            # Rendering failures (e.g. a page timeout) are recorded as a blocked input too.
            images = freeze_images([], directory / "images", pdf)
            atomic_json(directory / "status.json", {"status": "transcribing", "approved_pages": 0, "total_pages": len(images)})
            for image in images:
                number = image["page_number"]
                log(f"PDF page {number}/{len(images)}: transcribe and independently check")
                page_dir = directory / "pages" / f"page-{number:04d}"
                payload = {"page_number": number, "paper_images": [image]}
                draft = _call(caller, "transcribe-pdf-page", payload, Transcription, page_dir / "transcribe")
                if draft.page_number != number:
                    raise ValueError("Transcription returned the wrong page number")
                text, unreadable = draft.text, draft.unreadable
                approved = None
                for n in range(max_checks):
                    check = _call(caller, "check-pdf-transcription", {**payload, "transcription": text, "unreadable": unreadable},
                                  PageCheck, page_dir / f"check-{n + 1:04d}")
                    if check.page_number != number:
                        raise ValueError("Transcription check returned the wrong page number")
                    if check.status == "approved":
                        if check.text != text or check.corrections or check.unreadable or unreadable:
                            raise ValueError("Approval must preserve the exact draft and resolve all unreadable content")
                        approved = check
                        break
                    if check.status == "unreadable" or check.unreadable:
                        raise OcrSourceBlocked(f"Page {number} is unreadable; see {page_dir}")
                    if check.text == text or not check.corrections:
                        raise ValueError("A corrected check must supply changed text and correction evidence")
                    text, unreadable = check.text, check.unreadable
                if approved is None:
                    raise OcrSourceBlocked(f"Page {number} has no independent approval after {max_checks} checks")
                # Explicit markers and exact line ranges map every input line to a physical page.
                start = len(text_lines) + 1
                text_lines.extend([f"<!-- PDF page {number} -->", *text.splitlines(), ""])
                pages.append({"page_number": number, "start_line": start, "end_line": len(text_lines),
                              "image_id": image["image_id"], "approval": str((page_dir / f"check-{n + 1:04d}" / "approved.json").resolve())})
                atomic_json(directory / "status.json", {"status": "checking", "approved_pages": number, "total_pages": len(images)})
            text = "\n".join(text_lines) + "\n"
            atomic_text(directory / "paper.md", text)
            tracked = [provenance, directory / "paper.md", *sorted((directory / "pages").rglob("approved.json"))]
            manifest = {"format": "ocr-1", "paper": str(directory / "paper.md"), "paper_sha256": _sha(directory / "paper.md"),
                        "pdf": str(directory / "images/source.pdf"), "pdf_sha256": frozen["pdf_sha256"],
                        "images": images, "pages": pages, "preparation": frozen,
                        "files": {str(p.resolve()): _sha(p) for p in tracked}}
            check_images(images)
            atomic_json(directory / "ocr-manifest.json", manifest)
            atomic_json(directory / "status.json", {"status": "approved", "approved_pages": len(images), "total_pages": len(images)})
            return load_preparation(directory / "ocr-manifest.json")
        except BaseException as exc:
            atomic_json(directory / "status.json", {"status": "blocked", "reason": type(exc).__name__, "detail": str(exc), "approved_pages": len(pages), "total_pages": len(images)})
            if isinstance(exc, QuotaExhausted):
                record_quota(directory, exc, phase="pdf_transcription")
            raise


def load_preparation(path):
    path = Path(path).resolve()
    m = read_json(path)
    if m.get("format") != "ocr-1":
        raise ValueError("Unsupported OCR preparation manifest")
    for name, expected in m["files"].items():
        _check_file(name, expected)
    _check_file(m["paper"], m["paper_sha256"])
    _check_file(m["pdf"], m["pdf_sha256"])
    check_images(m["images"])
    # Verify all pages and line coverage, even when a modified manifest looks plausible.
    lines = Path(m["paper"]).read_text().splitlines()
    cursor = 1
    if len(m["pages"]) != len(m["images"]):
        raise FrozenRunMismatch("OCR page coverage changed")
    for number, page in enumerate(m["pages"], 1):
        if page["page_number"] != number or page["start_line"] != cursor or page["end_line"] < cursor:
            raise FrozenRunMismatch("OCR page mapping changed")
        image = m["images"][number - 1]
        if image.get("page_number") != number or page["image_id"] != image["image_id"]:
            raise FrozenRunMismatch("OCR image mapping changed")
        approved = read_json(Path(page["approval"]))
        check = PageCheck.model_validate(approved["output"])
        if check.status != "approved" or check.unreadable or check.corrections or check.page_number != number:
            raise FrozenRunMismatch("OCR page has no clean approval")
        expected = [f"<!-- PDF page {number} -->", *check.text.splitlines(), ""]
        if lines[cursor - 1:page["end_line"]] != expected:
            raise FrozenRunMismatch("OCR text differs from its independent page approval")
        cursor = page["end_line"] + 1
    if cursor != len(lines) + 1:
        raise FrozenRunMismatch("OCR mapping does not cover the full text")
    return m


def required_spans(payload, paper):
    objects = [payload.get("proposed_issue", {}), payload.get("issue_review", {})]
    spans = {}
    for obj in objects:
        for raw in obj.get("source_spans", []):
            span = SourceSpan.model_validate(raw)
            if span.document == "candidate_solution":
                if span.end_line > len(paper.splitlines()):
                    raise ValueError("OCR source span outside paper")
                spans[(span.start_line, span.end_line)] = span.model_dump(mode="json")
    if not spans:
        raise OcrSourceBlocked("OCR issue needs a candidate-paper location for original-page checking")
    return list(spans.values())


class OcrPaperBackend:
    """A source-fidelity gate around paper calls; failure never becomes a paper defect."""

    def __init__(self, backend, manifest_path, caller):
        self.backend, self.manifest_path, self.caller = backend, Path(manifest_path).resolve(), caller
        self.manifest = load_preparation(self.manifest_path)
        self.manifest_sha256 = _sha(self.manifest_path)
        self.profile = assets()
        if isinstance(caller, CodexWriter) and isinstance(backend, CodexBackend):
            caller.process_backend = backend

    def check(self):
        _check_file(self.manifest_path, self.manifest_sha256)
        load_preparation(self.manifest_path)
        if assets() != self.profile:
            raise FrozenRunMismatch("OCR code or skills changed during review")

    @property
    def identity(self):
        self.check()
        return {**self.backend.identity, "ocr_input": {"manifest_sha256": self.manifest_sha256,
                "preparation": self.manifest["preparation"], "profile": self.profile}}

    def _source_check(self, payload, directory):
        paper = payload["request"]["candidate_solution"]
        if hashlib.sha256(paper.encode()).hexdigest() != self.manifest["paper_sha256"]:
            raise FrozenRunMismatch("Review text differs from approved PDF transcription")
        spans = required_spans(payload, paper)
        # Deterministic physical-page mapping, plus adjacent context. Full catalogue
        # remains available to follow definitions, hypotheses and omission allegations.
        needed = {p["page_number"] for p in self.manifest["pages"] for s in spans
                  if p["start_line"] <= s["end_line"] and s["start_line"] <= p["end_line"]}
        near = needed | {n + d for n in needed for d in (-1, 1)}
        attached = [i for i in self.manifest["images"] if i["page_number"] in near]
        source_payload = {"paper": paper, "numbered_paper": "\n".join(f"{n}: {line}" for n, line in enumerate(paper.splitlines(), 1)),
                          "required_spans": spans, "pages": self.manifest["pages"],
                          "paper_images": self.manifest["images"], "proposed_issue": payload.get("proposed_issue") or {},
                          "issue_review": payload.get("issue_review") or {}, "ocr_attached_image_ids": [i["image_id"] for i in attached]}
        result = self.caller.call("check-pdf-issue-source", source_payload, SourceCheck, directory)
        visual = directory / "paper-observations.json"
        if visual.exists():
            visual.rename(directory / "ocr-source-observations.json")
        covered = {(c.start_line, c.end_line) for c in result.checks}
        required = {(s["start_line"], s["end_line"]) for s in spans}
        if covered != required or len(result.checks) != len(required):
            raise ValueError("PDF source check must cover each exact required span once")
        for c in result.checks + result.dependencies_checked:
            if c.end_line < c.start_line or c.end_line > len(paper.splitlines()):
                raise ValueError("PDF source-check dependency outside paper")
            mapped = {p["page_number"] for p in self.manifest["pages"]
                      if p["start_line"] <= c.end_line and c.start_line <= p["end_line"]}
            if set(c.page_numbers) != mapped:
                raise ValueError("PDF source-check page numbers do not match frozen line locations")
        atomic_json(directory / "source-check.json", result.model_dump(mode="json"))
        visual = directory / "ocr-source-observations.json"
        if result.status == "faithful" and visual.exists():
            inspected = {c["image_id"] for c in read_json(visual)["visual_inspections"] if c["status"] == "inspected"}
            used_pages = {n for c in result.checks + result.dependencies_checked for n in c.page_numbers}
            used_ids = {i["image_id"] for i in self.manifest["images"] if i["page_number"] in used_pages}
            if not used_ids.issubset(inspected):
                raise OcrSourceBlocked("Faithful source check requires legible visual inspection of every cited/dependency page")
        if result.status != "faithful":
            raise OcrSourceBlocked(f"PDF source check: {result.status}; preserve input and reprepare/review affected material in a new run; see {directory / 'source-check.json'}")
        return result

    def generate_with_references(self, role, payload, schema, attempt_dir, *, reference_tools):
        return self.generate(role, payload, schema, attempt_dir, reference_tools=reference_tools)

    def generate(self, role, payload, schema, attempt_dir, *, reference_tools=None):
        self.check()
        is_ai = payload.get("paper_review", {}).get("field") == "ai"
        if role in ("issue_validator", "feedback_auditor"):
            source = self._source_check(payload, attempt_dir / "ocr-source")
            payload = {**payload, "ocr_source_check": source.model_dump(mode="json")}
        context = {"manifest_sha256": self.manifest_sha256,
                   "instructions": "This input is OCR transcription. Consult original page pixels for doubtful content. OCR/layout defects are input failures, never paper errors. No confirmed finding or diagnosis may depend on unreadable or mismatched source. A source-fidelity approval does not establish mathematical correctness.",
                   "pages": self.manifest["pages"]}
        payload = {**payload, "ocr_input": context}
        substantive = role in ("proposer", "issue_validator", "feedback_auditor")
        images = self.manifest["images"] if substantive else []
        if substantive and not is_ai:
            payload["paper_images"] = images
            context["visual_instructions"] = attachment_prompt(images)
            schema = observation_schema(schema, images)
        if isinstance(self.backend, CodexBackend):
            # AI wrapper owns its own image schema/observations. Math OCR is opt-in
            # and adds pixels without touching the normal math role bundle.
            if is_ai:
                images = [i for i in payload.get("paper_images", [])
                          if i["image_id"] in payload["paper_review"].get("attached_image_ids", [])]
            result = self.backend.generate_with_skills(role, payload, schema, attempt_dir,
                        AI_SKILLS if is_ai else SKILLS_ROOT, images=attachment_paths(images), ephemeral=True,
                        **({"reference_tools": reference_tools} if reference_tools else {}))
        elif reference_tools and hasattr(self.backend, "generate_with_references"):
            result = self.backend.generate_with_references(
                role, payload, schema, attempt_dir, reference_tools=reference_tools)
        else:
            result = self.backend.generate(role, payload, schema, attempt_dir)
        self.check()
        if substantive and not is_ai:
            result, observations = take_observations(result, images)
            atomic_json(attempt_dir / "ocr-observations.json", observations)
        return result

    def attempt_artifacts(self, attempt_dir):
        return {str(p.relative_to(attempt_dir)): _sha(p) for p in attempt_dir.rglob("*.json")
                if p.name in ("source-check.json", "ocr-source-observations.json", "ocr-observations.json")}

    def cancel(self):
        if hasattr(self.backend, "cancel"):
            self.backend.cancel()

    def halt_for_quota(self, error):
        if hasattr(self.backend, "halt_for_quota"):
            self.backend.halt_for_quota(error)


class OcrAIPaperBackend(AIPaperBackend):
    """Retain both AI visual receipts and mandatory OCR gates in the job cache."""

    def attempt_artifacts(self, attempt_dir):
        return {**self.backend.attempt_artifacts(attempt_dir), **super().attempt_artifacts(attempt_dir)}


def parser():
    p = argparse.ArgumentParser(description="Opt-in PDF transcription and image-grounded paper review")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("prepare-pdf", "verify-pdf"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--model", default=DEFAULT_MODEL)
        cmd.add_argument("--codex-bin", default="codex")
        cmd.add_argument("--codex-home", type=Path)
        # Explicit values also apply to severity classification and report writing.
        cmd.add_argument("--reasoning-effort", default="xhigh", action=ExplicitOption)
        cmd.add_argument("--timeout-seconds", type=float, default=900, action=ExplicitOption)
        cmd.set_defaults(explicit_options=frozenset())
        cmd.add_argument("--run-dir", type=Path, required=True)
        if name == "prepare-pdf":
            cmd.add_argument("--pdf", type=Path, required=True)
            cmd.add_argument("--max-checks", type=int, default=3)
        else:
            cmd.add_argument("--ocr-manifest", type=Path, required=True)
            cmd.set_defaults(field="math")  # mathematics only for now
            cmd.add_argument("--question", type=Path, help="Optional exact user-selected review scope")
            cmd.add_argument("--request-id", default="paper-pdf")
            cmd.add_argument("--workers", type=int, default=16)
            cmd.add_argument("--max-attempts", type=int, default=2)
            cmd.add_argument("--max-issues", type=int)
            cmd.add_argument("--root-only", action="store_true")
            cmd.add_argument("--early-stop", action="store_true")
            cmd.add_argument("--web-search", choices=("live", "cached", "disabled"), default="live")
            cmd.add_argument("--no-report", action="store_true")
    return p


FULL_PAPER_SCOPE = (
    "Review correctness of the full supplied paper, including all claims, assumptions, proofs, "
    "supporting evidence and appendices. Preserve qualifications. Exclude novelty, significance "
    "and publication recommendations.")


def transcribe(pdf, directory, *, model, codex_bin="codex", codex_home=None, reasoning_effort="xhigh",
               timeout_seconds=900, max_checks=3, log=print):
    """Checked page-by-page transcription of one PDF into ``directory`` (reused when approved)."""
    caller = CodexWriter(model, codex_bin, codex_home, timeout=timeout_seconds, reasoning_effort=reasoning_effort)
    backend = CodexBackend(model, codex_bin, codex_home, reasoning_effort, timeout_seconds, "disabled")
    caller.process_backend = backend
    return prepare_pdf(pdf, directory, caller, identity=backend.identity, max_checks=max_checks, log=log)


def review_transcription(args):
    """Paper review of an approved transcription, with original-page source checks.

    ``args`` carries the ``osverify verify`` paper options plus ``ocr_manifest``;
    ``args.question`` is an optional scope file (default: the full paper).
    """
    caller = CodexWriter(args.model, args.codex_bin, args.codex_home, timeout=args.timeout_seconds, reasoning_effort=args.reasoning_effort)
    m = load_preparation(args.ocr_manifest)
    if args.early_stop and not args.decompose:
        raise ValueError("--early-stop requires decomposition")
    args.paper_image = []
    args.paper_pdf = Path(m["pdf"]) if args.field == "ai" else None
    scope = args.question.read_text() if args.question else (
        FULL_PAPER_SCOPE + " Input is checked PDF transcription; verify original "
        "pages before confirming a defect. OCR errors are input failures, not paper errors.")
    request = ReviewRequest(request_id=args.request_id, question=scope, candidate_solution=Path(m["paper"]).read_text())
    base = CodexBackend(args.model, args.codex_bin, args.codex_home, args.reasoning_effort,
                        args.timeout_seconds, args.web_search)
    backend = OcrPaperBackend(base, args.ocr_manifest, caller)
    if args.field == "ai":
        # Same frozen image paths run_paper will validate; it recognizes this
        # profile subclass and does not wrap away the OCR cache artifacts.
        with run_lock(args.run_dir.resolve()):
            images = freeze_images([], args.run_dir.resolve() / "paper-images", Path(m["pdf"]))
        backend = OcrAIPaperBackend(backend, images)
    from .paper import run_paper

    report = run_paper(args, request, backend)
    atomic_json(args.run_dir / "ocr-input.json", {"manifest": str(args.ocr_manifest.resolve()), "sha256": _sha(args.ocr_manifest)})
    if not args.no_report and report.run_status == "completed" and report.classification_status == "completed":
        from .automatic import after_ai_review, after_verify

        if args.field == "ai":
            after_ai_review(args.run_dir)
        else:
            after_verify(args)
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0 if report.run_status == "completed" and report.classification_status == "completed" else 2


def main(argv=None):
    """Legacy two-step PDF entry point (also used by the packaged skill)."""
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare-pdf":
            m = transcribe(args.pdf, args.run_dir, model=args.model, codex_bin=args.codex_bin,
                           codex_home=args.codex_home, reasoning_effort=args.reasoning_effort,
                           timeout_seconds=args.timeout_seconds, max_checks=args.max_checks)
            print(json.dumps({"status": "approved", "paper": m["paper"], "ocr_manifest": str(args.run_dir.resolve() / "ocr-manifest.json")}, indent=2))
            return 0
        args.decompose = not args.root_only
        return review_transcription(args)
    except QuotaExhausted as exc:
        record_quota(args.run_dir, exc, phase="pdf_input_or_review")
        print(str(exc), file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"PDF input/review blocked: {exc}", file=sys.stderr)
        return 2



def ocr_main(argv=None):
    """``osverify ocr``: checked transcription of one PDF for later paper review."""
    p = argparse.ArgumentParser(
        prog="osverify ocr",
        description="Transcribe a PDF page by page with independent checks; review it with "
        "`osverify verify --review-type paper --paper PAPER.pdf --ocr-dir DIR`.",
    )
    p.add_argument("--pdf", type=Path, required=True)
    p.add_argument("--run-dir", type=Path, required=True, help="Directory for the approved transcription")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--codex-bin", default="codex")
    p.add_argument("--codex-home", type=Path)
    p.add_argument("--reasoning-effort", default="xhigh")
    p.add_argument("--timeout-seconds", type=float, default=900)
    p.add_argument("--max-checks", type=int, default=3, help="Independent checks per page")
    args = p.parse_args(argv)
    try:
        m = transcribe(args.pdf, args.run_dir, model=args.model, codex_bin=args.codex_bin,
                       codex_home=args.codex_home, reasoning_effort=args.reasoning_effort,
                       timeout_seconds=args.timeout_seconds, max_checks=args.max_checks)
    except QuotaExhausted as exc:
        record_quota(args.run_dir, exc, phase="pdf_transcription")
        print(str(exc), file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"PDF transcription blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "approved", "paper": m["paper"], "ocr_dir": str(args.run_dir.resolve())}, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
