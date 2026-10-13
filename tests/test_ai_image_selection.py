"""Focused visual input keeps access to the complete frozen paper."""
import hashlib
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from osverifier.backend import CodexBackend
from osverifier.models import ProposalBatch
from osverifier_reports.evidence import check_images, observation_schema, take_observations
from osverifier_reports.fields import AIPaperBackend
from osverifier_reports.image_selection import select_images


def catalogue(tmp_path):
    texts = [
        "Our method minimizes a weighted loss over the training examples.",
        "Figure 7: The effect of trigger sizes on segmentation accuracy.",
        "Appendix: Figure 7 also uses a clean model without poisoning.",
        "A separate limitation concerns the evaluation of physical attacks.",
    ]
    images = []
    for n, text in enumerate(texts, 1):
        p = tmp_path / f"page-{n}.txt"
        p.write_text(text)
        pixels = tmp_path / f"page-{n}.png"
        Image.new("RGB", (20, 20), "white").save(pixels)
        images.append({"image_id": f"image-{n:04}", "page_number": n,
                       "path": str(pixels), "sha256": hashlib.sha256(pixels.read_bytes()).hexdigest(),
                       "navigation": {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}})
    return images, texts


def test_root_sees_all_pages_and_focused_calls_follow_figure_references(tmp_path):
    images, text = catalogue(tmp_path)
    payload = {"request": {"candidate_solution": "\n".join([text[0], *["" for _ in range(5)], text[1]])},
               "scope": {"scope_id": "root"},
               "proposed_issue": {"source_spans": [{"document": "candidate_solution", "start_line": 7, "end_line": 7}]}}
    assert select_images(images, "proposer", payload)[0] == images
    for role in ("issue_validator", "feedback_auditor"):
        selected, reason = select_images(images, role, payload)
        assert [i["page_number"] for i in selected] == [2, 3]
        assert reason == "source_spans_and_figure_table_references"
    payload["scope"] = {"kind": "segment", "scope_id": "node-0002", **payload["proposed_issue"]}
    assert [i["page_number"] for i in select_images(images, "proposer", payload)[0]] == [2, 3]


def test_unmatched_source_and_unindexed_images_are_not_silently_dropped(tmp_path):
    images, text = catalogue(tmp_path)
    assert select_images(images, "issue_validator", {})[0] == images
    scan = {k: v for k, v in images[-1].items() if k != "navigation"}
    images[-1] = scan
    selected, _ = select_images(images, "issue_validator", {"proposed_issue": {"target": "Figure 7"}})
    assert selected == images[1:]
    Path(images[0]["navigation"]["path"]).write_text("tampered")
    with pytest.raises(Exception, match="Frozen paper image changed"):
        check_images(images)


def test_receipts_require_attached_images_and_allow_extra_source_inspections(tmp_path):
    images, _ = catalogue(tmp_path)
    required = images[1:2]
    schema = observation_schema({"properties": {}, "required": []}, images, required_images=required)
    assert schema["properties"]["visual_inspections"]["minItems"] == 1
    assert schema["properties"]["visual_inspections"]["maxItems"] == 4
    checks = [{"image_id": i["image_id"], "status": "inspected", "observation": "Concrete visible plot detail"} for i in images[1:3]]
    _, receipt = take_observations({"visual_inspections": checks}, images, required_images=required)
    assert len(receipt["visual_inspections"]) == 2
    with pytest.raises(ValueError, match="attached paper image"):
        take_observations({"visual_inspections": checks[1:]}, images, required_images=required)
    with pytest.raises(ValueError, match="catalogue"):
        take_observations({"visual_inspections": checks + [{**checks[0], "image_id": "outside"}]}, images, required_images=required)


def test_focused_transport_attaches_subset_and_keeps_full_catalogue(tmp_path):
    images, texts = catalogue(tmp_path)
    fake = tmp_path / "codex"
    fake.write_text(f"#!{sys.executable}\n" + '''
import json, sys
from pathlib import Path
if "--version" in sys.argv:
    print("fixture")
    raise SystemExit(0)
args = sys.argv
assert "--ephemeral" in args
p = json.loads((Path(args[args.index("--cd")+1])/"input.json").read_text())
assert len(p["paper_images"]) == 4
ids = p["paper_review"]["attached_image_ids"]
assert ids == ["image-0002", "image-0003"]
attached = [args[n+1] for n, x in enumerate(args) if x == "--image"]
assert attached == [i["path"] for i in p["paper_images"] if i["image_id"] in ids]
assert all(Path(path).read_bytes().startswith(b"\\x89PNG") for path in attached)
result = {"scope_id":"node-2", "search_status":"completed", "search_limitations":[], "issues":[],
          "evidence_limits":[], "visual_inspections":[{"image_id":i,"status":"inspected",
          "observation":"Fixture direct pixel observation"} for i in ids]}
Path(args[args.index("--output-last-message")+1]).write_text(json.dumps(result))
''')
    fake.chmod(0o755)
    backend = AIPaperBackend(CodexBackend("fixture", codex_bin=str(fake)), images)
    result = backend.generate("proposer", {
        "request": {"candidate_solution": texts[1]},
        "scope": {"kind": "segment", "scope_id": "node-2", "source_spans": [
            {"document": "candidate_solution", "start_line": 1, "end_line": 1}]}},
        ProposalBatch.model_json_schema(), tmp_path / "attempt")
    assert ProposalBatch.model_validate(result).issues == []
    receipt = json.loads((tmp_path / "attempt/paper-observations.json").read_text())
    assert receipt["attached_image_ids"] == ["image-0002", "image-0003"]
    assert len(receipt["images"]) == 4 and len(receipt["visual_inspections"]) == 2
