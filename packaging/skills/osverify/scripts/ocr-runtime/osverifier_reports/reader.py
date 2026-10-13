"""Offline English AI reports over immutable released feedback, never new judgments."""
from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path

from osverifier.exporting import render_latex
from osverifier.storage import atomic_json, atomic_text, read_json, run_lock

from .paper import PaperReport, public_report
from .references import REFERENCE_NOTICE, public_references

LABELS = {
    "diagnosis": "Finding",
    "repair_request": "Requested response",
    "repair_suggestion": "Suggested correction",
    "clarification_request": "Question for the authors",
}
KINDS = {
    "calculation_error": "Calculation", "false_claim": "Claim accuracy",
    "invalid_inference": "Inference", "omission": "Evidence or specification gap",
    "presentation": "Presentation", "other": "Correctness concern",
}
CSS = """
:root{color-scheme:light;font-family:system-ui,sans-serif;color:#203040;background:#f2f5f7}
*{box-sizing:border-box}body{margin:0;line-height:1.65}main{max-width:980px;margin:40px auto;padding:36px;background:white;border:1px solid #dde4e9;border-radius:12px}
h1,h2,h3{line-height:1.25;color:#143448}h1{font-size:2.2rem}h2{margin-top:2.5rem;border-bottom:1px solid #dbe3e8;padding-bottom:.6rem}h3{font-size:1.25rem}
a{color:#126778}nav{display:flex;flex-wrap:wrap;gap:20px}.eyebrow,.meta{font-size:.85rem;color:#5e6c77}.banner{padding:16px 20px;background:#edf4f7;border-left:4px solid #28728a}
.stats{display:flex;flex-wrap:wrap;gap:30px;margin:24px 0}.stat strong{display:block;font-size:2rem;line-height:1.2}.stat span{font-size:.85rem;color:#5e6c77}
article{padding:24px 0;border-bottom:1px solid #dbe3e8}article h3{margin-top:0}.label{font-weight:650;margin:20px 0 6px}.feedback{white-space:pre-wrap;margin:0;overflow-wrap:anywhere}
ul{padding-left:24px}.scope{color:#536473;font-size:.9rem}
@media(max-width:640px){main{margin:0;border:0;border-radius:0;padding:22px}h1{font-size:1.8rem}}@media print{body{background:white}main{margin:0;border:0;padding:0}article{break-inside:avoid}nav{display:none}}
"""


def _view(report: PaperReport) -> dict:
    details = {d.issue_id: d for d in report.finding_details}
    ratings = {i.issue_id: i for i in report.issues}
    ids = list(dict.fromkeys(f.issue_id for f in report.feedback))
    if set(ids) != set(ratings):
        raise ValueError("Every reported issue must have released feedback")
    keys = [(f.issue_id, f.feedback_id) for f in report.feedback]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate released feedback")
    groups = [
        ("major", "Major findings"),
        ("minor", "Minor findings"),
        ("unknown", "Findings with undetermined severity"),
        ("questions", "Questions for the authors"),
        ("unassessed", "Other review comments"),
    ]
    buckets = {key: [] for key, _ in groups}
    entries = []
    for index, issue_id in enumerate(ids, 1):
        detail, rating = details.get(issue_id), ratings[issue_id]
        assessment = detail.assessment if detail else "not recorded"
        bucket = "questions" if assessment == "unresolved" else rating.severity if assessment == "confirmed" else "unassessed"
        feedback = [f for f in report.feedback if f.issue_id == issue_id]
        entry = {"anchor": f"finding-{index}", "number": index, "issue_id": issue_id,
                 "assessment": assessment, "kind": KINDS.get(detail.kind, "Correctness concern") if detail else "Correctness concern",
                 "severity": rating.severity, "rationale": rating.rationale,
                 "feedback": feedback}
        buckets[bucket].append(entry)
        entries.append(entry)
    entries = [entry for key, _ in groups for entry in buckets[key]]
    for index, entry in enumerate(entries, 1):
        entry.update(number=index, anchor=f"finding-{index}")
    p = report.progress
    partial = report.run_status != "completed" or not report.coverage_complete or report.classification_status != "completed" or bool(report.interruption_reason)
    execution = p.execution if p else report.run_status
    status = "Review in progress" if execution == "running" else "Review completed" if not partial else "Review incomplete"
    summary = []
    if p and p.failed_findings:
        n = p.failed_findings
        excluded = "It is" if n == 1 else "They are"
        summary.append(f"The review of {n} additional potential {'issue' if n == 1 else 'issues'} could not be completed. {excluded} not included in this report.")
    if p and p.unreviewed_findings:
        summary.append(f"{p.unreviewed_findings} additional potential issues still await review.")
    if not report.coverage_complete:
        summary.append("Some of the selected material has not yet been fully reviewed.")
    if report.run_status != "completed" and report.coverage_complete and not (p and (p.failed_findings or p.unreviewed_findings)):
        summary.append("The review did not finish successfully; the findings below are the results available so far.")
    classification = {
        "pending": "Severity assessment is not yet available.",
        "skipped": "Severity assessment has not been performed.",
        "failed": "Severity assessment could not be completed.",
        "withheld": "The proposed severity ratings could not be independently confirmed and are omitted.",
        "completed": "The findings and severity ratings below have passed independent review.",
    }
    if entries:
        summary.append(classification[report.classification_status])
    if report.interruption_reason:
        summary.append("The review stopped when model usage limits were reached.")
    return {"groups": [(key, label, buckets[key]) for key, label in groups if buckets[key]], "entries": entries,
            "partial": partial, "status": status, "summary": summary,
            "confirmed": sum(e["assessment"] == "confirmed" for e in entries),
            "unresolved": sum(e["assessment"] == "unresolved" for e in entries)}


