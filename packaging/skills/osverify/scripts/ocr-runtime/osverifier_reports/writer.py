"""Bounded, separately audited report writing over immutable public feedback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from osverifier.backend import DEFAULT_MODEL, stop_process
from osverifier.connection_config import ConnectionConfig
from osverifier.exporting import escape_tex, render_latex
from osverifier.quota import communicate_with_quota_detection
from osverifier.storage import atomic_json, atomic_text, read_json, run_lock

ROOT = Path(__file__).parent
REPORT_FORMAT_VERSION = "reader-summary-8"  # 8: Unicode math typeset; report.tex only after it compiles.


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PresentationEdit(Strict):
    original: str = Field(min_length=1)
    occurrence: int = Field(ge=0)
    replacement: str = Field(min_length=1)
    kind: Literal["math", "location"]


class Placement(Strict):
    ref: int
    section: Literal["Concern", "Check", "Suggestion", "Clarification"]
    presentation: list[PresentationEdit] = Field(default_factory=list)


class PaperPassage(Strict):
    document: Literal["candidate_solution", "question"]
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    role: Literal["issue", "background", "downstream"]
    description: str
    quote: str
    display_quote: str
    explanation: str


class Entry(Strict):
    issue_id: str
    title: str
    issue_type: str
    feedback: list[Placement]
    passages: list[PaperPassage] = Field(default_factory=list)


class ReferenceLimit(Strict):
    refs: list[int] = Field(min_length=1)
    not_checked: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Draft(Strict):
    overview: str
    reference_limits: list[ReferenceLimit] = Field(default_factory=list)
    entries: list[Entry]


class Fidelity(Strict):
    approved: bool
    reasons: list[str]


def source_snapshot(path):
    source = read_json(path)
    if source.get("review_kind") == "paper" and source.get("classification_status") == "pending":
        raise ValueError("Wait for paper severity classification to finish")
    if source.get("run_status") not in ("completed", "incomplete"):
        raise ValueError("Wait for verification to stop before drafting a report")
    if not isinstance(source.get("feedback"), list):
        raise ValueError("A released report.json with feedback is required")
    # Never copy diagnostics/rationales or use model-written unreleased artifacts.
    payload = {
        key: source.get(key)
        for key in (
            "request_id",
            "mode",
            "review_kind",
            "classification_status",
            "classification_skip_reason",
            "reference_limitations",
            "supplemental_reviews",
            "decision",
            "run_status",
            "backend_kind",
            "coverage_complete",
            "termination_reason",
        )
    }
    payload["feedback"] = [
        {
            "ref": i,
            **{
                key: item[key]
                for key in (
                    "issue_id",
                    "feedback_id",
                    "kind",
                    "text",
                    "source_spans",
                    "audit_artifact",
                )
            },
        }
        for i, item in enumerate(source["feedback"])
    ]
    metadata = {item["issue_id"]: item for item in source.get("issues", [])}
    payload["issues"] = [
        {"issue_id": key, "severity": metadata.get(key, {}).get("severity", "unknown"), "rationale": metadata.get(key, {}).get("rationale", "")}
        for key in dict.fromkeys(item["issue_id"] for item in payload["feedback"])
    ]
    if source.get("review_kind") == "paper":
        from .references import internal_reference_limits

        # Internal editorial input only: full records from the run's internal
        # diagnostics, used for the audited reference summaries.
        payload["reference_limitations"] = internal_reference_limits(Path(path).parent, source.get("reference_limitations") or [])
        payload.pop("decision", None)
        payload["field"] = source.get("field", "math")
        if source.get("paper_title"):
            payload["paper_title"] = source["paper_title"]
        payload["report_generated_on"] = datetime.now(timezone.utc).date().isoformat()
        payload["finding_details"] = source.get("finding_details", [])
        if source.get("progress"):
            payload["progress"] = source["progress"]
    return payload


def keep_valid_presentation(source, draft):
    """Keep the usable presentation edits; never let one bad edit cost the report.

    Edits only typeset or locate released feedback, so dropping one never changes what
    is said. Longer edits win overlaps. A math edit whose occurrence overlaps a kept
    edit moves to the next free occurrence of the same expression (delimiters cannot
    change meaning); location edits are never moved. Solutions have no printed paper
    locators and keep math edits only. Returns every dropped or moved edit.
    """
    from .presentation import _token, presented_feedback

    changes = []
    for entry in draft.entries:
        for placement in entry.feedback:
            if not placement.presentation or not 0 <= placement.ref < len(source["feedback"]):
                continue
            text = source["feedback"][placement.ref]["text"]
            kept, taken = [], []
            for edit in sorted(placement.presentation, key=lambda e: -len(e.original)):
                note = {"issue_id": entry.issue_id, "ref": placement.ref, "original": edit.original}
                if source.get("review_kind") != "paper" and edit.kind != "math":
                    changes.append({**note, "reason": "solution reports have no printed paper locators"})
                    continue
                spans = [(m.start(), m.end()) for m in _token(edit.original).finditer(text)] if edit.original else []
                order = spans[edit.occurrence:edit.occurrence + 1]
                if edit.kind == "math":
                    order += [span for span in spans if span not in order]
                free = next((span for span in order if not any(a < span[1] and span[0] < b for a, b in taken)), None)
                if free is None:
                    changes.append({**note, "reason": "not anchored in the feedback or overlaps a longer edit"})
                    continue
                candidate = edit.model_copy(update={"occurrence": spans.index(free)})
                try:
                    presented_feedback(source, entry.issue_id, placement.model_copy(update={"presentation": [candidate]}))
                except ValueError as exc:
                    changes.append({**note, "reason": str(exc)})
                    continue
                if candidate.occurrence != edit.occurrence:
                    changes.append({**note, "reason": f"moved from occurrence {edit.occurrence} to {candidate.occurrence}"})
                kept.append((free, candidate))
                taken.append(free)
            placement.presentation = [edit for _, edit in sorted(kept, key=lambda pair: pair[0])]
    return changes


def printed_feedback(source, draft):
    """The exact text each placement prints once code applies its presentation edits.

    The checker judges these results instead of re-deriving token occurrences itself.
    """
    from .presentation import presented_feedback

    return [
        {"issue_id": entry.issue_id, "ref": placement.ref,
         "original": source["feedback"][placement.ref]["text"],
         "printed": presented_feedback(source, entry.issue_id, placement)
         if placement.presentation else source["feedback"][placement.ref]["text"]}
        for entry in draft.entries for placement in entry.feedback
    ]


def validate_draft(source, draft):
    expected = [item["issue_id"] for item in source["issues"]]
    if [entry.issue_id for entry in draft.entries] != expected:
        raise ValueError("Report must retain each source issue exactly once in original order")
    if source.get("review_kind") == "paper":
        reference_refs = [i for item in draft.reference_limits for i in item.refs]
        if sorted(reference_refs) != list(range(len(source.get("reference_limitations") or []))):
            raise ValueError("Every unresolved reference must appear exactly once in the concise summary")
        for item in draft.reference_limits:
            if not item.not_checked.strip() or not item.reason.strip():
                raise ValueError("Reference summaries need an unchecked dependency and reason")
    refs = []
    allowed = {
        "diagnosis": {"Concern", "Check"},
        "title": {"Concern"},
        "check": {"Check"},
        "repair_request": {"Suggestion"},
        "repair_suggestion": {"Suggestion"},
        "clarification_request": {"Clarification"},
    }
    for entry in draft.entries:
        if not entry.title.strip() or not entry.issue_type.strip():
            raise ValueError("Empty report heading")
        if source.get("review_kind") == "paper":
            from .locations import selected_passage

            locations = source.get("paper_locations", {})
            candidates = locations.get("issues", {}).get(entry.issue_id, {}).get("passages", [])
            if candidates and not any(p.role == "issue" for p in entry.passages):
                raise ValueError("Every paper issue with source text needs a precise issue passage")
            for passage in entry.passages:
                selected_passage(locations, entry.issue_id, passage)
        elif entry.passages:
            raise ValueError("Paper passages are only supported for paper reports")
        for placement in entry.feedback:
            if not 0 <= placement.ref < len(source["feedback"]):
                raise ValueError("Unknown feedback reference")
            item = source["feedback"][placement.ref]
            if item["issue_id"] != entry.issue_id or placement.section not in allowed.get(
                item["kind"], set()
            ):
                raise ValueError("Feedback assigned to an unsupported issue or section")
            if placement.presentation:
                if source.get("review_kind") != "paper" and any(e.kind != "math" for e in placement.presentation):
                    raise ValueError("Solution reports support only math presentation edits")
                from .presentation import presented_feedback

                presented_feedback(source, entry.issue_id, placement)
            refs.append(placement.ref)
    if sorted(refs) != list(range(len(source["feedback"]))):
        raise ValueError("Every released feedback must appear exactly once")


# The text font (FreeSerif) covers Unicode, but the legacy math fonts do not, and a
# missing glyph stops compilation. Inside math, each common Unicode symbol is
# typeset by its standard TeX command; any other character is set in the text
# font via \text{...}. The released text itself is never changed.
MATH_SYMBOLS = {
    **{c: "\\" + n for c, n in zip(
        "αβγδεϵζηθϑικλμνξπϖρϱσςτυφϕχψωΓΔΘΛΞΠΣΥΦΨΩ",
        "alpha beta gamma delta varepsilon epsilon zeta eta theta vartheta iota kappa lambda mu nu xi pi "
        "varpi rho varrho sigma varsigma tau upsilon varphi phi chi psi omega Gamma Delta Theta Lambda Xi "
        "Pi Sigma Upsilon Phi Psi Omega".split(), strict=True)},
    **{c: "\\" + n for c, n in zip(
        "∈∉∋⊂⊆⊃⊇⊊⊋∪∩∖∅∀∃∄¬∧∨⇒⇐⇔→←↔↦↪⊗⊕×÷±∓≠≈≡≅≃∼≤≥≪≫∞∂∇∑∏∫∘·⋅∣‖⟨⟩ℓℏℵ…√",
        "in notin ni subset subseteq supset supseteq subsetneq supsetneq cup cap setminus emptyset "
        "forall exists nexists neg wedge vee Rightarrow Leftarrow Leftrightarrow to leftarrow "
        "leftrightarrow mapsto hookrightarrow otimes oplus times div pm mp neq approx equiv cong simeq "
        "sim leq geq ll gg infty partial nabla sum prod int circ cdot cdot mid | langle rangle ell hbar "
        "aleph ldots surd".split(), strict=True)},
    **{c: "\\mathbb{" + c2 + "}" for c, c2 in zip("ℕℤℚℝℂℙ𝔽", "NZQRCPF", strict=True)},
    "−": "-", "∗": "*", "′": "'", "″": "''",
}
SCRIPTS = {**{c: ("^", d) for c, d in zip("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ", "0123456789+-=()ni", strict=True)},
           **{c: ("_", d) for c, d in zip("₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎", "0123456789+-=()", strict=True)}}


def math_unicode(math):
    """TeX for Unicode inside one math segment; text-mode groups stay verbatim."""
    out, plain, i = [], [], 0

    def flush():
        if plain:
            out.append(r"\text{" + "".join(plain) + "}")
            plain.clear()

    while i < len(math):
        group = re.match(r"\\(?:text[a-z]*|mbox|hbox)\s*\{", math[i:])
        if group:
            depth, j = 1, i + group.end()
            while j < len(math) and depth:
                depth += {"{": 1, "}": -1}.get(math[j], 0) if math[j - 1] != "\\" else 0
                j += 1
            flush()
            out.append(math[i:j])
            i = j
            continue
        char = math[i]
        if char in SCRIPTS:
            flush()
            kind, run = SCRIPTS[char][0], ""
            while i < len(math) and math[i] in SCRIPTS and SCRIPTS[math[i]][0] == kind:
                run += SCRIPTS[math[i]][1]
                i += 1
            out.append(kind + "{" + run + "}")
            continue
        if ord(char) < 128:
            flush()
            out.append(char)
        elif char in MATH_SYMBOLS:
            flush()
            command = MATH_SYMBOLS[char]
            out.append(command + (" " if re.search(r"\\[A-Za-z]+$", command) else ""))
        else:
            plain.append(char)
        i += 1
    flush()
    return "".join(out)


MATH_SEGMENT = r"(\$\$[\s\S]*?\$\$|\$[^$\n]+\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\))"


def math_unicode_markdown(value):
    """Apply math_unicode to every delimited math segment of a Markdown string."""
    parts = re.split(MATH_SEGMENT, value)
    for n in range(1, len(parts), 2):
        opening = 2 if parts[n].startswith(("$$", "\\[", "\\(")) else 1
        parts[n] = parts[n][:opening] + math_unicode(parts[n][opening:-opening]) + parts[n][-opening:]
    return "".join(parts)


def report_text(value):
    # Bare identifiers such as p_*ω and H*(B) are not Markdown emphasis.
    # Explicit math, code and link syntax retains the normal renderer behavior.
    protected = r"(\$\$[\s\S]*?\$\$|\$[^$\n]+\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|`[^`]+`|\[[^\]]+\]\([^\s)]+\))"
    parts = re.split(protected, value)
    for n in range(0, len(parts), 2):
        parts[n] = re.sub(r"(?<=\w)([_*])(?=[\w{(])", r"\\\1", parts[n])
    for n in range(1, len(parts), 2):
        if re.fullmatch(MATH_SEGMENT, parts[n]):
            parts[n] = math_unicode_markdown(parts[n])
    return render_latex("".join(parts), fragment=True).replace(r"\_", r"\_\allowbreak{}")


def wrap_metadata(value):
    # Visible metadata is unchanged; URLs and audit paths may break at separators.
    return "".join(escape_tex(char) + (r"\allowbreak{}" if char in "/_:.?&=-" else "")
                   for char in value)


def render_passages(source, entry):
    from .locations import selected_passage

    if not entry.passages:
        return ("\\paragraph{Where the issue occurs} "
                "Exact quotation unavailable: the reviewed source text was not supplied. "
                "Released source spans remain in the audit data.\n\n")
    body = ""
    for roles, heading in (({"issue"}, "Where the issue occurs"),
                           ({"background", "downstream"}, "Related context")):
        selections = [p for p in entry.passages if p.role in roles]
        if not selections:
            continue
        body += "\\paragraph{" + heading + "}\n"
        for passage in selections:
            resolved = selected_passage(source["paper_locations"], entry.issue_id, passage)
            label = {"issue": "", "background": "Background: ", "downstream": "Downstream use: "}[passage.role]
            refs = "; ".join(resolved["references"])
            locator = (refs + " — " if refs else "") + passage.description
            body += "\\textbf{" + escape_tex(label + locator) + "}\n\n"
            if passage.display_quote:
                body += "\\begin{quote}\n" + report_text(passage.display_quote) + "\\end{quote}\n"
            body += escape_tex(passage.explanation) + "\n\n"
    return body


def render(source, draft):
    validate_draft(source, draft)
    if source.get("review_kind") == "paper":
        from .public_referee import render_paper
        return render_paper(source, draft)
    body = "\\section*{Overall opinion}\n"
    body += (("Execution status: " if source.get("progress") else "Review status: ") + escape_tex(source["run_status"]) + ".\n\n") if source.get("review_kind") == "paper" else (
        "Decision: "
        + escape_tex(source["decision"] or "Pending")
        + ". Run: "
        + escape_tex(source["run_status"])
        + ".\n\n"
    )
    body += (
        "This is an editorial presentation of automated review; it is not a proof certificate.\n\n"
    )
    if source.get("mode") == "partial":
        body += "Partial-progress review: PASS permits continuing, not completion or certified correctness. REVISE does not guarantee repairability. REJECT concerns the current route.\n\n"
    if source.get("backend_kind") == "scripted_demo":
        body += "\\textbf{SCRIPTED DEMO: no live mathematical judgments.}\n\n"
    if source["run_status"] != "completed":
        body += "\\textbf{Review is unfinished; feedback is partial.}\n\n"
    if source.get("coverage_complete") is not None:
        body += "Full planned scope coverage: " + str(source["coverage_complete"]) + ".\n\n"
    if source.get("termination_reason") == "audited_rejection":
        body += "Stopped after an audited rejection; remaining work was skipped or cancelled.\n\n"
    if source.get("review_kind") == "paper":
        from .fields import AI_REVIEW_SCOPE, FIELD_LABELS
        from .progress import PaperProgress, progress_lines

        if source.get("progress"):
            body += "\n\n".join(escape_tex(line) for line in progress_lines(PaperProgress.model_validate(source["progress"]))) + "\n\n"
        body += "Field: " + escape_tex(FIELD_LABELS[source.get("field", "math")]) + ". Correctness review only.\n\n"
        if source.get("field") == "ai":
            body += escape_tex(AI_REVIEW_SCOPE) + "\n\n"
        body += "Severity classification: " + escape_tex(source.get("classification_status") or "unknown") + ".\n\n"
        if source.get("classification_skip_reason"):
            body += "Classification skipped: " + escape_tex(source["classification_skip_reason"].replace("_", " ")) + ".\n\n"
    if source.get("reference_limitations"):
        body += "\\section*{Unresolved external references}\nThese limitations are not established mathematical defects.\n"
        from .references import REFERENCE_NOTICE, public_references

        for ref in public_references(source["reference_limitations"]):
            body += wrap_metadata(f"{ref['title']}: {ref['url']}") + ". " + escape_tex(REFERENCE_NOTICE) + "\n\n"
    body += render_latex(math_unicode_markdown(draft.overview), fragment=True)
    for severity, label in (
        ("major", "Major Imprecisions"),
        ("minor", "Minor Imprecisions"),
        ("unknown", "Unclassified Imprecisions and Clarifications"),
    ):
        entries = [
            e
            for e in draft.entries
            if next(i["severity"] for i in source["issues"] if i["issue_id"] == e.issue_id)
            == severity
        ]
        if not entries:
            continue
        body += "\\section{" + label + "}\n"
        if severity == "unknown":
            body += "The source report does not provide a released major/minor classification.\n\n"
        for entry in entries:
            body += "\\subsection{" + escape_tex(entry.title) + "}\n"
            items = [source["feedback"][p.ref] for p in entry.feedback]
            locations = dict.fromkeys(
                f"{s['document']} lines {s['start_line']}–{s['end_line']}"
                for item in items
                for s in item["source_spans"]
            )
            if source.get("review_kind") == "paper":
                body += render_passages(source, entry)
            else:
                body += "\\paragraph{Location} " + escape_tex("; ".join(locations) or "Not supplied.") + "\n\n"
            rationale = next(i.get("rationale", "") for i in source["issues"] if i["issue_id"] == entry.issue_id)
            if rationale:
                body += "\\paragraph{Classification basis} " + escape_tex(rationale) + "\n\n"
            detail = next((d for d in source.get("finding_details", []) if d["issue_id"] == entry.issue_id), None)
            if detail:
                body += "\\paragraph{Assessment} " + escape_tex(detail["assessment"] + "; " + detail["kind"].replace("_", " ")) + "\n\n"
            body += "\\paragraph{Type} " + escape_tex(entry.issue_type) + "\n\n"
            for section in ("Concern", "Check", "Suggestion", "Clarification"):
                selected = [p for p in entry.feedback if p.section == section]
                if not selected and section == "Clarification":
                    continue
                heading = "Proposed repair by OSVerify" if section == "Suggestion" else section
                body += "\\paragraph{" + heading + "}\n"
                if not selected:
                    body += (
                        "No separate "
                        + ("proposed repair" if section == "Suggestion" else section.lower())
                        + " was supplied in the released feedback.\n\n"
                    )
                for placement in selected:
                    item = source["feedback"][placement.ref]
                    if placement.presentation:
                        from .presentation import presented_feedback

                        body += report_text(presented_feedback(source, entry.issue_id, placement))
                    else:
                        body += report_text(item["text"])
                    body += (
                        "\n{\\footnotesize Source: "
                        + wrap_metadata(
                            item["issue_id"]
                            + "/"
                            + item["feedback_id"]
                            + "; "
                            + item["audit_artifact"]
                        )
                        + "}\n\n"
                    )
    if not draft.entries:
        body += "No feedback text was released.\n"
    return render_latex("", title=f"Referee Report on “{source['request_id']}”").replace(
        "\\title{", "\\title[OSVerify referee report]{", 1
    ).replace(
        r"\usepackage{fontspec}",
        r"\usepackage{fontspec}" + "\n"
        + r"\setmainfont{FreeSerif.otf}[BoldFont=FreeSerifBold.otf,ItalicFont=FreeSerifItalic.otf,BoldItalicFont=FreeSerifBoldItalic.otf]" + "\n"
        + r"\setmonofont{DejaVuSansMono.ttf}" + "\n"
        + r"\tracinglostchars=3",
    ).replace(
        "\\end{document}", body + "\\end{document}"
    )


def strict_output_schema(schema):
    """Require every output key while retaining defaults for historical draft loading."""
    if isinstance(schema, dict):
        schema.pop("default", None)
        if "properties" in schema:
            schema["required"] = list(schema["properties"])
            schema["additionalProperties"] = False
        for value in schema.values():
            strict_output_schema(value)
    elif isinstance(schema, list):
        for value in schema:
            strict_output_schema(value)
    return schema


DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_REASONING_EFFORT = "high"


def report_writer(args):
    """Classifier/editorial caller: explicit run overrides win; otherwise editorial defaults."""
    explicit = getattr(args, "explicit_options", ())
    return CodexWriter(
        args.model, args.codex_bin, args.codex_home,
        args.timeout_seconds if "timeout_seconds" in explicit else DEFAULT_TIMEOUT_SECONDS,
        reasoning_effort=args.reasoning_effort if "reasoning_effort" in explicit else DEFAULT_REASONING_EFFORT,
    )


class CodexWriter:
    def __init__(self, model, codex_bin, codex_home=None, timeout=DEFAULT_TIMEOUT_SECONDS, *,
                 reasoning_effort=DEFAULT_REASONING_EFFORT, process_backend=None):
        self.reasoning_effort = reasoning_effort
        self.process_backend = process_backend
        self.model, self.timeout = model, timeout
        self.binary = shutil.which(codex_bin)
        if not self.binary:
            raise ValueError("Codex binary not found")
        from osverifier.backend import supported_codex_version

        supported_codex_version(self.binary)
        self.connection = ConnectionConfig(codex_home)

    def call(self, role, payload, schema, directory):
        from .evidence import (
            attachment_paths,
            attachment_prompt,
            check_images,
            observation_schema,
            take_observations,
        )

        images = payload.get("paper_images", [])
        check_images(images)
        from .image_selection import select_images

        attached, selection_reason = select_images(images, role, payload)
        if role == "check-pdf-issue-source" and "ocr_attached_image_ids" in payload:
            wanted = payload["ocr_attached_image_ids"]
            attached = [i for i in images if i["image_id"] in wanted]
            if len(attached) != len(wanted):
                raise ValueError("Unknown or duplicate OCR source-check attachment")
            selection_reason = "frozen_ocr_page_spans_and_adjacent_context"
        ai = payload.get("field", payload.get("source", {}).get("field")) == "ai"
        if images:
            payload = {**payload, "attached_image_ids": [i["image_id"] for i in attached],
                       "image_selection_reason": selection_reason}
        workspace = directory / "workspace"
        shutil.copytree(ROOT / "skills" / role, workspace / ".agents" / "skills" / role)
        shutil.copy2(ROOT / "template.tex", workspace / "template.tex")
        atomic_json(workspace / "input.json", payload)
        output_schema = observation_schema(schema.model_json_schema(), images, required_images=attached) if images else schema.model_json_schema()
        atomic_json(directory / "schema.json", strict_output_schema(output_schema))
        prompt = f"Use ${role}. Read .agents/skills/{role}/SKILL.md, input.json and template.tex. Return only the schema-constrained result."
        if images:
            prompt += " " + attachment_prompt(images, attached)
        atomic_text(directory / "prompt.txt", prompt)
        argv = [
            self.binary,
            "exec",
            "--ignore-user-config",
            *self.connection.arguments,
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            self.model,
            "--json",
            "--cd",
            str(workspace),
            "--output-schema",
            str(directory / "schema.json"),
            "--output-last-message",
            str(directory / "result.json"),
            "-c",
            'approval_policy="never"',
            "-c",
            'web_search="disabled"',
            "-c",
            f'model_reasoning_effort={json.dumps(self.reasoning_effort)}',
            "-",
        ]
        if images:
            argv[2:2] = [arg for path in attachment_paths(attached) for arg in ("--image", str(path))]
        if ai or role in ("transcribe-pdf-page", "check-pdf-transcription", "check-pdf-issue-source"):
            argv.insert(2, "--ephemeral")
        atomic_json(
            directory / "command.json",
            {"argv": self.connection.redact(argv), "connection": self.connection.identity},
        )
        with (
            (directory / "events.jsonl").open("w") as out,
            (directory / "stderr.log").open("w") as err,
        ):
            guard = self.process_backend
            # OCR shares cancellation/quota state with the verifier transport.
            if guard is not None:
                guard._process_lock.acquire()
                if guard._quota_error is not None or guard._cancelled.is_set():
                    guard._process_lock.release()
                    if guard._quota_error is not None:
                        raise guard._quota_error
                    from osverifier.backend import BackendCancelled
                    raise BackendCancelled("OCR call cancelled")
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=out,
                    stderr=err,
                    text=True,
                    env={**os.environ, "CODEX_HOME": str(self.connection.home)},
                    start_new_session=True,
                )
                if guard is not None:
                    guard._processes.add(process)
            finally:
                if guard is not None:
                    guard._process_lock.release()
            try:
                communicate_with_quota_detection(process, prompt, self.timeout, directory)
            except BaseException as exc:
                stop_process(process)
                if guard is not None:
                    from osverifier.quota import QuotaExhausted
                    if isinstance(exc, QuotaExhausted):
                        guard.halt_for_quota(exc)
                raise
            finally:
                if guard is not None:
                    with guard._process_lock:
                        guard._processes.discard(process)
            if guard is not None:
                if guard._quota_error is not None:
                    raise guard._quota_error
                if guard._cancelled.is_set():
                    from osverifier.backend import BackendCancelled
                    raise BackendCancelled("OCR call cancelled")
        if process.returncode:
            raise RuntimeError(f"Report {role} failed; inspect {directory}")
        check_images(images)
        result = read_json(directory / "result.json")
        if images:
            result, visual = take_observations(result, images, required_images=attached)
            atomic_json(directory / "paper-observations.json", {
                "attached_image_ids": [i["image_id"] for i in attached],
                "image_selection_reason": selection_reason, **visual})
        return schema.model_validate(result)


WRITER_ATTEMPTS = 2  # one retry with the checker's reasons: at most two extra calls


def write_and_check(source, backend, output_dir):
    """Draft, repair presentation, validate and editorially check; retry once on rejection.

    A withheld or invalid first draft goes back to a fresh writer call together with the
    reasons; a fresh, independent checker then judges the new draft. Every attempt keeps
    its own artifacts; draft.json and editorial-check.json hold the last attempt.
    """
    reasons = previous = None
    for attempt in range(1, WRITER_ATTEMPTS + 1):
        suffix = "" if attempt == 1 else f"-retry-{attempt - 1}"
        payload = {"source": source}
        if reasons is not None:
            payload.update(previous_draft=previous, editorial_reasons=reasons)
        draft = backend.call("write-referee-report", payload, Draft, output_dir / f"writer{suffix}")
        fallbacks = keep_valid_presentation(source, draft)
        if fallbacks:
            atomic_json(output_dir / f"presentation-fallbacks{suffix}.json", fallbacks)
        atomic_json(output_dir / "draft.json", draft.model_dump())
        atomic_json(output_dir / f"draft-attempt-{attempt}.json", draft.model_dump())
        previous = draft.model_dump()
        try:
            validate_draft(source, draft)
        except ValueError as exc:
            if attempt == WRITER_ATTEMPTS:
                raise
            reasons = [f"The draft was rejected by the format checks: {exc}"]
            continue
        audit = backend.call(
            "check-referee-report",
            {"source": source, "draft": draft.model_dump(), "printed_feedback": printed_feedback(source, draft)},
            Fidelity,
            output_dir / f"editorial-check{suffix}",
        )
        atomic_json(output_dir / "editorial-check.json", audit.model_dump())
        atomic_json(output_dir / f"editorial-check-attempt-{attempt}.json", audit.model_dump())
        if audit.approved:
            return draft
        reasons = audit.reasons
    raise ValueError(
        f"Editorial fidelity check withheld the report after {WRITER_ATTEMPTS} drafts; "
        "see editorial-check-attempt-*.json"
    )


def write_report(source_dir, output_dir, backend, *, paper_assets=None, paper_pdf=None):
    source_dir, output_dir = Path(source_dir).resolve(), Path(output_dir).resolve()
    if output_dir.is_relative_to(source_dir) or source_dir.is_relative_to(output_dir):
        raise ValueError("Choose a separate output directory outside the verification run")
    with run_lock(output_dir):
        if (output_dir / "status.json").exists():
            raise ValueError("Use a fresh report output directory")
        atomic_json(output_dir / "status.json", {"status": "running"})
        try:
            source = source_snapshot(source_dir / "report.json")
            if source.get("review_kind") == "paper":
                from .locations import locations_for_run

                source["paper_locations"] = locations_for_run(
                    source_dir, source, output_dir / "paper-locations", assets_dir=paper_assets, paper_pdf=paper_pdf
                )
            atomic_json(output_dir / "source.json", source)
            provenance = {
                "source_directory": str(source_dir),
                "source_sha256": hashlib.sha256(
                    json.dumps(source, sort_keys=True).encode()
                ).hexdigest(),
                "assets": {
                    str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in ROOT.rglob("*")
                    if p.is_file() and p.suffix in (".py", ".md", ".tex")
                },
            }
            atomic_json(output_dir / "provenance.json", provenance)
            draft = write_and_check(source, backend, output_dir)
            if source.get("review_kind") == "paper":
                from .locations import selected_passage

                atomic_json(output_dir / "issue-passages.json", {
                    "schema_version": "issue-passages-1",
                    "request_id": source["request_id"],
                    "source_sha256": source["paper_locations"].get("source_sha256"),
                    "issues": {entry.issue_id: [selected_passage(source["paper_locations"], entry.issue_id, p)
                                                for p in entry.passages] for entry in draft.entries},
                })
            atomic_text(output_dir / "report.tex", render(source, draft))
            atomic_json(
                output_dir / "status.json", {"status": "completed", "editorial_check": "approved"}
            )
        except BaseException as exc:
            atomic_json(
                output_dir / "status.json",
                {"status": "failed", "error": type(exc).__name__ + ": " + str(exc)},
            )
            raise
    return output_dir / "report.tex"


def main(argv=None, prog=None):
    p = argparse.ArgumentParser(
        prog=prog,
        description="Draft a template-based referee report from released verification results",
    )
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--codex-bin", default="codex")
    p.add_argument("--codex-home", type=Path)
    p.add_argument("--timeout", "--timeout-seconds", dest="timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    p.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    p.add_argument("--paper-pdf", type=Path, help="Original reviewed PDF for exact heading matching if LaTeX cannot compile")
    p.add_argument("--paper-assets", type=Path, help="Local LaTeX figures/styles needed to resolve printed paper references")
    args = p.parse_args(argv)
    try:
        result = write_report(
            args.run_dir,
            args.output_dir,
            CodexWriter(args.model, args.codex_bin, args.codex_home, args.timeout, reasoning_effort=args.reasoning_effort),
            paper_assets=args.paper_assets, paper_pdf=args.paper_pdf,
        )
        print(result)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
