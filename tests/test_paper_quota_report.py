
import pytest
from test_paper_review import Classifier, args, review
from test_workflow import FixtureBackend, request

from osverifier.quota import QuotaExhausted
from osverifier.storage import read_json
from osverifier_reports import automatic, paper


def fake_compile(tex):
    path = tex.with_suffix('.pdf')
    path.write_bytes(b'%PDF-fixture')
    return path


@pytest.mark.parametrize("field", ["math", "ai"])
def test_verification_quota_partial_then_resume_reuses_audits(tmp_path, monkeypatch, field):
    monkeypatch.setattr(automatic, 'compile_pdf', fake_compile)

    class Exhausted(FixtureBackend):
        def generate(self, role, payload, schema, directory):
            if role == 'decomposer':
                raise QuotaExhausted('fixture')
            return super().generate(role, payload, schema, directory)

    classifier = Classifier()
    options = args(tmp_path)
    options.field = field
    report = paper.run_paper(options, request(), Exhausted({'root': ['local']}), classifier)
    assert report.run_status == 'incomplete' and report.interruption_reason == 'quota_exhausted'
    assert len(report.feedback) and not classifier.calls
    assert report.classification_status == 'skipped'
    assert report.classification_skip_reason == 'quota_exhausted'
    if field == "ai":
        assert report.progress.execution == "interrupted"
    partial_dir = automatic.report_directory(tmp_path)
    if field == "ai":
        partial_dir = next(partial_dir.glob('reader-*'))
        artifact_dir = partial_dir
    else:
        artifact_dir = partial_dir/'partial'
    assert read_json(partial_dir/'status.json')['partial']
    text = (artifact_dir/'report.tex').read_text()
    if field == "math":
        assert "Review summary" in text and "Review status" in text and "Partial" in text
        assert "Overall opinion" not in text and "Full planned scope coverage" not in text
        assert "Report editing" in text
    assert ('model usage limits' if field == 'ai' else 'quota exhausted') in text.lower()
    assert ('undetermined severity' if field == 'ai' else 'Unclassified issues') in text
    cached = {p: p.read_bytes() for p in (tmp_path/'.verification/jobs').glob('*/result.json')}
    resumed = FixtureBackend({'root': ['local']})
    final = paper.run_paper(options, request(), resumed, classifier)
    assert final.run_status == 'completed' and final.interruption_reason is None
    assert final.coverage_complete and final.classification_status == 'completed'
    assert final.classification_skip_reason is None
    assert not any(role == 'proposer' and data['scope']['scope_id'] == 'root' for role,data in resumed.calls)
    assert all(p.read_bytes() == b for p,b in cached.items())
    assert (artifact_dir/'report.pdf').exists()


def test_classification_quota_exports_without_more_calls_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(automatic, 'compile_pdf', fake_compile)

    class Exhausted(Classifier):
        def call(self, *args):
            self.calls.append(args[0])
            raise QuotaExhausted('fixture')

    classifier = Exhausted()
    report, _ = review(tmp_path, classifier)
    assert report.interruption_phase == 'classification'
    assert len(classifier.calls) == 1 and report.run_status == 'completed'
    assert all(i.severity == 'unknown' for i in report.issues)
    assert (automatic.report_directory(tmp_path)/'partial/report.pdf').exists()
    resumed = FixtureBackend()
    final = paper.run_paper(args(tmp_path), request(), resumed, Classifier())
    assert not resumed.calls and final.interruption_reason is None
    assert not (tmp_path/'interruption.json').exists()




def test_partial_missing_compiler_keeps_text_and_checkpoint(tmp_path, monkeypatch):
    report, _ = review(tmp_path)
    report.interruption_reason = 'quota_exhausted'
    report.interruption_phase = 'report_writing'
    paper.save(tmp_path,report)
    before = (tmp_path/'.verification/report.json').read_bytes()

    def missing(*a):
        raise RuntimeError('compiler missing')

    monkeypatch.setattr(automatic,'compile_pdf',missing)
    automatic.partial_after_quota(tmp_path)
    directory = automatic.report_directory(tmp_path)
    assert read_json(directory/'status.json')['status'] == 'failed'
    assert (directory/'partial/report.md').exists()
    assert (directory/'partial/report.tex').exists()
    assert (tmp_path/'.verification/report.json').read_bytes() == before
