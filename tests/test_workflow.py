"""Behavioral checks of traversal, exact release, deduplication, cancellation and resume."""

import json
import os
import threading
from copy import deepcopy

import pytest

from osverifier.backend import BackendCancelled
from osverifier.demo import feedback_decision
from osverifier.models import ReviewRequest
from osverifier.storage import FrozenRunMismatch, read_json
from osverifier.trajectory import TrajectoryCorrupted
from osverifier.workflow import DecomposingVerifier, WorkflowConfig, file_sha


class FixtureBackend:
    identity = {"kind": "scripted_demo"}

    def __init__(self, proposals=None, reject=(), veto=(), dedup=False, confirm_duplicate=True):
        self.proposals = proposals or {}
        self.reject, self.veto = set(reject), set(veto)
        self.dedup, self.confirm_duplicate = dedup, confirm_duplicate
        self.calls = []
        self.cancelled = threading.Event()
        self.slow_started = threading.Event()
        self.slow = False
        self.bad_partition = False

    def cancel(self):
        self.cancelled.set()

    def generate(self, role, payload, schema, attempt_dir):
        result = self._generate(role, payload, schema, attempt_dir)
        if payload.get("paper_review") and role == "proposer":
            result["evidence_limits"] = []
        if payload.get("paper_images"):
            result["visual_inspections"] = [
                {"image_id": i["image_id"], "status": "inspected", "observation": "Fixture pixels checked"}
                for i in payload["paper_images"]]
        return result

    def _generate(self, role, payload, schema, attempt_dir):
        if self.cancelled.is_set():
            raise BackendCancelled("fixture cancelled")
        self.calls.append((role, deepcopy(payload)))
        if role == "proposer":
            sid = payload["scope"]["scope_id"]
            return {
                "scope_id": sid,
                "search_status": "completed",
                "search_limitations": [],
                "issues": [
                    {
                        "issue_id": f"p{index}",
                        "scope_id": sid,
                        "kind": "invalid_inference",
                        "target": target,
                        "source_spans": [
                            {"document": "candidate_solution", "start_line": 1, "end_line": 1}
                        ],
                        "allegation": target,
                        "evidence": "Fixture evidence",
                        "proposed_feedback": [],
                    }
                    for index, target in enumerate(self.proposals.get(sid, []))
                ],
            }
        if role == "decomposer":
            return {
                "segments": [
                    {
                        "start_line": 1,
                        "end_line": 3,
                        "context_before": "",
                        "context_after": "Later source text.",
                    },
                    {
                        "start_line": 5 if self.bad_partition else 4,
                        "end_line": 6,
                        "context_before": "Earlier source text.",
                        "context_after": "",
                    },
                ]
            }
        if role == "deduplicator":
            earlier = payload["earlier_candidates"]
            incoming = payload["incoming_candidates"]
            return {
                "matches": [
                    {
                        "issue_id": incoming[0]["issue_id"],
                        "duplicate_of": earlier[0]["issue_id"],
                        "rationale": "Fixture duplicate link",
                    }
                ]
                if self.dedup and earlier
                else []
            }
        if role == "duplicate_auditor":
            return {
                "checks": [
                    {**m, "confirmed": self.confirm_duplicate}
                    for m in payload["proposed_matches"]["matches"]
                ]
            }
        if role == "issue_validator":
            issue = payload["proposed_issue"]
            if self.slow and issue["target"] == "slow":
                self.slow_started.set()
                assert self.cancelled.wait(5), "expected early cancellation"
                raise BackendCancelled("slow issue cancelled")
            if self.slow and issue["target"] == "reject":
                assert self.slow_started.wait(5)
            rejecting = issue["target"] in self.reject
            return {
                "issue_id": issue["issue_id"],
                "assessment": "confirmed",
                "kind": "invalid_inference" if rejecting else "omission",
                "impact": "blocking",
                "rejection_supported": rejecting,
                "completion_effect": "none" if rejecting else "missing_required_content",
                "source_spans": issue["source_spans"],
                "rationale": "Fixture rationale",
                "evidence": "Fixture evidence",
                "feedback_candidates": [
                    {
                        "feedback_id": "diagnosis",
                        "kind": "diagnosis",
                        "text": f"Audited fixture: {issue['target']}",
                    }
                ],
            }
        if role == "feedback_auditor":
            issue, review = payload["proposed_issue"], payload["issue_review"]
            approved = issue["target"] not in self.veto
            return {
                "issue_id": issue["issue_id"],
                "issue_review_assessment": "upheld" if approved else "disputed",
                "rationale": "Fixture audit",
                "decisions": [
                    feedback_decision(f["feedback_id"], approved)
                    for f in review["feedback_candidates"]
                ],
            }
        raise AssertionError(role)


