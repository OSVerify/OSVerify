from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from osverifier_reports import locations
from osverifier_reports.writer import Draft, Fidelity, write_report

PAPER = r"""\documentclass{article}
\usepackage{amsmath,amsthm}
\newtheorem{theorem}{Theorem}[section]
\newtheorem{lemma}[theorem]{Lemma}
\numberwithin{equation}{section}
\begin{document}
\setcounter{section}{6}
\section{A numbered section}
\subsection{Local statement}
% \section{This commented heading must never affect locations}
\begin{theorem}\label{thm:misleading-999}
The original theorem.
\end{theorem}
\begin{lemma}\label{lem:second}
The shared-counter lemma.
\begin{equation}\label{eq:W2}
x = 1.
\end{equation}
\end{lemma}
\begin{proof}
The exact released concern refers to this proof.
\end{proof}
\section*{An unnumbered section}
A concern in an unnumbered section.
\begin{proof}[Proof of Theorem~\ref{thm:misleading-999}]
The separate proof refers back to the first theorem.
\end{proof}
\begin{thebibliography}{9}
\bibitem{Smith2020} A bibliographic correction.
\end{thebibliography}
\end{document}
"""


def feedback_at(text):
    line = next(i for i, value in enumerate(PAPER.splitlines(), 1) if text in value)
    return {"issue_id": "i1", "feedback_id": "f1", "kind": "diagnosis",
            "text": "Exact released concern.", "audit_artifact": "audit.json",
            "source_spans": [{"document": "candidate_solution", "start_line": line, "end_line": line}]}


def has_latex():
    return shutil.which("pdflatex") or Path("/Library/TeX/texbin/pdflatex").is_file()


@pytest.mark.skipif(not has_latex(), reason="LaTeX not installed")
@pytest.mark.parametrize("needle,expected,excluded", [
    ("x = 1", ["Section 7.1", "Lemma 7.2", "Equation (7.1)"], ["Equation (W2)", "Theorem 999"]),
    ("exact released", ["Proof of Lemma 7.2"], ["Proof of Theorem 7.1"]),
    ("separate proof", ["Proof of Theorem 7.1", "Section “An unnumbered section”"], ["Proof of Lemma 7.2"]),
    ("bibliographic", ["Bibliography, entry “Smith2020”"], ["Equation (7.1)"]),
])
def test_numbers_come_from_latex_and_proofs_keep_their_target(tmp_path, needle, expected, excluded):
    result = locations.build_locations({"request_id": "paper", "candidate_solution": PAPER}, [feedback_at(needle)], tmp_path)
    assert result["numbering_status"] == "compiled"
    refs = result["issues"]["i1"]["references"]
    assert all(value in refs for value in expected)
    assert all(value not in refs for value in excluded)
    assert "source_spans" in json.dumps(feedback_at(needle))


def test_failed_build_uses_named_heading_not_guessed_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr(locations, "_compile", lambda *args: ({}, "compilation_failed"))
    result = locations.build_locations({"request_id": "paper", "candidate_solution": PAPER}, [feedback_at("x = 1")], tmp_path)
    assert locations.location_text(result, "i1") == "Section “Local statement”"
    assert result["issues"]["i1"]["spans"] == feedback_at("x = 1")["source_spans"]
    assert result["numbering_status"] == "compilation_failed"


def test_missing_or_mismatched_source_never_borrows_another_paper(tmp_path):
    report = {"request_id": "paper", "feedback": []}
    result = locations.locations_for_run(tmp_path, report, tmp_path / "out")
    assert "unavailable" in locations.location_text(result, "i1")
    (tmp_path / "request.json").write_text(json.dumps({"request_id": "different", "candidate_solution": PAPER}))
    with pytest.raises(ValueError, match="different requests"):
        locations.locations_for_run(tmp_path, report, tmp_path / "out")


def test_pdf_fallback_requires_an_unambiguous_exact_heading(tmp_path, monkeypatch):
    monkeypatch.setattr(locations.shutil, "which", lambda _: "/usr/bin/pdftotext")

    def extract(command, **kwargs):
        Path(command[-1]).write_text(
            "3.6 The relative interaction operator\n"
            "Lemma 3.9 (Trace bound and small\neigenvalues). Text.\n"
            "3.1 Repeated title\n4.2 Repeated title\n"
        )

    monkeypatch.setattr(locations.subprocess, "run", extract)
    entries = [
        {"kind": "Section", "title": "The relative interaction operator", "labels": ["s"]},
        {"kind": "Lemma", "title": "Trace bound and small eigenvalues", "labels": ["l", "alias"]},
        {"kind": "Section", "title": "Repeated title", "labels": ["ambiguous"]},
        {"kind": "Section", "title": "A different heading", "labels": ["absent"]},
    ]
    numbers, status = locations._pdf_numbers(entries, tmp_path / "original.pdf", tmp_path)
    assert status == "pdf_headings"
    assert numbers == {"s": "3.6", "l": "3.9", "alias": "3.9"}


