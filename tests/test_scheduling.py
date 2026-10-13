"""Real overlapping fixture calls, deterministic priority, cancellation and resume."""

import threading
from collections import Counter

import pytest
from test_workflow import FixtureBackend, request

from osverifier.backend import BackendCancelled
from osverifier.quota import QuotaExhausted
from osverifier.storage import read_json
from osverifier.workflow import DecomposingVerifier, WorkflowConfig


def verify(backend, directory, workers=2, early_stop=True):
    return DecomposingVerifier(
        backend, WorkflowConfig(workers=workers, max_attempts=1, early_stop_on_reject=early_stop)
    ).verify(request(), directory)


def test_root_decomposition_and_local_validation_overlap(tmp_path):
    """A local issue reaches its audit before the root proposer returns."""

    class Backend(FixtureBackend):
        def __init__(self):
            super().__init__({"node-0001": ["local"]})
            self.root_started = threading.Event()
            self.local_audited = threading.Event()
            self.lock = threading.Lock()
            self.active = self.peak = 0
            self.roles = Counter()

        def generate(self, role, payload, schema, attempt_dir):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.roles[role] += 1
            try:
                if role == "proposer" and payload["scope"]["scope_id"] == "root":
                    self.root_started.set()
                    assert self.local_audited.wait(5), "local pipeline was blocked behind root"
                elif role == "decomposer":
                    assert self.root_started.wait(5), "root and decomposition must overlap"
                result = super().generate(role, payload, schema, attempt_dir)
                if role == "feedback_auditor":
                    self.local_audited.set()
                return result
            finally:
                with self.lock:
                    self.active -= 1

    backend = Backend()
    result = verify(backend, tmp_path)
    assert result.coverage_complete and result.decision == "REVISE"
    assert backend.peak == 2
    assert backend.roles == {
        "proposer": 3,
        "decomposer": 1,
        "issue_validator": 1,
        "feedback_auditor": 1,
    }


def test_feedback_then_validation_take_next_slot_before_queued_verify(tmp_path):
    backend = FixtureBackend({"node-0001": ["first", "second"]})
    report = verify(backend, tmp_path, workers=1)
    assert report.coverage_complete
    calls = [
        (role, p.get("proposed_issue", {}).get("target", p.get("scope", {}).get("scope_id")))
        for role, p in backend.calls
    ]
    assert calls == [
        ("proposer", "root"),
        ("decomposer", "root"),
        ("proposer", "node-0001"),
        ("deduplicator", None),
        ("issue_validator", "first"),
        ("feedback_auditor", "first"),
        ("issue_validator", "second"),
        ("feedback_auditor", "second"),
        ("proposer", "node-0002"),
    ]


def test_sibling_proposers_overlap_and_respect_global_cap(tmp_path):
    class Backend(FixtureBackend):
        def __init__(self):
            super().__init__({"node-0001": ["a", "b"], "node-0002": ["c", "d"]})
            self.siblings = threading.Barrier(2, timeout=5)
            self.lock = threading.Lock()
            self.active = self.peak = 0

        def generate(self, role, payload, schema, attempt_dir):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                if role == "proposer" and payload["scope"]["scope_id"] != "root":
                    self.siblings.wait()
                return super().generate(role, payload, schema, attempt_dir)
            finally:
                with self.lock:
                    self.active -= 1

    backend = Backend()
    assert verify(backend, tmp_path).coverage_complete
    assert backend.peak == 2
    assert sum(role == "issue_validator" for role, _ in backend.calls) == 4


