"""Turn ``osverify verify --review-type paper --paper ...`` into review inputs.

Without ``--question`` the review scope is selected automatically (the same extraction
the GUI uses: smart, lazy, manual labels, or the full paper). A PDF is first
transcribed page by page with independent checks and then reviewed against the
original pages. Inputs are staged inside the run directory and reused on resume.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from osverifier.quota import QuotaExhausted, record_quota
from osverifier.storage import atomic_text

INPUTS = "paper-inputs"
PDF_INPUT = "pdf-input"


def _log(message):
    print(f"[paper] {message}", file=sys.stderr, flush=True)


def _stage(paper: Path, inputs: Path, name: str) -> Path:
    """Copy the paper into the run once; a changed paper needs a new run."""
    text = Path(paper).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("the paper is empty")
    staged = inputs / name
    if staged.exists():
        if staged.read_text(encoding="utf-8") != text:
            raise ValueError("The paper changed since this run started; use a new run directory")
    else:
        inputs.mkdir(parents=True, exist_ok=True)
        atomic_text(staged, text)
    return staged


def _scope(args, targets: str, staged: Path, inputs: Path) -> tuple[Path, Path]:
    """Return (question, solution) files for the selected targets."""
    if targets == "full":
        from .ocr import FULL_PAPER_SCOPE

        question = inputs / "question-full.md"
        if not question.exists():
            atomic_text(question, FULL_PAPER_SCOPE + "\n")
        return question, staged
    from .paper_prepare import prepare

    prepare(argparse.Namespace(
        paper=staged, mode=targets, tags=list(args.labels), model=args.model,
        codex_bin=args.codex_bin, codex_home=str(args.codex_home) if args.codex_home else None,
        reasoning_effort=args.reasoning_effort, timeout_seconds=args.timeout_seconds,
        max_attempts=args.max_attempts, run_dir=args.run_dir,
    ), _log)
    return inputs / "question.md", inputs / "solution.md"


DEFAULT_REQUEST_ID = "review"


def paper_title(text: str, fallback: str) -> str:
    """The paper's own title (LaTeX \\title, else the first Markdown heading)."""
    match = re.search(r"\\title\s*(?:\[[^\]]*\])?\s*{((?:[^{}]|{[^{}]*})*)}", text)
    if match:
        title = re.sub(r"\\thanks\s*{[^{}]*}|\\\\|[{}~]", " ", match.group(1))
    else:
        heading = re.search(r"(?m)^#\s+(.+)$", text)
        title = heading.group(1) if heading else fallback
    title = " ".join(title.replace("*", "").split())
    if title.isupper():
        title = title.capitalize()
    return title or fallback


def _name_the_request(args, text: str, fallback: str) -> None:
    """Report titles use the request ID; name an unnamed paper review after the paper."""
    if args.request_id == DEFAULT_REQUEST_ID:
        args.request_id = paper_title(text, fallback)


def resolve_targets(args) -> str:
    targets = args.targets or ("full" if args.field == "ai" else "smart")
    if targets == "manual" and not args.labels:
        raise ValueError("--targets manual requires --labels")
    if args.labels and targets != "manual":
        raise ValueError("--labels requires --targets manual")
    return targets


def prepare_paper_review(args) -> int | None:
    """Fill ``args.question``/``args.solution`` for a native paper, or review a PDF.

    Returns ``None`` when the caller should continue with the ordinary paper review,
    or an exit code when the PDF review already ran here.
    """
    if args.solution is None:
        raise ValueError("--paper is required for --review-type paper")
    paper = Path(args.solution)
    is_pdf = paper.suffix.lower() == ".pdf"
    if args.question is not None:
        if args.targets or args.labels:
            raise ValueError("--targets/--labels select the scope automatically; omit them with --question")
        if not is_pdf:
            _name_the_request(args, paper.read_text(encoding="utf-8"), paper.stem)
            return None
    if args.ocr_dir is not None and not is_pdf:
        raise ValueError("--ocr-dir applies only to a PDF --paper")
    targets = None if args.question is not None else resolve_targets(args)
    inputs = args.run_dir / INPUTS
    if not is_pdf:
        staged = _stage(paper, inputs, "paper" + paper.suffix.lower())
        _name_the_request(args, staged.read_text(encoding="utf-8"), paper.stem)
        args.question, args.solution = _scope(args, targets, staged, inputs)
        return None
    return _review_pdf(args, paper, targets, inputs)


def _review_pdf(args, pdf: Path, targets: str | None, inputs: Path) -> int:
    import subprocess

    from .ocr import _sha, load_preparation, review_transcription, transcribe

    if args.paper_pdf or args.paper_image:
        raise ValueError("--paper-pdf/--paper-image are not used with a PDF --paper; its pages are attached")
    if targets == "manual":
        raise ValueError("--targets manual needs LaTeX labels; use the LaTeX source, or --targets smart, lazy or full")
    try:
        if args.ocr_dir is not None:
            manifest = Path(args.ocr_dir) / "ocr-manifest.json"
            m = load_preparation(manifest)
            if m["pdf_sha256"] != _sha(pdf):
                raise ValueError("--ocr-dir was transcribed from a different PDF")
        else:
            directory = args.run_dir / PDF_INPUT
            _log("transcribing the PDF page by page with independent checks")
            transcribe(pdf, directory, model=args.model, codex_bin=args.codex_bin,
                       codex_home=args.codex_home, reasoning_effort=args.reasoning_effort,
                       timeout_seconds=args.timeout_seconds, log=_log)
            manifest = directory / "ocr-manifest.json"
            m = load_preparation(manifest)
        _name_the_request(args, Path(m["paper"]).read_text(encoding="utf-8"), pdf.stem)
        if targets is not None and targets != "full":
            staged = _stage(Path(m["paper"]), inputs, "paper.md")
            args.question, _ = _scope(args, targets, staged, inputs)
        args.ocr_manifest = manifest
        return review_transcription(args)
    except QuotaExhausted as exc:
        record_quota(args.run_dir, exc, phase="pdf_input_or_review")
        print(str(exc), file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"PDF input/review blocked: {exc}", file=sys.stderr)
        return 2