def _locations(entry) -> str:
    spans = dict.fromkeys(
        f"{s.document} lines {s.start_line}-{s.end_line}"
        for f in entry["feedback"] for s in f.source_spans
    )
    return "; ".join(spans) or "No source location supplied."


def render_reader(report: PaperReport, *, paper_title: str | None = None) -> tuple[str, str, str, list[dict]]:
    """Return HTML, Markdown, TeX and an audit index; every feedback appears once."""
    view = _view(report)
    esc = html.escape
    title = "Correctness Review" + (" for " + paper_title if paper_title else "")
    scope = "This AI-assisted review examines the correctness of the paper's claims and supporting evidence. Questions for the authors remain unresolved and are not confirmed errors."
    counts = [(len(entries), label) for _, label, entries in view["groups"]]
    parts = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)}</title><style>{CSS}</style></head><body><main>',
             f'<div class="eyebrow">OSVerify</div><h1>{esc(title)}</h1>',
             f'<div class="banner"><strong>{esc(view["status"])}</strong><ul>' + "".join(f"<li>{esc(s)}</li>" for s in view["summary"]) + "</ul></div>",
             '<div class="stats">' + "".join(f'<div class="stat"><strong>{count}</strong><span>{label}</span></div>' for count, label in counts) + "</div>",
             f'<p class="scope">{scope}</p>',
             '<nav aria-label="Report sections">' + "".join(f'<a href="#{key}">{esc(label)} ({len(entries)})</a>' for key, label, entries in view["groups"]) + '<a href="#evidence">Scope and limitations</a></nav>']
    md = [f"# {title}", "", "**" + view["status"] + "**", ""]
    md += ["- " + s for s in view["summary"]]
    md += ["", " | ".join(f"**{count}** {label}" for count, label in counts), "", scope, ""]
    audit_index = []
    if not view["entries"]:
        text = "No findings are available for inclusion in this report. This does not establish that the paper is correct."
        parts.append(f"<p>{text}</p>")
        md += [text, ""]
    for key, label, entries in view["groups"]:
        parts.append(f'<section id="{key}"><h2>{esc(label)}</h2>')
        md += ["## " + label, ""]
        for entry in entries:
            heading = f"{entry['number']:02d}. {entry['kind']}"
            parts += [f'<article id="{entry["anchor"]}"><h3>{heading}</h3>']
            md += ["### " + heading, ""]
            for index, f in enumerate(entry["feedback"]):
                parts += [f'<div class="label">{LABELS[f.kind]}</div><p class="feedback">{esc(f.text)}</p>']
                md += ["**" + LABELS[f.kind] + "**", "", f.text, ""]
                if index == 0 and report.classification_status == "completed":
                    basis = "Why the impact is uncertain" if entry["severity"] == "unknown" else "Why this is " + entry["severity"]
                    parts.append(f'<p class="meta"><strong>{basis}:</strong> {esc(entry["rationale"])}</p>')
                    md += [basis + ": " + entry["rationale"], ""]
            parts.append("</article>")
            audit_index.append({"reader_number": entry["number"], "issue_id": entry["issue_id"], "location": _locations(entry),
                                "feedback": [f.model_dump(mode="json") for f in entry["feedback"]]})
        parts.append("</section>")
    # Title and URL only; model-written dependency notes stay in internal diagnostics.
    evidence = [f"Unresolved external reference: {r['title']}" + (f" ({r['url']})" if r['url'] else "") + f". {REFERENCE_NOTICE}"
                for r in public_references(report.reference_limitations)]
    if report.progress:
        p = report.progress
        names = {"code": "source code", "data": "raw data", "logs": "training logs", "supplement": "supplementary material", "video": "video", "unreadable_image": "readable versions of some figures"}
        # Unresolved references are listed once above, never repeated with their notes.
        material = [x for x in p.evidence_limits if not x.startswith("Unresolved reference: ")]
        if material:
            evidence.append("The following material was unavailable to this review: " + ", ".join(names.get(x, x.replace("_", " ")) for x in material) + ".")
        if p.image_review_calls:
            evidence.append("The review included visual inspection of supplied paper images.")
        elif p.images == "not_supplied":
            evidence.append("No paper images were supplied, so figure content was not visually checked.")
        else:
            evidence.append("The supplied images have not yet been visually checked.")
    evidence += ["This report does not reproduce the experiments or certify that the paper is free of other errors. It does not assess novelty or recommend acceptance or rejection.",
                 "Counts refer to listed findings and questions, which may concern overlapping claims."]
    if report.backend_kind == "scripted_demo":
        evidence.insert(0, "SCRIPTED DEMO: no live scientific judgments.")
    parts += ['<section id="evidence"><h2>Scope and limitations</h2>', *[f"<p>{esc(s)}</p>" for s in evidence], "</section></main></body></html>"]
    md += ["## Scope and limitations", "", *[s + "\n" for s in evidence]]
    markdown = "\n".join(md) + "\n"
    # The PDF has its own title block; omit the Markdown heading there.
    tex = render_latex("\n".join(md[2:]), title=title)
    tex = tex.replace(r"\documentclass[11pt,reqno]{amsart}", r"\documentclass[11pt]{article}")
    tex = tex.replace(r"\usepackage{fontspec}", r"\usepackage{fontspec}" + "\n" + r"\IfFontExistsTF{STIXGeneral}{\setmainfont{STIXGeneral}}{\setmainfont{TeX Gyre Termes}}" + "\n" + r"\usepackage{needspace}")
    tex = tex.replace(r"\begin{document}", r"\setlength{\parindent}{0pt}\setlength{\parskip}{0.45em}" + "\n" + r"\begin{document}\raggedright")
    tex = tex.replace(r"\subsubsection*{", r"\Needspace{9\baselineskip}\subsubsection*{")
    tex = tex.replace(r"\subsection*{", r"\Needspace{12\baselineskip}\subsection*{")
    for label in LABELS.values():
        heading = r"\textbf{" + label + "}"
        tex = tex.replace(heading + "\n\n", r"\Needspace{3\baselineskip}" + heading + "\n\n")
    return "".join(parts), markdown, tex, audit_index


