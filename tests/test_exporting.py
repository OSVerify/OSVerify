from __future__ import annotations

import pytest

from osverifier.cli import main
from osverifier.exporting import export_report, render_latex, safe_math

SAMPLE = r"""# Verifier report

- Decision: **REVISE**
- Run: incomplete

Review is unfinished. Any released feedback is partial.

## Audited feedback

### issue_1 / feedback_1 (diagnosis)

The claim $x_1 < x_2$ fails when \(x_1=2\). Check 50% & A_B.

\[
\sum_{i=1}^n i = \frac{n(n+1)}{2}
\]

**Repair**: use the following:

$$
a^2+b^2=c^2
$$

Source: candidate_solution lines 3–7

Audit: `jobs/issue_1/result.json`

## Workflow coverage

Termination: audited_rejection. Full planned scope coverage: False.

| Node | Status |
| --- | --- |
| root | completed |
"""


def test_latex_preserves_math_status_and_source():
    tex = render_latex(SAMPLE)
    for text in (
        r"\(x_1 < x_2\)",
        r"\(x_1=2\)",
        r"\sum_{i=1}^n",
        r"50\% \& A\_B",
        "Review is unfinished",
        "Full planned scope coverage",
        r"candidate\_solution lines 3–7",
        r"jobs/issue\_1/result.json",
    ):
        assert text in tex
    assert r"\begin{longtable}" in tex
    assert r"\textbf{Repair}" in tex
    assert render_latex(SAMPLE) == tex
    assert r"\input{secret}" not in render_latex(r"Raw \input{secret} and $\input{secret}$")


@pytest.mark.parametrize(
    "formula",
    [
        r"\write18{touch pwned}",
        r"\immediate\write18{touch pwned}",
        r"\openin15=/etc/hosts \read15 to\x \x",
        r"\InputIfFileExists{/etc/hosts}{}{}",
        r"\directlua{tex.print(io.open('/etc/hosts'):read('a'))}",
        r"\makeatletter\@@input /etc/hosts",
        r"\csname input\endcsname{/etc/hosts}",
        r"x^^5cinput{/etc/hosts}",
        r"\begin{filecontents}{a.tex}x\end{filecontents}",
    ],
)
def test_math_outside_the_allowlist_is_shown_literally(formula):
    for source in (f"${formula}$", f"\\({formula}\\)", f"$$\n{formula}\n$$"):
        tex = render_latex(f"Quoted {source} here", fragment=True)
        assert r"\(" not in tex and r"\[" not in tex
        assert "\\textbackslash{}" in tex or "\\textasciicircum{}" in tex


def test_allowlisted_math_is_typeset():
    tex = render_latex(
        r"$\mathbb{R}^n \setminus \{0\}$, $\left\lfloor x \right\rfloor \to \infty$ and "
        "\n\n$$\\begin{aligned} a &= b \\\\ c &\\le d \\end{aligned}$$\n",
        fragment=True,
    )
    assert r"\(\mathbb{R}^n \setminus \{0\}\)" in tex
    assert r"\(\left\lfloor x \right\rfloor \to \infty\)" in tex
    assert r"\begin{aligned} a &= b \\ c &\le d \end{aligned}" in tex
    # A trailing backslash would swallow the closing delimiter.
    assert safe_math(r"x \\") and not safe_math("x \\")


def test_cli_offline_export_does_not_modify_run(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.md").write_text(SAMPLE)
    before = (run / "report.md").read_bytes()
    output = tmp_path / "report.tex"
    assert main(["export", "--run-dir", str(run), "--output", str(output)]) == 0
    assert output.read_text() == render_latex(SAMPLE)
    assert main(["export", "--run-dir", str(run), "--output", str(output)]) == 1
    md = tmp_path / "copy.md"
    assert main(["export", "--run-dir", str(run), "--format", "markdown", "--output", str(md)]) == 0
    assert md.read_bytes() == before == (run / "report.md").read_bytes()
    assert (
        main(
            [
                "export",
                "--run-dir",
                str(tmp_path / "missing"),
                "--output",
                str(tmp_path / "absent.tex"),
            ]
        )
        == 1
    )
    assert not (tmp_path / "absent.tex").exists()




def test_markdown_code_is_literal_and_numbered_lists_keep_start():
    tex = render_latex("7. First\n8. Second\n\n`$x$`\n\n```tex\n\\input{file}\n```\n")
    assert r"\setcounter{enumi}{6}" in tex
    assert r"\texttt{\$x\$}" in tex
    assert r"\input{file}" not in tex




def test_referee_template_uses_only_released_feedback(tmp_path):
    import json

    from osverifier.demo import DemoBackend, demo_request
    from osverifier.pipeline import Verifier

    report = Verifier(DemoBackend()).verify(demo_request(), tmp_path / "run")
    run = tmp_path / "run"
    # Neither legacy prose nor internal diagnostics may be imported as new findings.
    (run / "report.md").write_text("legacy-markdown-sentinel")
    (run / "diagnostics.json").write_text(json.dumps({"rationale": "PRIVATE-SENTINEL"}))
    tex, _, _ = export_report(run, "latex")
    assert r"\documentclass[11pt,reqno]{amsart}" in tex
    assert r"\author{OSVerify}" in tex
    assert r"\maketitle" in tex
    assert "Overall opinion" in tex
    assert tex.count(r"\subsection{Issue ") == 1
    assert r"\paragraph{Concern}" in tex
    assert r"\paragraph{Suggested fix (request)}" in tex
    assert "5 major mistakes" not in tex and "plausible" not in tex
    assert "PRIVATE-SENTINEL" not in tex and "legacy-markdown-sentinel" not in tex
    assert "SCRIPTED DEMO" in tex
    for item in report.feedback:
        assert render_latex(item.text, fragment=True) in tex
    assert export_report(run, "markdown")[0] == "legacy-markdown-sentinel"


def test_referee_incomplete_and_early_stop_are_not_clean_handoff():
    from osverifier.exporting import render_referee_report
    from osverifier.models import ReviewReport
    from osverifier.workflow_models import WorkflowReport

    report = ReviewReport(
        request_id="example",
        decision=None,
        run_status="incomplete",
        backend_kind="codex",
        feedback=[],
    )
    assert "Review is unfinished" in render_referee_report(report)
    assert "No feedback text was released" in render_referee_report(report)
    stopped = WorkflowReport(
        **{
            **report.model_dump(),
            "schema_version": "3.0",
            "decision": "REJECT",
            "run_status": "completed",
            "feedback": [
                {
                    "issue_id": "i1",
                    "feedback_id": "f1",
                    "kind": "diagnosis",
                    "text": "A blocking error was confirmed.",
                    "source_spans": [],
                    "audit_artifact": "jobs/audit/result.json",
                }
            ],
            "termination_reason": "audited_rejection",
            "coverage_complete": False,
        }
    )
    tex = render_referee_report(stopped)
    assert "not every node was reviewed" in tex
    assert "Full planned scope coverage: False" in tex
