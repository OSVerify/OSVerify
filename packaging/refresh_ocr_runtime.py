#!/usr/bin/env python3
"""Refresh only the opt-in PDF runtime; never change the legacy native-text snapshot."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


def refresh(repo: Path):
    target = repo / "packaging/skills/osverify/scripts/ocr-runtime"
    if target.exists():
        if not (target / "bundle-manifest.json").exists():
            raise ValueError("Refusing to replace unrecognized OCR runtime")
        shutil.rmtree(target)
    for name in ("osverifier", "osverifier_reports"):
        shutil.copytree(repo / "src" / name, target / name,
                        ignore=shutil.ignore_patterns("integrations", "__pycache__", "*.pyc"))
    from provenance import source_provenance

    from osverifier.storage import atomic_json

    atomic_json(target / "bundle-manifest.json", {"format": 1, **source_provenance(repo),
                "scope": "opt-in PDF math/AI paper review, no generation",
                "files": {str(p.relative_to(target)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted(target.rglob("*")) if p.is_file()}})
    _record_known_runtime(repo)
    return target


def _record_known_runtime(repo: Path) -> None:
    """Allow runs pinned to this runtime: add its manifest to scripts/known-runtimes.json."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("known_runtimes", repo / "packaging/known_runtimes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.record(repo)


if __name__ == "__main__":
    import sys

    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "src"))
    print(refresh(repo))