def _title_metadata(run_dir: Path, request_id: str, override: str | None) -> dict:
    """Use an explicit title or an identified title field, never a request ID."""
    if override is not None:
        title = " ".join(override.split())
        if not title:
            raise ValueError("Paper title must not be blank")
        return {"paper_title": title, "source": "explicit"}
    path = run_dir / "request.json"
    metadata = {"paper_title": None, "source": "not_found"}
    if not path.exists():
        return metadata
    raw = path.read_bytes()
    request = json.loads(raw)
    if request.get("request_id") != request_id:
        raise ValueError("Report and title source refer to different requests")
    metadata["request_sha256"] = hashlib.sha256(raw).hexdigest()
    paper = request.get("candidate_solution", "").lstrip()
    heading = re.match(r"#[ \t]+([^\n]+)", paper)
    if heading:
        title = re.sub(r"[ \t]+#+[ \t]*$", "", heading.group(1)).strip()
        if title.lower() not in {"abstract", "introduction", "paper", "manuscript"}:
            return {**metadata, "paper_title": title, "source": "paper_markdown_title"}
    preamble = re.sub(r"(?<!\\)%[^\n]*", "", paper.split(r"\begin{document}")[0])
    latex = re.search(r"\\title\s*(?:\[[^\]]*\])?\s*{((?:[^{}]|{[^{}]*})*)}", preamble)
    if latex:
        title = re.sub(r"\\(?:textbf|textit|emph|textrm)\s*{([^{}]*)}", r"\1", latex.group(1))
        title = title.replace(r"\&", "&").replace(r"\%", "%").replace("{", "").replace("}", "")
        if "\\" not in title and title.strip():
            return {**metadata, "paper_title": " ".join(title.split()), "source": "paper_latex_title"}
    question = request.get("question", "").splitlines()
    named = re.fullmatch(r'Review the correctness of the selected claims in "(.+)"\.', question[0]) if question else None
    if named:
        return {**metadata, "paper_title": named.group(1), "source": "review_scope_title"}
    return metadata


