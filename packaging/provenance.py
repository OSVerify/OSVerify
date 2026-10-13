"""Source provenance for frozen skill runtimes.

Records the git tree hash of ``src/`` rather than a commit: the tree is content-
addressed, so it stays verifiable (``git rev-parse <commit>:src``) in any commit that
ships the same sources, including after history is rewritten.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def source_provenance(repo: Path) -> dict:
    try:
        tree = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD:src"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain", "--", "src"],
                                             text=True))
    except (OSError, subprocess.CalledProcessError):
        tree, dirty = "working-tree-export", True
    return {"source_tree": tree, "source_tree_dirty": dirty}
