"""Locate the Python entry point for helper processes, also inside a frozen app.

In development a helper runs as ``python -m module``. In a compiled desktop bundle
there is no interpreter; the bundle's own executable dispatches ``--entry module``
to that module's ``main()``. Every place that spawns a module must use this helper.

Inside a Nuitka bundle ``sys.executable`` is a placeholder ``python`` path next to the
binary that does not exist on disk, so the bundle executable is resolved from the
operating system instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENTRY_FLAG = "--entry"


def is_frozen() -> bool:
    main = sys.modules.get("__main__")
    return bool(getattr(sys, "frozen", False)) or hasattr(main, "__compiled__")


def _os_executable() -> str | None:
    """The running executable as reported by the operating system, if available."""
    if sys.platform == "darwin":
        try:
            import ctypes

            size = ctypes.c_uint32(0)
            libc = ctypes.CDLL(None)
            libc._NSGetExecutablePath(None, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value + 1)
            if libc._NSGetExecutablePath(buffer, ctypes.byref(size)) == 0:
                return os.fsdecode(buffer.value)
        except (OSError, AttributeError, ValueError):
            return None
        return None
    if sys.platform.startswith("linux"):
        try:
            return os.readlink("/proc/self/exe")
        except OSError:
            return None
    return None


def frozen_executable() -> str:
    """Path of the bundle executable that also serves as the helper runtime."""
    resolved = _os_executable()
    if resolved and Path(resolved).is_file():
        return resolved
    compiled = getattr(sys.modules.get("__main__"), "__compiled__", None)
    for candidate in (getattr(compiled, "original_argv0", None), sys.argv[0] if sys.argv else None):
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file():
                return str(path.resolve())
    return sys.executable


def python_module_command(module: str) -> list[str]:
    if is_frozen():
        return [frozen_executable(), ENTRY_FLAG, module]
    return [sys.executable, "-m", module]