def publish_reader(run_dir: Path, *, paper_title: str | None = None) -> Path:
    """Always save a standalone HTML report, with best-effort PDF and exact lineage."""
    from .automatic import compile_pdf

    run_dir = Path(run_dir).resolve()
    raw = (run_dir / "report.json").read_bytes()
    report = PaperReport.model_validate_json(raw)
    if report.field != "ai":
        raise ValueError("The English reader report is for AI paper review only")
    metadata = _title_metadata(run_dir, report.request_id, paper_title)
    source_hash = hashlib.sha256(raw).hexdigest()
    assets = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (Path(__file__), Path(__file__).parents[1] / "osverifier/exporting.py")}
    renderer_hash = hashlib.sha256(json.dumps({"assets": assets, "display_metadata": metadata}, sort_keys=True).encode()).hexdigest()[:12]
    out = run_dir.parent / (run_dir.name + "-reports") / source_hash / ("reader-" + renderer_hash)
    with run_lock(out):
        state = read_json(out / "status.json") if (out / "status.json").exists() else {}
        hashes = state.get("artifact_sha256", {})
        if state.get("status") == "completed" and state.get("pdf_status") == "completed" and all(
            (out / name).is_file() and hashlib.sha256((out / name).read_bytes()).hexdigest() == digest for name, digest in hashes.items()
        ) and hashes:
            return out / "report.html"
        content, markdown, tex, audit_index = render_reader(report, paper_title=metadata["paper_title"])
        atomic_json(out / "status.json", {"status": "running"})
        atomic_text(out / "report.html", content)
        atomic_text(out / "report.md", markdown)
        atomic_text(out / "report.tex", tex)
        # Reader-facing sidecar: the public projection (reference title/URL only).
        # source_sha256 in provenance.json still binds the exact run report.
        atomic_json(out / "source-report.json", public_report(report).model_dump(mode="json"))
        atomic_json(out / "audit-index.json", audit_index)
        atomic_json(out / "display-metadata.json", metadata)
        atomic_json(out / "provenance.json", {"source_directory": str(run_dir), "source_sha256": source_hash,
                                                   "source_report_projection": "reference_limitations reduced to title and url", "renderer_assets": assets, "display_metadata": metadata, "model_calls": 0})
        pdf_status, pdf_error = "completed", None
        try:
            compile_pdf(out / "report.tex")
        except Exception as exc:
            pdf_status, pdf_error = "failed", f"{type(exc).__name__}: {exc}"
        names = ["report.html", "report.md", "report.tex", "source-report.json", "audit-index.json", "display-metadata.json", "provenance.json"]
        if pdf_status == "completed":
            names.append("report.pdf")
        atomic_json(out / "status.json", {"status": "completed", "partial": _view(report)["partial"],
                    "pdf_status": pdf_status, "pdf_error": pdf_error,
                    "artifact_sha256": {name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in names}})
    return out / "report.html"


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Export an English AI reader report without model calls")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--paper-title", help="Display title if the input has no identifiable paper title")
    args = parser.parse_args(argv)
    print(publish_reader(args.run_dir, paper_title=args.paper_title))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
