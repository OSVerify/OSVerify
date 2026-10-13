"""Exercise actual PDF rendering, CLI subprocess/image transport and release gates."""
from __future__ import annotations

import sys

import pytest
from PIL import Image, ImageDraw

from osverifier.storage import read_json
from osverifier_reports import ocr

FAKE = r'''
import json, os, sys
from pathlib import Path
if '--version' in sys.argv:
 print('codex-cli fixture');sys.exit(0)
prompt=sys.stdin.read()
if os.environ.get('OCR_FIXTURE_SLEEP'):
 import time
 time.sleep(60)
args=sys.argv
workspace=Path(args[args.index('--cd')+1])
p=json.loads((workspace/'input.json').read_text())
schema=json.loads(Path(args[args.index('--output-schema')+1]).read_text())
role=next(d.name for d in sorted((workspace/'.agents/skills').iterdir()) if d.is_dir())  # skip policy.md
log=os.environ.get('OCR_FIXTURE_LOG')
if log:
 with open(log,'a') as f:f.write(role+'\n')
assert '--sandbox' in args and args[args.index('--sandbox')+1]=='read-only'
if role in ('transcribe-pdf-page','check-pdf-transcription','check-pdf-issue-source'):
 assert '--image' in args and '--ephemeral' in args
 assert 'model_reasoning_effort="xhigh"' in args
 assert 'web_search="disabled"' in args
text='Theorem. $1+1=2$.\nProof. Addition gives the result.'
if role=='transcribe-pdf-page':
 out=dict(page_number=p['page_number'],text=text,unreadable=[])
elif role=='check-pdf-transcription':
 out=dict(page_number=p['page_number'],status='approved',text=p['transcription'],corrections=[],unreadable=[],rationale='Fixture inspected all content.')
elif role=='check-pdf-issue-source':
 checks=[]
 status='ocr_error' if os.environ.get('OCR_FIXTURE_MISMATCH') else 'faithful'
 for s in p['required_spans']:
  pages=[i['page_number'] for i in p['pages'] if i['start_line']<=s['end_line'] and s['start_line']<=i['end_line']]
  checks.append(dict(start_line=s['start_line'],end_line=s['end_line'],page_numbers=pages,status=status,original_quote=text,rationale='Fixture compared original source.'))
 out=dict(status=status,checks=checks,dependencies_checked=checks[:1],rationale='Fixture source check.')
elif role in ('propose-solution-issues','propose-ai-paper-issues'):
 sid=p['scope']['scope_id']
 out=dict(scope_id=sid,search_status='completed',search_limitations=[],issues=[dict(issue_id='i1',scope_id=sid,kind='invalid_inference',target='fixture defect',source_spans=[dict(document='candidate_solution',start_line=2,end_line=3)],allegation='Fixture allegation',evidence='Fixture evidence',proposed_feedback=[])])
elif role in ('validate-solution-issue','validate-ai-paper-issue'):
 issue=p['proposed_issue']
 if 'ocr_input' in p:assert p['ocr_source_check']['status']=='faithful'
 out=dict(issue_id=issue['issue_id'],assessment='confirmed',kind='invalid_inference',impact='blocking',rejection_supported=True,completion_effect='none',source_spans=issue['source_spans'],rationale='Fixture rationale',evidence='Fixture evidence',feedback_candidates=[dict(feedback_id='f1',kind='diagnosis',text='Exact approved fixture feedback.')])
elif role in ('audit-verifier-feedback','audit-ai-paper-feedback'):
 issue=p['proposed_issue']
 if 'ocr_input' in p:assert p['ocr_source_check']['status']=='faithful'
 out=dict(issue_id=issue['issue_id'],issue_review_assessment='upheld',rationale='Fixture audit',decisions=[dict(feedback_id='f1',decision='approve',grounded_in_source=True,addresses_validated_issue=True,preserves_problem=True,mathematically_supported=True,uncertainty_calibrated=True,rationale='Fixture checks')])
elif role=='classify-paper-issues':
 out=dict(issues=[dict(issue_id=f['issue_id'],severity='major',rationale='Fixture major') for f in p['feedback']])
elif role=='audit-paper-severity':
 out=dict(approved=True,reasons=[])
else:raise RuntimeError(role)
if 'reference_checks' in schema.get('properties',{}):
 assert 'mcp_servers.osverify_literature.required=true' in args
 assert 'mcp_servers.osverify_literature.args=' in '\n'.join(args)
 assert '--download-timeout-seconds' in '\n'.join(args)
 out['reference_checks']=[]
if 'evidence_limits' in schema.get('properties',{}):out['evidence_limits']=[]
if 'visual_inspections' in schema.get('properties',{}):
 ids=p.get('attached_image_ids',p.get('paper_review',{}).get('attached_image_ids',[i['image_id'] for i in p.get('paper_images',[])]))
 out['visual_inspections']=[dict(image_id=i,status='inspected',observation='Fixture visible equation and proof.') for i in ids]
Path(args[args.index('--output-last-message')+1]).write_text(json.dumps(out))
print(json.dumps(dict(type='turn.completed',usage=dict(input_tokens=1,output_tokens=1))))
'''


