"""Frozen FirstProof cohort with sample-local rejection stops and shared concurrency."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import threading
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from osverifier import __version__
from osverifier.backend import BackendCancelled, CodexBackend
from osverifier.models import ReviewRequest
from osverifier.storage import (
    atomic_json,
    atomic_text,
    package_fingerprint,
    read_json,
    run_lock,
)
from osverifier.workflow import DecomposingVerifier, WorkflowConfig

DATASET = Path(os.environ.get("FIRSTPROOF_DATASET", "data/firstproof.jsonl"))
BINARY = os.environ.get("CODEX_BIN", "codex")
SEED = 20260914


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def prepare(root):
    path = root / "cohort.json"
    if path.exists():
        cohort = read_json(path)
        assert cohort["dataset_sha256"] == sha(DATASET), "dataset changed"
        assert cohort["driver_sha256"] == sha(Path(__file__)), "driver changed"
        assert cohort["package_sha256"] == package_fingerprint(), "package or skills changed"
        for sample in cohort["samples"]:
            assert sha(Path(sample["request_path"])) == sample["request_sha256"], "input changed"
        return cohort
    rows = list(map(json.loads, DATASET.read_text().splitlines()))

    def eligible(row):
        return not row.get("needs_data_review") and not re.search(
            r"!\[|\\includegraphics|<img", row["question"] + "\n" + row["answer"]
        )

    pool = [row for row in rows if eligible(row)]
    refs = sorted(
        [
            row
            for row in pool
            if row["label"] == "true"
            and row["answer_model"] == "human"
            and any(
                other["label"] == "false" and other["question"] == row["question"] for other in pool
            )
        ],
        key=lambda row: row["sample_id"],
    )
    rng, samples = random.Random(SEED), []
    for reference in rng.sample(refs, 3):
        wrong = rng.choice(
            sorted(
                [
                    row
                    for row in pool
                    if row["label"] == "false" and row["question"] == reference["question"]
                ],
                key=lambda row: row["sample_id"],
            )
        )
        for row in (reference, wrong):
            request = ReviewRequest(
                request_id=row["sample_id"],
                question=row["question"],
                candidate_solution=row["answer"],
            )
            target = root / "inputs" / f"{row['sample_id']}.json"
            atomic_json(target, request.model_dump(mode="json"))
            samples.append(
                {
                    "sample_id": row["sample_id"],
                    "problem_id": row["problem_id"],
                    "label": row["label"],
                    "source_id": row["source_id"],
                    "answer_model": row["answer_model"],
                    "solution_chars": len(row["answer"]),
                    "solution_lines": len(row["answer"].splitlines()),
                    "request_path": str(target),
                    "request_sha256": sha(target),
                }
            )
    cohort = {
        "created_at": now(),
        "dataset": str(DATASET),
        "dataset_sha256": sha(DATASET),
        "source_rows": len(rows),
        "eligible_rows": len(pool),
        "reference_pool_ids": [r["sample_id"] for r in refs],
        "excluded_ids": [r["sample_id"] for r in rows if not eligible(r)],
        "source_exclusions": [
            "row 51 inconclusive",
            "row 67 inconclusive",
            "row 79 missing label/answer",
        ],
        "selection_seed": SEED,
        "selection_rule": "Uniform sample of three sorted eligible human-labeled-correct references, followed by one seeded random labeled-incorrect solution to each identical question. Exclude needs_data_review and image-reference inputs. No verifier outcome used for selection.",
        "label_provenance": "Existing dataset labels, not fresh human adjudications.",
        "blinding": "Only raw ReviewRequest goes to the workflow. No label, source model, paired solution, old output, or desired decision is in model payloads. Original source content is retained; live search may find public solutions.",
        "config": {
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "web_search": "live",
            "decomposition_layers": 1,
            "node_order": "root then segments in source order",
            "max_issues": None,
            "workers_per_sample": 8,
            "global_codex_concurrency": 8,
            "parallel_samples": 6,
            "early_stop_on_audited_reject": True,
            "max_attempts": 2,
            "timeout_seconds": 900,
        },
        "call_estimate": "Approximately 200–400 role calls without early stops, depending on segment/issue counts. Not a quota or hard cap. Exact counts, cancellations and early stops are recorded.",
        "decision_policy": "policy 2.2 retains omission/local-repair revision rules; dataset labels and desired performance do not affect judgment.",
        "package_version": __version__,
        "package_sha256": package_fingerprint(),
        "driver_sha256": sha(Path(__file__)),
        "samples": samples,
    }
    atomic_json(path, cohort)
    return cohort


class LimitedBackend:
    def __init__(self, slots):
        self.backend = CodexBackend(
            model="gpt-5.6-sol", codex_bin=BINARY, reasoning_effort="xhigh", web_search="live"
        )
        self.slots = slots
        self.cancelled = threading.Event()

    @property
    def identity(self):
        return {**self.backend.identity, "pilot_global_call_concurrency": 8}

    def generate(self, role, payload, schema, attempt_dir):
        while not self.slots.acquire(timeout=0.1):
            if self.cancelled.is_set():
                raise BackendCancelled("sample cancelled while waiting for global slot")
        try:
            if self.cancelled.is_set():
                raise BackendCancelled("sample cancelled before backend dispatch")
            return self.backend.generate(role, payload, schema, attempt_dir)
        finally:
            self.slots.release()

    def cancel(self):
        self.cancelled.set()
        self.backend.cancel()


def snapshot(root, cohort, returned=None):
    rows = []
    for sample in cohort["samples"]:
        directory = root / "samples" / sample["sample_id"]
        report = (
            read_json(directory / "report.json") if (directory / "report.json").exists() else {}
        )
        diagnostic = (
            read_json(directory / "diagnostics.json")
            if (directory / "diagnostics.json").exists()
            else {}
        )
        usage = Counter()
        for path in directory.glob("jobs/*/attempt-*/events-summary.json"):
            for record in read_json(path)["usage"]:
                usage.update(record)
        nodes = diagnostic.get("nodes", [])
        rows.append(
            {
                **sample,
                "decision": report.get("decision"),
                "run_status": report.get("run_status", "pending"),
                "termination_reason": report.get("termination_reason"),
                "coverage_complete": report.get("coverage_complete", False),
                "decomposition": diagnostic.get("decomposition", "pending"),
                "nodes_created": len(nodes),
                "nodes_completed": sum(n["status"] == "completed" for n in nodes),
                "nodes_skipped": sum(n["status"] == "skipped_rejection" for n in nodes),
                "candidate_count": diagnostic.get("candidate_count", 0),
                "canonical_issue_count": diagnostic.get("canonical_issue_count", 0),
                "issues_processed": diagnostic.get("issues_processed", 0),
                "alias_count": len(diagnostic.get("aliases", {})),
                "calls_started": len(list(directory.glob("jobs/*/attempt-*/process-start.json"))),
                "failed_attempts": len(list(directory.glob("jobs/*/attempt-*/failure.json"))),
                "cancelled_attempts": len(list(directory.glob("jobs/*/attempt-*/cancelled.json"))),
                "reported_usage": dict(usage),
                "stop_basis": diagnostic.get("stop_basis"),
                "report_path": str(directory / "report.md"),
                "trajectory_path": str(directory / "trajectory.md"),
            }
        )
    complete = sum(row["run_status"] == "completed" for row in rows)
    results = {
        "updated_at": now(),
        "completed_samples": complete,
        "total_samples": len(rows),
        "returned_samples": returned or [],
        "decisions_by_dataset_label": {
            label: dict(
                Counter(r["decision"] or "NO_DECISION" for r in rows if r["label"] == label)
            )
            for label in ("true", "false")
        },
        "samples": rows,
    }
    atomic_json(root / "summary.json", results)
    lines = [
        "# FirstProof root-first decomposed workflow",
        "",
        f"Completed workflows: {complete}/{len(rows)}.",
        "",
        "gpt-5.6-sol / xhigh / live search. Root first; one model-selected text partition; sequential nodes; at most 8 active Codex calls across samples. Rejection stops are audited and have partial coverage.",
        "",
        "| Sample | Source label | Decision | Run | Termination | Nodes completed/created | Candidates/unique | Aliases | Calls | Trajectory |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| [{row['sample_id']}]({row['report_path']}) | {row['label']} | {row['decision'] or 'Pending'} | {row['run_status']} | {row['termination_reason'] or '—'} | {row['nodes_completed']}/{row['nodes_created']} | {row['candidate_count']}/{row['canonical_issue_count']} | {row['alias_count']} | {row['calls_started']} | [View]({row['trajectory_path']}) |"
        )
    lines.extend(
        [
            "",
            "Labels are used only for this table. REVISE on an incorrect sample is not a rejection. Cancelled/skipped work is not completed coverage. This pilot does not establish population error rates.",
            "",
            "[Frozen cohort and configuration](cohort.json) · [Live machine-readable summary](summary.json)",
            "",
        ]
    )
    atomic_text(root / "report.md", "\n".join(lines))
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--status-only", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    cohort = prepare(root)
    if args.prepare_only or args.status_only:
        print(json.dumps(snapshot(root, cohort), ensure_ascii=False))
        return
    with run_lock(root):
        slots = threading.BoundedSemaphore(8)
        backends = {s["sample_id"]: LimitedBackend(slots) for s in cohort["samples"]}
        atomic_json(root / "backends.json", {sid: b.identity for sid, b in backends.items()})
        returned = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            pending = {}
            for sample in cohort["samples"]:
                sid = sample["sample_id"]
                request = ReviewRequest.model_validate(read_json(Path(sample["request_path"])))
                pending[
                    executor.submit(
                        DecomposingVerifier(backends[sid], WorkflowConfig()).verify,
                        request,
                        root / "samples" / sid,
                    )
                ] = sid
                print(json.dumps({"sample_started": sid}), flush=True)
            try:
                while pending:
                    done, _ = wait(pending, timeout=15, return_when=FIRST_COMPLETED)
                    for future in done:
                        sid = pending.pop(future)
                        try:
                            report = future.result()
                            value = {
                                "sample_finished": sid,
                                "decision": report.decision,
                                "run_status": report.run_status,
                                "termination_reason": report.termination_reason,
                            }
                        except Exception as exc:
                            value = {
                                "sample_failed": sid,
                                "error": str(exc),
                                "type": type(exc).__name__,
                            }
                            atomic_json(root / f"{sid}-driver-failure.json", value)
                        returned.append(sid)
                        print(json.dumps(value), flush=True)
                    snapshot(root, cohort, returned)
            except BaseException:
                for backend in backends.values():
                    backend.cancel()
                for future in pending:
                    future.cancel()
                snapshot(root, cohort, returned)
                raise
        snapshot(root, cohort, returned)


if __name__ == "__main__":
    main()
