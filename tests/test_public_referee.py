import copy

import pytest

from osverifier_reports.public_referee import review_status
from osverifier_reports.writer import Draft, render, validate_draft


def source():
    return dict(
        request_id="A paper",
        review_kind="paper",
        run_status="completed",
        coverage_complete=True,
        classification_status="completed",
        progress=dict(
            execution="completed",
            search="completed",
            audit="completed",
            scopes_searched=48,
            scopes_total=48,
            search_limits=["INTERNAL_LIMIT_SENTINEL"],
        ),
        reference_limitations=[],
        issues=[dict(issue_id="i", severity="minor", rationale="PRIVATE_RATIONALE")],
        finding_details=[dict(issue_id="i", assessment="confirmed", kind="presentation")],
        feedback=[
            dict(
                issue_id="i",
                feedback_id="secret-f",
                kind="diagnosis",
                text="The cited theorem has a different number.",
                source_spans=[],
                audit_artifact=".verification/PRIVATE_JOB.json",
            )
        ],
    )


def draft(**extra):
    return Draft(
        overview="One citation correction was identified.",
        entries=[
            dict(
                issue_id="i",
                title="Citation number",
                issue_type="Citation error",
                feedback=[dict(ref=0, section="Concern")],
            )
        ],
        **extra,
    )


def test_public_export_preserves_feedback_without_internal_details():
    s = source()
    before = copy.deepcopy(s)
    tex = render(s, draft())
    assert s == before
    assert s["feedback"][0]["text"] in tex
    assert all(
        x not in tex
        for x in [
            "PRIVATE_RATIONALE",
            "PRIVATE_JOB",
            "secret-f",
            "INTERNAL_LIMIT_SENTINEL",
            "42/48",
            "confirmed; presentation",
        ]
    )
    assert "Exact quotation unavailable" in tex  # no invented quote
    assert "No separate" not in tex  # no empty evidence/repair boilerplate


@pytest.mark.parametrize(
    "change",
    [
        dict(coverage_complete=False),
        dict(run_status="incomplete"),
        dict(classification_status="incomplete"),
        dict(progress=dict(search="partial")),
        dict(progress=dict(audit="incomplete")),
        dict(progress=dict(unreviewed_findings=1)),
        dict(progress=dict(failed_findings=1)),
    ],
)
def test_finished_process_does_not_mask_incomplete_review(change):
    s = source()
    s.update(change)
    assert review_status(s)[0] == "Partial"


def test_unknown_coverage_is_not_promoted():
    s = source()
    s.pop("coverage_complete")
    assert review_status(s)[0] != "Completed"


def test_reference_summary_cannot_drop_duplicate_or_invent_source_indices():
    s = source()
    s["reference_limitations"] = [
        dict(
            title="Original theorem",
            note="Original proof inaccessible; application checked elsewhere.",
        )
    ]
    with pytest.raises(ValueError, match="Every unresolved reference"):
        validate_draft(s, draft())
    for refs in [[1], [0, 0], [-1]]:
        with pytest.raises(ValueError, match="Every unresolved reference"):
            validate_draft(
                s,
                draft(
                    reference_limits=[
                        dict(
                            refs=refs,
                            not_checked="Original proof.",
                            reason="Full text unavailable.",
                        )
                    ]
                ),
            )
    good = draft(
        reference_limits=[
            dict(
                refs=[0],
                not_checked="Original proof; application checked in an alternate source.",
                reason="Original full text unavailable.",
            )
        ]
    )
    tex = render(s, good)
    assert "Original theorem" in tex and good.reference_limits[0].reason in tex
    assert "application checked in an alternate source" in tex


def test_scope_counts_cannot_be_promoted_by_stale_complete_flag():
    s = source()
    s["progress"]["scopes_searched"] = 42
    assert review_status(s)[0] == "Partial"


def test_codex_output_schema_requires_nested_optional_keys():
    from osverifier_reports.writer import strict_output_schema

    schema = strict_output_schema(Draft.model_json_schema())

    def check(node):
        if isinstance(node, dict):
            if "properties" in node:
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
            assert "default" not in node
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)
    assert "reference_limits" in schema["required"]


def test_access_only_limits_do_not_claim_the_result_was_unchecked():
    s = source()
    s["reference_limitations"] = [
        dict(title="Author, Book, Section 5", url="https://example.org/book.pdf"),
        dict(title="Book alternate title", url="https://example.org/book.pdf"),
    ]
    d = draft(reference_limits=[dict(refs=[0, 1], not_checked="The cited copy could not be read.",
                                    reason="The result was checked in an accessible mirror.")])
    validate_draft(s, d)
    tex = render(s, d)
    assert "Some cited results remain incompletely checked" not in tex
    assert "Author, Book, Section 5" in tex
    assert "Book alternate title" not in tex
    assert "The result was checked in an accessible mirror." in tex
    assert len(s["reference_limitations"]) == 2


