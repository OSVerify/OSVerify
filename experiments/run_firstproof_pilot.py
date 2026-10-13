"""Small, frozen FirstProof pilot. Labels are used only for selection/reporting."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from osverifier.backend import CodexBackend
from osverifier.models import ReviewRequest
from osverifier.pipeline import RunConfig, Verifier
from osverifier.storage import (
    atomic_json,
    atomic_text,
    package_fingerprint,
    read_json,
    run_lock,
)

DATASET = Path(os.environ.get("FIRSTPROOF_DATASET", "data/firstproof.jsonl"))
BINARY = os.environ.get("CODEX_BIN", "codex")
SEED = 20260912


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(root):
    manifest = root / "cohort.json"
    if manifest.exists():
        return read_json(manifest)
    rows = [json.loads(line) for line in DATASET.read_text().splitlines()]
    eligible = [
        row
        for row in rows
        if row["label"] == "true"
        and row["answer_model"] == "human"
        and not row.get("needs_data_review")
        and any(
            other["problem_id"] == row["problem_id"] and other["label"] == "false" for other in rows
        )
    ]
    rng = random.Random(SEED)
    samples = []
    for correct in rng.sample(sorted(eligible, key=lambda row: row["sample_id"]), 3):
        incorrect = rng.choice(
            sorted(
                [
                    row
                    for row in rows
                    if row["problem_id"] == correct["problem_id"]
                    and row["label"] == "false"
                    and not row.get("needs_data_review")
                ],
                key=lambda row: row["sample_id"],
            )
        )
        assert correct["question"] == incorrect["question"]
        for row in (correct, incorrect):
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
                    "ground_truth": row["ground_truth"],
                    "answer_model": row["answer_model"],
                    "source_id": row["source_id"],
                    "row_idx": row["row_idx"],
                    "solution_chars": len(row["answer"]),
                    "request_path": str(target),
                    "request_sha256": sha(target),
                }
            )
    cohort = {
        "created_at": utc_now(),
        "dataset": str(DATASET),
        "dataset_sha256": sha(DATASET),
        "selection_seed": SEED,
        "selection_rule": "Uniform sample of 3 eligible human-labeled-correct references, then one random labeled-incorrect answer for each identical question. Sorted sample IDs before seeded selection. No previous verifier outcomes used.",
        "label_provenance": "Existing FirstProof dataset labels; not newly established human adjudications.",
        "excluded_data_review_ids": [
            row["sample_id"] for row in rows if row.get("needs_data_review")
        ],
        "source_filtered_rows": 96,
        "eligible_after_data_review_exclusion": 94,
        "source_exclusions": [
            "row 51: inconclusive",
            "row 67: inconclusive",
            "row 79: missing label/answer",
        ],
        "config": {
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "web_search": "live",
            "max_issues": None,
            "workers_per_sample": 8,
            "max_active_codex_calls_total": 8,
            "samples_in_parallel": 6,
            "max_attempts": 2,
            "timeout_seconds": 900,
        },
        "call_accounting": "6 + 2 * total proposed issues without retries; each stage allows up to 2 attempts per invocation. No hard token cap.",
        "blinding": "Only ReviewRequest goes to the verifier. No labels, answer-model metadata, paired solution, or old proposer findings in role payloads. Live search remains enabled and may find public solutions.",
        "package_sha256": package_fingerprint(),
        "driver_sha256": sha(Path(__file__)),
        "samples": samples,
    }
    atomic_json(manifest, cohort)
    return cohort


class LimitedBackend:
    def __init__(self):
        self.backend = CodexBackend(
            model="gpt-5.6-sol",
            codex_bin=BINARY,
            reasoning_effort="xhigh",
            web_search="live",
            timeout_seconds=900,
        )
        self.slots = threading.BoundedSemaphore(8)

    @property
    def identity(self):
        return {**self.backend.identity, "pilot_global_call_concurrency": 8}

    def generate(self, role, payload, schema, attempt_dir):
        with self.slots:
            started = time.monotonic()
            try:
                return self.backend.generate(role, payload, schema, attempt_dir)
            finally:
                atomic_json(
                    attempt_dir / "timing.json",
                    {
                        "role": role,
                        "elapsed_seconds": time.monotonic() - started,
                        "finished_at": utc_now(),
                    },
                )
                print(
                    json.dumps(
                        {
                            "sample": payload["request"]["request_id"],
                            "stage": role,
                            "attempt": str(attempt_dir),
                            "elapsed_seconds": round(time.monotonic() - started, 1),
                        }
                    ),
                    flush=True,
                )

    def cancel(self):
        self.backend.cancel()


def snapshot(root, cohort):
    records = []
    for sample in cohort["samples"]:
        sid = sample["sample_id"]
        directory = root / "samples" / sid
        report = (
            read_json(directory / "report.json") if (directory / "report.json").exists() else {}
        )
        diagnostics = (
            read_json(directory / "diagnostics.json")
            if (directory / "diagnostics.json").exists()
            else {}
        )
        usage = Counter()
        for path in directory.glob("jobs/*/attempt-*/events-summary.json"):
            for event in read_json(path)["usage"]:
                usage.update(
                    {key: value for key, value in event.items() if isinstance(value, (int, float))}
                )
        attempts = list(directory.glob("jobs/*/attempt-*/command.json"))
        failures = list(directory.glob("jobs/*/attempt-*/failure.json"))
        records.append(
            {
                **sample,
                "decision": report.get("decision"),
                "run_status": report.get("run_status", "pending"),
                "proposer_status": diagnostics.get("proposer_status", "pending"),
                "issues_proposed": diagnostics.get("issues_proposed", 0),
                "issues_processed": diagnostics.get("issues_processed", 0),
                "feedback_count": len(report.get("feedback", [])),
                "calls_started": len(attempts),
                "failed_attempts": len(failures),
                "reported_usage": dict(usage),
                "report_path": str(directory / "report.md"),
            }
        )
    complete = sum(row["run_status"] == "completed" for row in records)
    decisions = {
        label: dict(
            Counter(row["decision"] or "NO_DECISION" for row in records if row["label"] == label)
        )
        for label in ("true", "false")
    }
    result = {
        "updated_at": utc_now(),
        "review_complete": complete == len(records),
        "completed_samples": complete,
        "total_samples": len(records),
        "decisions_by_dataset_label": decisions,
        "samples": records,
    }
    atomic_json(root / "summary.json", result)
    lines = [
        "# FirstProof pilot: gpt-5.6-sol xhigh",
        "",
        f"Completed reviews: {complete}/{len(records)}. Labels are from the existing dataset.",
        "",
        "Full-solution mode; no decomposer; live search; no issue-count cap; at most 8 active Codex calls across this pilot.",
        "",
        "| Sample | Problem | Dataset label | Decision | Run | Issues processed/proposed | Feedback | Calls |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in records:
        lines.append(
            f"| [{row['sample_id']}]({row['report_path']}) | {row['problem_id']} | {row['label']} | {row['decision'] or 'Pending'} | {row['run_status']} | {row['issues_processed']}/{row['issues_proposed']} | {row['feedback_count']} | {row['calls_started']} |"
        )
    lines.extend(
        [
            "",
            "This small pilot checks behavior; it does not establish population FPR/TPR. REVISE on a dataset-incorrect answer is a repair route, not a rejection. Unfinished reviews are not clean human handoffs.",
            "",
            f"Frozen selection, labels, exclusions, hashes, and settings: [cohort.json]({root / 'cohort.json'}).",
            "",
        ]
    )
    for row in records:
        report_path = root / "samples" / row["sample_id"] / "report.json"
        report = read_json(report_path) if report_path.exists() else {}
        if report.get("feedback"):
            lines.extend([f"## {row['sample_id']}: audited feedback", ""])
            for item in report["feedback"]:
                lines.extend(
                    [
                        f"**{item['issue_id']} / {item['feedback_id']} ({item['kind']})**",
                        "",
                        item["text"],
                        "",
                    ]
                )
    atomic_text(root / "report.md", "\n".join(lines))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--status-only", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    cohort = prepare(root)
    if args.prepare_only or args.status_only:
        print(json.dumps(snapshot(root, cohort), indent=2))
        return
    assert cohort["package_sha256"] == package_fingerprint(), (
        "Package changed; use a new pilot root"
    )
    assert cohort["driver_sha256"] == sha(Path(__file__)), "Driver changed; use a new pilot root"
    for row in cohort["samples"]:
        assert row["request_sha256"] == sha(Path(row["request_path"]))
    with run_lock(root):
        backend = LimitedBackend()
        atomic_json(root / "backend.json", backend.identity)
        executor = ThreadPoolExecutor(max_workers=6)
        futures = {}
        try:
            for row in cohort["samples"]:
                request = ReviewRequest.model_validate(read_json(Path(row["request_path"])))
                futures[
                    executor.submit(
                        Verifier(backend, RunConfig()).verify,
                        request,
                        root / "samples" / row["sample_id"],
                    )
                ] = row["sample_id"]
            for future in as_completed(futures):
                sid = futures[future]
                try:
                    report = future.result()
                    print(
                        json.dumps(
                            {
                                "sample_finished": sid,
                                "decision": report.decision,
                                "run_status": report.run_status,
                            }
                        ),
                        flush=True,
                    )
                except Exception as exc:
                    atomic_json(
                        root / f"{sid}-driver-error.json",
                        {"error": str(exc), "type": type(exc).__name__},
                    )
                    print(json.dumps({"sample_failed": sid, "error": str(exc)}), flush=True)
                snapshot(root, cohort)
        except BaseException:
            backend.cancel()
            for future in futures:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            snapshot(root, cohort)


if __name__ == "__main__":
    main()
