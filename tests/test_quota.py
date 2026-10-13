"""Offline provider-error, stop-scheduling, and manual-resume regressions."""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import BaseModel
from test_verifier import ScriptedBackend
from test_workflow import FixtureBackend, request

from osverifier.backend import CodexBackend
from osverifier.demo import demo_proposal, demo_request
from osverifier.jobs import JobRunner
from osverifier.models import ProposalBatch
from osverifier.pipeline import RunConfig, Verifier
from osverifier.quota import QuotaExhausted, quota_from_event
from osverifier.storage import package_fingerprint, read_json
from osverifier.workflow import DecomposingVerifier, WorkflowConfig


@pytest.mark.parametrize(
    "error",
    [
        {"code": "usage_limit_reached"},
        {"type": "insufficient_quota"},
        {"codex_error_info": "UsageLimitExceeded"},
        {"message": "You've hit your usage limit. Try again at 3 PM."},
        {"message": "You have reached your usage limit."},
        {"message": "You exceeded your current quota, please check your plan and billing details."},
        {"message": "You've exceeded your current quota."},
        {"message": "usage_limit_reached: limit exceeded"},
    ],
)
def test_explicit_provider_quota_is_recognized(error):
    assert isinstance(quota_from_event({"type": "turn.failed", "error": error}), QuotaExhausted)


@pytest.mark.parametrize(
    "event",
    [
        {"type": "error", "code": "rate_limit_exceeded", "message": "429 Too Many Requests"},
        {"type": "turn.failed", "error": {"message": "model is at capacity"}},
        {"type": "error", "message": "connection timed out"},
        {"type": "error", "message": "context window exceeded"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "You've hit your usage limit"},
        },
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "output": "insufficient_quota"},
        },
        {
            "type": "item.completed",
            "item": {"type": "mcp_tool_call", "error": "usage_limit_reached"},
        },
        {"type": "turn.completed", "usage": {"usedPercent": 100}},
        ["usage_limit_reached"],
    ],
)
def test_temporary_errors_and_untrusted_content_are_not_quota(event):
    assert quota_from_event(event) is None


@pytest.fixture
def fake_quota_codex(tmp_path):
    path = tmp_path / "fake-codex"
    path.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys, time
from pathlib import Path
if "--version" in sys.argv:
    print("codex-cli quota-fixture")
    raise SystemExit(0)
args = sys.argv
sys.stdin.read()
control = json.loads(Path(__file__).with_suffix(".json").read_text())
if "--cd" in args:
    workspace = Path(args[args.index("--cd")+1])
    payload = json.loads((workspace / "input.json").read_text())
    behavior = payload.get("behavior", control["behavior"])
    output = {"ok": True}
    thread = "verifier-session"
else:
    workspace = Path.cwd()
    behavior = control["behavior"]
    output = {"candidate_ready": True, "progress": "Fixture complete"}
    (workspace / "results/blueprint.md").write_text("Persisted candidate")
    thread = args[args.index("resume")+1] if "resume" in args else "generator-session"
log = Path(__file__).with_suffix(".calls")
previous = log.read_text().splitlines() if log.exists() else []
with log.open("a") as stream:
    stream.write(json.dumps({"behavior": behavior, "pid": os.getpid()})+"\\n")
print(json.dumps({"type":"thread.started", "thread_id":thread}),flush=True)
if behavior == "sleep":
    time.sleep(60)
if behavior in ("quota", "quota_hang", "quota_zero"):
    print(json.dumps({"type":"turn.failed", "error":{"code":"usage_limit_reached","message":"You've hit your usage limit."}}),flush=True)
    if behavior == "quota_hang":
        time.sleep(60)
    if behavior != "quota_zero":
        raise SystemExit(1)
elif behavior == "stderr_quota":
    print("ERROR: You exceeded your current quota, please check your plan and billing details.",file=sys.stderr,flush=True)
    raise SystemExit(1)
