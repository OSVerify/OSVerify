"""Reader-facing paper reports; complete audit metadata stays in source.json."""

import re
from collections import Counter
from datetime import date, datetime, timezone

from osverifier.exporting import escape_tex, render_latex

from .references import REFERENCE_NOTICE


def paper_title(source):
    """Keep explicit titles intact; strip recognized version dates from legacy run names."""
    if source.get("paper_title"):
        return source["paper_title"]
    title = source["request_id"]
    months = "January February March April May June July August September October November December".split()
    match = re.search(
        r"[\s_-]+(?:(" + "|".join(months) + r")[\s_-]+(\d{1,2}),?[\s_-]+(\d{4})|(\d{4})-(\d{2})-(\d{2}))$",
        title,
    )
    if match:
        try:
            if match[1]:
                date(int(match[3]), months.index(match[1]) + 1, int(match[2]))
            else:
                date(int(match[4]), int(match[5]), int(match[6]))
        except ValueError:
            return title
        return title[:match.start()]
    return title


def generation_date(source):
    # Saved in source.json for reproducible recompilation; old snapshots get today's date.
    return source.get("report_generated_on") or datetime.now(timezone.utc).date().isoformat()


def review_status(source):
    """Do not confuse a finished process with a fully reviewed paper."""
    p = source.get("progress") or {}
    unfinished = (
        source.get("editorial_status") == "interrupted"
        or source.get("run_status") != "completed"
        or source.get("coverage_complete") is False
        or p.get("search") not in (None, "completed")
        or (p.get("scopes_total", 0) > p.get("scopes_searched", 0))
        or p.get("execution") not in (None, "completed")
        or p.get("audit") not in (None, "completed")
        or source.get("classification_status") not in (None, "completed")
        or any(
            p.get(k, 0) for k in ("failed_findings", "unresolved_findings", "unreviewed_findings")
        )
    )
    if unfinished:
        return "Partial", "Some review work remains unfinished."
    if source.get("coverage_complete") is True:
        return "Completed", "The planned review has finished."
    return "Coverage not recorded", "Completion of the planned review could not be established."


def render_excerpt(source, entry):
    from .locations import selected_passage
    from .writer import report_text

    if not entry.passages:
        if source.get("editorial_status") == "interrupted":
            return "Paper quotation selection is unfinished; see the retained source records.\n\n"
        return "Exact quotation unavailable: the reviewed source text was not supplied.\n\n"
    body = ""
    for passage in entry.passages:
        selected = selected_passage(source.get("paper_locations", {}), entry.issue_id, passage)
        label = "Paper excerpt" if passage.role == "issue" else "Related context"
        locator = "; ".join(selected["references"]) or passage.description
        body += "\\paragraph{" + label + "} " + escape_tex(locator) + "\n\n"
        if passage.display_quote:
            body += "\\begin{quote}\n" + report_text(passage.display_quote) + "\\end{quote}\n"
        if passage.role != "issue":
            body += escape_tex(passage.explanation) + "\n\n"
    return body


