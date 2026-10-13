#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2.10,<3", "mistune>=3,<4", "pillow>=11,<13"]
# ///
"""Portable entry point for the bundled OSVerify snapshot."""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
RUNTIME = SCRIPTS / "review-runtime"
# Digests of every runtime manifest this skill has shipped. A run's pinned engine is
# executed only when it is a copy of one of them (packaging/known_runtimes.py adds new ones).
KNOWN_RUNTIMES = SCRIPTS / "known-runtimes.json"
# Commands of earlier skill builds, still accepted for their runs. Current usage
# is the `osverify` command line: verify (solutions and papers, incl. PDF), ocr, report.
LEGACY_COMMANDS = ("prepare-paper", "prepare-pdf", "verify-pdf")


def manifest_digest(manifest: dict) -> str:
    """Digest of the code a runtime manifest pins (its file hashes only)."""
    files = json.dumps(manifest["files"], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(files.encode()).hexdigest()


def check_bundle(runtime=RUNTIME) -> None:
    manifest = json.loads((runtime / "bundle-manifest.json").read_text())
    actual, stray = {}, []
    for path in runtime.rglob("*"):
        parts = path.relative_to(runtime).parts
        if not path.is_file() or path.name == "bundle-manifest.json" or "__pycache__" in parts:
            continue
        if path.suffix == ".pyc":
            stray.append(path)  # sourceless bytecode would be imported instead of the source
            continue
        actual[str(path.relative_to(runtime))] = hashlib.sha256(path.read_bytes()).hexdigest()
    if stray or actual != manifest["files"]:
        raise RuntimeError("Bundled engine changed; rebuild the skill before using it.")


def check_known(runtime: Path) -> None:
    """A run's pinned engine must be a copy of a runtime this skill shipped."""
    manifest = json.loads((runtime / "bundle-manifest.json").read_text())
    known = set(json.loads(KNOWN_RUNTIMES.read_text())["digests"])
    if manifest_digest(manifest) not in known:
        raise RuntimeError(
            f"Refusing to run the engine pinned in {runtime}: it matches no OSVerify runtime "
            "this skill has shipped, so it may not be OSVerify code. Start a fresh run directory. "
            "If you built that engine yourself, record it from the OSVerify repository with "
            f"`python packaging/known_runtimes.py --add {runtime}`."
        )


def select_runtime(args):
    # Explicit legacy mode never creates or migrates a run snapshot.
    if args and args[0] == "--legacy-runtime":
        args.pop(0)
        pdf = bool(args and args[0] in ("prepare-pdf", "verify-pdf"))
        return RUNTIME.with_name("ocr-runtime" if pdf else "runtime")
    if not args or "--help" in args or "-h" in args:
        return RUNTIME
    run_dir = next((arg.split("=", 1)[1] for arg in args if arg.startswith("--run-dir=")), None)
    if "--run-dir" in args and args.index("--run-dir") + 1 < len(args):
        run_dir = args[args.index("--run-dir") + 1]
    if not run_dir:
        return RUNTIME
    directory = Path(run_dir).expanduser().resolve()
    pinned = directory / ".osverify-runtime"
    if pinned.exists():
        return pinned
    # Never add a new engine to an old run or silently upgrade its preparation.
    markers = ("manifest.json", ".verification", "workflow.json", "paper-task.json",
               "report.json", "preparation.json", "ocr-manifest.json", "inputs/extraction.json",
               "paper-inputs/extraction.json", "pdf-input/preparation.json")
    if any((directory / name).exists() for name in markers):
        raise RuntimeError("Existing run has no pinned skill runtime. Use its original engine; "
                           "for the preserved legacy skill use --legacy-runtime before the command. "
                           "Start a fresh run to use literature tools.")
    if args[0] not in ("verify", "demo", "ocr") + LEGACY_COMMANDS:
        return RUNTIME
    check_bundle(RUNTIME)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".osverify-pin-", dir=directory) as temp:
        copy = Path(temp) / "runtime"
        shutil.copytree(RUNTIME, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        check_bundle(copy)
        try:
            copy.rename(pinned)
        except OSError:
            # Another invocation may have pinned the same run first; never replace it.
            if not pinned.is_dir():
                raise
    return pinned


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    runtime = select_runtime(args)
    ocr = bool(args and args[0] in ("prepare-pdf", "verify-pdf"))
    check_bundle(runtime)
    if runtime.resolve().parent != SCRIPTS:
        check_known(runtime)
    # Execute only the checked sources: never read bytecode found next to them, here or in
    # helper subprocesses (a crafted __pycache__ entry would otherwise replace a module).
    sys.dont_write_bytecode = True
    sys.pycache_prefix = tempfile.mkdtemp(prefix="osverify-no-bytecode-")
    atexit.register(shutil.rmtree, sys.pycache_prefix, ignore_errors=True)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONPYCACHEPREFIX"] = sys.pycache_prefix
    sys.path.insert(0, str(runtime))
    # Helper subprocesses must import the same snapshot even outside this skill.
    os.environ["PYTHONPATH"] = str(runtime) + (
        os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
    )
    if ocr:
        from osverifier_reports.ocr import main as pdf_review
        return pdf_review(args)
    if args and args[0] == "prepare-paper":
        try:  # engines pinned by earlier builds carry the extractor here
            from osverifier_gui.paper import main as prepare
        except ImportError:
            from osverifier_reports.paper_prepare import main as prepare
        return prepare(args[1:])
    from osverifier.cli import main as osverifier
    return osverifier(args)


if __name__ == "__main__":
    raise SystemExit(main())
