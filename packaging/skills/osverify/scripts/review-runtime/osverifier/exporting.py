"""Offline report export. Formatting only: no model calls or feedback rewriting."""

from __future__ import annotations

import re
from pathlib import Path

import mistune

from .models import ReviewReport
from .storage import read_json
from .workflow_models import WorkflowReport


def escape_tex(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "$": r"\$",
        "&": r"\&",
        "#": r"\#",
        "%": r"\%",
        "_": r"\_",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


# Mathematical control sequences that may be emitted verbatim. Released feedback can
# quote untrusted solution text, so anything else (file, catcode, macro-definition and
# engine primitives, unknown packages' commands) makes the formula display literally.
MATH_COMMANDS = frozenset(
    """
    alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa varkappa
    lambda mu nu xi pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi
    psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega digamma
    aleph beth gimel daleth hbar hslash ell wp Re Im imath jmath partial nabla infty
    forall exists nexists emptyset varnothing neg lnot top bot angle measuredangle
    triangle square blacksquare Box Diamond lozenge prime backprime dagger ddagger flat
    sharp natural clubsuit diamondsuit heartsuit spadesuit checkmark circledS complement
    sum prod coprod int iint iiint oint bigcup bigcap bigsqcup bigvee bigwedge bigoplus
    bigotimes bigodot biguplus lim liminf limsup sup inf max min sin cos tan cot sec csc
    arcsin arccos arctan sinh cosh tanh coth log ln lg exp det dim ker deg gcd hom arg Pr
    pm mp times div cdot cdotp ldotp ast star circ bullet oplus ominus otimes oslash odot
    cup cap sqcup sqcap uplus amalg setminus smallsetminus wedge vee land lor wr intercal
    diamond bigtriangleup bigtriangledown triangleleft triangleright lhd rhd unlhd unrhd
    leq le geq ge leqslant geqslant neq ne equiv approx approxeq sim simeq cong propto
    lesssim gtrsim ll gg lll ggg prec succ preceq succeq nleq ngeq nless ngtr subset
    supset subseteq supseteq subsetneq supsetneq nsubseteq nsupseteq varsubsetneq
    varsupsetneq sqsubset sqsupset sqsubseteq sqsupseteq in notin ni mid nmid parallel
    nparallel perp models vdash dashv vDash Vdash asymp doteq triangleq bowtie smile
    frown trianglelefteq trianglerighteq
    to gets rightarrow leftarrow Rightarrow Leftarrow leftrightarrow Leftrightarrow iff
    implies impliedby mapsto longmapsto longrightarrow longleftarrow Longrightarrow
    Longleftarrow longleftrightarrow Longleftrightarrow hookrightarrow hookleftarrow
    uparrow downarrow Uparrow Downarrow updownarrow Updownarrow nearrow searrow swarrow
    nwarrow rightharpoonup rightharpoondown leftharpoonup leftharpoondown
    rightleftharpoons leftrightarrows rightleftarrows rightrightarrows leftleftarrows
    twoheadrightarrow twoheadleftarrow rightsquigarrow leadsto nrightarrow nleftarrow
    nRightarrow nLeftarrow nLeftrightarrow xrightarrow xleftarrow circlearrowleft
    circlearrowright curvearrowleft curvearrowright
    dots ldots cdots vdots ddots dotsc dotsb dotsm dotsi dotso colon therefore because
    lbrace rbrace lbrack rbrack langle rangle lceil rceil lfloor rfloor vert Vert lvert
    rvert lVert rVert backslash ulcorner urcorner llcorner lrcorner
    mathbb mathbf mathcal mathfrak mathit mathrm mathsf mathtt mathnormal boldsymbol
    pmb operatorname text textrm textbf textit textsf texttt textup textnormal emph mbox
    displaystyle textstyle scriptstyle scriptscriptstyle
    frac dfrac tfrac cfrac binom dbinom tbinom choose over atop sqrt overline underline
    widehat widetilde hat tilde bar vec dot ddot dddot check breve acute grave mathring
    overbrace underbrace overset underset stackrel substack overleftarrow
    overrightarrow overleftrightarrow underleftarrow underrightarrow boxed
    pmod bmod mod pod
    left right middle big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr bigm
    Bigm
    quad qquad enspace thinspace medspace thickspace negthinspace negmedspace
    negthickspace phantom hphantom vphantom smash mathstrut strut
    limits nolimits displaylimits not mathop mathbin mathrel mathord mathopen mathclose
    mathpunct mathinner tag notag nonumber shoveleft shoveright intertext
    begin end
    """.split()
)
MATH_ENVIRONMENTS = frozenset(
    """
    aligned alignedat gathered split cases matrix pmatrix bmatrix Bmatrix vmatrix
    Vmatrix smallmatrix array subarray
    """.split()
)
# Control symbols: a backslash followed by one non-letter character.
MATH_SYMBOLS = frozenset("\\{},;:!| %#&_$>")
CONTROL_SEQUENCE = re.compile(r"\\([^\W\d_]+|.|$)", re.DOTALL)
ENVIRONMENT = re.compile(r"\\(?:begin|end)[ \t]*\{([^{}]*)\}")


def safe_math(raw: str) -> bool:
    """Whether formula source uses only allowlisted mathematical control sequences."""
    # TeX's ^^ notation can spell any character, including a backslash.
    if "^^" in raw:
        return False
    environments = 0
    for match in CONTROL_SEQUENCE.finditer(raw):
        name = match.group(1)
        if name not in MATH_COMMANDS and name not in MATH_SYMBOLS:
            return False
        environments += name in ("begin", "end")
    named = ENVIRONMENT.findall(raw)
    return len(named) == environments and all(name in MATH_ENVIRONMENTS for name in named)


def _latex_math(inline, match, state):
    raw = match.group(0)
    state.append_token(
        {"type": "block_math" if raw.startswith(r"\[") else "inline_math", "raw": raw[2:-2]}
    )
    return match.end()


def _math_plugin(md):
    md.inline.register(
        "latex_math", r"\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]", _latex_math, before="escape"
    )


def render_latex(markdown: str, *, title: str = "Referee Report", fragment: bool = False) -> str:
    parser = mistune.create_markdown(
        renderer="ast", plugins=["math", "table", "strikethrough", _math_plugin]
    )

    def render(tokens):
        parts = []
        for token in tokens:
            kind = token["type"]
            raw = token.get("raw", "")
            children = token.get("children", [])
            body = render(children)
            if kind in ("text", "inline_html", "block_html"):
                value = escape_tex(raw)
            elif kind in ("inline_math", "block_math"):
                raw = raw.strip()
                # Only allowlisted mathematics is typeset; anything else is shown literally.
                if not safe_math(raw):
                    value = escape_tex(raw)
                else:
                    value = (
                        (r"\(" + raw + r"\)")
                        if kind == "inline_math"
                        else "\n\\[\n" + raw + "\n\\]\n"
                    )
            elif kind == "heading":
                command = {1: "section", 2: "subsection"}.get(
                    token["attrs"]["level"], "subsubsection"
                )
                value = f"\\{command}*{{{body}}}\n"
            elif kind in ("paragraph", "block_text"):
                value = body + "\n\n"
            elif kind in ("strong", "emphasis", "strikethrough"):
                command = {"strong": "textbf", "emphasis": "emph", "strikethrough": "sout"}[kind]
                value = f"\\{command}{{{body}}}"
            elif kind == "codespan":
                value = r"\texttt{" + escape_tex(raw) + "}"
            elif kind == "block_code":
                value = (
                    "\\begin{quote}\\ttfamily\n"
                    + "\\\\\n".join(escape_tex(line) for line in raw.splitlines())
                    + "\n\\end{quote}\n"
                )
            elif kind == "list":
                env = "enumerate" if token["attrs"]["ordered"] else "itemize"
                start = token["attrs"].get("start", 1)
                counter = ""
                if env == "enumerate" and start != 1:
                    depth = token["attrs"].get("depth", 0)
                    counters = ("enumi", "enumii", "enumiii", "enumiv")
                    counter = f"\\setcounter{{{counters[min(depth, 3)]}}}{{{start - 1}}}\n"
                value = f"\\begin{{{env}}}\n{counter}{body}\\end{{{env}}}\n"
            elif kind == "list_item":
                value = "\\item " + body
            elif kind == "block_quote":
                value = "\\begin{quote}\n" + body + "\\end{quote}\n"
            elif kind in ("link", "image"):
                # Keep destinations visible without executing URL/TeX commands.
                value = body + " (" + escape_tex(token["attrs"]["url"]) + ")"
            elif kind == "table":
                count = len(children[0]["children"])
                width = 1 / max(count, 1)
                columns = "".join(
                    f"p{{\\dimexpr {width:.3f}\\linewidth-2\\tabcolsep\\relax}}"
                    for _ in range(count)
                )
                value = f"\\begin{{longtable}}{{{columns}}}\n{body}\\end{{longtable}}\n"
            elif kind in ("table_head", "table_row"):
                value = " & ".join(render([cell]) for cell in children) + r" \\" + "\n"
                if kind == "table_head":
                    value += "\\hline\n"
            elif kind == "linebreak":
                value = "\\\\\n"
            elif kind in ("softbreak", "blank_line"):
                value = "\n"
            elif kind == "thematic_break":
                value = "\n\\noindent\\rule{\\linewidth}{0.4pt}\n"
            else:
                value = body or escape_tex(raw)
            parts.append(value)
        return "".join(parts)

    body = render(parser(markdown))
    if fragment:
        return body
    return (
        r"""% Generated offline by OSVerify from released report content.
% Compile with XeLaTeX or LuaLaTeX. Review source before compiling external content.
\documentclass[11pt,reqno]{amsart}
\usepackage[margin=1in]{geometry}
\usepackage{fontspec}
\usepackage{amsmath,amssymb,amsthm,longtable}
\usepackage[normalem]{ulem}
\usepackage[hidelinks]{hyperref}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.5em}
\setlength{\emergencystretch}{3em}
\author{OSVerify}
\date{}
"""
        + "\\title{"
        + escape_tex(title)
        + "}\n"
        + "\\begin{document}\n\\maketitle\n"
        + body
        + "\n\\end{document}\n"
    )


def render_referee_report(report: ReviewReport) -> str:
    """Use the collaborator's amsart layout with released fields only."""
    groups = {}
    for item in report.feedback:
        groups.setdefault(item.issue_id, []).append(item)
    overall = [
        "## Overall opinion",
        "",
        f"Decision: **{report.decision or 'Pending'}**. Run status: **{report.run_status}**.",
        "",
        f"Released feedback concerns {len(groups)} issue(s). Severity is not classified in the public report.",
        "",
    ]
    if report.mode == "partial":
        overall += ["Partial progress: PASS permits continuing, not a correctness certificate. REVISE does not guarantee a repair; REJECT concerns the current route. Unfinished obligations alone are not errors.", ""]
    if report.backend_kind == "scripted_demo":
        overall += ["**SCRIPTED DEMO — no live model judgments.**", ""]
    if report.run_status != "completed":
        overall += [
            "Review is unfinished. Released feedback is partial and does not authorize a clean human handoff.",
            "",
        ]
    if report.decision == "READY_FOR_HUMAN":
        overall += [
            "No outstanding issue or revision request was identified. Final human confirmation is required; this is not a proof certificate.",
            "",
        ]
    if isinstance(report, WorkflowReport):
        overall += [
            f"Termination: {report.termination_reason or 'running'}. Full planned scope coverage: {report.coverage_complete}.",
            "",
        ]
        if report.termination_reason == "audited_rejection":
            overall += [
                "Stopped intentionally after an audited rejection. Remaining work was skipped or cancelled; not every node was reviewed.",
                "",
            ]
    body = render_latex("\n".join(overall), fragment=True).replace(
        r"\subsection*{Overall opinion}", r"\section*{Overall opinion}", 1
    )
    body += "\\section{Imprecisions and clarification requests}\n"
    if not groups:
        body += "No feedback text was released.\n"
    for number, (issue_id, items) in enumerate(groups.items(), 1):
        body += "\\subsection{Issue " + str(number) + " (" + escape_tex(issue_id) + ")}\n"
        spans = dict.fromkeys(
            f"{span.document} lines {span.start_line}–{span.end_line}"
            for item in items
            for span in item.source_spans
        )
        body += (
            "\\paragraph{Location} "
            + escape_tex("; ".join(spans) or "Not supplied in the released report.")
            + "\n\n"
        )
        kinds = dict.fromkeys(item.kind.replace("_", " ") for item in items)
        body += "\\paragraph{Type} Released feedback: " + escape_tex(", ".join(kinds)) + ".\n\n"
        for item in items:
            label = {
                "diagnosis": "Concern",
                "repair_request": "Suggested fix (request)",
                "repair_suggestion": "Suggestion",
                "clarification_request": "Clarification request",
            }[item.kind]
            body += "\\paragraph{" + label + "}\n" + render_latex(item.text, fragment=True)
            body += (
                "\n{\\footnotesize Feedback: "
                + escape_tex(item.feedback_id)
                + "; Audit: "
                + escape_tex(item.audit_artifact)
                + ".}\n\n"
            )
    # No separate Check/severity is inferred from internal rationale or model-written summaries.
    shell = render_latex("", title=f"Referee Report on “{report.request_id}”")
    return shell.replace("\\end{document}", body + "\\end{document}")


def export_report(run_dir: Path, format: str) -> tuple[str, str, str]:
    """Export released JSON as a referee report, or retain the Markdown snapshot.

    Existing runs can be exported without resuming or changing frozen artifacts.
    Callers pass a verification run or a specific Rethlas reviews/<key> directory.
    """
    if format not in ("latex", "markdown"):
        raise ValueError("format must be latex or markdown")
    run_dir = Path(run_dir)
    if format == "latex" and (run_dir / "report.json").is_file():
        data = read_json(run_dir / "report.json")
        if data.get("review_kind") == "paper":
            from osverifier_reports.paper import PaperReport, markdown

            report = PaperReport.model_validate(data)
            return render_latex(markdown(report), title=f"Referee Report on {report.request_id}"), "report.tex", "application/x-tex; charset=utf-8"
        model = WorkflowReport if data.get("schema_version") == "3.0" else ReviewReport
        report = model.model_validate(data)
        return render_referee_report(report), "report.tex", "application/x-tex; charset=utf-8"
    markdown = (run_dir / "report.md").read_text(encoding="utf-8")
    if format == "markdown":
        return markdown, "report.md", "text/markdown; charset=utf-8"
    return render_latex(markdown), "report.tex", "application/x-tex; charset=utf-8"