def test_feedback_math_and_locations_are_anchored_and_reversible():
    from osverifier_reports.presentation import presented_feedback
    from osverifier_reports.writer import Placement

    s = source()
    s["feedback"][0]["text"] = "At lines 3–4, B_0(a+b) contains z_p."
    s["paper_locations"] = {"issues": {"i": {"passages": [{
        "source_span": {"document": "candidate_solution", "start_line": 3, "end_line": 4},
        "line_references": {"3": ["Section 2"], "4": ["Section 2"]},
    }]}}}
    before = copy.deepcopy(s)
    edits = [dict(original="B_0(a+b)", occurrence=0, replacement="$B_{0}(a+b)$", kind="math"),
             dict(original="z_p", occurrence=0, replacement="$z_p$", kind="math"),
             dict(original="lines 3–4", occurrence=0, replacement="the final paragraph in Section 2", kind="location")]
    placement = Placement(ref=0, section="Concern", presentation=edits)
    assert presented_feedback(s, "i", placement) == "At the final paragraph in Section 2, $B_{0}(a+b)$ contains $z_p$."
    assert s == before
    for bad in [
        [dict(edits[0], replacement="$B_0(a-b)$")],
        [dict(edits[0], occurrence=2)],
        [edits[0], edits[0]],
        [dict(edits[2], replacement="Section 9")],
    ]:
        with pytest.raises(ValueError):
            presented_feedback(s, "i", Placement(ref=0, section="Concern", presentation=bad))


def test_presentation_anchors_and_verified_locators_are_whole_tokens():
    from osverifier_reports.presentation import presented_feedback
    from osverifier_reports.writer import Placement

    s = source()
    s["feedback"][0]["text"] = "The estimate in line 120 is not implied by the bound in line 12 for max x."
    s["paper_locations"] = {"issues": {"i": {"passages": [{
        "source_span": {"document": "candidate_solution", "start_line": 10, "end_line": 130},
        "line_references": {str(n): ["Lemma 2.1"] if n < 100 else ["Theorem 4.3"] for n in range(10, 131)},
    }]}}}

    def present(*edits):
        return presented_feedback(s, "i", Placement(ref=0, section="Concern", presentation=list(edits)))

    # "line 12" occurs once as a token; its prefix inside "line 120" is not an occurrence.
    line_12 = dict(original="line 12", occurrence=0, replacement="Lemma 2.1", kind="location")
    assert present(line_12) == "The estimate in line 120 is not implied by the bound in Lemma 2.1 for max x."
    with pytest.raises(ValueError, match="not anchored"):
        present(dict(line_12, occurrence=1))
    # The single letter inside "max" is not the variable x.
    assert present(dict(original="x", occurrence=0, replacement="$x$", kind="math")).endswith("for max $x$.")
    # A verified "Lemma 2.1" cannot vouch for an unverified finer or different number.
    for invented in ("Lemma 2.17", "Lemma 2.1.3", "Sublemma 2.1"):
        with pytest.raises(ValueError, match="verified printed reference"):
            present(dict(line_12, replacement=invented))
    assert present(dict(line_12, replacement="the final display of Lemma 2.1.")).endswith(
        "bound in the final display of Lemma 2.1. for max x.")


def test_classic_paragraph_labels_use_bold_font():
    assert r"{\normalfont\bfseries}" in render(source(), draft())


@pytest.mark.parametrize('legacy', [
    'A power saving for planar unit distances September 23 2026',
    'A power saving for planar unit distances September 23, 2026',
    'A power saving for planar unit distances-September-23-2026',
    'A power saving for planar unit distances 2026-09-23',
])
def test_title_drops_legacy_version_date_but_footer_uses_generation_date(legacy):
    s = source()
    s.update(request_id=legacy, report_generated_on='2026-10-12')
    tex = render(s, draft())
    assert 'Referee Report on ``A power saving for planar unit distances\'\'}' in tex
    assert '2026-09-23' not in tex and 'September' not in tex
    assert r'\footnotesize\color{ReportMuted}' in tex
    assert 'Report generated: 2026-10-12 (UTC).' in tex
    assert s['request_id'] == legacy


def test_explicit_title_and_non_date_suffixes_are_preserved():
    from osverifier_reports.public_referee import paper_title

    assert paper_title({'paper_title': 'Results of September 23 2026', 'request_id': 'old'}) == 'Results of September 23 2026'
    for title in ['Bounds in dimension 2026', 'A paper February 30 2026', 'A paper 2026-13-01', 'A paper']:
        assert paper_title({'request_id': title}) == title


def test_snapshot_records_generation_date_separate_from_review_date(tmp_path):
    import json
    from datetime import datetime, timezone

    from osverifier_reports.writer import source_snapshot

    s = source()
    s['created_at'] = '2020-01-01T00:00:00Z'
    s['paper_title'] = 'Exact title'
    p = tmp_path / 'report.json'
    p.write_text(json.dumps(s))
    snapshot = source_snapshot(p)
    assert snapshot['report_generated_on'] == datetime.now(timezone.utc).date().isoformat()
    assert snapshot['paper_title'] == 'Exact title'
    assert json.loads(p.read_text()) == s


def test_unicode_math_is_typeset_and_compiles_without_changing_feedback(tmp_path):
    from osverifier_reports import automatic

    s = source()
    text = "The map $p_*ω$ sends $x ∈ ℝ^{n}$ to $‖f‖_∞ ≤ 1$ with $x²⁻¹$, $\\text{α-stable}$ and $x ⊛ y$; prose ω stays."
    s["feedback"][0]["text"] = text
    before = copy.deepcopy(s)
    summary = draft()
    summary.overview = "One issue about $α ≤ β$."
    tex = render(s, summary)
    assert s == before
    for expected in (r"\(p_*\omega\)", r"\in  \mathbb{R}^{n}", r"\|f\|_\infty", r"\leq  1", "x^{2-1}",
                     r"\text{α-stable}", r"\text{⊛}", r"\(\alpha  \leq  \beta\)", "prose ω stays"):
        assert expected in tex
    path = tmp_path / "report.tex"
    path.write_text(tex)
    try:
        assert automatic.compile_pdf(path).read_bytes().startswith(b"%PDF-")
    except automatic.CompilerUnavailable:
        pytest.skip("XeLaTeX is not installed")