def render_paper(source, draft):
    from .presentation import presented_feedback
    from .writer import math_unicode_markdown, report_text

    status, explanation = review_status(source)
    counts = Counter(i.get("severity", "unknown") for i in source["issues"])
    findings = (
        ", ".join(
            f"{counts[k]} {label}"
            for k, label in [("major", "major"), ("minor", "minor"), ("unknown", "unclassified")]
            if counts[k]
        )
        or "No issues reported"
    )
    body = r"\section*{Review summary}" + "\n" + render_latex(math_unicode_markdown(draft.overview), fragment=True)
    rows = [
        ("Review status", status if status == "Completed" else status + ". " + explanation),
        ("Issues reported", findings),
    ]
    if draft.reference_limits:
        rows.append(
            ("Reference checks", "Source-access and verification limitations are noted below.")
        )
    body += r"""\par\medskip
\begingroup
\small\setlength{\parskip}{0pt}\renewcommand{\arraystretch}{1.3}
\begin{tabularx}{\linewidth}{@{}p{0.25\linewidth}X@{}}
\toprule
"""
    body += "\n".join(r"\textbf{\textcolor{ReportInk}{" + escape_tex(a) + "}} & " + escape_tex(b) + r" \\" for a, b in rows)
    body += "\n" + r"\bottomrule\end{tabularx}\endgroup" + "\n\n"
    if source.get("backend_kind") == "scripted_demo":
        body += r"\textbf{Demonstration only: no live mathematical review.}" + "\n\n"
    body += (
        r"{\small\color{ReportMuted}This report records issues identified during automated review; it does not certify the paper\textquotesingle s correctness.}"
        + "\n\n"
    )
    for severity, heading in [
        ("major", "Major issues"),
        ("minor", "Minor issues"),
        ("unknown", "Unclassified issues"),
    ]:
        entries = [
            e
            for e in draft.entries
            if next(i["severity"] for i in source["issues"] if i["issue_id"] == e.issue_id)
            == severity
        ]
        if not entries:
            continue
        body += "\\section*{" + heading + "}\n"
        for number, entry in enumerate(entries, 1):
            body += "\\subsection*{" + escape_tex(f"{number}. {entry.title}") + "}\\leavevmode\\par\n"
            body += (
                r"{\small\color{ReportMuted}\textbf{Type:} "
                + escape_tex(entry.issue_type)
                + "}\n\n"
            )
            body += render_excerpt(source, entry)
            for section in ("Concern", "Check", "Suggestion", "Clarification"):
                selected = [p for p in entry.feedback if p.section == section]
                if not selected:
                    continue
                label = {
                    "Concern": "Issue",
                    "Check": "Evidence",
                    "Suggestion": "Suggestion by OSVerify",
                }.get(section, section)
                if section == "Suggestion":
                    body += r"\begingroup\small\color{ReportMuted}\raggedright" + "\n"
                body += "\\paragraph{" + label + "}\n"
                for placement in selected:
                    # Typography/locators are separately audited; exact feedback stays in source.json.
                    body += report_text(presented_feedback(source, entry.issue_id, placement)) + "\n\n"
                if section == "Suggestion":
                    body += "\\endgroup\n"
    if draft.reference_limits:
        body += r"\Needspace{8\baselineskip}\section*{References not fully checked}" + "\n"
        body += "These are limits of the review, not additional issues in the paper.\n\n"
        refs = source["reference_limitations"]
        for limit in draft.reference_limits:
            # A grouped dependency can contain several titles for one source URL.
            # Keep every internal record while printing that source just once.
            titles_by_source = {}
            for i in limit.refs:
                title = refs[i].get("title") or "Unnamed reference"
                key = refs[i].get("url") or title
                titles_by_source.setdefault(key, title)
            titles = list(dict.fromkeys(titles_by_source.values()))
            body += r"\Needspace{6\baselineskip}\noindent\textbf{" + escape_tex("; ".join(titles)) + "}\n\n"
            if source.get("editorial_status") == "interrupted":
                # No audited summary exists: title, URL and a fixed notice only.
                urls = [key for key in titles_by_source if key.startswith(("http://", "https://"))]
                if urls:
                    body += r"{\small " + escape_tex("; ".join(urls)) + "}\n\n"
                body += escape_tex(REFERENCE_NOTICE) + "\n\n"
                continue
            body += r"\textit{Not checked:} " + escape_tex(limit.not_checked) + "\n\n"
            body += r"\textit{Reason:} " + escape_tex(limit.reason) + "\n\n"
    body += (
        r"\medskip{\footnotesize\color{ReportMuted}Detailed review records, source locations and audit history are retained in the accompanying internal report files. "
        + escape_tex("Report generated: " + generation_date(source) + " (UTC).")
        + "}"
        + "\n"
    )
    return (
        r"""\documentclass[11pt,reqno]{amsart}
\usepackage[margin=1in]{geometry}
\usepackage{fontspec}
\setmainfont{FreeSerif.otf}[BoldFont=FreeSerifBold.otf,ItalicFont=FreeSerifItalic.otf,BoldItalicFont=FreeSerifBoldItalic.otf]
\setmonofont{DejaVuSansMono.ttf}
\usepackage{amsmath,amssymb,amsthm,longtable,tabularx,booktabs,xcolor,textcomp}
\usepackage{needspace}
% Keep the classic amsart layout, with bold run-in paragraph labels.
\makeatletter
\renewcommand\paragraph{\@startsection{paragraph}{4}{\z@}{.5\baselineskip\@plus.7\baselineskip}{-.5em}{\normalfont\bfseries}}
\makeatother
\usepackage[normalem]{ulem}
\usepackage{xurl}
\usepackage[hidelinks]{hyperref}
\definecolor{ReportInk}{HTML}{28465D}
\definecolor{ReportMuted}{HTML}{626870}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.45em}
\setlength{\emergencystretch}{3em}
\tracinglostchars=3
\title[OSVerify referee report]{Referee Report on ``"""
        + escape_tex(paper_title(source))
        + r"""''}
\author{OSVerify}
\date{}
\begin{document}
\maketitle
"""
        + body
        + "\n\\end{document}\n"
    )
