#!/usr/bin/env python3
"""Record skill runtime manifests that a run's pinned engine may match.

``scripts/osverify.py`` executes a run directory's pinned ``.osverify-runtime`` only when
its manifest digest is listed in ``scripts/known-runtimes.json``. The refresh scripts call
``record`` after rewriting a runtime. By hand:

    python packaging/known_runtimes.py             # add the current shipped manifests
    python packaging/known_runtimes.py --history   # also every manifest in git history
    python packaging/known_runtimes.py --add DIR   # a runtime directory you built yourself
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = "packaging/skills/osverify/scripts"
RUNTIMES = ("runtime", "ocr-runtime", "review-runtime")


def _entry(repo: Path):
    spec = importlib.util.spec_from_file_location("osverify_entry", repo / SCRIPTS / "osverify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def current_manifests(repo: Path) -> list[dict]:
    return [json.loads((repo / SCRIPTS / name / "bundle-manifest.json").read_text())
            for name in RUNTIMES]


def history_manifests(repo: Path) -> list[dict]:
    """Every runtime manifest committed on any branch or tag."""
    manifests = []
    for name in RUNTIMES:
        path = f"{SCRIPTS}/{name}/bundle-manifest.json"
        commits = subprocess.check_output(
            ["git", "-C", str(repo), "log", "--all", "--format=%H", "--", path], text=True
        ).split()
        for commit in commits:
            shown = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{path}"],
                                   capture_output=True, text=True)
            if shown.returncode == 0:  # the commit deleted the manifest
                manifests.append(json.loads(shown.stdout))
    return manifests


def record(repo: Path = REPO, manifests: list[dict] | None = None) -> list[str]:
    """Add manifest digests to the allowlist; return the digests that were new."""
    entry = _entry(repo)
    path = repo / SCRIPTS / "known-runtimes.json"
    data = json.loads(path.read_text()) if path.exists() else {"format": 1, "digests": []}
    known = set(data["digests"])
    digests = {entry.manifest_digest(m) for m in (manifests or current_manifests(repo))}
    added = sorted(digests - known)
    if added or not path.exists():
        data["digests"] = sorted(known | digests)
        path.write_text(json.dumps(data, indent=2) + "\n")
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--history", action="store_true", help="include git history")
    parser.add_argument("--add", type=Path, action="append", default=[],
                        help="a runtime directory containing bundle-manifest.json")
    args = parser.parse_args()
    manifests = current_manifests(REPO)
    if args.history:
        manifests += history_manifests(REPO)
    for directory in args.add:
        _entry(REPO).check_bundle(directory)  # its files must match its manifest
        manifests.append(json.loads((directory / "bundle-manifest.json").read_text()))
    added = record(REPO, manifests)
    print(f"{len(added)} new runtime digest(s) recorded")


if __name__ == "__main__":
    main()