def request():
    return ReviewRequest(
        request_id="fixture",
        question="Inspect this fixture.",
        candidate_solution="first\r\nsecond\r\nthird\r\nfourth\r\nfifth\r\nsixth\r\n",
    )


def run(backend, directory, workers=1):
    return DecomposingVerifier(backend, WorkflowConfig(workers=workers, max_attempts=1)).verify(
        request(), directory
    )


def test_root_reject_skips_decomposition_and_terminal_resume_has_no_calls(tmp_path):
    backend = FixtureBackend({"root": ["reject"]}, reject=["reject"])
    report = run(backend, tmp_path)
    assert report.decision == "REJECT" and report.run_status == "completed"
    assert report.termination_reason == "audited_rejection" and not report.coverage_complete
    assert [role for role, _ in backend.calls] == [
        "proposer",
        "issue_validator",
        "feedback_auditor",
    ]
    assert read_json(tmp_path / "diagnostics.json")["decomposition"] == "skipped_rejection"
    fresh = FixtureBackend()
    assert run(fresh, tmp_path) == report
    assert fresh.calls == []


def test_revise_continues_to_all_segments_and_preserves_full_context(tmp_path):
    backend = FixtureBackend({"root": ["gap"], "node-0001": ["another gap"]})
    report = run(backend, tmp_path)
    assert report.decision == "REVISE" and report.coverage_complete
    assert report.termination_reason == "all_nodes_reviewed"
    roles = [role for role, _ in backend.calls]
    assert roles.index("decomposer") > roles.index("feedback_auditor")
    proposals = [p for role, p in backend.calls if role == "proposer"]
    assert [p["scope"]["scope_id"] for p in proposals] == ["root", "node-0001", "node-0002"]
    assert all(p["request"] == request().model_dump(mode="json") for p in proposals)
    assert proposals[1]["focus_text"] == "first\r\nsecond\r\nthird\r\n"
    assert proposals[2]["scope"]["context_before"] == "Earlier source text."
    assert (tmp_path / "nodes/node-0001/focus.md").read_bytes() + (
        tmp_path / "nodes/node-0002/focus.md"
    ).read_bytes() == request().candidate_solution.encode()


def test_child_reject_skips_later_nodes(tmp_path):
    backend = FixtureBackend({"node-0001": ["reject"]}, reject=["reject"])
    report = run(backend, tmp_path)
    assert report.decision == "REJECT"
    nodes = read_json(tmp_path / "diagnostics.json")["nodes"]
    assert [n["status"] for n in nodes] == ["completed", "stopped_rejection", "skipped_rejection"]
    assert not any(
        role == "proposer" and p["scope"]["scope_id"] == "node-0002" for role, p in backend.calls
    )


def test_auditor_veto_does_not_stop_or_authorize_clean_handoff(tmp_path):
    backend = FixtureBackend({"root": ["reject"]}, reject=["reject"], veto=["reject"])
    report = run(backend, tmp_path)
    assert report.decision is None and report.run_status == "incomplete"
    assert report.termination_reason == "review_inconclusive"
    assert not backend.cancelled.is_set()
    assert sum(role == "proposer" for role, _ in backend.calls) == 3


def test_bad_partition_is_not_clean_handoff(tmp_path):
    backend = FixtureBackend()
    backend.bad_partition = True
    report = run(backend, tmp_path)
    assert report.decision is None and report.run_status == "incomplete"
    assert report.termination_reason == "execution_incomplete"
    assert read_json(tmp_path / "diagnostics.json")["decomposition"] == "failed"


class UnlocatableSourceBackend(FixtureBackend):
    def __init__(self, cause):
        super().__init__({"root": ["cited source"]} if cause != "search" else {})
        self.cause = cause

    def _generate(self, role, payload, schema, attempt_dir):
        result = super()._generate(role, payload, schema, attempt_dir)
        if role == "proposer" and self.cause == "search" and payload["scope"]["scope_id"] == "root":
            result.update(search_status="incomplete", search_limitations=["Source not found"])
        if role == "issue_validator" and self.cause == "unresolved":
            result.update(assessment="unresolved", impact="unknown", completion_effect="uncertain",
                          rejection_supported=False, feedback_candidates=[])
        if role == "issue_validator" and self.cause == "failure":
            raise ValueError("fixture contract failure")
        return result


@pytest.mark.parametrize("cause", ["unresolved", "search"])
def test_finished_but_inconclusive_review_is_distinguished_from_unfinished(tmp_path, cause):
    report = run(UnlocatableSourceBackend(cause), tmp_path)
    assert report.decision is None and report.run_status == "incomplete"
    assert report.termination_reason == "review_inconclusive" and not report.coverage_complete
    assert "Resuming replays the same results" in (tmp_path / "report.md").read_text()
    fresh = UnlocatableSourceBackend(cause)
    assert run(fresh, tmp_path) == report
    assert fresh.calls == []


