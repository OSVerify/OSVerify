from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .locking import file_lock


class FrozenRunMismatch(ValueError):
    pass


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def atomic_json(path: Path, value: object) -> None:
    atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def package_fingerprint() -> str:
    root = Path(__file__).parent
    files = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in (".py", ".md")
    }
    return digest(files)


@contextmanager
def run_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        try:
            stack.enter_context(file_lock(directory / ".lock"))
        except BlockingIOError as exc:
            raise RuntimeError("another verifier process owns this run directory") from exc
        yield