def test_duplicate_of_inflight_issue_is_validated_once(tmp_path):
    class Backend(FixtureBackend):
        def __init__(self):
            super().__init__({"root": ["same"], "node-0001": ["same"]}, dedup=True)
            self.validation_started = threading.Event()
            self.duplicate_audited = threading.Event()

        def generate(self, role, payload, schema, attempt_dir):
            if role == "decomposer":
                assert self.validation_started.wait(5)
            if role == "issue_validator":
                self.validation_started.set()
                assert self.duplicate_audited.wait(5), "dedup must progress during validation"
            result = super().generate(role, payload, schema, attempt_dir)
            if role == "duplicate_auditor":
                self.duplicate_audited.set()
            return result

    backend = Backend()
    result = verify(backend, tmp_path)
    assert result.coverage_complete and len(result.feedback) == 1
    assert sum(role == "issue_validator" for role, _ in backend.calls) == 1
    diagnostics = read_json(tmp_path / "diagnostics.json")
    assert diagnostics["candidate_count"] == 2 and diagnostics["canonical_issue_count"] == 1
    assert diagnostics["nodes"][1]["canonical_ids"] == diagnostics["nodes"][0]["canonical_ids"]


@pytest.mark.parametrize("stop_kind", ["quota", "interrupt"])
def test_resume_replays_admission_order_and_caches_with_reversed_proposal_completion(
    tmp_path, stop_kind
):
    class Backend(FixtureBackend):
        def __init__(self, failing):
            super().__init__({"root": ["r"], "node-0001": ["a"], "node-0002": ["b"]})
            self.failing = failing
            self.local_validated = threading.Event()
            self.validation_count = 0

        def generate(self, role, payload, schema, attempt_dir):
            if self.failing and role == "proposer" and payload["scope"]["scope_id"] == "root":
                assert self.local_validated.wait(5)
            if role == "issue_validator":
                self.local_validated.set()
            if self.failing and role == "feedback_auditor":
                # End after a local batch is admitted, before root is replayed.
                if stop_kind == "quota":
                    raise QuotaExhausted("fixture quota")
                raise KeyboardInterrupt()
            return super().generate(role, payload, schema, attempt_dir)

    backend = Backend(True)
    if stop_kind == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            verify(backend, tmp_path)
    else:
        assert verify(backend, tmp_path).run_status == "incomplete"
    assert not (tmp_path / "completion.json").exists()
    order = read_json(tmp_path / "proposal-order.json")["order"]
    assert order[0]["scope_id"] != "root"
    cached = {p: p.read_bytes() for p in tmp_path.glob("jobs/*/result.json")}
    fresh = Backend(False)
    report = verify(fresh, tmp_path)
    assert report.coverage_complete and report.run_status == "completed"
    assert read_json(tmp_path / "proposal-order.json")["order"][: len(order)] == order
    assert all(p.read_bytes() == data for p, data in cached.items())
    old_jobs = {p.parent.name for p in cached}
    for role, payload in fresh.calls:
        if role == "proposer":
            assert f"{payload['scope']['scope_id']}-proposer" not in old_jobs
        elif role == "issue_validator":
            assert f"{payload['proposed_issue']['issue_id']}-validation" not in old_jobs
    assert len(report.feedback) == 3


def test_audited_rejection_cancels_slow_proposer_without_waiting_for_root(tmp_path):
    class Backend(FixtureBackend):
        def __init__(self):
            super().__init__({"node-0001": ["reject"]}, reject=["reject"])
            self.root_started = threading.Event()

        def generate(self, role, payload, schema, attempt_dir):
            if role == "proposer" and payload["scope"]["scope_id"] == "root":
                self.root_started.set()
                assert self.cancelled.wait(5)
                raise BackendCancelled("slow root stopped")
            if role == "decomposer":
                assert self.root_started.wait(5)
            return super().generate(role, payload, schema, attempt_dir)

    backend = Backend()
    report = verify(backend, tmp_path)
    assert report.decision == "REJECT" and not report.coverage_complete
    assert report.run_status == "completed"
    assert read_json(tmp_path / "diagnostics.json")["stop_basis"]["scope_id"] == "node-0001"
    fresh = FixtureBackend()
    assert verify(fresh, tmp_path) == report and not fresh.calls


