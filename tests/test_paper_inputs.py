"""`osverify verify --review-type paper --paper ...` prepares its own inputs."""
from __future__ import annotations

import argparse
import hashlib

import pytest

from osverifier import cli
from osverifier_reports import ocr, paper_inputs, paper_prepare

PAPER = r"""\documentclass{article}
\newtheorem{theorem}{Theorem}
\title{A tiny paper}
\begin{document}
\begin{theorem}\label{thm:main}
Every even integer greater than 2 is a sum of two primes.
\end{theorem}
\end{document}
"""


def namespace(tmp_path, paper, **overrides):
    values = dict(solution=paper, question=None, targets=None, labels=[], ocr_dir=None, request_id="review",
                  run_dir=tmp_path / "run", field=None, model="fixture", codex_bin="codex",
                  codex_home=None, reasoning_effort="xhigh", timeout_seconds=900, max_attempts=2,
                  paper_pdf=None, paper_image=[])
    values.update(overrides)
    return argparse.Namespace(**values)


def write_paper(tmp_path, text=PAPER, name="paper.tex"):
    path = tmp_path / "source" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    return path


def test_manual_labels_build_the_question_inside_the_run(tmp_path):
    paper = write_paper(tmp_path)
    args = namespace(tmp_path, paper, targets="manual", labels=["thm:main"])
    assert paper_inputs.prepare_paper_review(args) is None
    inputs = tmp_path / "run" / "paper-inputs"
    assert args.question == inputs / "question.md" and args.solution == inputs / "solution.md"
    assert "Every even integer greater than 2" in args.question.read_text()
    assert args.solution.read_text() == PAPER
    # Resume reuses the extraction; the user's source folder is never written.
    again = namespace(tmp_path, paper, targets="manual", labels=["thm:main"])
    assert paper_inputs.prepare_paper_review(again) is None
    assert sorted(p.name for p in paper.parent.iterdir()) == ["paper.tex"]
    paper.write_text(PAPER.replace("two primes", "three primes"))
    with pytest.raises(ValueError, match="paper changed"):
        paper_inputs.prepare_paper_review(namespace(tmp_path, paper, targets="manual", labels=["thm:main"]))


def test_default_targets_are_smart_and_full_is_selectable(tmp_path, monkeypatch):
    seen = []

    def extract(path, mode, **kwargs):
        seen.append(mode)
        raise RuntimeError("stop after selecting the mode")

    monkeypatch.setattr(paper_prepare, "codex_extract", extract)
    with pytest.raises(RuntimeError, match="stop"):
        paper_inputs.prepare_paper_review(namespace(tmp_path, write_paper(tmp_path)))
    assert seen == ["smart"]
    args = namespace(tmp_path / "full", write_paper(tmp_path, name="full.md"), targets="full")
    assert paper_inputs.prepare_paper_review(args) is None
    assert args.question.read_text().startswith("Review correctness of the full supplied paper")
    assert seen == ["smart"]


@pytest.mark.parametrize("overrides,message", [
    ({"targets": "manual"}, "requires --labels"),
    ({"labels": ["thm:main"]}, "requires --targets manual"),
    ({"question": "scope", "targets": "smart"}, "omit them with --question"),
])
def test_conflicting_scope_options_are_refused(tmp_path, overrides, message):
    if "question" in overrides:
        overrides["question"] = write_paper(tmp_path, "scope", "scope.md")
    with pytest.raises(ValueError, match=message):
        paper_inputs.prepare_paper_review(namespace(tmp_path, write_paper(tmp_path), **overrides))


def fake_ocr(tmp_path, monkeypatch, calls):
    text = tmp_path / "transcribed.md"
    text.write_text("<!-- PDF page 1 -->\nTheorem 1. Every even integer is a sum of two primes.\n")

    def transcribe(pdf, directory, **kwargs):
        calls.append(("transcribe", directory))

    def load(manifest):
        return {"paper": str(text), "pdf_sha256": hashlib.sha256(b"%PDF fixture").hexdigest()}

    def review(args):
        calls.append(("review", args.question, args.ocr_manifest))
        return 0

    monkeypatch.setattr(ocr, "transcribe", transcribe)
    monkeypatch.setattr(ocr, "load_preparation", load)
    monkeypatch.setattr(ocr, "review_transcription", review)


