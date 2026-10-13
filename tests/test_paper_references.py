import io
import re
import urllib.error
from copy import deepcopy

import pytest
from test_paper_review import args
from test_workflow import FixtureBackend, request

from osverifier.storage import FrozenRunMismatch, atomic_json, read_json
from osverifier_reports import paper
from osverifier_reports import references as refs
from osverifier_reports.paper import markdown, run_paper
from osverifier_reports.writer import Draft, render, source_snapshot


def check(status='unresolved'):
    return dict(url='https://example.org/paper.tex', title='External lemma', dependency='Lemma 2 requires Theorem 1', status=status, note='Source unavailable' if status=='unresolved' else 'Inspected the statement and hypotheses', pages=[])


class Response(io.BytesIO):
    url = 'https://example.org/paper.tex'


def public_source(monkeypatch):
    monkeypatch.setattr(refs, 'public_url', lambda url: url)
    class Opener:
        def open(self, *a, **kw):
            return Response(b'\\documentclass{article}\n\\begin{document}Lemma.\\end{document}')
    monkeypatch.setattr(refs.urllib.request, 'build_opener', lambda *a: Opener())


def test_download_tex_cache_and_tamper(tmp_path, monkeypatch):
    public_source(monkeypatch)
    store = refs.ReferenceStore(tmp_path)
    record = store.fetch(check()['url'])
    assert record['status'] == 'available'
    assert store.fetch(check()['url']) == record
    from pathlib import Path
    Path(record['paths'][0]).write_text('changed')
    with pytest.raises(FrozenRunMismatch):
        store.lookup(check()['url'])


def test_permission_failure_is_cached_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(refs, 'public_url', lambda url: url)
    calls = []
    class Denied:
        def open(self, *a, **kw):
            calls.append(1)
            raise urllib.error.HTTPError(check()['url'], 403, 'Forbidden', {}, None)
    monkeypatch.setattr(refs.urllib.request, 'build_opener', lambda *a: Denied())
    store = refs.ReferenceStore(tmp_path)
    for _ in range(4):
        record = store.fetch(check()['url'])
    assert record['status'] == 'unresolved' and record['reason'] == 'subscription_or_permission'
    assert len(calls) == 2


def test_private_address_is_rejected(monkeypatch):
    monkeypatch.setattr(refs.socket, 'getaddrinfo', lambda *a: [(2,1,6,'',('127.0.0.1',80))])
    with pytest.raises(ValueError, match='Non-public'):
        refs.public_url('http://localhost/paper.pdf')


def test_download_then_recheck_preserves_unresolved_if_omitted(tmp_path, monkeypatch):
    public_source(monkeypatch)
    class Backend:
        identity = {'kind': 'fixture'}
        calls = []
        def generate(self, role, payload, schema, directory):
            self.calls.append(deepcopy(payload))
            return {'reference_checks': [check()] if len(self.calls)==1 else [], 'value': 'unchanged'}
    backend = Backend()
    wrapper = refs.ReferenceBackend(backend, refs.ReferenceStore(tmp_path/'refs'))
    assert wrapper.generate('proposer', {}, {'properties': {}, 'required': []}, tmp_path/'attempt') == {'value': 'unchanged'}
    assert len(backend.calls) == 2 and backend.calls[1]['reference_context'][0]['status']=='available'
    assert read_json(tmp_path/'attempt/reference-observations.json')['checks'][0]['status']=='unresolved'