def test_failed_job_remains_execution_incomplete(tmp_path):
    report = run(UnlocatableSourceBackend("failure"), tmp_path)
    assert report.run_status == "incomplete"
    assert report.termination_reason == "execution_incomplete"


@pytest.mark.parametrize("confirmed,expected_reviews", [(True, 1), (False, 2)])
def test_semantic_dedup_requires_independent_confirmation(tmp_path, confirmed, expected_reviews):
    backend = FixtureBackend(
        {"root": ["same"], "node-0001": ["same"]}, dedup=True, confirm_duplicate=confirmed
    )
    report = run(backend, tmp_path)
    assert report.decision == "REVISE" and report.coverage_complete
    assert sum(role == "issue_validator" for role, _ in backend.calls) == expected_reviews
    assert len(report.feedback) == expected_reviews
    aliases = read_json(tmp_path / "aliases.json")
    assert bool(aliases) == confirmed
    assert len(read_json(tmp_path / "candidates.json")) == 2


def test_rejection_cancels_inflight_issue_and_logs_skips(tmp_path):
    backend = FixtureBackend({"root": ["reject", "slow", "queued"]}, reject=["reject"])
    backend.slow = True
    report = run(backend, tmp_path, workers=2)
    assert report.decision == "REJECT" and backend.cancelled.is_set()
    assert not any(
        role == "feedback_auditor" and p["proposed_issue"]["target"] == "slow"
        for role, p in backend.calls
    )
    events = [json.loads(line) for line in (tmp_path / "trajectory.jsonl").read_text().splitlines()]
    assert any(e["event"] == "early_stop" for e in events)
    assert any(e["event"] == "issue_cancelled" for e in events)
    assert list(tmp_path.glob("jobs/*/attempt-*/cancelled.json"))
    assert not list(tmp_path.glob("jobs/*/attempt-*/failure.json"))


def test_all_finished_empty_scopes_are_ready(tmp_path):
    report = run(FixtureBackend(), tmp_path)
    assert report.decision == "READY_FOR_HUMAN" and report.coverage_complete


def test_completion_integrity_checks_original_artifacts(tmp_path):
    run(FixtureBackend(), tmp_path)
    p = tmp_path / "nodes/root/focus.md"
    p.write_text("changed")
    backend = FixtureBackend()
    with pytest.raises(FrozenRunMismatch):
        run(backend, tmp_path)
    assert backend.calls == []


def test_completion_checkpoint_covers_only_core_artifacts(tmp_path):
    # The GUI writes its job record and logs into the run directory and keeps
    # updating them after the verifier exits; Finder may add metadata.
    (tmp_path / "gui-job.json").write_text('{"status": "running"}')
    (tmp_path / "gui-stdout.log").write_text("")
    report = run(FixtureBackend(), tmp_path)
    hashes = read_json(tmp_path / "completion.json")["artifact_hashes"]
    assert "report.json" in hashes and "jobs/root-proposer/result.json" in hashes
    assert not any(name.startswith("gui-") for name in hashes)
    (tmp_path / "gui-job.json").write_text('{"status": "exited", "exit_code": 0}')
    (tmp_path / "gui-stdout.log").write_text(report.model_dump_json())
    (tmp_path / ".DS_Store").write_bytes(b"finder")
    fresh = FixtureBackend()
    assert run(fresh, tmp_path) == report and fresh.calls == []
    (tmp_path / "jobs/root-proposer/result.json").unlink()
    with pytest.raises(FrozenRunMismatch, match="missing"):
        run(FixtureBackend(), tmp_path)


def test_older_completion_checkpoint_with_gui_entries_remains_resumable(tmp_path):
    report = run(FixtureBackend(), tmp_path)
    checkpoint = read_json(tmp_path / "completion.json")
    (tmp_path / "gui-job.json").write_text('{"status": "running"}')
    # Checkpoints written before the core-artifact allowlist also hashed GUI files.
    checkpoint["artifact_hashes"]["gui-job.json"] = file_sha(tmp_path / "gui-job.json")
    (tmp_path / "completion.json").write_text(json.dumps(checkpoint))
    (tmp_path / "gui-job.json").write_text('{"status": "exited"}')
    fresh = FixtureBackend()
    assert run(fresh, tmp_path) == report and fresh.calls == []
    (tmp_path / "report.json").write_text(report.model_dump_json(indent=1))
    with pytest.raises(FrozenRunMismatch, match="report.json"):
        run(FixtureBackend(), tmp_path)


