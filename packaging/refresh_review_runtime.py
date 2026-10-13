#!/usr/bin/env python3
"""Snapshot the literature-enabled skill engine without changing legacy runtimes."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path


def refresh(repo: Path) -> Path:
    scripts = repo / "packaging/skills/osverify/scripts"
    target = scripts / "review-runtime"
    if target.exists() and not (target / "bundle-manifest.json").is_file():
        raise ValueError("Refusing to replace unrecognized review runtime")
    with tempfile.TemporaryDirectory(prefix="review-runtime-", dir=scripts) as temp:
        staged = Path(temp) / "runtime"
        origins = {}
        for name in ("osverifier", "osverifier_reports"):
            shutil.copytree(repo / "src" / name, staged / name,
                            ignore=shutil.ignore_patterns("integrations", "__pycache__", "*.pyc"))
        # The skill uses the same command line as `osverifier` (verify, ocr, report, ...).
        files = {}
        for path in sorted(staged.rglob("*")):
            if not path.is_file():
                continue
            relative = str(path.relative_to(staged))
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            origins[relative] = "src/" + relative
        from provenance import source_provenance

        manifest = {"format": 1, **source_provenance(repo),
                    "scope": "solution and paper review (native or checked PDF, math/AI) with literature tools; no generation",
                    "files": files, "origins": origins}
        (staged / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(staged), target)
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
