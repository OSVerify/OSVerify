import os
import sys
from types import SimpleNamespace

from osverifier import frozen


def test_development_mode_runs_the_module_with_the_interpreter():
    assert frozen.python_module_command("osverifier.cli") == [
        sys.executable,
        "-m",
        "osverifier.cli",
    ]


def test_os_resolver_finds_the_running_interpreter():
    resolved = frozen._os_executable()
    assert resolved is not None and os.path.isfile(resolved)
    assert os.path.samefile(resolved, sys.executable)


def test_frozen_mode_dispatches_through_the_real_binary(monkeypatch):
    """A Nuitka bundle reports a placeholder sys.executable that does not exist."""
    monkeypatch.setattr(sys.modules["__main__"], "__compiled__", SimpleNamespace(), raising=False)
    monkeypatch.setattr(sys, "executable", "/nonexistent/Contents/MacOS/python")
    assert frozen.is_frozen()
    command = frozen.python_module_command("osverifier.cli")
    assert command[1:] == ["--entry", "osverifier.cli"]
    assert os.path.isfile(command[0]) and "nonexistent" not in command[0]


def test_frozen_mode_falls_back_to_argv0_when_the_os_gives_nothing(monkeypatch, tmp_path):
    fake = tmp_path / "Rethlas-OSV"
    fake.write_text("")
    monkeypatch.setattr(sys.modules["__main__"], "__compiled__", SimpleNamespace(), raising=False)
    monkeypatch.setattr(sys, "executable", "/nonexistent/python")
    monkeypatch.setattr(frozen, "_os_executable", lambda: None)
    monkeypatch.setattr(sys, "argv", [str(fake)])
    assert frozen.python_module_command("osverifier.cli")[0] == str(fake.resolve())
