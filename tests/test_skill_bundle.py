"""Verify that the distributable skill runs without the source checkout."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("build_skill", REPO / "packaging/build_skill.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path_factory.mktemp("skill") / "osverify"
    archive = module.build(REPO, output)
    # Exercise the distributed zip at a different location, not the build tree.
    relocated = tmp_path_factory.mktemp("relocated")
    with zipfile.ZipFile(archive) as z:
        z.extractall(relocated)
    return relocated / "osverify"


def run(bundle, cwd, *args):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(bundle / "scripts/osverify.py"), *map(str, args)],
        cwd=cwd, env=env, text=True, capture_output=True, check=True,
    )


def test_bundle_contains_verifier_without_generation(bundle, tmp_path):
    manifest = json.loads((bundle / "scripts/runtime/bundle-manifest.json").read_text())
    assert not any("integrations/" in p or "rethlas" in p.lower() for p in manifest["files"])
    assert not any("ai_skills" in p or "extract-ai" in p for p in manifest["files"])
    # The default engine has the same command line as `osverifier`.
    result = run(bundle, tmp_path, "verify", "--help")
    assert "--mode {full_solution,partial}" in result.stdout
    assert "--targets {smart,lazy,manual,full}" in result.stdout and "--ocr-dir" in result.stdout
    for command in ("ocr", "report"):
        assert f"osverify {command}" in run(bundle, tmp_path, command, "--help").stdout


def test_relocated_demo_and_export(bundle, tmp_path):
    directory = tmp_path / "review"
    run(bundle, tmp_path, "demo", "--run-dir", directory)
    report = json.loads((directory / "report.json").read_text())
    assert report["run_status"] == "completed"
    assert report["decision"] == "REVISE"
    assert report["backend_kind"] == "scripted_demo"
    destination = tmp_path / "approved.md"
    run(bundle, tmp_path, "export", "--run-dir", directory,
        "--format", "markdown", "--output", destination)
    assert destination.read_text().strip()


def test_paper_scope_and_resume(bundle, tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    paper = inputs / "paper.tex"
    paper.write_text(r"\begin{theorem}\label{thm:main}Every even integer is divisible by two.\end{theorem}")
    args = ("prepare-paper", "--paper", paper, "--mode", "manual",
            "--run-dir", tmp_path, "--tags", "thm:main")
    run(bundle, tmp_path, *args)
    before = (inputs / "extraction.json").read_bytes()
    run(bundle, tmp_path, *args)
    assert (inputs / "extraction.json").read_bytes() == before
    assert (inputs / "solution.md").read_text() == paper.read_text()
    assert "Every even integer" in (inputs / "question.md").read_text()


def test_modified_bundle_is_rejected(bundle, tmp_path):
    import shutil

    changed = tmp_path / "changed"
    shutil.copytree(bundle, changed)
    with (changed / "scripts/review-runtime/osverifier/models.py").open("a") as f:
        f.write("\n# altered snapshot\n")
    with pytest.raises(subprocess.CalledProcessError) as failure:
        run(changed, tmp_path, "verify", "--help")
    assert "Bundled engine changed" in failure.value.stderr


def test_pdf_commands_use_separate_checked_runtime(bundle, tmp_path):
    original = json.loads((bundle / "scripts/runtime/bundle-manifest.json").read_text())
    pdf = json.loads((bundle / "scripts/ocr-runtime/bundle-manifest.json").read_text())
    assert "osverifier_reports/ocr.py" not in original["files"]
    assert "osverifier_reports/ocr.py" in pdf["files"]
    assert not any("integrations/" in name for name in pdf["files"])
    for command in ("prepare-pdf", "verify-pdf"):
        result = run(bundle, tmp_path, command, "--help")
        assert "--codex-home" in result.stdout
    result = run(bundle, tmp_path, "verify-pdf", "--help")
    assert "--ocr-manifest" in result.stdout and "--field" not in result.stdout


def test_pdf_runtime_tampering_does_not_change_native_runtime(bundle, tmp_path):
    import shutil

    changed = tmp_path / "changed-pdf"
    shutil.copytree(bundle, changed)
    with (changed / "scripts/ocr-runtime/osverifier_reports/ocr.py").open("a") as f:
        f.write("\n# altered\n")
    with pytest.raises(subprocess.CalledProcessError) as failure:
        run(changed, tmp_path, "--legacy-runtime", "prepare-pdf", "--help")
    assert "Bundled engine changed" in failure.value.stderr
    run(changed, tmp_path, "verify", "--help")


@pytest.mark.parametrize("runtime", ["runtime", "ocr-runtime", "review-runtime"])
def test_bundled_scheduler_matches_tested_source_and_default(bundle, tmp_path, runtime):
    engine = bundle / "scripts" / runtime
    for name in ("scheduling.py", "workflow.py", "pipeline.py", "workflow_models.py"):
        assert (engine / "osverifier" / name).read_bytes() == (REPO / "src/osverifier" / name).read_bytes()
    # Import only the relocated engine, not this checkout's editable install.
    code = """
