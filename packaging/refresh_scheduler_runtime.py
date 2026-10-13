#!/usr/bin/env python3
"""Overlay the tested scheduler on the math-only snapshot without importing AI features."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

MODULES = ("pipeline.py", "workflow.py", "workflow_models.py", "scheduling.py")
# Core modules the scheduler overlay depends on (quota/cache replay, redaction,
# trajectory recovery, UTF-8 I/O). They are backward compatible with the legacy
# backend, which simply has no auxiliary attempt artifacts.
SUPPORT = ("jobs.py", "quota.py", "connection_config.py", "storage.py", "trajectory.py", "exporting.py")


def refresh(repo: Path):
    target = repo / "packaging/skills/osverify/scripts/runtime"
    manifest_path = target / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for name in MODULES + SUPPORT:
        shutil.copyfile(repo / "src/osverifier" / name, target / "osverifier" / name)
    cli = target / "osverifier/cli.py"
    text = cli.read_text().replace("default=8,", "default=16,")
    text = text.replace(
        "Maximum concurrent issue-review chains (one active Codex call each)",
        "Maximum concurrent verifier calls across all stages (default: 16)",
    ).replace(
        "Review root, then one original-text decomposition layer with audited deduplication",
        "Review root and one original-text decomposition layer with global priority scheduling",
    )
    cli.write_text(text)
    from provenance import source_provenance

    manifest["scheduler_overlay"] = {
        **source_provenance(repo),
        "modules": list(MODULES + SUPPORT),
        "cli_changes": "worker default and scheduling help only",
    }
    manifest["files"] = {
        str(p.relative_to(target)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(target.rglob("*"))
        if p.is_file() and p != manifest_path and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
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
    print(refresh(Path(__file__).resolve().parents[1]))
