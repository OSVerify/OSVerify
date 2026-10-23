"""Reference files supplied with a solution review, frozen with it and read by reference roles.

A supplied file is source material for the review policy's citation procedure, never proof,
a verified premise, or instructions. The files are copied into the run directory and their
hashes enter the run fingerprint. Only the proposer, issue validator and feedback auditor can
read them, through the ``--supplied`` mode of the literature MCP helper
(``osverifier_reports.reference_tools``); they are not placed in any call workspace.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .storage import FrozenRunMismatch, atomic_json, read_json

# Roles that judge cited material. Segmentation and deduplication never read sources.
REFERENCE_ROLES = frozenset({"proposer", "issue_validator", "feedback_auditor"})
SNAPSHOT = "supplied-references"
# Per-attempt artifacts of the reader: its tool journal and derived PDF text/page images.
JOURNAL = "supplied-reference-calls.jsonl"
READS = "supplied-reference-reads"


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finder_metadata(path: Path) -> bool:
    """macOS folder metadata, which Finder may add to any folder it opens; never a source."""
    return path.name == ".DS_Store" or path.name.startswith("._")


def snapshot_files(source: Path) -> dict[str, str]:
    """Relative POSIX name -> SHA-256 of every regular file; links are refused."""
    if not source.is_dir():
        raise ValueError(f"reference directory does not exist: {source}")
    files = {}
    for path in sorted(source.rglob("*")):
        if finder_metadata(path):
            continue
        if path.is_symlink():
            raise ValueError("reference snapshots must not contain symlinks")
        if path.is_file():
            files[path.relative_to(source).as_posix()] = file_sha(path)
        elif not path.is_dir():
            raise ValueError(f"reference snapshots hold only regular files: {path}")
    if not files:
        raise ValueError("reference directory contains no files")
    return files


def file_format(data: bytes) -> str:
    """How the reader can present a file, decided by its content rather than its name."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        return "image"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "unsupported"
    if text.strip() and not any(ord(c) < 32 and c not in "\t\r\n\f" for c in text):
        return "text"
    return "unsupported"


def describe(name: str, path: Path) -> dict:
    data = path.read_bytes()
    return {
        "name": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "format": file_format(data),
    }


def reader_fingerprint() -> dict[str, str]:
    """The reader lives outside this package, so its code is recorded explicitly."""
    from osverifier_reports import reference_tools, references
    from osverifier_reports.fields import code_sha256

    return {Path(m.__file__).name: code_sha256(Path(m.__file__)) for m in (reference_tools, references)}


class SuppliedReferences:
    """A directory of user-supplied files, hashed when constructed."""

    def __init__(self, source):
        self.source = Path(source).expanduser().resolve()
        self.files = snapshot_files(self.source)

    @property
    def provenance(self) -> dict:
        return {"files": self.files, "reader": reader_fingerprint()}

    def freeze(self, run_dir: Path) -> list[dict]:
        """Copy the files into the run once, then verify that copy; return its catalogue.

        Call only after the run manifest has accepted this snapshot's provenance.
        """
        snapshot = run_dir / SNAPSHOT
        catalogue = snapshot / "catalogue.json"
        if not catalogue.exists():
            entries = []
            for name in self.files:
                copy = snapshot / "files" / name
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.source / name, copy)
                entries.append(describe(name, copy))
            if {e["name"]: e["sha256"] for e in entries} != self.files:
                raise FrozenRunMismatch("supplied references changed while they were copied")
            atomic_json(catalogue, {"schema_version": 1, "references": entries})
        return verified_catalogue(snapshot, self.files)


def verified_catalogue(snapshot: Path, files: dict[str, str]) -> list[dict]:
    """The frozen catalogue, after checking it and every copied file against ``files``."""
    entries = read_json(snapshot / "catalogue.json")["references"]
    if [e["name"] for e in entries] != list(files):
        raise FrozenRunMismatch("supplied reference catalogue changed")
    stored = snapshot / "files"
    present = {
        p.relative_to(stored).as_posix()
        for p in stored.rglob("*")
        if not p.is_dir() and not finder_metadata(p)
    }
    if present != set(files):
        raise FrozenRunMismatch("frozen supplied references were added or removed")
    for entry, (name, expected) in zip(entries, files.items()):
        path = stored / name
        if path.is_symlink() or not path.is_file():
            raise FrozenRunMismatch(f"frozen supplied reference changed: {name}")
        actual = describe(name, path)
        if actual["sha256"] != expected:
            raise FrozenRunMismatch(f"frozen supplied reference changed: {name}")
        if entry != actual:
            raise FrozenRunMismatch("supplied reference catalogue changed")
    return entries


class SuppliedReferenceBackend:
    """Give one run's reference roles read access to its frozen snapshot.

    Other roles, and backends without ``generate_with_references`` (scripted test
    backends), use the plain call. Everything else is the wrapped backend's.
    """

    def __init__(self, backend, snapshot: Path, reader: dict[str, str]):
        self.backend, self.snapshot, self.reader = backend, Path(snapshot), reader

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def generate(self, role, payload, schema, attempt_dir):
        generate = getattr(self.backend, "generate_with_references", None)
        if role not in REFERENCE_ROLES or "supplied_references" not in payload or generate is None:
            return self.backend.generate(role, payload, schema, attempt_dir)
        # The reader is outside the package fingerprint that JobRunner checks per call.
        if reader_fingerprint() != self.reader:
            raise FrozenRunMismatch("supplied-reference reader changed during the run")
        tools = {
            "supplied": str(self.snapshot.resolve()),
            "journal": str((attempt_dir / JOURNAL).resolve()),
            "work_dir": str((attempt_dir / READS).resolve()),
        }
        return generate(role, payload, schema, attempt_dir, reference_tools=tools)

    def attempt_artifacts(self, attempt_dir: Path) -> dict[str, str]:
        collect = getattr(self.backend, "attempt_artifacts", None)
        records = dict(collect(attempt_dir)) if collect else {}
        produced = [attempt_dir / JOURNAL, *sorted((attempt_dir / READS).rglob("*"))]
        records.update(
            {p.relative_to(attempt_dir).as_posix(): file_sha(p) for p in produced if p.is_file()}
        )
        return records