def test_access_limits_do_not_block_paper_completion_or_report(tmp_path, monkeypatch):
    class Limited(FixtureBackend):
        def generate(self, role, payload, schema, directory):
            result = super().generate(role,payload,schema,directory)
            if role == 'proposer':
                result.update(search_status='incomplete', search_limitations=['Cannot access the subscribed source'], reference_checks=[check()])
            return result
    monkeypatch.setattr(refs.ReferenceStore, 'fetch', lambda self,*a: {'status':'unresolved','reason':'subscription_or_permission'})
    options = args(tmp_path)
    options.decompose=False
    report = run_paper(options, request(), Limited())
    assert report.run_status=='completed' and not report.coverage_complete
    assert report.progress.search=='partial' and report.progress.evidence=='limited'
    assert report.reference_limitations and not report.feedback
    assert 'Unresolved external references' in markdown(report)
    source=source_snapshot(tmp_path/'report.json')
    draft=Draft(overview='Review with evidence limitations.',entries=[], reference_limits=[
        dict(refs=[i], not_checked=ref['dependency'], reason=ref['note'])
        for i, ref in enumerate(source['reference_limitations'])
    ])
    tex=render(source,draft)
    assert 'References not fully checked' in tex and 'External lemma' in tex
    assert 'Partial' in tex and 'Reason:' in tex
    assert 'READY_FOR_HUMAN' not in tex


def test_later_source_triggers_scoped_supplement_not_status_flip(tmp_path, monkeypatch):
    public_source(monkeypatch)
    store=refs.ReferenceStore(tmp_path/'refs')
    scope={'scope_id':'root','source_spans':[{'document':'candidate_solution','start_line':1,'end_line':1}]}
    attempt=tmp_path/'core/jobs/root/attempt-0001'
    atomic_json(attempt/'reference-observations.json',{'scope':scope,'checks':[check()],'catalogue':[]})
    import hashlib
    atomic_json(attempt.parent/'result.json',{'auxiliary_artifacts':{'attempt-0001/reference-observations.json':hashlib.sha256((attempt/'reference-observations.json').read_bytes()).hexdigest()}})
    store.fetch(check()['url'])
    class Supplement(FixtureBackend):
        def generate(self, role,payload,schema,directory):
            result=super().generate(role,payload,schema,directory)
            if role=='proposer':
                assert payload['reference_recheck_scope']['scope']==scope
                assert payload['request']['question']==request().question
                result['reference_checks']=[check('verified')]
            return result
    from osverifier.pipeline import RunConfig
    supplements=refs.reconcile_references(request(),Supplement(),store,tmp_path/'core',tmp_path,RunConfig())
    assert len(supplements)==1 and supplements[0][1].run_status=='completed'
    assert refs.reference_limits(tmp_path/'core')  # Original frozen record remains unchanged.
    assert not refs.reference_limits(supplements[0][0])


@pytest.mark.parametrize('field', ['math', 'ai'])
def test_supplementary_findings_keep_assessment_and_severity_grouping(tmp_path, monkeypatch, field):
    from test_paper_review import Classifier

    from osverifier.pipeline import Verifier
    from osverifier_reports import paper
    from osverifier_reports.reader import render_reader

    def supplement(request, backend, store, core_dir, run_dir, config):
        destination = run_dir / '.reference-rechecks' / 'fixture'
        fixture = FixtureBackend({'whole_solution': ['reject']}, reject=['reject'])
        return [(destination, Verifier(fixture, config).verify(request, destination), [])]

    monkeypatch.setattr(paper, 'reconcile_references', supplement)
    options = args(tmp_path)
    options.decompose, options.field = False, field
    backend = FixtureBackend({'whole_solution': ['reject']}, reject=['reject'])
    report = run_paper(options, request(), backend, Classifier(by_kind=False))
    extra = [i.issue_id for i in report.issues if i.issue_id.startswith('reference-1-')]
    assert extra and report.classification_status == 'completed'
    details = {d.issue_id: d for d in report.finding_details}
    assert all(details[i].assessment == 'confirmed' and details[i].kind == 'invalid_inference' for i in extra)
    if field == 'ai':
        _, markdown_text, _, _ = render_reader(report)
        severities = {i.issue_id: i.severity for i in report.issues}
        assert 'Other review comments' not in markdown_text
        assert f"**{sum(s == 'major' for s in severities.values())}** Major findings" in markdown_text


