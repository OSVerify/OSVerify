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


