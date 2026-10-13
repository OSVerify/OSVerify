"""Conservative, deterministic image navigation; never a limit on source access."""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from pathlib import Path


def _words(text):
    return re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", text).lower())


def _shingles(text):
    words = _words(text)
    return {tuple(words[n:n + 5]) for n in range(max(0, len(words) - 4))}


def _references(text):
    return {(kind.lower().rstrip("s").replace("fig.", "figure"), number)
            for kind, number in re.findall(r"\b(Fig\.|Figures?|Tables?)\s*(\d+)\b", text, re.I)}


def select_images(images, role, payload):
    """Seed each call with source-matched pages and explicit figure/table references.

    A whole-paper proposer sees every page. Other roles retain the complete image
    catalogue and full original text; the model must expand this navigation set
    whenever the claim or its qualifications require it. Scans without usable text
    and standalone images are always retained, and a failed match falls back to all.
    """
    if not images:
        return [], "no_images"
    scope = payload.get("scope", {})
    if role == "proposer" and scope.get("kind") != "segment" and scope.get("scope_id", "root") in ("root", "whole_solution"):
        return list(images), "whole_paper_visual_sweep"
    source = payload.get("request", {}).get("candidate_solution", payload.get("paper", ""))
    lines = source.splitlines()
    objects = [payload.get("proposed_issue", {}), payload.get("issue_review", {})]
    if role == "proposer":
        objects.append(scope)
    objects.extend(payload.get("feedback", []))
    excerpts = []
    for obj in objects:
        for span in obj.get("source_spans", []):
            if span.get("document") == "candidate_solution":
                start, end = span.get("start_line", 1), span.get("end_line", 0)
                if 1 <= start <= end <= len(lines):
                    excerpts.append("\n".join(lines[max(0, start - 3):min(len(lines), end + 2)]))
    if payload.get("focus_text") and role == "proposer":
        excerpts.append(payload["focus_text"])
    pages = {}
    unindexed = set()
    for item in images:
        navigation = item.get("navigation")
        text = Path(navigation["path"]).read_text() if navigation else ""
        if len(_words(text)) >= 5:
            pages[item["image_id"]] = text
        else:
            unindexed.add(item["image_id"])
    shingles = {key: _shingles(text) for key, text in pages.items()}
    frequency = Counter(s for values in shingles.values() for s in values)
    selected = set()
    for excerpt in excerpts:
        query = {s for s in _shingles(excerpt) if frequency[s] <= 2}
        for key, values in shingles.items():
            if len(query & values) >= 2:
                selected.add(key)
    references = _references("\n".join(excerpts) + "\n" + json.dumps(objects, ensure_ascii=False))
    for key, text in pages.items():
        if references & _references(text):
            selected.add(key)
    if not selected:
        return list(images), "no_reliable_page_match_fallback"
    selected |= unindexed
    return [i for i in images if i["image_id"] in selected], "source_spans_and_figure_table_references"
