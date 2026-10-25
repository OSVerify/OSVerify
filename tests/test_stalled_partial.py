import subprocess
import sys

import pytest
from test_workflow import FixtureBackend

from osverifier.pipeline import RunConfig, Verifier
from osverifier.quota import BackendStalled, communicate_with_quota_detection
from osverifier.storage import read_json


def test_timeout_retry_reuses_job_budget(tmp_path):
    from osverifier.models import ReviewRequest
    class Flaky(FixtureBackend):
        attempts = 0
        def generate(self, role, payload, schema, attempt_dir):
            self.attempts += 1
            if self.attempts == 1:
                raise BackendStalled('fixture', 0.1)
            return super().generate(role, payload, schema, attempt_dir)
    backend = Flaky()
    report = Verifier(backend, RunConfig(max_attempts=2)).verify(
        ReviewRequest(request_id='trial', question='Q', candidate_solution='Progress', mode='partial'), tmp_path)
    assert backend.attempts == 2 and report.decision == 'PASS'
    assert read_json(tmp_path / 'jobs/proposer/attempt-0001/failure.json')['type'] == 'BackendStalled'


def test_real_process_silence_triggers_stall(tmp_path):
    (tmp_path / 'stderr.log').write_text('')
    with (tmp_path / 'events.jsonl').open('w') as events:
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'], stdin=subprocess.PIPE, stdout=events, text=True)
        try:
            with pytest.raises(BackendStalled):
                communicate_with_quota_detection(p, '', 5, tmp_path, idle_timeout=0.15)
            assert read_json(tmp_path / 'stall.json')['reason'] == 'no_progress_timeout'
        finally:
            p.kill()
            p.wait()


def test_real_process_progress_resets_idle_clock(tmp_path):
    (tmp_path / 'stderr.log').write_text('')
    script = "import time,json; [(print(json.dumps({'type':'item.updated'}),flush=True),time.sleep(.08)) for _ in range(6)]"
    with (tmp_path / 'events.jsonl').open('w') as events:
        p = subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE, stdout=events, text=True)
        communicate_with_quota_detection(p, '', 5, tmp_path, idle_timeout=0.3)
    assert p.returncode == 0 and not (tmp_path / 'stall.json').exists()




def test_watchdog_backend_cleans_up_process(tmp_path):
    import os

    from osverifier.backend import CodexBackend
    binary = tmp_path / 'fake-codex'
    binary.write_text(f'#!{sys.executable}\nimport sys,time\nif "--version" in sys.argv:\n print("fixture"); sys.exit(0)\ntime.sleep(10)\n')
    binary.chmod(0o755)
    backend = CodexBackend(model='fixture', codex_bin=str(binary), idle_timeout_seconds=0.15)
    attempt = tmp_path / 'attempt'
    with pytest.raises(BackendStalled):
        backend.generate('proposer', {}, {}, attempt)
    pid = read_json(attempt / 'process-start.json')['pid']
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not backend._processes




class Timeouts:
    identity = {"kind": "scripted_demo"}

    def __init__(self, error=subprocess.TimeoutExpired):
        self.error, self.scales = error, []

    def generate(self, role, payload, schema, attempt_dir):
        from osverifier.backend import DEADLINE_SCALE
        self.scales.append(DEADLINE_SCALE.get())
        raise self.error('fixture', 0.1)


def test_timeouts_earn_bounded_retries_with_doubled_limits(tmp_path):
    from pydantic import BaseModel

    from osverifier.jobs import JobFailed, JobRunner
    from osverifier.storage import package_fingerprint

    class Output(BaseModel):
        ok: bool
    backend = Timeouts()
    runner = JobRunner(backend, tmp_path, 2, package_fingerprint(), timeout_retries=1)
    with pytest.raises(JobFailed, match='after 3 attempts'):
        runner.call('proposer', 'proposer', {}, Output, lambda value: None)
    assert backend.scales == [1, 2, 2]
    # A resumed job that timed out before starts with the longer limits.
    with pytest.raises(JobFailed):
        runner.call('proposer', 'proposer', {}, Output, lambda value: None)
    assert backend.scales[3:] == [2, 2, 2]

    other = Timeouts(error=lambda *_: ValueError('malformed'))
    runner = JobRunner(other, tmp_path / 'other', 2, package_fingerprint(), timeout_retries=1)
    with pytest.raises(JobFailed, match='after 2 attempts'):
        runner.call('proposer', 'proposer', {}, Output, lambda value: None)
    assert other.scales == [1, 1]


def test_codex_backend_scales_limits_for_retries(tmp_path):
    from osverifier.backend import DEADLINE_SCALE, CodexBackend
    binary = tmp_path / 'fake-codex'
    binary.write_text(f'#!{sys.executable}\nimport sys,time\nif "--version" in sys.argv:\n print("fixture"); sys.exit(0)\ntime.sleep(10)\n')
    binary.chmod(0o755)
    backend = CodexBackend(model='fixture', codex_bin=str(binary), timeout_seconds=5, idle_timeout_seconds=0.15)
    token = DEADLINE_SCALE.set(2)
    try:
        with pytest.raises(BackendStalled):
            backend.generate('proposer', {}, {}, tmp_path / 'attempt')
    finally:
        DEADLINE_SCALE.reset(token)
    assert read_json(tmp_path / 'attempt/command.json')['limits'] == {'timeout_seconds': 10, 'idle_timeout_seconds': 0.3}
    assert read_json(tmp_path / 'attempt/stall.json')['idle_seconds'] == 0.3
