"""Question preparation from one paper: deterministic label extraction, verbatim checks,
rendering, and the driver/server plumbing with a scripted fake Codex."""

from __future__ import annotations

import json

import pytest

from osverifier_reports import paper_prepare as paper_tools

PAPER = r"""\documentclass{article}
\newtheorem{theorem}{Theorem}[section]
\newtheorem{prop}[theorem]{Proposition}
\newtheorem{remark}[theorem]{Remark}
\title{On Even Numbers}
\begin{document}
\section{Introduction}
Our main result is Theorem~\ref{thm:main}.
\section{Results}
\begin{theorem}[Main theorem]\label{thm:main}
Every even integer $n > 2$ is a sum of two primes.
\end{theorem}
\begin{prop}\label{prop:key}
For every prime $p > 2$, $p$ is odd.
\end{prop}
\begin{remark}\label{rem:aside}
Nothing to see here.
\end{remark}
Also see \ref{thm:main} again with \label{eq:free} outside any environment.
\end{document}
"""

STATEMENT = "Every even integer $n > 2$ is a sum of two primes."


def test_theorem_environments_include_declared_names():
    names = paper_tools.theorem_environments(PAPER)
    assert {"theorem", "prop", "remark", "lemma"} <= names


def test_manual_mode_extracts_labeled_environments_verbatim():
    extraction = paper_tools.manual_extraction(PAPER, ["thm:main", "prop:key"], "On Even Numbers")
    first, second = extraction.targets
    assert first.label == "thm:main" and first.name == "Theorem (Main theorem)"
    assert first.statement == "[Main theorem]\\label{thm:main}\n" + STATEMENT
    assert second.name == "Proposition"
    assert second.statement == "\\label{prop:key}\nFor every prime $p > 2$, $p$ is odd."
    with pytest.raises(ValueError, match="not found"):
        paper_tools.manual_extraction(PAPER, ["thm:missing"], "t")
    with pytest.raises(ValueError, match="not inside a theorem-like environment"):
        paper_tools.manual_extraction(PAPER, ["eq:free"], "t")
    with pytest.raises(ValueError, match="at least one label"):
        paper_tools.manual_extraction(PAPER, [], "t")


def test_validation_rejects_paraphrase_unknown_label_and_duplicates():
    good = {
        "title": "On Even Numbers",
        "targets": [{"label": "thm:main", "name": "Theorem 2.1", "statement": STATEMENT}],
    }
    assert paper_tools.validate_extraction(PAPER, good).targets[0].role == "main"
    spaced = {
        **good,
        "targets": [
            {
                **good["targets"][0],
                "statement": "Every even  integer\n$n > 2$ is a sum of two primes.",
            }
        ],
    }
    assert paper_tools.validate_extraction(PAPER, spaced)
    with pytest.raises(ValueError, match="verbatim"):
        paper_tools.validate_extraction(
            PAPER,
            {
                **good,
                "targets": [
                    {
                        **good["targets"][0],
                        "statement": "Every even integer is a sum of two primes.",
                    }
                ],
            },
        )
    with pytest.raises(ValueError, match="does not occur"):
        paper_tools.validate_extraction(
            PAPER, {**good, "targets": [{**good["targets"][0], "label": "thm:other"}]}
        )
    with pytest.raises(ValueError, match="duplicate"):
        paper_tools.validate_extraction(PAPER, {**good, "targets": good["targets"] * 2})
    with pytest.raises(Exception):
        paper_tools.validate_extraction(PAPER, {**good, "targets": []})


def test_rendered_question_quotes_statements_and_names_mode():
    extraction = paper_tools.manual_extraction(PAPER, ["thm:main"], "On Even Numbers")
    text = paper_tools.render_question(extraction, "manual")
    assert '"On Even Numbers"' in text and "selected by label" in text
    assert "[thm:main]" in text and STATEMENT in text and "## Results to establish" in text
    assert "## Standing conventions" not in text
    smart = paper_tools.Extraction(
        title="T",
        setup="Let $k$ be a field.",
        targets=extraction.targets,
        coverage_note="Skipped Remark 2.3.",
    )
    text = paper_tools.render_question(smart, "smart")
    assert "## Standing conventions\n\nLet $k$ be a field." in text and "## Not selected" in text


FAKE_CODEX_EXTRACT = r"""#!/bin/sh
# Scripted stand-in. An extraction call (workspace under gui-extraction) gets a canned
# answer; verifier calls block until interrupted.
case "$1" in
  --version) echo "codex-cli 0.162.0-fake"; exit 0 ;;
  login) echo "Logged in using ChatGPT"; exit 0 ;;
  exec)
    out=""; cd=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --output-last-message) out="$2"; shift ;;
        --cd) cd="$2"; shift ;;
      esac
      shift
    done
    case "$cd" in
      *gui-extraction*)
        cat > "$out" <<'JSON'
{"title": "On Even Numbers", "setup": "Integers are as usual.", "targets": [{"label": "thm:main", "name": "Theorem 2.1", "statement": "Every even integer $n > 2$ is a sum of two primes.", "role": "main", "reason": "announced in the introduction"}], "coverage_note": ""}
JSON
        exit 0 ;;
    esac
    sleep 60; exit 1 ;;
esac
exit 1
"""


def test_extraction_output_schema_requires_every_property():
    from osverifier_reports.paper_prepare import extraction_schema

    def objects(node):
        if isinstance(node, dict):
            if "properties" in node:
                yield node
            for value in node.values():
                yield from objects(value)
        elif isinstance(node, list):
            for value in node:
                yield from objects(value)

    found = list(objects(extraction_schema()))
    assert len(found) >= 2  # Extraction and Target
    for node in found:
        assert sorted(node["required"]) == sorted(node["properties"])
        assert node["additionalProperties"] is False


def test_extraction_uses_the_selected_provider_settings(tmp_path):
    fake = tmp_path / "codex"
    fake.write_text(FAKE_CODEX_EXTRACT)
    fake.chmod(0o755)
    home = tmp_path / "account"
    home.mkdir()
    (home / "config.toml").write_text('model_provider = "gateway"\n[model_providers.gateway]\n'
                                      'name = "Gateway"\nbase_url = "https://gateway.invalid/v1"\n')
    paper = tmp_path / "paper.tex"
    paper.write_text("\\begin{theorem}\\label{thm:main}Every even integer $n > 2$ is a sum of two primes.\\end{theorem}")
    work = tmp_path / "run" / "gui-extraction"
    extraction, _ = paper_tools.codex_extract(
        paper, "smart", model="fixture", codex_bin=str(fake), codex_home=str(home),
        reasoning_effort="low", timeout_seconds=30, max_attempts=1, work_dir=work, log=lambda m: None)
    assert extraction.title == "On Even Numbers"
    argv = json.loads((work / "attempt-0001" / "command.json").read_text())["argv"]
    assert "model_provider=<redacted connection config>" in argv
    assert not any("gateway.invalid" in arg for arg in argv)
