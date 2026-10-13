from __future__ import annotations

import json
import os
import sys

import pytest

from osverifier.backend import CodexBackend


@pytest.fixture
def fake_codex(tmp_path):
    path = tmp_path / "fake-codex"
    path.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys, time
from pathlib import Path
if "--version" in sys.argv:
    print("codex-cli test-fixture")
    raise SystemExit(0)
args = sys.argv
prompt = sys.stdin.read()
workspace = Path(args[args.index("--cd") + 1])
payload = json.loads((workspace / "input.json").read_text())
if payload.get("sleep"):
    time.sleep(60)
schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
assert schema["type"] == "object"
assert (workspace / ".agents/skills/audit-verifier-feedback/SKILL.md").is_file()
assert (workspace / ".agents/skills/policy.md").is_file()
assert "$audit-verifier-feedback" in prompt
output = {"ok": True, "selected_home": os.environ.get("CODEX_HOME")}
Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(output))
print(json.dumps({"type": "thread.started", "thread_id": "fixture-session"}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
"""
    )
    path.chmod(0o755)
    return path


def test_cli_transport_uses_files_argv_and_invocation_scoped_auth(fake_codex, tmp_path):
    original_home = os.environ.get("CODEX_HOME")
    selected = tmp_path / "labeled account"
    attempt = tmp_path / "call"
    attempt.mkdir()
    backend = CodexBackend(model="fixture-model", codex_bin=str(fake_codex), codex_home=selected)
    response = backend.generate(
        "feedback_auditor",
        {"text": "Literal `echo secret` and $(echo secret)"},
        {"type": "object"},
        attempt,
    )
    assert response == {"ok": True, "selected_home": str(selected)}
    assert os.environ.get("CODEX_HOME") == original_home
    command = json.loads((attempt / "command.json").read_text())["argv"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in command
    assert "--output-schema" in command
    config = [command[index + 1] for index, arg in enumerate(command) if arg == "-c"]
    assert 'model_reasoning_effort="xhigh"' in config
    assert 'web_search="live"' in config
    metadata = json.loads((attempt / "events-summary.json").read_text())
    assert metadata["thread_ids"] == ["fixture-session"]


def test_explicit_reasoning_and_search_overrides_are_preserved(fake_codex, tmp_path):
    attempt = tmp_path / "override"
    attempt.mkdir()
    backend = CodexBackend(
        model="fixture-model",
        codex_bin=str(fake_codex),
        reasoning_effort="high",
        web_search="disabled",
    )
    backend.generate("feedback_auditor", {}, {"type": "object"}, attempt)
    command = json.loads((attempt / "command.json").read_text())["argv"]
    config = [command[index + 1] for index, arg in enumerate(command) if arg == "-c"]
    assert 'model_reasoning_effort="high"' in config
    assert 'web_search="disabled"' in config


def test_timeout_does_not_return_successful_output(fake_codex, tmp_path):
    import subprocess

    attempt = tmp_path / "timeout"
    attempt.mkdir()
    backend = CodexBackend(model="fixture-model", codex_bin=str(fake_codex), timeout_seconds=0.1)
    with pytest.raises(subprocess.TimeoutExpired):
        backend.generate("feedback_auditor", {"sleep": True}, {"type": "object"}, attempt)
    assert not (attempt / "raw-output.json").exists()


def test_cancel_stops_an_active_call_and_prevents_new_processes(fake_codex, tmp_path):
    import time
    from concurrent.futures import ThreadPoolExecutor

    backend = CodexBackend(model="fixture-model", codex_bin=str(fake_codex), timeout_seconds=30)
    attempt = tmp_path / "active"
    attempt.mkdir()
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            backend.generate, "feedback_auditor", {"sleep": True}, {"type": "object"}, attempt
        )
        deadline = time.monotonic() + 5
        active = False
        while time.monotonic() < deadline:
            with backend._process_lock:
                active = bool(backend._processes)
            if active:
                break
            time.sleep(0.01)
        try:
            assert active
        finally:
            backend.cancel()
        with pytest.raises(RuntimeError):
            result.result(timeout=5)
    later = tmp_path / "later"
    later.mkdir()
    with pytest.raises(RuntimeError, match="cancelled"):
        backend.generate("feedback_auditor", {}, {"type": "object"}, later)




@pytest.mark.parametrize("deadline", ["timeout", "idle"])
def test_timeout_exceptions_do_not_carry_connection_overrides(tmp_path, deadline):
    import subprocess

    from osverifier.quota import BackendStalled, communicate_with_quota_detection

    attempt = tmp_path / "attempt"
    attempt.mkdir()
    argv = [sys.executable, "-c", "import time; time.sleep(30)", "-c",
            'model_providers={ "proxy" = { "http_headers" = { "Authorization" = "fixture-secret" } } }']
    with (attempt / "events.jsonl").open("w") as events:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=events, text=True)
        try:
            with pytest.raises(subprocess.TimeoutExpired) as raised:
                communicate_with_quota_detection(
                    process, "", 0.2 if deadline == "timeout" else None, attempt,
                    idle_timeout=0.2 if deadline == "idle" else None,
                )
        finally:
            process.kill()
            process.wait()
    assert isinstance(raised.value, BackendStalled) == (deadline == "idle")
    assert "fixture-secret" not in str(raised.value)
    assert "Authorization" not in str(raised.value)


def test_job_failure_artifacts_never_persist_connection_overrides(tmp_path):
    from pydantic import BaseModel

    from osverifier.jobs import JobFailed, JobRunner
    from osverifier.storage import package_fingerprint

    home = tmp_path / "account"
    home.mkdir()
    (home / "config.toml").write_text("""model_provider = "proxy"
[model_providers.proxy]
name = "Proxy"
base_url = "https://private-gateway.invalid/v1"
http_headers = { Authorization = "Bearer fixture-secret" }
""")
    binary = tmp_path / "codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "if '--version' in sys.argv:\n"
        "    print('fixture')\n"
        "    raise SystemExit\n"
        "sys.stdin.read()\n"
        "time.sleep(30)\n"
    )
    binary.chmod(0o755)

    class Output(BaseModel):
        ok: bool

    backend = CodexBackend(model="fixture", codex_bin=str(binary), codex_home=home,
                           timeout_seconds=0.3)
    runner = JobRunner(backend, tmp_path / "run", 1, package_fingerprint())
    with pytest.raises(JobFailed):
        runner.call("proposer", "proposer", {}, Output, lambda value: None)
    failure = json.loads(next((tmp_path / "run").glob("jobs/proposer/attempt-*/failure.json")).read_text())
    assert failure["type"] == "TimeoutExpired" and "timed out" in failure["error"]
    for path in (tmp_path / "run").rglob("*"):
        if path.is_file():
            text = path.read_text(errors="replace")
            assert "fixture-secret" not in text and "private-gateway" not in text, path


STUBBORN = """
import os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, lambda *_: Path(sys.argv[1]).with_suffix(".term").touch())
Path(sys.argv[1]).touch()
time.sleep(60)
"""


def stubborn_groups(tmp_path, count):
    """Session leaders that survive SIGTERM, as a slowly exiting CLI would."""
    import subprocess
    import time

    markers = [tmp_path / f"stubborn-{index}" for index in range(count)]
    processes = [
        subprocess.Popen([sys.executable, "-c", STUBBORN, str(marker)], start_new_session=True)
        for marker in markers
    ]
    deadline = time.monotonic() + 10
    while not all(marker.exists() for marker in markers):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    return processes, markers


def test_stop_processes_terms_every_group_before_one_shared_grace(tmp_path, monkeypatch):
    import signal
    import time

    from osverifier import backend

    monkeypatch.setattr(backend, "STOP_GRACE_SECONDS", 1.0)
    processes, markers = stubborn_groups(tmp_path, 4)
    started = time.monotonic()
    backend.stop_processes(processes)
    # One shared deadline, not one grace period per process group.
    assert time.monotonic() - started < 3
    assert all(marker.with_suffix(".term").exists() for marker in markers)
    assert [process.returncode for process in processes] == [-signal.SIGKILL] * 4


def test_stop_process_kills_group_members_left_by_an_exited_leader():
    import subprocess
    import time

    from osverifier.backend import stop_process

    leader = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 60 & echo $!"], stdout=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    member = int(leader.stdout.readline())
    leader.wait(timeout=5)
    leader.stdout.close()
    stop_process(leader)
    deadline = time.monotonic() + 5
    while True:
        try:
            os.killpg(leader.pid, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            pass  # macOS reports an exiting (zombie) member this way
        assert time.monotonic() < deadline, f"group member {member} survived"
        time.sleep(0.02)


def test_second_ctrl_c_is_held_until_every_group_is_stopped(tmp_path, monkeypatch):
    import signal
    import threading

    from osverifier import backend

    monkeypatch.setattr(backend, "STOP_GRACE_SECONDS", 1.0)
    processes, _ = stubborn_groups(tmp_path, 3)
    timer = threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            backend.stop_processes(processes)
    finally:
        timer.cancel()
    assert all(process.returncode == -signal.SIGKILL for process in processes)
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_codex_reported_failure_reaches_the_error_message(tmp_path):
    from osverifier.backend import codex_error_message

    provider = json.dumps({"type": "error", "status": 400, "error": {
        "type": "invalid_request_error",
        "message": "The 'fixture' model requires a newer version of Codex."}})
    assert codex_error_message({"type": "turn.failed", "error": {"message": provider}}) == (
        "The 'fixture' model requires a newer version of Codex.")
    assert codex_error_message({"type": "error", "message": "plain failure"}) == "plain failure"
    assert codex_error_message({"type": "turn.completed"}) is None
    binary = tmp_path / "codex"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "if '--version' in sys.argv:\n"
        "    print('fixture'); raise SystemExit\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({{'type': 'turn.failed', 'error': {{'message': {provider!r}}}}}))\n"
        "raise SystemExit(1)\n"
    )
    binary.chmod(0o755)
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    backend = CodexBackend(model="fixture", codex_bin=str(binary), codex_home=tmp_path / "home")
    with pytest.raises(RuntimeError, match="requires a newer version of Codex"):
        backend.generate("proposer", {}, {"type": "object"}, attempt)


@pytest.mark.parametrize("version,supported", [
    ("codex-cli 0.144.1", False), ("codex-cli 0.161.9", False),
    ("codex-cli 0.162.0-alpha.17.2", True), ("codex-cli 0.162.1", True), ("fixture", True)])
def test_codex_older_than_the_supported_minimum_is_refused(tmp_path, version, supported):
    binary = tmp_path / "codex"
    binary.write_text(f"#!/bin/sh\necho '{version}'\n")
    binary.chmod(0o755)
    if supported:
        CodexBackend(model="gpt-6-astra", codex_bin=str(binary), codex_home=tmp_path / "home")
    else:
        with pytest.raises(RuntimeError, match="too old.*gpt-6-astra"):
            CodexBackend(model="gpt-6-astra", codex_bin=str(binary), codex_home=tmp_path / "home")


def test_models_come_from_the_codex_account_with_the_default_first(tmp_path, capsys):
    from osverifier import cli
    from osverifier.backend import DEFAULT_MODEL, KNOWN_MODELS, available_models

    assert DEFAULT_MODEL == "gpt-6-astra"
    assert available_models(tmp_path / "empty") == list(KNOWN_MODELS)
    home = tmp_path / "account"
    home.mkdir()
    (home / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-6.1-sol", "visibility": "list"}, {"slug": "gpt-reserve", "visibility": "hide"},
        {"slug": "gpt-6-astra", "visibility": "list"}, {"slug": "gpt-5.6-sol", "visibility": "list"}]}))
    assert available_models(home) == ["gpt-6-astra", "gpt-6.1-sol", "gpt-5.6-sol"]
    assert cli.main(["models", "--codex-home", str(home)]) == 0
    assert capsys.readouterr().out.splitlines() == ["gpt-6-astra  (default)", "gpt-6.1-sol", "gpt-5.6-sol"]
    args = cli.parser().parse_args(["verify", "--question", "q", "--solution", "s", "--run-dir", "r"])
    assert args.model == "gpt-6-astra"
