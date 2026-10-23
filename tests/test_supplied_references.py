"""User-supplied reference files: frozen provenance, role access, reader and transport.

Scripted backends establish plumbing only, never that a supplied source was judged correctly.
"""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from test_workflow import FixtureBackend, request

from osverifier import cli
from osverifier.backend import ROLE_SKILLS, CodexBackend
from osverifier.pipeline import RunConfig, Verifier
from osverifier.storage import FrozenRunMismatch, read_json
from osverifier.supplied_references import (
    JOURNAL,
    SNAPSHOT,
    SuppliedReferenceBackend,
    SuppliedReferences,
    file_format,
    reader_fingerprint,
)
from osverifier.workflow import DecomposingVerifier, WorkflowConfig
from osverifier_reports import references
from osverifier_reports.reference_tools import SuppliedReferenceTools, tool_config

PAPER = "Smith (1962), On sequences.\nLemma 1. Every bounded sequence has a limit point.\nLemma 2. Every monotone bounded sequence converges.\n"


def supplied(tmp_path, files=None):
    source = tmp_path / "refs"
    for name, body in (files or {"smith1962.txt": PAPER}).items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body if isinstance(body, bytes) else body.encode())
    return source


class ReadingBackend(FixtureBackend):
    """Reference roles read the supplied file through the actual reader before answering."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tool_calls = []

    def generate_with_references(self, role, payload, schema, attempt_dir, *, reference_tools):
        self.tool_calls.append((role, reference_tools))
        tools = SuppliedReferenceTools(
            reference_tools["supplied"], reference_tools["journal"], reference_tools["work_dir"]
        )
        listed = tools.call("list_supplied_references", {})["references"]
        passage = tools.call(
            "read_supplied_reference", {"reference": listed[0]["name"], "query": "Lemma 2"}
        )
        assert passage["text_match_found"] and "monotone bounded" in passage["text"]
        return self.generate(role, payload, schema, attempt_dir)


def workflow(backend, tmp_path, references):
    config = WorkflowConfig(workers=1, max_attempts=1)
    return DecomposingVerifier(backend, config).verify(request(), tmp_path / "run", references)


def test_reference_roles_read_the_frozen_snapshot_and_structural_roles_do_not(tmp_path):
    source = supplied(tmp_path)
    backend = ReadingBackend({"root": ["gap"], "node-0001": ["another gap"]})
    report = workflow(backend, tmp_path, source)
    assert report.decision == "REVISE" and report.run_status == "completed"
    run = tmp_path / "run"
    catalogue = [{"name": "smith1962.txt", "sha256": SuppliedReferences(source).files["smith1962.txt"],
                  "bytes": len(PAPER.encode()), "format": "text"}]
    assert read_json(run / SNAPSHOT / "catalogue.json")["references"] == catalogue
    assert (run / SNAPSHOT / "files/smith1962.txt").read_text() == PAPER
    provenance = read_json(run / "manifest.json")["provenance"]["supplied_references"]
    assert provenance["files"] == {"smith1962.txt": catalogue[0]["sha256"]}
    assert set(provenance["reader"]) == {"reference_tools.py", "references.py"}
    for role, payload in backend.calls:
        reads = role in ("proposer", "issue_validator", "feedback_auditor")
        assert (payload.get("supplied_references") == catalogue) is reads, role
        assert ("supplied_references" not in payload) is not reads, role
    assert sorted({role for role, _ in backend.tool_calls}) == ["feedback_auditor", "issue_validator", "proposer"]
    assert all(set(tools) == {"supplied", "journal", "work_dir"} for _, tools in backend.tool_calls)
    # Every journal of what a role read is bound to its cached job.
    result = read_json(run / "jobs/root-proposer/result.json")
    journal = f"{result['attempt']}/{JOURNAL}"
    assert journal in result["auxiliary_artifacts"]
    rows = [json.loads(line) for line in (run / "jobs/root-proposer" / journal).read_text().splitlines()]
    assert [row["tool"] for row in rows] == ["list_supplied_references", "read_supplied_reference"]
    # Resume with the same files replays every cached job.
    fresh = ReadingBackend()
    assert workflow(fresh, tmp_path, source) == report and fresh.calls == []


def test_runs_without_references_keep_their_inputs_and_provenance(tmp_path):
    backend = ReadingBackend({"root": ["gap"]})
    workflow(backend, tmp_path, None)
    assert backend.tool_calls == []
    assert "supplied_references" not in read_json(tmp_path / "run/manifest.json")["provenance"]
    assert all("supplied_references" not in payload for _, payload in backend.calls)
    assert not (tmp_path / "run" / SNAPSHOT).exists()


@pytest.mark.parametrize("change", ["source", "omitted", "added_later", "frozen_copy", "journal"])
def test_snapshot_changes_require_a_new_run(tmp_path, change):
    source = supplied(tmp_path)
    if change == "added_later":
        workflow(FixtureBackend({"root": ["gap"]}), tmp_path, None)
    else:
        workflow(ReadingBackend({"root": ["gap"]}), tmp_path, source)
    run = tmp_path / "run"
    if change == "source":
        (source / "smith1962.txt").write_text(PAPER + "Lemma 3.\n")
    elif change == "frozen_copy":
        (run / SNAPSHOT / "files/smith1962.txt").write_text("Lemma 2 is false.\n")
    elif change == "journal":
        result = read_json(run / "jobs/root-proposer/result.json")
        with (run / "jobs/root-proposer" / result["attempt"] / JOURNAL).open("a") as journal:
            journal.write("{}\n")
    with pytest.raises(FrozenRunMismatch):
        workflow(ReadingBackend(), tmp_path, None if change == "omitted" else source)


def unfinished(tmp_path, source):
    backend = ReadingBackend({"root": ["gap"]})
    backend.bad_partition = True  # leaves the run incomplete, so no completion checkpoint
    assert workflow(backend, tmp_path, source).run_status == "incomplete"
    return tmp_path / "run"


@pytest.mark.parametrize("change,message", [
    ("frozen_copy", "frozen supplied reference changed"),
    ("added_copy", "added or removed"),
    ("catalogue_format", "catalogue changed"),
    ("journal", "cached auxiliary artifact changed"),
])
def test_unfinished_run_detects_changed_reference_artifacts_before_any_call(tmp_path, change, message):
    source = supplied(tmp_path)
    run = unfinished(tmp_path, source)
    (run / "interruption.json").write_text('{"reason": "quota_exhausted"}')
    if change == "frozen_copy":
        (run / SNAPSHOT / "files/smith1962.txt").write_text("Lemma 2 is false.\n")
    elif change == "added_copy":
        (run / SNAPSHOT / "files/extra.txt").write_text("Unrecorded source.\n")
    elif change == "catalogue_format":
        catalogue = read_json(run / SNAPSHOT / "catalogue.json")
        catalogue["references"][0]["format"] = "unsupported"
        (run / SNAPSHOT / "catalogue.json").write_text(json.dumps(catalogue))
    else:
        result = read_json(run / "jobs/root-proposer/result.json")
        (run / "jobs/root-proposer" / result["attempt"] / JOURNAL).write_text("{}\n")
    fresh = ReadingBackend()
    with pytest.raises(FrozenRunMismatch, match=message):
        workflow(fresh, tmp_path, source)
    assert fresh.calls == []
    if change != "journal":  # a refused snapshot keeps the earlier quota record
        assert (run / "interruption.json").exists()


def test_finder_metadata_is_never_a_reference_and_does_not_block_resume(tmp_path):
    source = supplied(tmp_path, {"smith1962.txt": PAPER, ".DS_Store": b"\x00\x00\x00\x01Bud1"})
    assert set(SuppliedReferences(source).files) == {"smith1962.txt"}
    run = unfinished(tmp_path, source)
    assert not (run / SNAPSHOT / "files/.DS_Store").exists()
    (run / SNAPSHOT / "files/._smith1962.txt").write_bytes(b"\x00\x05\x16\x07")
    (run / SNAPSHOT / ".DS_Store").write_bytes(b"\x00")
    (source / "._smith1962.txt").write_bytes(b"\x00\x05\x16\x07")
    resumed = ReadingBackend({"root": ["gap"]})
    resumed.bad_partition = True
    assert workflow(resumed, tmp_path, source).run_status == "incomplete"


def test_reader_code_change_during_a_run_stops_reference_calls(tmp_path):
    backend = SuppliedReferenceBackend(ReadingBackend(), tmp_path / SNAPSHOT, {"reference_tools.py": "old"})
    with pytest.raises(FrozenRunMismatch, match="reader changed"):
        backend.generate("proposer", {"supplied_references": []}, {}, tmp_path / "attempt")


def test_root_only_verifier_gives_every_role_the_snapshot(tmp_path):
    source = supplied(tmp_path)
    backend = ReadingBackend({"whole_solution": ["gap"]})
    verifier = Verifier(backend, RunConfig(workers=1, max_attempts=1))
    report = verifier.verify(request(), tmp_path / "run", references=source)
    assert report.decision == "REVISE"
    assert [role for role, _ in backend.tool_calls] == ["proposer", "issue_validator", "feedback_auditor"]
    assert all(payload["supplied_references"][0]["name"] == "smith1962.txt" for _, payload in backend.calls)
    with pytest.raises(FrozenRunMismatch):
        verifier.verify(request(), tmp_path / "run")


def test_snapshot_refuses_links_special_and_empty_directories(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        SuppliedReferences(tmp_path / "missing")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="no files"):
        SuppliedReferences(tmp_path / "empty")
    source = supplied(tmp_path)
    (source / "link.txt").symlink_to(source / "smith1962.txt")
    with pytest.raises(ValueError, match="symlinks"):
        SuppliedReferences(source)


def test_format_is_decided_by_content(tmp_path):
    cases = {"a.pdf": b"%PDF-1.4 fixture", "b.dat": b"\x89PNG\r\n\x1a\nfixture", "c.jpg": b"\xff\xd8\xff\xe0",
             "d.pdf": "Plain text despite its name.\n".encode(), "e.tex": "caf\xe9".encode("latin-1"),
             "f.bin": b"\x00\x01\x02", "g.txt": b"   \n"}
    expected = {"a.pdf": "pdf", "b.dat": "image", "c.jpg": "image", "d.pdf": "text",
                "e.tex": "unsupported", "f.bin": "unsupported", "g.txt": "unsupported"}
    assert {name: file_format(data) for name, data in cases.items()} == expected


def reader(tmp_path, files):
    source = supplied(tmp_path, files)
    snapshot = SuppliedReferences(source).freeze(tmp_path / "run") and tmp_path / "run" / SNAPSHOT
    return SuppliedReferenceTools(snapshot, tmp_path / "calls.jsonl", tmp_path / "reads")


def test_reader_returns_numbered_passages_and_refuses_other_names(tmp_path):
    tools = reader(tmp_path, {"notes/smith1962.txt": PAPER, "data.bin": b"\x00\x01"})
    listed = tools.call("list_supplied_references", {})
    assert [r["name"] for r in listed["references"]] == ["data.bin", "notes/smith1962.txt"]
    assert "not instructions" in listed["notice"]
    passage = tools.call("read_supplied_reference", {"reference": "notes/smith1962.txt", "query": "monotone"})
    assert passage["status"] == "passage" and passage["matches"] == [3]
    assert passage["text"].splitlines()[2] == "3: Lemma 2. Every monotone bounded sequence converges."
    window = tools.read_supplied_reference("notes/smith1962.txt", start_line=2, max_lines=1)
    assert window["text"] == "2: Lemma 1. Every bounded sequence has a limit point." and window["next_start_line"] == 3
    missing = tools.read_supplied_reference("notes/smith1962.txt", query="Theorem 9")
    assert missing["status"] == "passage_not_found" and "does not prove absence" in missing["notice"]
    assert tools.read_supplied_reference("data.bin")["status"] == "unreadable"
    for name in ("../refs/notes/smith1962.txt", "/etc/hosts", "smith1962.txt"):
        with pytest.raises(ValueError, match="Unknown supplied reference"):
            tools.read_supplied_reference(name)
    with pytest.raises(ValueError, match="page applies only"):
        tools.read_supplied_reference("notes/smith1962.txt", page=1)
    (tmp_path / "run" / SNAPSHOT / "files/notes/smith1962.txt").write_text("Lemma 2 is false.\n")
    with pytest.raises(ValueError, match="changed after it was frozen"):
        tools.read_supplied_reference("notes/smith1962.txt")
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [row["tool"] for row in rows] == ["list_supplied_references", "read_supplied_reference"]


@pytest.mark.parametrize("scan", [False, True])
def test_reader_extracts_pdf_text_and_renders_pages_outside_the_snapshot(tmp_path, monkeypatch, scan):
    calls = []

    def poppler(command, **kwargs):
        calls.append(command[0])
        if command[0] == "pdftotext":
            # pdftotext writes only page breaks for a scan without a text layer.
            Path(command[-1]).write_text("\f\f" if scan else "Page one.\fLemma 2. Monotone bounded sequences converge.\f")
        else:
            Image.new("RGB", (20, 20), "white").save(command[-1] + ".png")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(references.subprocess, "run", poppler)
    tools = reader(tmp_path, {"smith1962.pdf": b"%PDF-fixture"})
    first = tools.read_supplied_reference("smith1962.pdf", query="Lemma 2")
    second = tools.read_supplied_reference("smith1962.pdf", page=2)
    if scan:
        assert first["page_images"] and first["processing_errors"][0]["kind"] == "empty_output"
        assert not first["text_match_found"] and first["status"] == "passage"
    else:
        assert first["text_match_found"] and not first["page_images"]
        assert second["text"] == "1: Lemma 2. Monotone bounded sequences converge."
    assert second["page_images"][0]["page"] == 2 and second["page_image_status"] == "available"
    assert calls.count("pdftotext") == 1  # extracted once per call process
    rendered = Path(second["page_images"][0]["path"])
    assert rendered.is_relative_to(tmp_path / "reads") and rendered.is_file()
    assert sorted(p.name for p in (tmp_path / "run" / SNAPSHOT / "files").iterdir()) == ["smith1962.pdf"]


def test_reader_returns_image_pixels_and_scales_large_photographs(tmp_path):
    small, large = tmp_path / "small.png", tmp_path / "large.jpg"
    Image.new("RGB", (40, 30), "white").save(small)
    Image.new("RGB", (4000, 3000), "white").save(large)
    tools = reader(tmp_path, {"page.png": small.read_bytes(), "photo.jpg": large.read_bytes()})
    kept = tools.read_supplied_reference("page.png")["page_images"][0]
    assert kept["mime_type"] == "image/png" and Path(kept["path"]).read_bytes() == small.read_bytes()
    scaled = tools.read_supplied_reference("photo.jpg")["page_images"][0]
    assert scaled["mime_type"] == "image/png" and Path(scaled["path"]).is_relative_to(tmp_path / "reads")
    with Image.open(scaled["path"]) as image:
        assert max(image.size) == 2000
    with pytest.raises(ValueError, match="only page 1"):
        tools.read_supplied_reference("photo.jpg", page=2)


def test_stdio_server_lists_only_supplied_reference_tools(tmp_path):
    tools = reader(tmp_path, {"smith1962.txt": PAPER})
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "read_supplied_reference", "arguments": {"reference": "smith1962.txt", "query": "monotone"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "read_reference", "arguments": {"reference": "https://example.org"}}},
    ]
    result = subprocess.run(
        [sys.executable, "-m", "osverifier_reports.reference_tools", "--supplied", str(tools.snapshot),
         "--journal", str(tmp_path / "server.jsonl"), "--work-dir", str(tmp_path / "server-reads")],
        input="\n".join(map(json.dumps, requests)) + "\n", text=True, capture_output=True, check=True,
    )
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert [t["name"] for t in responses[1]["result"]["tools"]] == ["list_supplied_references", "read_supplied_reference"]
    assert "monotone bounded" in responses[2]["result"]["content"][0]["text"]
    assert responses[3]["result"]["isError"]


def test_tool_config_adds_the_supplied_server_next_to_literature_tools(tmp_path):
    descriptor = {"supplied": str(tmp_path / "snap"), "journal": str(tmp_path / "j"), "work_dir": str(tmp_path / "w")}
    config = tool_config(descriptor)
    assert not any(key.startswith("mcp_servers.osverify_literature.") for key in config)
    args = config["mcp_servers.osverify_supplied_references.args"]
    assert args[:2] == ["-m", "osverifier_reports.reference_tools"]
    assert args[args.index("--supplied") + 1] == descriptor["supplied"]
    assert config["mcp_servers.osverify_supplied_references.enabled_tools"] == [
        "list_supplied_references", "read_supplied_reference"]
    both = tool_config({**descriptor, "store": str(tmp_path / "store")})
    assert both["mcp_servers.osverify_literature.enabled_tools"] == ["search_literature", "resolve_reference", "read_reference"]
    assert both["mcp_servers.osverify_supplied_references.args"] == args


@pytest.mark.parametrize("role", ["proposer", "issue_validator", "feedback_auditor", "decomposer"])
def test_codex_command_wires_the_reader_only_for_reference_roles(tmp_path, role):
    from test_codex_transport import fake_codex as fixture

    binary = fixture.__wrapped__(tmp_path)
    binary.write_text(binary.read_text().replace("audit-verifier-feedback", ROLE_SKILLS[role]))
    snapshot = tmp_path / SNAPSHOT
    backend = SuppliedReferenceBackend(
        CodexBackend(model="fixture", codex_bin=str(binary), codex_home=tmp_path / "home"), snapshot,
        reader_fingerprint())
    attempt = tmp_path / "attempt"
    backend.generate(role, {"supplied_references": []}, {"type": "object"}, attempt)
    argv = read_json(attempt / "command.json")["argv"]
    config = " ".join(argv)
    assert ("mcp_servers.osverify_supplied_references.command=" in config) is (role != "decomposer")
    assert "mcp_servers.osverify_literature" not in config
    if role != "decomposer":
        assert f"--supplied\", \"{snapshot.resolve()}\"" in config
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert backend.identity == backend.backend.identity


def test_cli_passes_references_to_solution_review_only(tmp_path, monkeypatch, capsys):
    question, solution = tmp_path / "q.md", tmp_path / "s.md"
    question.write_text("Question.\n")
    solution.write_text("Solution.\n")
    seen = {}

    class Recorder:
        def __init__(self, backend, config):
            pass

        def verify(self, request, run_dir, references=None):
            seen.update(run_dir=run_dir, references=references)
            return SimpleNamespace(run_status="incomplete", model_dump=lambda mode: {})

    class LegacyRecorder(Recorder):
        def verify(self, request, run_dir, *, proposals=None, references=None):
            return super().verify(request, run_dir, references=references)

    monkeypatch.setattr(cli, "CodexBackend", lambda **kwargs: None)
    monkeypatch.setattr(cli, "DecomposingVerifier", Recorder)
    monkeypatch.setattr(cli, "Verifier", LegacyRecorder)
    base = ["verify", "--question", str(question), "--solution", str(solution), "--run-dir", str(tmp_path / "run")]
    for workflow_flag in (["--decompose"], []):
        seen.clear()
        assert cli.main([*base, *workflow_flag, "--references", str(tmp_path / "refs")]) == 2
        assert seen["references"] == tmp_path / "refs"
    assert cli.main([*base, "--review-type", "paper", "--references", str(tmp_path / "refs")]) == 1
    assert "--references applies only to solution review" in capsys.readouterr().err