def test_no_early_stop_finishes_all_scopes(tmp_path):
    backend = FixtureBackend({"root": ["reject"], "node-0001": ["extra"]}, reject=["reject"])
    result = verify(backend, tmp_path, early_stop=False)
    assert result.decision == "REJECT" and result.coverage_complete
    assert not backend.cancelled.is_set()
    assert len(result.feedback) == 2


def test_quota_cancels_active_call_and_does_not_dispatch_waiting_validation(tmp_path):
    class Backend(FixtureBackend):
        def __init__(self):
            super().__init__()
            self.root_started = threading.Event()

        def generate(self, role, payload, schema, attempt_dir):
            if role == "proposer":
                self.root_started.set()
                assert self.cancelled.wait(5)
                raise BackendCancelled("cancelled by quota")
            assert role == "decomposer"
            assert self.root_started.wait(5)
            raise QuotaExhausted("quota fixture")

    backend = Backend()
    result = verify(backend, tmp_path)
    assert result.run_status == "incomplete" and result.decision is None
    assert backend.cancelled.is_set()
    assert read_json(tmp_path / "interruption.json")
    assert not (tmp_path / "completion.json").exists()


def test_dedup_fallback_remains_frozen_on_resume(tmp_path):
    class Backend(FixtureBackend):
        def generate(self, role, payload, schema, attempt_dir):
            if role == "deduplicator":
                raise RuntimeError("fixture dedup failure")
            if role == "feedback_auditor":
                raise QuotaExhausted("quota fixture")
            return super().generate(role, payload, schema, attempt_dir)

    proposals = {"root": ["a", "b"]}
    assert verify(Backend(proposals), tmp_path, workers=1).run_status == "incomplete"
    fresh = FixtureBackend(proposals, dedup=True)
    result = verify(fresh, tmp_path, workers=1)
    assert result.coverage_complete and len(result.feedback) == 2
    assert not any(role == "deduplicator" for role, _ in fresh.calls)


def test_single_segment_is_not_proposed_twice(tmp_path):
    class Backend(FixtureBackend):
        def generate(self, role, payload, schema, attempt_dir):
            if role == "decomposer":
                return {
                    "segments": [
                        {"start_line": 1, "end_line": 6, "context_before": "", "context_after": ""}
                    ]
                }
            return super().generate(role, payload, schema, attempt_dir)

    backend = Backend()
    assert verify(backend, tmp_path).coverage_complete
    assert sum(role == "proposer" for role, _ in backend.calls) == 1


@pytest.mark.parametrize("workers", [4, 16])
def test_many_nodes_fill_global_pool_without_exceeding_it(tmp_path, workers):
    class Backend(FixtureBackend):
        def __init__(self):
            super().__init__({f"node-{i:04d}": [f"issue-{i}"] for i in range(1, 25)})
            self.barrier = threading.Barrier(workers, timeout=5)
            self.lock = threading.Lock()
            self.active = self.peak = 0

        def generate(self, role, payload, schema, attempt_dir):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                if role == "decomposer":
                    return {
                        "segments": [
                            {
                                "start_line": i,
                                "end_line": i,
                                "context_before": "",
                                "context_after": "",
                            }
                            for i in range(1, 25)
                        ]
                    }
                if role == "proposer" and payload["scope"]["scope_id"] != "root":
                    index = int(payload["scope"]["scope_id"].split("-")[1])
                    if index <= workers:
                        self.barrier.wait()
                return super().generate(role, payload, schema, attempt_dir)
            finally:
                with self.lock:
                    self.active -= 1

    backend = Backend()
    req = request().model_copy(
        update={"candidate_solution": "\n".join(f"line {i}" for i in range(24))}
    )
    config = (
        WorkflowConfig(max_attempts=1)
        if workers == 16
        else WorkflowConfig(workers=workers, max_attempts=1)
    )
    result = DecomposingVerifier(backend, config).verify(req, tmp_path)
    assert result.coverage_complete and len(result.feedback) == 24
    assert backend.peak == workers
    assert sum(role == "issue_validator" for role, _ in backend.calls) == 24