def test_torn_final_trajectory_record_is_discarded_and_recorded(tmp_path):
    backend = FixtureBackend({"root": ["reject"]}, reject=["reject"], veto=["reject"])
    assert run(backend, tmp_path).run_status == "incomplete"
    path = tmp_path / "trajectory.jsonl"
    intact = path.read_bytes()
    count = len(intact.splitlines())
    fragment = '{"sequence": 999, "event": "node_failed", "error": "trunc\u2028'.encode()
    path.write_bytes(intact + fragment)
    assert run(FixtureBackend(veto=["reject"]), tmp_path).run_status == "incomplete"
    events = [json.loads(line) for line in path.read_bytes().split(b"\n")[:-1]]
    assert path.read_bytes().startswith(intact)
    discarded = events[count]
    assert discarded["event"] == "trajectory_tail_discarded"
    assert discarded["sequence"] == count + 1 and discarded["bytes"] == len(fragment)
    assert (tmp_path / discarded["artifact"]).read_bytes() == fragment
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))


def test_trajectory_records_with_unicode_line_separators_reload(tmp_path):
    from osverifier.trajectory import Trajectory

    Trajectory(tmp_path).record("node_failed", error="model text\u2028continues")
    reloaded = Trajectory(tmp_path)
    reloaded.record("run_started")
    assert reloaded.sequence == 2
    reloaded.render()
    assert "continues" in (tmp_path / "trajectory.md").read_text(encoding="utf-8")


def test_interior_trajectory_corruption_is_not_dropped(tmp_path):
    run(FixtureBackend({"root": ["gap"]}), tmp_path)
    (tmp_path / "completion.json").unlink()
    path = tmp_path / "trajectory.jsonl"
    lines = path.read_bytes().split(b"\n")
    lines[1] = lines[1][:10]
    path.write_bytes(b"\n".join(lines))
    with pytest.raises(TrajectoryCorrupted, match="record 2"):
        run(FixtureBackend({"root": ["gap"]}), tmp_path)


CORE_TEXT_IO = r"""
import sys, warnings
from pathlib import Path
warnings.filterwarnings("error", category=EncodingWarning, module=r"osverifier(\.|$)")
sys.path.insert(0, sys.argv[1])
from test_workflow import FixtureBackend, request
from osverifier.cli import main
from osverifier.connection_config import ConnectionConfig
from osverifier.quota import QuotaMonitor
from osverifier.workflow import DecomposingVerifier
root = Path(sys.argv[2])
math = request().model_copy(update={"question": "Show x ≤ y for all é."})
report = DecomposingVerifier(FixtureBackend({"root": ["gap ∑"]})).verify(math, root / "run")
assert report.decision == "REVISE", report
assert "∑" in (root / "run/report.md").read_bytes().decode("utf-8")
assert main(["export", "--run-dir", str(root / "run"), "--output", str(root / "r.tex")]) == 0
assert main(["demo", "--run-dir", str(root / "demo")]) in (0, 2)
(root / "q.md").write_bytes("x ≤ y".encode())
assert main(["verify", "--question", str(root / "q.md"), "--solution", str(root / "q.md"),
             "--model", "m", "--codex-bin", str(root / "absent"), "--run-dir", str(root / "v")]) == 1
(root / "home").mkdir()
(root / "home/config.toml").write_bytes('model_provider = "prøxy"\n'.encode())
assert ConnectionConfig(root / "home").values["model_provider"] == "prøxy"
(root / "attempt").mkdir()
(root / "attempt/stderr.log").write_bytes("ERROR: usage limit reached ≤".encode())
QuotaMonitor(root / "attempt").failed_stderr()
"""


def test_core_text_io_uses_explicit_utf8(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    tests = Path(__file__).parent
    # A non-UTF-8 default encoding (Latin-1, or ASCII where that locale is missing). The
    # check runs from a file: source files are UTF-8, but a -c argument is decoded with
    # the locale and cannot carry the non-ASCII fixtures.
    env = {**os.environ, "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "en_US.ISO8859-1"}
    script = tmp_path / "core_text_io.py"
    script.write_text(CORE_TEXT_IO, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-X", "warn_default_encoding", str(script), str(tests), str(tmp_path)],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "EncodingWarning" not in result.stderr and "UnicodeEncodeError" not in result.stderr


def test_partial_empty_workflow_passes_after_all_nodes(tmp_path):
    result = DecomposingVerifier(FixtureBackend()).verify(
        request().model_copy(update={"mode": "partial"}), tmp_path
    )
    assert result.decision == "PASS" and result.coverage_complete


def test_incomplete_resume_detects_tampered_job_payload(tmp_path):
    backend = FixtureBackend({"root": ["reject"]}, reject=["reject"], veto=["reject"])
    assert run(backend, tmp_path).run_status == "incomplete"
    path = tmp_path / "jobs/root-proposer/input.json"
    data = read_json(path)
    data["payload"]["request"]["question"] = "changed input"
    path.write_text(json.dumps(data))
    fresh = FixtureBackend()
    with pytest.raises(FrozenRunMismatch):
        run(fresh, tmp_path)
    assert fresh.calls == []
