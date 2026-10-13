"""Paper-only operational status; the solution handoff gate remains unchanged."""
from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import Field

from osverifier.models import StrictModel
from osverifier.storage import FrozenRunMismatch, read_json


class PaperProgress(StrictModel):
    execution: Literal["running", "completed", "failed", "interrupted"]
    search: Literal["pending", "completed", "partial", "unknown"]
    scopes_searched: int = 0
    scopes_total: int = 0
    evidence: Literal["not_assessed", "no_reported_limits", "limited"] = "not_assessed"
    evidence_limits: list[str] = Field(default_factory=list)
    search_limits: list[str] = Field(default_factory=list)
    images: Literal["not_supplied", "pending", "completed", "partial", "limited"] = "not_supplied"
    image_count: int = 0
    image_review_calls: int = 0
    audit: Literal["pending", "completed", "withheld", "failed", "partial"] = "pending"
    released_findings: int = 0
    withheld_findings: int = 0
    failed_findings: int = 0
    unresolved_findings: int = 0
    unreviewed_findings: int = 0


def observations(directory: Path | None) -> list[dict]:
    if directory is None:
        return []
    records = []
    for cache in sorted((directory / "jobs").glob("*/result.json")):
        for relative, expected in read_json(cache).get("auxiliary_artifacts", {}).items():
            path = (cache.parent / relative).resolve()
            if not path.is_relative_to(cache.parent.resolve()) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise FrozenRunMismatch("Paper observation artifact changed")
            if path.name == "paper-observations.json":
                records.append(read_json(path))
    return records


def paper_progress(core: dict, diagnostics: dict, *, directory=None, images=()) -> PaperProgress:
    findings = Counter(f["status"] for f in diagnostics.get("findings", []))
    nodes = diagnostics.get("nodes", [])
    running = core["run_status"] == "running"
    errors = bool(diagnostics.get("execution_errors")) or bool(findings["failed"])
    errors |= diagnostics.get("proposer_status") == "failed" or diagnostics.get("decomposition") == "failed"
    pending = bool(findings["pending"]) or diagnostics.get("proposer_status") == "pending" or any(n["status"] in ("pending", "running", "queued") for n in nodes)
    errors |= any(n["status"] == "failed" for n in nodes)
    execution = "running" if running else "failed" if errors else "interrupted" if pending else "completed"
    early = core.get("termination_reason") == "audited_rejection"
    if early and not running and not errors:
        execution = "completed"
    if not running and not diagnostics and core["run_status"] != "completed":
        execution = "interrupted"  # Do not infer successful execution from an old ambiguous label.
    if nodes:
        searched = sum(n.get("search_status") == "completed" for n in nodes)
        coverage = diagnostics.get("decomposition") == "completed" and searched == len(nodes)
        search = "completed" if coverage else "pending" if running else "partial"
        total = len(nodes)
    else:
        status = diagnostics.get("proposer_status")
        search = "completed" if status == "completed" or core.get("coverage_complete") else "partial" if status == "incomplete" else "pending" if running else "unknown"
        searched, total = int(search == "completed"), 1
    records = observations(directory)
    proposals = [r for r in records if r["role"] == "proposer"]
    limits = sorted({limit for r in proposals for limit in r["evidence_limits"]})
    # Preserve model-reported search limitations as limitations, never findings.
    search_limits = []
    if directory:
        from .references import public_references, reference_limits
        # Title only: model-written notes stay in internal reference observations.
        limits.extend(f"Unresolved reference: {c['title']}" for c in public_references(reference_limits(directory)))
        for path in (Path(directory) / 'jobs').glob('*/result.json'):
            value = read_json(path).get('output', {})
            if 'search_limitations' in value:
                search_limits.extend(value['search_limitations'])
        limits = sorted(set(limits))
    visual = [r for r in records if r["images"]]
    unreadable = any(c["status"] == "unreadable" for r in visual for c in r["visual_inspections"])
    if unreadable:
        limits = sorted(set(limits) | {"unreadable_image"})
    return PaperProgress(
        execution=execution, search=search, scopes_searched=searched, scopes_total=total,
        evidence="limited" if limits else "no_reported_limits" if proposals else "not_assessed",
        evidence_limits=limits, search_limits=sorted(set(search_limits)),
        images="not_supplied" if not images else "limited" if unreadable else "completed" if visual and execution == "completed" else "partial" if visual else "pending",
        image_count=len(images), image_review_calls=len(visual),
        audit="failed" if findings["failed"] else "partial" if early and findings["pending"] else "pending" if running or pending or diagnostics.get("proposer_status") == "failed" else "withheld" if findings["audit_blocked"] else "completed",
        released_findings=len({f["issue_id"] for f in core.get("feedback", [])}),
        withheld_findings=findings["audit_blocked"], failed_findings=findings["failed"],
        unresolved_findings=findings["unresolved"],
        unreviewed_findings=findings["pending"],
    )


def progress_lines(progress: PaperProgress) -> list[str]:
    p = progress
    # Unchecked references are listed in their own section (title and URL only);
    # older reports may still carry their model-written notes in this list.
    access = [limit for limit in p.evidence_limits if not limit.startswith("Unresolved reference: ")]
    return [
        f"Execution: {p.execution}.",
        f"Search coverage: {p.search} ({p.scopes_searched}/{p.scopes_total} created scopes searched).",
        f"Original search limitations: {'; '.join(p.search_limits) or 'none recorded'}.",
        f"Evidence availability: {p.evidence}. Reported access limits: {', '.join(access) or 'none recorded'}"
        + ("; unresolved external references are listed separately." if len(access) < len(p.evidence_limits) else "."),
        f"Visual inspection: {p.images} ({p.image_count} supplied images; {p.image_review_calls} calls with recorded inspections).",
        f"Feedback audit: {p.audit}. Released findings: {p.released_findings}; withheld: {p.withheld_findings}; failed: {p.failed_findings}; not reviewed: {p.unreviewed_findings}; unresolved released findings: {p.unresolved_findings}.",
        "Execution completion does not certify correctness, complete evidence availability, or empirical reproduction. Visual inspection records are model observations, not independent verification.",
    ]