import sys
sys.path.insert(0, sys.argv[1])
from osverifier.cli import parser
from osverifier.workflow import WorkflowConfig
from osverifier.scheduling import FEEDBACK, VALIDATION, DEDUPLICATION, VERIFY
args = parser().parse_args(['verify', '--request', 'in.json', '--run-dir', 'out', '--model', 'fixture'])
assert args.workers == WorkflowConfig().workers == 16
assert FEEDBACK < VALIDATION < DEDUPLICATION < VERIFY
"""
    subprocess.run([sys.executable, "-c", code, str(engine)], cwd=tmp_path, check=True)


def test_default_runtime_contains_exact_literature_source(bundle, tmp_path):
    engine = bundle / "scripts/review-runtime"
    manifest = json.loads((engine / "bundle-manifest.json").read_text())
    assert not any("integrations/" in p or "rethlas" in p.lower() for p in manifest["files"])
    for name in ("references.py", "reference_html.py", "reference_tools.py", "ocr.py", "fields.py"):
        assert (engine / "osverifier_reports" / name).read_bytes() == (REPO / "src/osverifier_reports" / name).read_bytes()
    code = """
import sys
sys.path.insert(0, sys.argv[1])
from osverifier_reports.reference_tools import tool_config
from osverifier_reports.references import ReferenceStore
store = ReferenceStore(sys.argv[2])
assert store.timeout_policy == {'idle_seconds': 20.0, 'download_seconds': 300.0}
config = tool_config({'store': sys.argv[2], 'journal': sys.argv[2] + '/calls.jsonl'})
assert config['mcp_servers.osverify_literature.enabled_tools'] == ['search_literature', 'resolve_reference', 'read_reference']
assert config['mcp_servers.osverify_literature.env.PYTHONPATH'] == sys.argv[1]
"""
    subprocess.run([sys.executable, "-c", code, str(engine), str(tmp_path / "refs")], cwd=tmp_path, check=True)


def test_new_run_keeps_its_snapshot_after_skill_changes(bundle, tmp_path):
    import shutil

    changed = tmp_path / "updated-skill"
    shutil.copytree(bundle, changed)
    directory = tmp_path / "review"
    run(changed, tmp_path, "demo", "--run-dir", directory)
    pinned = directory / ".osverify-runtime"
    before = (pinned / "osverifier/models.py").read_bytes()
    (changed / "scripts/review-runtime/osverifier/models.py").write_text("raise RuntimeError('new engine')")
    run(changed, tmp_path, "demo", "--run-dir", directory)
    assert (pinned / "osverifier/models.py").read_bytes() == before
    (pinned / "osverifier/models.py").write_text("# tampered")
    with pytest.raises(subprocess.CalledProcessError) as failure:
        run(changed, tmp_path, "demo", "--run-dir", directory)
    assert "Bundled engine changed" in failure.value.stderr


def test_legacy_run_is_not_migrated_or_modified(bundle, tmp_path):
    directory = tmp_path / "legacy"
    run(bundle, tmp_path, "--legacy-runtime", "demo", "--run-dir", directory)
    before = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    with pytest.raises(subprocess.CalledProcessError) as failure:
        run(bundle, tmp_path, "demo", "--run-dir", directory)
    assert "Use its original engine" in failure.value.stderr
    assert before == {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    run(bundle, tmp_path, "--legacy-runtime", "demo", "--run-dir", directory)
    assert not (directory / ".osverify-runtime").exists()


def test_relocated_paper_review_selects_its_own_scope(bundle, tmp_path):
    from test_ocr_transport import FAKE

    executable = tmp_path / "fake-codex"
    executable.write_text(f"#!{sys.executable}\n" + FAKE)
    executable.chmod(0o755)
    paper = tmp_path / "paper.tex"
    # The scripted Codex cites lines 1-3 of the paper.
    paper.write_text("\\begin{theorem}\\label{thm:main}\n$1+1=2$.\n\\end{theorem}\n"
                     "\\begin{proof}\nAddition gives the result.\n\\end{proof}\n")
    directory = tmp_path / "review"
    run(bundle, tmp_path, "verify", "--review-type", "paper", "--paper", paper,
        "--targets", "manual", "--labels", "thm:main", "--model", "fixture",
        "--codex-bin", executable, "--codex-home", tmp_path / "account",
        "--run-dir", directory, "--no-report")
    assert "thm:main" in (directory / "paper-inputs/question.md").read_text()
    assert json.loads((directory / "report.json").read_text())["run_status"] == "completed"
    assert (directory / ".osverify-runtime/bundle-manifest.json").exists()


def test_relocated_native_paper_roles_receive_literature(bundle, tmp_path):
    from test_ocr_transport import FAKE

    executable = tmp_path / "fake-codex"
    executable.write_text(f"#!{sys.executable}\n" + FAKE)
    executable.chmod(0o755)
    paper = tmp_path / "paper.md"
    paper.write_text("Theorem. $1+1=2$.\nProof.\nAddition gives the result.")
    question = tmp_path / "question.md"
    question.write_text("Review this theorem and proof.")
    directory = tmp_path / "review"
    run(bundle, tmp_path, "verify", "--review-type", "paper", "--question", question,
        "--solution", paper, "--model", "fixture", "--codex-bin", executable,
        "--codex-home", tmp_path / "account", "--run-dir", directory, "--no-report")
    report = json.loads((directory / "report.json").read_text())
    assert report["run_status"] == "completed"
    assert [f["text"] for f in report["feedback"]] == ["Exact approved fixture feedback."]


@pytest.mark.parametrize("style", ["unified", "former"])
def test_relocated_pdf_roles_receive_literature_and_keep_source_gates(bundle, tmp_path, monkeypatch, style):
    import shutil

    from PIL import Image
    from test_ocr_transport import FAKE

    if not shutil.which("pdftoppm"):
        pytest.skip("Poppler unavailable")
    executable = tmp_path / "fake-codex"
    executable.write_text(f"#!{sys.executable}\n" + FAKE)
    executable.chmod(0o755)
    source = tmp_path / "paper.pdf"
    Image.new("RGB", (300, 150), "white").save(source, "PDF")
    log = tmp_path / "calls.txt"
    monkeypatch.setenv("OCR_FIXTURE_LOG", str(log))
    common = ("--model", "fixture", "--codex-bin", executable, "--codex-home", tmp_path / "account")
    prepared, review = tmp_path / "prepared", tmp_path / "review"
    if style == "unified":
        run(bundle, tmp_path, "ocr", "--pdf", source, "--run-dir", prepared, *common)
        run(bundle, tmp_path, "verify", "--review-type", "paper", "--paper", source,
            "--ocr-dir", prepared, "--targets", "full",
            "--run-dir", review, "--no-report", *common)
    else:
        run(bundle, tmp_path, "prepare-pdf", "--pdf", source, "--run-dir", prepared, *common)
        run(bundle, tmp_path, "verify-pdf", "--ocr-manifest", prepared / "ocr-manifest.json",
            "--run-dir", review, "--root-only", "--no-report", *common)
    report = json.loads((review / "report.json").read_text())
    assert report["run_status"] == "completed"
    assert [f["text"] for f in report["feedback"]] == ["Exact approved fixture feedback."]
    assert log.read_text().splitlines().count("check-pdf-issue-source") == 2
    assert (prepared / ".osverify-runtime/bundle-manifest.json").exists()
    assert (review / ".osverify-runtime/bundle-manifest.json").exists()


def test_relocated_reader_template_and_skills_match_source(bundle, tmp_path):
    engine = bundle / "scripts/review-runtime"
    for name in ["writer.py", "public_referee.py", "presentation.py", "automatic.py", "template.tex",
                 "skills/write-referee-report/SKILL.md", "skills/check-referee-report/SKILL.md"]:
        assert (engine / "osverifier_reports" / name).read_bytes() == (REPO / "src/osverifier_reports" / name).read_bytes()
    code = r"""