@pytest.mark.skipif(not has_latex(), reason="LaTeX not installed")
def test_report_renders_paper_numbers_and_keeps_source_immutable(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "request.json").write_text(json.dumps({"request_id": "paper", "candidate_solution": PAPER}))
    (run / "report.json").write_text(json.dumps({
        "request_id": "paper", "review_kind": "paper", "run_status": "completed", "classification_status": "completed",
        "feedback": [feedback_at("x = 1")], "issues": [{"issue_id": "i1", "severity": "minor", "rationale": "Audited severity."}],
    }))
    before = {p.name: p.read_bytes() for p in run.iterdir()}

    class Backend:
        def call(self, role, payload, schema, directory):
            assert "Equation (7.1)" in payload["source"]["paper_locations"]["issues"]["i1"]["references"]
            if schema is Fidelity:
                return Fidelity(approved=True, reasons=[])
            return Draft(overview="A released concern.", entries=[{
                "issue_id": "i1", "title": "Concern", "issue_type": "Claim",
                "feedback": [{"ref": 0, "section": "Concern"}],
                "passages": [selection().model_dump()],
            }])

    tex = write_report(run, tmp_path / "export", Backend()).read_text()
    assert "Section 7.1; Lemma 7.2; Equation (7.1)" in tex
    assert "Paper excerpt" in tex and r"\(x = 1\)" in tex
    approved = json.loads((tmp_path / "export/issue-passages.json").read_text())
    assert approved["issues"]["i1"][0]["role"] == "issue"
    assert approved["issues"]["i1"][0]["quote"] == "x = 1."
    assert "candidate\\_solution lines" not in tex
    assert "Exact released concern." in tex
    assert before == {p.name: p.read_bytes() for p in run.iterdir()}
    sidecar = json.loads((tmp_path / "export/paper-locations/locations.json").read_text())
    assert sidecar["issues"]["i1"]["spans"] == feedback_at("x = 1")["source_spans"]


def selection(needle="x = 1", **changes):
    from osverifier_reports.writer import PaperPassage

    line = feedback_at(needle)["source_spans"][0]["start_line"]
    data = dict(document="candidate_solution", start_line=line, end_line=line,
                role="issue", description="The displayed identity.", quote="x = 1.",
                display_quote=r"\(x = 1\).", explanation="The released concern challenges this identity.")
    return PaperPassage(**{**data, **changes})


def test_quote_must_belong_to_this_issue_and_selected_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(locations, "_compile", lambda *args: ({}, "compilation_failed"))
    mapping = locations.build_locations({"request_id": "paper", "candidate_solution": PAPER},
                                        [feedback_at("x = 1")], tmp_path)
    resolved = locations.selected_passage(mapping, "i1", selection())
    assert resolved["quote"] == "x = 1."
    for bad, message in [
        (selection(quote="x = 2."), "not verbatim"),
        (selection(start_line=1, end_line=1), "outside"),
        (selection(quote=""), "exact source quote"),
        (selection(explanation=""), "relevance"),
    ]:
        with pytest.raises(ValueError, match=message):
            locations.selected_passage(mapping, "i1", bad)
    with pytest.raises(ValueError, match="outside"):
        locations.selected_passage(mapping, "another-issue", selection())


def test_context_is_not_rendered_as_the_error_location(tmp_path, monkeypatch):
    from osverifier_reports.writer import Entry, render_passages, validate_draft

    monkeypatch.setattr(locations, "_compile", lambda *args: ({}, "compilation_failed"))
    feedback = [feedback_at("x = 1"), feedback_at("separate proof")]
    mapping = locations.build_locations({"request_id": "paper", "candidate_solution": PAPER}, feedback, tmp_path)
    entry = Entry(issue_id="i1", title="Concern", issue_type="Inference", feedback=[], passages=[
        selection(), selection("separate proof", role="downstream", description="Later use of the assertion.",
                               quote="", display_quote="", explanation="This later use must be preserved."),
    ])
    rendered = render_passages({"paper_locations": mapping}, entry)
    primary, context = rendered.split(r"\paragraph{Related context}")
    assert "Where the issue occurs" in primary and "Later use" not in primary
    assert "Downstream use" in context and "must be preserved" in context
    source = {"review_kind": "paper", "paper_locations": mapping, "issues": [{"issue_id": "i1"}], "feedback": []}
    entry.passages = entry.passages[1:]
    with pytest.raises(ValueError, match="precise issue passage"):
        validate_draft(source, Draft(overview="Concern", entries=[entry]))


def test_missing_source_has_no_fabricated_quote():
    from osverifier_reports.writer import Entry, render_passages

    rendered = render_passages({}, Entry(issue_id="i1", title="Concern", issue_type="Claim", feedback=[]))
    assert "Exact quotation unavailable" in rendered
    assert "begin{quote}" not in rendered