def test_pdf_paper_is_transcribed_inside_the_run_then_reviewed(tmp_path, monkeypatch):
    calls = []
    fake_ocr(tmp_path, monkeypatch, calls)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF fixture")
    args = namespace(tmp_path, pdf, targets="full")
    assert paper_inputs.prepare_paper_review(args) == 0
    run = tmp_path / "run"
    assert calls == [("transcribe", run / "pdf-input"), ("review", None, run / "pdf-input" / "ocr-manifest.json")]
    with pytest.raises(ValueError, match="needs LaTeX labels"):
        paper_inputs.prepare_paper_review(namespace(tmp_path, pdf, targets="manual", labels=["x"]))


def test_reused_transcription_must_come_from_the_same_pdf(tmp_path, monkeypatch, capsys):
    calls = []
    fake_ocr(tmp_path, monkeypatch, calls)
    pdf = tmp_path / "other.pdf"
    pdf.write_bytes(b"%PDF another file")
    args = namespace(tmp_path, pdf, targets="full", ocr_dir=tmp_path / "ocr")
    assert paper_inputs.prepare_paper_review(args) == 2
    assert "different PDF" in capsys.readouterr().err and calls == []


class Reached(BaseException):
    pass


def test_cli_paper_review_needs_no_question(tmp_path, monkeypatch):
    from osverifier_reports import paper

    def run_paper(arguments, request, backend):
        raise Reached(request)

    monkeypatch.setattr(paper, "run_paper", run_paper)
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\necho codex-cli 0.162.0\n")
    codex.chmod(0o755)
    with pytest.raises(Reached) as reached:
        cli.main(["verify", "--review-type", "paper", "--paper", str(write_paper(tmp_path)),
                  "--targets", "manual", "--labels", "thm:main", "--run-dir", str(tmp_path / "run"),
                  "--model", "fixture", "--codex-bin", str(codex), "--codex-home", str(tmp_path / "home")])
    request = reached.value.args[0]
    assert "thm:main" in request.question and request.candidate_solution == PAPER


def test_cli_solution_review_still_requires_a_question(tmp_path, capsys):
    assert cli.main(["verify", "--solution", str(write_paper(tmp_path)), "--run-dir",
                     str(tmp_path / "run"), "--model", "fixture"]) == 1
    assert "--question (or --request) is required" in capsys.readouterr().err
    assert cli.main(["verify", "--question", str(write_paper(tmp_path)), "--targets", "smart",
                     "--run-dir", str(tmp_path / "run2"), "--model", "fixture"]) == 1
    assert "apply only to --review-type paper" in capsys.readouterr().err


def test_ocr_and_report_subcommands_have_their_own_help(capsys):
    for command in ("ocr", "report"):
        with pytest.raises(SystemExit):
            cli.main([command, "--help"])
        assert f"osverify {command}" in capsys.readouterr().out


def test_unnamed_paper_reviews_are_titled_after_the_paper(tmp_path):
    args = namespace(tmp_path, write_paper(tmp_path), targets="manual", labels=["thm:main"])
    paper_inputs.prepare_paper_review(args)
    assert args.request_id == "A tiny paper"
    named = namespace(tmp_path / "named", write_paper(tmp_path), targets="manual",
                      labels=["thm:main"], request_id="my-id")
    paper_inputs.prepare_paper_review(named)
    assert named.request_id == "my-id"
    assert paper_inputs.paper_title("# A NOTE ON SUMS\n\ntext", "x") == "A note on sums"
    assert paper_inputs.paper_title("no title here", "fallback-stem") == "fallback-stem"
    assert paper_inputs.paper_title(r"\title[Short]{Long \\ title\thanks{Funded}}", "x") == "Long title"