import sys
sys.path.insert(0, sys.argv[1])
from osverifier_reports.writer import Draft, render, REPORT_FORMAT_VERSION
assert REPORT_FORMAT_VERSION.startswith('reader-summary-')
s = dict(request_id='Fixture paper', review_kind='paper', run_status='completed',
         classification_status='completed', coverage_complete=True, issues=[dict(issue_id='i', severity='major')],
         feedback=[dict(issue_id='i', kind='diagnosis', text='B_0 is too small.')], reference_limitations=[])
d = Draft(overview='One major issue.', entries=[dict(issue_id='i', title='Field condition', issue_type='Invalid inference',
    feedback=[dict(ref=0, section='Concern', presentation=[dict(original='B_0', occurrence=0, replacement='$B_0$', kind='math')])])])
t = render(s, d)
assert 'Review summary' in t and 'Review status' in t and 'Major issues' in t
assert 'Referee Report on' in t and r'\(B_0\)' in t and r'{\normalfont\bfseries}' in t
assert 'Overall opinion' not in t and 'Classification basis' not in t
"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    subprocess.run([sys.executable, "-c", code, str(engine)], cwd=tmp_path, env=env, check=True)


def _pinned_demo(bundle, tmp_path):
    directory = tmp_path / "review"
    run(bundle, tmp_path, "demo", "--run-dir", directory)
    return directory, directory / ".osverify-runtime"