@pytest.fixture
def transport(tmp_path, monkeypatch):
    import shutil

    if not shutil.which('pdftoppm'):
        pytest.skip('Poppler unavailable')
    executable = tmp_path / 'fake-codex'
    executable.write_text(f'#!{sys.executable}\n' + FAKE)
    executable.chmod(0o755)
    image = Image.new('RGB', (500, 200), 'white')
    ImageDraw.Draw(image).text((20, 20), 'Theorem. 1+1=2. Proof. Addition gives the result.', fill='black')
    source = tmp_path / 'scan.pdf'
    image.save(source, 'PDF')
    log = tmp_path / 'calls.txt'
    monkeypatch.setenv('OCR_FIXTURE_LOG', str(log))
    common = ['--model', 'fixture', '--codex-bin', str(executable), '--codex-home', str(tmp_path / 'account')]
    assert ocr.main(['prepare-pdf', '--pdf', str(source), '--run-dir', str(tmp_path / 'prepared'), *common]) == 0
    return common, log


def test_pdf_cli_source_checks_before_validation_and_again_before_release(transport, tmp_path):
    common, log = transport
    out = tmp_path / 'review'
    assert ocr.main(['verify-pdf', '--ocr-manifest', str(tmp_path / 'prepared/ocr-manifest.json'),
                     '--run-dir', str(out), '--root-only', '--no-report', *common]) == 0
    report = read_json(out / 'report.json')
    assert report['run_status'] == 'completed'
    assert [f['text'] for f in report['feedback']] == ['Exact approved fixture feedback.']
    roles = log.read_text().splitlines()
    assert roles.count('check-pdf-issue-source') == 2
    core = read_json(out / '.verification/manifest.json')
    assert core['provenance']['backend']['ocr_input']['manifest_sha256']
    jobs = list((out / '.verification/jobs').glob('*/result.json'))
    source_artifacts = [k for job in jobs for k in read_json(job).get('auxiliary_artifacts', {}) if k.endswith('source-check.json')]
    assert len(source_artifacts) == 2
    # An unchanged resume must use approved core caches, never new math/source calls.
    before = roles.count('check-pdf-issue-source')
    assert ocr.main(['verify-pdf', '--ocr-manifest', str(tmp_path / 'prepared/ocr-manifest.json'),
                     '--run-dir', str(out), '--root-only', '--no-report', *common]) == 0
    assert log.read_text().splitlines().count('check-pdf-issue-source') == before


def test_pdf_cli_mismatch_is_input_failure_never_released_diagnosis(transport, tmp_path, monkeypatch):
    common, log = transport
    monkeypatch.setenv('OCR_FIXTURE_MISMATCH', '1')
    out = tmp_path / 'review'
    assert ocr.main(['verify-pdf', '--ocr-manifest', str(tmp_path / 'prepared/ocr-manifest.json'),
                     '--run-dir', str(out), '--root-only', '--no-report', '--max-attempts', '1', *common]) == 2
    report = read_json(out / 'report.json')
    assert report['run_status'] == 'incomplete'
    assert report['feedback'] == []
    roles = log.read_text().splitlines()
    assert not any('validate-' in role or 'audit-verifier' in role or 'audit-ai' in role for role in roles)


def test_ocr_calls_share_verifier_cancellation_and_quota(transport, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from conftest import wait_for

    from osverifier.backend import BackendCancelled, CodexBackend
    from osverifier.quota import QuotaExhausted
    from osverifier_reports.writer import CodexWriter

    common, _ = transport
    binary = common[common.index("--codex-bin") + 1]
    home = tmp_path / "account"
    backend = CodexBackend("fixture", binary, home)
    caller = CodexWriter("fixture", binary, home, timeout=30, reasoning_effort="xhigh", process_backend=backend)
    m = ocr.load_preparation(tmp_path / "prepared/ocr-manifest.json")
    payload = {"page_number": 1, "paper_images": m["images"]}
    monkeypatch.setenv("OCR_FIXTURE_SLEEP", "1")
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(caller.call, "transcribe-pdf-page", payload, ocr.Transcription, tmp_path / "cancelled-call")
        wait_for(lambda: bool(backend._processes))
        backend.cancel()
        with pytest.raises(BackendCancelled):
            result.result(timeout=5)
    assert not backend._processes
    backend = CodexBackend("fixture", binary, home)
    caller.process_backend = backend
    backend.halt_for_quota(QuotaExhausted("fixture quota"))
    with pytest.raises(QuotaExhausted):
        caller.call("transcribe-pdf-page", payload, ocr.Transcription, tmp_path / "quota-call")
    assert not backend._processes
