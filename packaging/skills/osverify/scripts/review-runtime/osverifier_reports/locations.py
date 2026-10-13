"""Resolve released source spans to paper references, without changing feedback.

Numbers come from a fresh, isolated LaTeX build of the reviewed source. We never
guess a printed number from a label name or by counting environments ourselves.
Uncompilable or non-LaTeX input falls back to named headings, not invented numbers.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from osverifier.backend import stop_process
from osverifier.storage import atomic_json


def _group(text, pos, opening="{", closing="}"):
    while pos < len(text) and text[pos].isspace():
        pos += 1
    if pos >= len(text) or text[pos] != opening:
        return "", pos
    start, depth = pos + 1, 1
    pos += 1
    while pos < len(text):
        if text[pos] == "\\":
            pos += 2
            continue
        if text[pos] == opening:
            depth += 1
        elif text[pos] == closing:
            depth -= 1
            if not depth:
                return text[start:pos], pos + 1
        pos += 1
    return "", start - 1


def _uncomment(source):
    # Preserve offsets and line numbers. Escaped percent signs are not comments.
    return re.sub(r"(?<!\\)(?:\\\\)*%[^\n]*", lambda m: " " * len(m[0]), source)


def _plain(text):
    text = re.sub(r"\\(?:emph|textbf|textit|textrm|text|mathrm)\s*{([^{}]*)}", r"\1", text)
    text = text.replace("~", " ").replace(r"\&", "&").replace(r"\%", "%")
    return " ".join(text.replace("{", "").replace("}", "").split())


def _index(source):
    text = _uncomment(source)
    begin = text.find(r"\begin{document}")
    body_end = text.find(r"\end{document}")
    body_end = len(text) if body_end < 0 else body_end
    if begin < 0:
        return [], source
    definitions = {}
    for match in re.finditer(r"\\newtheorem(\*)?\s*", text[:begin]):
        name, pos = _group(text, match.end())
        _, pos = _group(text, pos, "[", "]")
        title, _ = _group(text, pos)
        if name and title:
            definitions[name] = (_plain(title), bool(match[1]))
    headings = {"section": 1, "subsection": 2, "subsubsection": 3}
    equations = {"equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*",
                 "flalign", "flalign*", "alignat", "alignat*", "eqnarray", "eqnarray*"}
    # Math sub-environments that keep the enclosing equation's number for \label.
    transparent = {"split", "aligned", "alignedat", "gathered", "cases", "dcases", "array", "subarray",
                   "matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "smallmatrix"}
    entries, stack, active_sections, inserts = [], [], [], []
    last_statement = None
    prefix = "osverify-location-" + hashlib.sha256(source.encode()).hexdigest()[:16] + "-"
    pattern = r"\\(section|subsection|subsubsection)(\*)?\s*|\\(begin|end|label|bibitem)\s*"
    for match in re.finditer(pattern, text[begin:body_end]):
        start, pos = begin + match.start(), begin + match.end()
        command = match[1] or match[3]
        if command in headings:
            _, pos = _group(text, pos, "[", "]")
            title, pos = _group(text, pos)
            if not title:
                continue
            level = headings[command]
            while active_sections and active_sections[-1]["level"] >= level:
                active_sections.pop()["end"] = start
            entry = {"kind": "Section", "title": _plain(title), "level": level,
                     "start": start, "end": body_end, "labels": []}
            # A starred heading has no number; paragraph numbering varies by class.
            if not match[2] and level <= 3:
                label = prefix + str(len(entries))
                entry["labels"].append(label)
                inserts.append((pos, r"\label{" + label + "}"))
            entries.append(entry)
            active_sections.append(entry)
            last_statement = None
        elif command == "begin":
            name, pos = _group(text, pos)
            title, pos = _group(text, pos, "[", "]")
            if name not in definitions and name not in equations and name != "proof":
                # Untracked environments still matter: a \label inside enumerate,
                # subequations, figure, ... names their counter, not the enclosing one.
                stack.append({"environment": name, "kind": None})
                continue
            kind = definitions[name][0] if name in definitions else "Proof" if name == "proof" else "Equation"
            entry = {"kind": kind, "title": _plain(title), "start": start,
                     "end": body_end, "labels": [], "environment": name}
            if name in definitions:
                if not definitions[name][1]:
                    label = prefix + str(len(entries))
                    entry["labels"].append(label)
                    inserts.append((pos, r"\label{" + label + "}"))
                last_statement = entry
            elif name == "proof":
                explicit = re.findall(r"\\(?:ref|autoref|cref|Cref)\s*{([^}]+)}", title)
                if explicit:
                    entry["proof_of"] = explicit
                elif not title and last_statement is not None:
                    entry["proof_of"] = last_statement["labels"]
            entries.append(entry)
            stack.append(entry)
        elif command == "end":
            name, pos = _group(text, pos)
            for i in range(len(stack) - 1, -1, -1):
                if stack[i]["environment"] == name:
                    closed = stack.pop(i)
                    if closed["kind"] is not None:
                        closed["end"] = pos
                    if name == "proof":
                        last_statement = None
                    break
        elif command == "label":
            label, _ = _group(text, pos)
            depth = len(stack) - 1
            while depth >= 0 and stack[depth]["kind"] is None and stack[depth]["environment"] in transparent:
                depth -= 1
            owner = stack[depth] if label and depth >= 0 else None
            if owner is None or owner["kind"] is None or owner["kind"] == "Proof":
                continue
            if owner["kind"] == "Equation":
                owner["labels"].append(label)
            else:
                # Numbered statements already carry an inserted label; an author's label
                # only lets explicit "Proof of \ref{...}" find its statement. It never
                # supplies a number (a starred statement would print an outer counter).
                owner.setdefault("aliases", []).append(label)
        elif command == "bibitem":
            _, pos = _group(text, pos, "[", "]")
            key, _ = _group(text, pos)
            entries.append({"kind": "Bibliography", "title": key, "start": start,
                            "end": body_end, "labels": []})
    # Flattened review inputs can append .bib source after end{document}.
    for match in re.finditer(r"(?m)^\s*@[A-Za-z]+\s*{\s*([^,\s]+)\s*,", text):
        entries.append({"kind": "Bibliography", "title": match[1], "start": match.start(),
                        "end": len(text), "labels": []})
    bibliographies = sorted((e for e in entries if e["kind"] == "Bibliography"), key=lambda e: e["start"])
    for previous, following in zip(bibliographies, bibliographies[1:]):
        previous["end"] = following["start"]
    for pos, insertion in sorted(inserts, reverse=True):
        source = source[:pos] + insertion + source[pos:]
    return entries, source


def _aux_numbers(aux):
    values = {}
    for match in re.finditer(r"\\newlabel\s*", aux):
        key, pos = _group(aux, match.end())
        payload, _ = _group(aux, pos)
        number, _ = _group(payload, 0)
        number = _plain(number)
        if number and re.fullmatch(r"[A-Za-z0-9.():+−–-]+", number):
            values[key] = number
    return values


def _compile(source, directory, assets_dir=None):
    directory.mkdir(parents=True, exist_ok=True)
    if assets_dir is not None:
        # Copy only explicitly supplied local assets into the isolated build.
        root = Path(assets_dir).resolve()
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in {".tex", ".bib", ".sty", ".cls", ".png", ".jpg", ".jpeg", ".pdf", ".eps"}:
                dest = directory / path.relative_to(root)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
    (directory / "osverify-locations.tex").write_text(source)
    engine = shutil.which("pdflatex")
    if not engine and Path("/Library/TeX/texbin/pdflatex").is_file():
        engine = "/Library/TeX/texbin/pdflatex"
    if not engine:
        return {}, "compiler_unavailable"
    env = {**os.environ, "openin_any": "p", "openout_any": "p", "TEXMFOUTPUT": str(directory)}
    with (directory / "compile.log").open("w") as log:
        process = subprocess.Popen([engine, "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "osverify-locations.tex"],
                                   cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            code = process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            stop_process(process)
            return {}, "compilation_timeout"
        except BaseException:
            stop_process(process)
            raise
    aux = directory / "osverify-locations.aux"
    if code or not aux.is_file():
        return {}, "compilation_failed"
    return _aux_numbers(aux.read_text()), "compiled"


def _pdf_numbers(entries, pdf, directory):
    """Match complete named headings in an explicitly supplied original PDF."""
    binary = shutil.which("pdftotext")
    if not binary:
        return {}, "pdf_reader_unavailable"
    destination = directory / "original-paper.txt"
    try:
        subprocess.run([binary, "-layout", str(Path(pdf).resolve()), str(destination)],
                       check=True, capture_output=True, timeout=45)
    except (OSError, subprocess.SubprocessError):
        return {}, "pdf_extraction_failed"
    text = destination.read_text()
    numbers = {}
    for entry in entries:
        title = entry["title"]
        if not title or "\\" in title or entry["kind"] in {"Proof", "Equation", "Bibliography"}:
            continue
        words = r"\s+".join(re.escape(word) for word in title.split())
        if entry["kind"] == "Section":
            pattern = r"(?m)^\s*([A-Z0-9]+(?:\.[0-9]+)*)\s+" + words + r"(?=\s|$)"
        else:
            pattern = re.escape(entry["kind"]) + r"\s+([A-Z0-9]+(?:\.[0-9]+)*)\s*\(\s*" + words + r"\s*\)"
        matches = set(re.findall(pattern, text))
        if len(matches) == 1:
            number = next(iter(matches))
            for label in entry["labels"]:
                numbers[label] = number
    return numbers, "pdf_headings" if numbers else "pdf_headings_unresolved"


def build_locations(request, feedback, output_dir, *, assets_dir=None, paper_pdf=None):
    """Return an auditable display map; raw source spans stay in released feedback."""
    source = request.get("candidate_solution", "")
    entries, instrumented = _index(source)
    output_dir = Path(output_dir)
    numbers, status = _compile(instrumented, output_dir / "latex", assets_dir) if entries else ({}, "not_latex")
    compilation_status = status
    if status != "compiled" and paper_pdf is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        numbers, status = _pdf_numbers(entries, paper_pdf, output_dir)
    # Span line numbers follow str.splitlines(), which also breaks on \r, \f, \x1c, ...
    line_bounds, position = [], 0
    for line in source.splitlines(keepends=True):
        line_bounds.append((position, position + len(line.splitlines()[0])))
        position += len(line)
    by_label = {label: entry for entry in entries for label in entry["labels"] + entry.get("aliases", [])}

    def describe(entry):
        kind = entry["kind"]
        if kind == "Bibliography":
            return [f'Bibliography, entry “{entry["title"]}”']
        if kind == "Proof":
            targets = [by_label[label] for label in entry.get("proof_of", []) if label in by_label]
            return list(dict.fromkeys("Proof of " + value for target in targets for value in describe(target)))
        values = list(dict.fromkeys(numbers[label] for label in entry["labels"] if label in numbers))
        if values:
            return [f"Equation ({n})" if kind == "Equation" else f"{kind} {n}" for n in values]
        if entry["title"] and kind != "Equation":
            return [f'{kind} “{entry["title"]}”']
        return []

    def references_at(a, b):
        start, end = line_bounds[a - 1][0], line_bounds[b - 1][1]
        overlap = [e for e in entries if e["start"] < end and e["end"] > start]
        refs = []
        sections = [e for e in overlap if e["kind"] == "Section"]
        for point in (start, max(start, end - 1)):
            enclosing = [e for e in sections if e["start"] <= point < e["end"]]
            if enclosing:
                refs.extend(describe(max(enclosing, key=lambda e: e["level"])))
        for entry in overlap:
            if entry["kind"] != "Section":
                refs.extend(describe(entry))
        return list(dict.fromkeys(refs))

    issues = {}
    for item in feedback:
        issue = issues.setdefault(item["issue_id"], {"references": [], "spans": [], "passages": []})
        for span in item["source_spans"]:
            if span in issue["spans"]:
                continue
            issue["spans"].append(span)
            a, b = span["start_line"], span["end_line"]
            document = request.get(span["document"], "")
            lines = document.splitlines()
            if not 1 <= a <= b <= len(lines):
                continue
            refs = references_at(a, b) if span["document"] == "candidate_solution" else []
            issue["references"].extend(r for r in refs if r not in issue["references"])
            # These are source candidates, never an assertion that every span is erroneous.
            issue["passages"].append({
                "source_span": span, "text": "\n".join(lines[a - 1:b]), "references": refs,
                "line_references": {str(n): references_at(n, n) for n in range(a, b + 1)}
                if span["document"] == "candidate_solution" else {},
            })
    result = {"schema_version": "paper-locations-2", "request_id": request.get("request_id"),
              "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "numbering_status": status,
              "issues": issues}
    if paper_pdf is not None:
        result.update(original_pdf_sha256=hashlib.sha256(Path(paper_pdf).read_bytes()).hexdigest(),
                      compilation_status=compilation_status)
    atomic_json(output_dir / "locations.json", result)
    return result


def locations_for_run(run_dir, report, output_dir, *, assets_dir=None, paper_pdf=None):
    path = Path(run_dir) / "request.json"
    if not path.is_file():
        return {"numbering_status": "source_unavailable", "issues": {}}
    request = json.loads(path.read_text())
    if request.get("request_id") != report["request_id"]:
        raise ValueError("Report and paper-location source refer to different requests")
    return build_locations(request, report["feedback"], output_dir, assets_dir=assets_dir, paper_pdf=paper_pdf)


def location_text(locations, issue_id):
    refs = locations.get("issues", {}).get(issue_id, {}).get("references", [])
    return "; ".join(refs) or "Paper reference unavailable; source spans are retained in the audit data."


def selected_passage(locations, issue_id, selection):
    """Validate exact source anchoring and resolve only the selected lines.

    Role, readable transcription and relevance remain subject to the independent
    editorial check. A valid source span alone does not prove that it is an error.
    """
    candidates = locations.get("issues", {}).get(issue_id, {}).get("passages", [])
    for candidate in candidates:
        span = candidate["source_span"]
        if selection.document != span["document"] or not (
            span["start_line"] <= selection.start_line <= selection.end_line <= span["end_line"]
        ):
            continue
        lines = candidate["text"].splitlines()
        excerpt = "\n".join(lines[selection.start_line - span["start_line"]:
                                  selection.end_line - span["start_line"] + 1])
        if selection.role == "issue" and (not selection.quote.strip() or not selection.display_quote.strip()):
            raise ValueError("An issue passage requires an exact source quote and readable quotation")
        if selection.quote and " ".join(selection.quote.split()) not in " ".join(excerpt.split()):
            raise ValueError("Quotation is not verbatim within the selected source lines")
        if selection.display_quote and not selection.quote.strip():
            raise ValueError("A readable quotation requires its verbatim source anchor")
        if not selection.description.strip() or not selection.explanation.strip():
            raise ValueError("Every passage needs a precise description and its relevance")
        refs = list(dict.fromkeys(ref for n in range(selection.start_line, selection.end_line + 1)
                                 for ref in candidate["line_references"].get(str(n), [])))
        return {**selection.model_dump(), "references": refs}
    raise ValueError("Selected passage is outside this issue's released source spans")