def _payload(marker):
    return f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\ndef main(args):\n    return 0\n"


def test_unknown_pinned_runtime_is_refused(bundle, tmp_path):
    # A shared run folder can carry its own self-consistent "pinned engine".
    import hashlib

    directory, pinned = _pinned_demo(bundle, tmp_path)
    marker = tmp_path / "executed"
    (pinned / "osverifier/cli.py").write_text(_payload(marker))
    manifest = json.loads((pinned / "bundle-manifest.json").read_text())
    manifest["files"]["osverifier/cli.py"] = hashlib.sha256(
        (pinned / "osverifier/cli.py").read_bytes()).hexdigest()
    (pinned / "bundle-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(subprocess.CalledProcessError) as failure:
        run(bundle, tmp_path, "demo", "--run-dir", directory)
    assert "matches no OSVerify runtime" in failure.value.stderr
    assert not marker.exists()


def test_pinned_runtime_bytecode_is_never_executed(bundle, tmp_path):
    import importlib.util
    import py_compile

    directory, pinned = _pinned_demo(bundle, tmp_path)
    marker = tmp_path / "executed"
    source = tmp_path / "payload.py"
    source.write_text(_payload(marker))
    # Bytecode in __pycache__ that skips the source check is ignored ...
    cached = Path(importlib.util.cache_from_source(str(pinned / "osverifier/cli.py")))
    cached.parent.mkdir(exist_ok=True)
    py_compile.compile(str(source), cfile=str(cached), doraise=True,
                       invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    run(bundle, tmp_path, "demo", "--run-dir", directory)
    assert not marker.exists()
    # ... and a sourceless package that would shadow a module is refused.
    shadow = pinned / "osverifier/cli/__init__.pyc"
    shadow.parent.mkdir()
    py_compile.compile(str(source), cfile=str(shadow), doraise=True)
    with pytest.raises(subprocess.CalledProcessError) as failure:
        run(bundle, tmp_path, "demo", "--run-dir", directory)
    assert "Bundled engine changed" in failure.value.stderr
    assert not marker.exists()


def test_shipped_runtimes_are_in_the_allowlist():
    spec = importlib.util.spec_from_file_location("known_runtimes", REPO / "packaging/known_runtimes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    entry = module._entry(REPO)
    known = set(json.loads((REPO / "packaging/skills/osverify/scripts/known-runtimes.json").read_text())["digests"])
    assert {entry.manifest_digest(m) for m in module.current_manifests(REPO)} <= known
