from __future__ import annotations

import errno
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from osverifier import locking
from osverifier.locking import file_lock
from osverifier.storage import run_lock


def test_run_lock_excludes_other_process_then_releases(tmp_path):
    code = """
import sys
from pathlib import Path
from osverifier.storage import run_lock
try:
    with run_lock(Path(sys.argv[1])):
        print('acquired')
except RuntimeError as exc:
    print(str(exc))
    sys.exit(3)
"""
    def attempt():
        return subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True, text=True)
    with run_lock(tmp_path):
        blocked = attempt()
        assert blocked.returncode == 3 and 'another verifier process' in blocked.stdout
    allowed = attempt()
    assert allowed.returncode == 0 and allowed.stdout.strip() == 'acquired'


def test_lock_timeout_and_release_after_exception(tmp_path):
    path = tmp_path / 'lock'
    with file_lock(path):
        with pytest.raises(TimeoutError):
            with file_lock(path, timeout=0.02):
                pytest.fail('entered held lock')
    with pytest.raises(ValueError):
        with file_lock(path):
            raise ValueError('caller error')
    with file_lock(path):
        pass


def test_windows_lock_uses_fixed_byte_and_maps_only_contention(tmp_path, monkeypatch):
    calls = []
    failure = [None]
    def windows_lock(fd, mode, size):
        calls.append((mode, size, os.lseek(fd, 0, os.SEEK_CUR)))
        if failure[0]:
            raise failure[0]
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=windows_lock))
    # Patch this module's platform selection, not pathlib's global os.name.
    monkeypatch.setattr(locking, 'os', SimpleNamespace(name='nt'))
    path = tmp_path / 'lock'
    path.write_bytes(b'old lock contents')
    with path.open('a+b') as stream:
        locking._try_lock(stream)
        stream.seek(8)
        locking._unlock(stream)
        assert calls == [(2, 1, 0), (0, 1, 0)]
        failure[0] = OSError(errno.EACCES, 'lock violation')
        with pytest.raises(BlockingIOError):
            locking._try_lock(stream)
        failure[0] = OSError(errno.EBADF, 'bad descriptor')
        with pytest.raises(OSError) as error:
            locking._try_lock(stream)
        assert error.value.errno == errno.EBADF


def test_imports_do_not_require_unix_fcntl():
    code = """
import builtins
original = builtins.__import__
def no_fcntl(name, *args, **kwargs):
    if name == 'fcntl':
        raise ModuleNotFoundError("No module named 'fcntl'")
    return original(name, *args, **kwargs)
builtins.__import__ = no_fcntl
from osverifier.storage import run_lock
from osverifier_reports.reference_tools import LiteratureTools
print('imports succeeded')
"""
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'imports succeeded'