elif behavior == "temporary_once" and not previous:
    print(json.dumps({"type":"error", "message":"429 Too Many Requests"}),flush=True)
    raise SystemExit(1)
elif behavior == "quoted_quota":
    print(json.dumps({"type":"item.completed", "item":{"type":"agent_message","text":"You've hit your usage limit"}}),flush=True)
Path(args[args.index("--output-last-message")+1]).write_text(json.dumps(output))
print(json.dumps({"type":"turn.completed", "usage":{"input_tokens":0,"output_tokens":0}}),flush=True)
"""
    )
    path.chmod(0o755)
    path.with_suffix(".json").write_text(json.dumps({"behavior": "quota"}))
    return path


def behavior(binary, value):
    binary.with_suffix(".json").write_text(json.dumps({"behavior": value}))


def calls(binary):
    path = binary.with_suffix(".calls")
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Output(BaseModel):
    ok: bool


@pytest.mark.parametrize("mode", ["quota", "quota_hang", "quota_zero", "stderr_quota"])
@pytest.mark.parametrize("timeout", [None, 20])
def test_transport_quota_never_retries_or_waits_for_cli_timeout(
    fake_quota_codex, tmp_path, mode, timeout
):
    binary = fake_quota_codex
    behavior(binary, mode)
    backend = CodexBackend(model="fixture", codex_bin=str(binary), timeout_seconds=timeout)
    runner = JobRunner(backend, tmp_path / "run", 3, package_fingerprint())
    started = time.monotonic()
    with pytest.raises(QuotaExhausted):
        runner.call("audit", "feedback_auditor", {}, Output, lambda _: None)
    assert time.monotonic() - started < 5
    assert len(calls(binary)) == 1
    assert read_json(tmp_path / "run/interruption.json")["reason"] == "quota_exhausted"
    assert not list((tmp_path / "run").glob("jobs/*/result.json"))
    assert not list((tmp_path / "run").glob("jobs/*/attempt-*/failure.json"))
    with pytest.raises(ProcessLookupError):
        os.kill(calls(binary)[0]["pid"], 0)


def test_temporary_failure_still_retries(fake_quota_codex, tmp_path):
    behavior(fake_quota_codex, "temporary_once")
    backend = CodexBackend(model="fixture", codex_bin=str(fake_quota_codex))
    runner = JobRunner(backend, tmp_path / "run", 2, package_fingerprint())
    assert runner.call("audit", "feedback_auditor", {}, Output, lambda _: None).ok
    assert len(calls(fake_quota_codex)) == 2
    assert not (tmp_path / "run/interruption.json").exists()


def test_quoted_quota_text_does_not_stop_transport(fake_quota_codex, tmp_path):
    behavior(fake_quota_codex, "quoted_quota")
    backend = CodexBackend(model="fixture", codex_bin=str(fake_quota_codex))
    runner = JobRunner(backend, tmp_path / "run", 2, package_fingerprint())
    assert runner.call("audit", "feedback_auditor", {}, Output, lambda _: None).ok
    assert len(calls(fake_quota_codex)) == 1


def test_quota_stops_inflight_process_and_blocks_queued_jobs(fake_quota_codex, tmp_path):
    backend = CodexBackend(model="fixture", codex_bin=str(fake_quota_codex), timeout_seconds=20)
    runner = JobRunner(backend, tmp_path / "run", 3, package_fingerprint())
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(
            runner.call, "slow", "feedback_auditor", {"behavior": "sleep"}, Output, lambda _: None
        )
        try:
            deadline = time.monotonic() + 5
            while not calls(fake_quota_codex) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert calls(fake_quota_codex)
            exhausted = pool.submit(
                runner.call, "quota", "feedback_auditor", {}, Output, lambda _: None
            )
            with pytest.raises(QuotaExhausted):
                exhausted.result(timeout=5)
            with pytest.raises(QuotaExhausted):
                slow.result(timeout=5)
            with pytest.raises(QuotaExhausted):
                runner.call("queued", "feedback_auditor", {}, Output, lambda _: None)
        finally:
            backend.cancel()
    assert len(calls(fake_quota_codex)) == 2
    assert not (tmp_path / "run/jobs/queued/attempt-0001").exists()






def test_root_only_audit_quota_preserves_validated_caches(tmp_path):
    backend = ScriptedBackend()
    backend.outputs["feedback_auditor"] = QuotaExhausted("quota fixture")
    config = RunConfig(workers=1, max_attempts=3)
    first = Verifier(backend, config).verify(demo_request(), tmp_path)
    assert first.run_status == "incomplete" and not first.feedback
    assert Counter(backend.calls) == {"proposer": 1, "issue_validator": 1, "feedback_auditor": 1}
    cached = {p: p.read_bytes() for p in tmp_path.glob("jobs/*/result.json")}
    recovered = ScriptedBackend()
    second = Verifier(recovered, config).verify(demo_request(), tmp_path)
    assert second.run_status == "completed" and recovered.calls == ["feedback_auditor"]
    assert all(p.read_bytes() == contents for p, contents in cached.items())
    assert not (tmp_path / "interruption.json").exists()




def test_quota_on_resume_still_replays_exact_cached_chains(tmp_path):
    class Backend(FixtureBackend):
        def __init__(self, failure):
            super().__init__({"root": ["blocked", "cached"]})
            self.failure = failure

        def generate(self, role, payload, schema, attempt_dir):
            if role == "issue_validator" and payload["proposed_issue"]["target"] == "blocked":
                self.calls.append((role, payload))
                raise self.failure
            return super().generate(role, payload, schema, attempt_dir)

    config = WorkflowConfig(workers=1, max_attempts=1)
    first = DecomposingVerifier(Backend(RuntimeError("transient")), config).verify(request(), tmp_path)
    assert first.decision == "REVISE" and first.run_status == "incomplete"
    exhausted = Backend(QuotaExhausted("quota fixture"))
    second = DecomposingVerifier(exhausted, config).verify(request(), tmp_path)
    # The uncached validation is dispatched first; quota stops it and any further
    # backend launch, while the other issue's cached validation and audit replay.
    assert [role for role, _ in exhausted.calls] == ["issue_validator"]
    assert second.run_status == "incomplete" and second.feedback == first.feedback
    assert second.decision == "REVISE"
    assert read_json(tmp_path / "interruption.json")["reason"] == "quota_exhausted"
    events = [json.loads(line) for line in (tmp_path / "trajectory.jsonl").read_text().splitlines()]
    blocked = [e for e in events if e["event"] == "job_cancelled" and e.get("reason") == "quota_exhausted"]
    assert [e["job"] for e in blocked] == ["root-issue-000001-validation"]
    assert any(e["event"] == "job_dispatched" and e.get("cache_only") for e in events)


def test_root_only_quota_keeps_cached_issue_chains(tmp_path):
    from test_verifier import ScriptedBackend as Scripted

    class Backend(Scripted):
        def __init__(self, failure):
            super().__init__()
            self.failure = failure

        def generate(self, role, payload, schema, attempt_dir):
            if role == "issue_validator" and payload["proposed_issue"]["issue_id"] != "I1":
                self.calls.append(role)
                raise self.failure
            return super().generate(role, payload, schema, attempt_dir)

    proposals = demo_proposal()
    second_issue = {**proposals["issues"][0], "issue_id": "I0"}
    proposals["issues"].insert(0, second_issue)
    batch = ProposalBatch.model_validate(proposals)
    config = RunConfig(workers=1, max_attempts=1)
    first = Verifier(Backend(RuntimeError("transient")), config).verify(demo_request(), tmp_path, proposals=batch)
    assert first.feedback and first.run_status == "incomplete"
    exhausted = Backend(QuotaExhausted("quota fixture"))
    second = Verifier(exhausted, config).verify(demo_request(), tmp_path, proposals=batch)
    assert exhausted.calls == ["issue_validator"]
    assert second.run_status == "incomplete" and second.feedback == first.feedback