def test_quoted_math_identifiers_do_not_become_markdown_emphasis():
    from osverifier_reports.writer import report_text

    tex = report_text(r"E=p_*ω_{H×N}, H*(B). Explicit math: \(p_*\omega\). **Bold**.")
    assert "p\\_\\allowbreak{}*ω" in tex and "H*(B)" in tex
    assert r"\(p_*\omega\)" in tex and r"\textbf{Bold}" in tex
    assert r"\emph" not in tex


def test_new_format_does_not_reuse_or_overwrite_historical_pdf(tmp_path):
    import hashlib

    from osverifier_reports.automatic import report_directory
    from osverifier_reports.writer import REPORT_FORMAT_VERSION

    run = tmp_path / "verification"
    run.mkdir()
    report = b'{"request_id": "same frozen report"}'
    (run / "report.json").write_bytes(report)
    old = tmp_path / "verification-reports" / hashlib.sha256(report).hexdigest()
    old.mkdir(parents=True)
    (old / "report.pdf").write_bytes(b"%PDF-old-immutable")
    assert report_directory(run) == old / REPORT_FORMAT_VERSION
    assert not report_directory(run).exists()
    assert (old / "report.pdf").read_bytes() == b"%PDF-old-immutable"


NESTED = r"""\documentclass{article}
\usepackage{amsmath,amsthm}
\newtheorem{theorem}{Theorem}[section]
\newtheorem*{maintheorem}{Main Theorem}
\begin{document}
\section{Introduction}
\section{Body}
\begin{theorem}\label{thm:body}
The statement.
\begin{enumerate}
\item The first claim.
\item\label{it:second} The second claim needs care.
\end{enumerate}
\begin{eqnarray}
a &=& b \label{eq:array}
\end{eqnarray}
\[ w = v \tag{16}\label{eq:tagged} \]
\end{theorem}
\begin{maintheorem}\label{thm:main}
The main statement.
\end{maintheorem}
\begin{proof}[Proof of Theorem~\ref{thm:body}]
The deferred proof.
\end{proof}
\begin{equation}
\begin{split}
y &= z \label{eq:split}
\end{split}
\end{equation}
\end{document}
"""


def nested_refs(tmp_path, needle):
    line = next(i for i, value in enumerate(NESTED.splitlines(), 1) if needle in value)
    item = {"issue_id": "i1", "feedback_id": "f1", "kind": "diagnosis", "text": "Concern.",
            "audit_artifact": "audit.json",
            "source_spans": [{"document": "candidate_solution", "start_line": line, "end_line": line}]}
    result = locations.build_locations({"request_id": "paper", "candidate_solution": NESTED}, [item], tmp_path)
    assert result["numbering_status"] == "compiled"
    return result["issues"]["i1"]["references"]


@pytest.mark.skipif(not has_latex(), reason="LaTeX not installed")
@pytest.mark.parametrize("needle,expected,excluded", [
    # An item or eqnarray label names its own counter, never the enclosing theorem.
    ("second claim", ["Section 2", "Theorem 2.1"], ["Theorem 2", "Theorem 1"]),
    ("a &=& b", ["Theorem 2.1", "Equation (1)"], ["Theorem 1"]),
    # A tagged \[...\] display inside a statement names the tag, not the statement.
    ("w = v", ["Theorem 2.1"], ["Theorem 16"]),
    # A starred statement has no number; its label would print the section counter.
    ("main statement", ["Section 2"], ["Main Theorem 2"]),
    ("deferred proof", ["Proof of Theorem 2.1"], []),
    # A label inside split keeps the enclosing equation's number.
    ("y &= z", ["Equation (2)"], []),
])
def test_nested_labels_never_renumber_their_statement(tmp_path, needle, expected, excluded):
    refs = nested_refs(tmp_path, needle)
    assert all(value in refs for value in expected), refs
    assert all(value not in refs for value in excluded), refs


def test_span_lines_follow_splitlines_offsets(tmp_path, monkeypatch):
    # Spans are numbered with str.splitlines(), which also breaks on form feeds.
    source = "\\documentclass{article}\n\\begin{document}\n\\section{Alpha}\nfoo\fbar\n\\section{Beta}\nbaz\n\\end{document}\n"
    monkeypatch.setattr(locations, "_compile", lambda *args: ({}, "compilation_failed"))
    line = source.splitlines().index("bar") + 1
    item = {"issue_id": "i1", "feedback_id": "f1", "kind": "diagnosis", "text": "Concern.",
            "audit_artifact": "audit.json",
            "source_spans": [{"document": "candidate_solution", "start_line": line, "end_line": line}]}
    result = locations.build_locations({"request_id": "paper", "candidate_solution": source}, [item], tmp_path)
    assert result["issues"]["i1"]["references"] == ["Section “Alpha”"]
