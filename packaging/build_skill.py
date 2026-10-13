#!/usr/bin/env python3
"""Build a portable verification-only skill, including preserved legacy snapshots."""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import tempfile
import zipfile
from pathlib import Path


def build(repo: Path, output: Path) -> Path:
    output = output.resolve()
    source = repo / "packaging/skills/osverify"
    if output == source.resolve() or source.resolve() in output.parents:
        raise ValueError("output must not replace the maintained skill source")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="osverify-build-", dir=output.parent) as temp:
        skill = Path(temp) / "osverify"
        shutil.copytree(source, skill, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # Distribute the checked-in snapshot, never untested worktree features.
        spec = importlib.util.spec_from_file_location("skill_entry", skill / "scripts/osverify.py")
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        known = set(json.loads((skill / "scripts/known-runtimes.json").read_text())["digests"])
        for name in ("runtime", "ocr-runtime", "review-runtime"):
            entry.check_bundle(skill / "scripts" / name)
            # Runs pin a copy of the runtime; osverify.py only runs pins it knows.
            manifest = json.loads((skill / "scripts" / name / "bundle-manifest.json").read_text())
            if entry.manifest_digest(manifest) not in known:
                raise ValueError(f"{name} is not in known-runtimes.json; run "
                                 "`python packaging/known_runtimes.py` and commit the result")
        if output.exists():
            # Replace only a directory recognizable as our generated artifact.
            if not (output / "scripts/runtime/bundle-manifest.json").is_file():
                raise ValueError("refusing to replace an unrecognized output directory")
            shutil.rmtree(output)
        shutil.move(str(skill), output)
    archive = output.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(output.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                bundle.write(path, Path(output.name) / path.relative_to(output))
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    output = args.output or repo / "dist/osverify"
    print(build(repo, output))


if __name__ == "__main__":
    main()