PLANTED_DEPENDENCY = 'Candidate lines 3081–3085: contact approximation PLANTED_DEPENDENCY'
PLANTED_NOTE = 'Its construction does not verify the positivity application. PLANTED_NOTE'


def planted_report(field, *, legacy_progress=False):
    from osverifier_reports.progress import PaperProgress

    title, url = 'Eliashberg–Thurston: Confoliations', 'https://example.org/confoliations.pdf'
    records = [dict(url=url, title=title, dependency=PLANTED_DEPENDENCY, dependency_id='paper:L3081:positivity',
                    note=PLANTED_NOTE, status='unresolved', pages=[], source_evidence=[], scope_id='node-0007', role=role)
               for role in ('proposer', 'issue_validator', 'feedback_auditor')]
    limits = ['code'] + ([f'Unresolved reference: {title}: {PLANTED_NOTE}'] if legacy_progress else [])
    return paper.PaperReport(
        field=field, request_id='planted', run_status='completed', backend_kind='fixture', coverage_complete=True,
        classification_status='completed', interruption_reason='quota_exhausted', interruption_phase='report_writing',
        progress=PaperProgress(execution='completed', search='completed', audit='completed', evidence='limited', evidence_limits=limits),
        issues=[dict(issue_id='i1', severity='major', rationale='Audited impact.')],
        finding_details=[dict(issue_id='i1', assessment='confirmed', kind='false_claim')],
        feedback=[dict(issue_id='i1', feedback_id='f1', kind='diagnosis', text='Exact audited feedback.',
                       source_spans=[], audit_artifact='.verification/jobs/i1/result.json')],
        reference_limitations=records)


@pytest.mark.parametrize('legacy', [False, True])
def test_unaudited_reference_notes_never_reach_reader_facing_files(tmp_path, monkeypatch, legacy):
    from test_paper_quota_report import fake_compile

    from osverifier_reports import automatic, reader
    from osverifier_reports.references import INTERNAL_REFERENCE_LIMITS, REFERENCE_NOTICE

    monkeypatch.setattr(automatic, 'compile_pdf', fake_compile)
    planted = (PLANTED_DEPENDENCY, PLANTED_NOTE, 'PLANTED_')
    # The internal line number, but not as a fragment of a hex digest in provenance files.
    line_number = re.compile(r'(?<![0-9a-fA-F])3081(?![0-9a-fA-F])')

    def clean(path):
        text = path.read_text(errors='replace')
        assert not any(marker in text for marker in planted) and not line_number.search(text), path
        return text

    for field in ('ai', 'math'):
        run = tmp_path / str(legacy) / field
        run.mkdir(parents=True)
        report = planted_report(field, legacy_progress=legacy)
        if legacy:  # Earlier releases stored the raw records in the public report.json.
            atomic_json(run / 'report.json', report.model_dump(mode='json'))
        else:
            paper.save(run, report)
            # Raw records remain internal diagnostics, still available to the editorial writer.
            assert read_json(run / INTERNAL_REFERENCE_LIMITS)['records'] == report.reference_limitations
            assert source_snapshot(run / 'report.json')['reference_limitations'] == report.reference_limitations
            for name in ('report.json', 'report.md'):
                text = clean(run / name)
            assert text.count('Eliashberg') == 1 and REFERENCE_NOTICE in text
        if field == 'ai':
            output = reader.publish_reader(run).parent
            html = clean(output / 'report.html')
            assert html.count('Eliashberg') == 1 and REFERENCE_NOTICE in html and 'confoliations.pdf' in html
        else:
            automatic.publish_partial(run)
            output = automatic.report_directory(run)
            tex = (output / 'partial/report.tex').read_text()
            assert tex.count('Eliashberg') == 1 and 'Not checked' not in tex and 'Reason:' not in tex
        files = [p for p in output.rglob('*') if p.is_file() and p.suffix != '.pdf']
        assert files and all(clean(p) is not None for p in files)
