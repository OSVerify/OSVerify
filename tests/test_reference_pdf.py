"""PDF retrieval failures must not hide an independently readable representation."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from osverifier.storage import read_json
from osverifier_reports import references
from osverifier_reports.reference_tools import LiteratureTools, serve
from osverifier_reports.references import ReferenceStore

URL = 'https://example.org/source.pdf'
TEXT = 'First page: α ≤ β\fSecond page theorem\fThird page\f'


def pdf_http(monkeypatch, data=b'%PDF-fixture', *, truncated=False):
    calls = []

    class Response(io.BytesIO):
        url = URL
        headers = {'Content-Type': 'application/octet-stream',
                   'Content-Length': str(len(data) + int(truncated))}

    class Opener:
        def open(self, request, **kwargs):
            calls.append(request.full_url)
            return Response(data)

    monkeypatch.setattr(references, 'public_url', lambda url: url)
    monkeypatch.setattr(references.urllib.request, 'build_opener', lambda *a: Opener())
    return calls


def fail(command, mode):
    if mode == 'missing':
        raise FileNotFoundError(2, 'No such file or directory', command[0])
    if mode == 'timeout':
        raise subprocess.TimeoutExpired(command, 60)
    return SimpleNamespace(returncode=1, stderr=b'PDF processing failed')


def fake_poppler(monkeypatch, *, text_failure=None, render_failure=None, failed_page=None):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == 'pdftotext':
            if text_failure and text_failure != 'empty':
                return fail(command, text_failure)
            Path(command[-1]).write_text('' if text_failure else TEXT, encoding='utf-8')
        else:
            page = int(command[command.index('-f') + 1])
            if render_failure and (failed_page is None or page == failed_page):
                return fail(command, render_failure)
            Image.new('RGB', (20, 20), 'white').save(command[-1] + '.png')
        return SimpleNamespace(returncode=0, stderr=b'')

    monkeypatch.setattr(references.subprocess, 'run', run)
    return calls


@pytest.mark.parametrize('failure', ['missing', 'timeout', 'nonzero', 'empty'])
def test_failed_text_extraction_still_renders_original_pages(tmp_path, monkeypatch, failure):
    pdf_http(monkeypatch)
    fake_poppler(monkeypatch, text_failure=failure)
    record = ReferenceStore(tmp_path).fetch(URL)
    assert record['status'] == 'available'
    assert record['text_path'] is None and record['content_quality'] == 'image_only'
    assert [p['page'] for p in record['page_images']] == [1]
    error = record['processing_errors'][0]
    assert error['stage'] == 'text_extraction'
    assert error['kind'] == {'missing': 'tool_missing', 'timeout': 'timeout',
                             'nonzero': 'tool_failed', 'empty': 'empty_output'}[failure]
    assert Path(record['pdf_path']).read_bytes() == b'%PDF-fixture'


@pytest.mark.parametrize('failure', ['missing', 'timeout', 'nonzero'])
def test_failed_render_preserves_text_and_reports_page_limitation(tmp_path, monkeypatch, failure):
    pdf_http(monkeypatch)
    fake_poppler(monkeypatch, render_failure=failure)
    tools = LiteratureTools(ReferenceStore(tmp_path))
    result = tools.read_reference(URL, page=2)
    assert result['status'] == 'passage' and 'Second page theorem' in result['text']
    assert result['page_images'] == [] and result['page_image_status'] == 'unavailable'
    assert result['processing_errors'][0]['stage'] == 'page_render'
    assert result['processing_errors'][0]['page'] == 2


def test_render_failure_on_one_page_keeps_other_pages(tmp_path, monkeypatch):
    pdf_http(monkeypatch)
    fake_poppler(monkeypatch, text_failure='empty', render_failure='timeout', failed_page=2)
    record = ReferenceStore(tmp_path).fetch(URL, [1, 2, 3])
    assert record['status'] == 'available'
    assert [p['page'] for p in record['page_images']] == [1, 3]
    assert any(e['stage'] == 'page_render' and e['page'] == 2 for e in record['processing_errors'])


def test_both_pdf_tools_missing_is_processing_failure_not_download_failure(tmp_path, monkeypatch):
    pdf_http(monkeypatch)
    fake_poppler(monkeypatch, text_failure='missing', render_failure='missing')
    record = ReferenceStore(tmp_path).fetch(URL)
    assert record['status'] == 'unresolved' and record['access_status'] == 'pdf_processing'
    assert record['retrievals'][0]['http_status'] == 200
    assert record['retrievals'][0]['byte_count'] == len(b'%PDF-fixture')
    assert {e['stage'] for e in record['processing_errors']} == {'text_extraction', 'page_render'}
    assert all(e['kind'] == 'tool_missing' for e in record['processing_errors'])
    assert Path(record['pdf_path']).is_file()


def test_cached_page_retry_retains_immutable_receipts_and_clears_current_error(tmp_path, monkeypatch):
    downloads = pdf_http(monkeypatch)
    fake_poppler(monkeypatch)
    store = ReferenceStore(tmp_path)
    first = store.fetch(URL)
    fake_poppler(monkeypatch, render_failure='nonzero')
    failed = store.fetch(URL, [2])
    assert failed['processing_errors'][0]['page'] == 2
    old_receipts = {p: p.read_bytes() for p in tmp_path.glob('*/view-*/receipt.json')}
    fake_poppler(monkeypatch)
    recovered = store.fetch(URL, [2])
    assert recovered['processing_errors'] == []
    assert recovered['page_images'][0]['page'] == 2
    assert all(p.read_bytes() == value for p, value in old_receipts.items())
    assert all(recovered['files'][p] == digest for p, digest in first['files'].items())
    assert len(downloads) == 1 and store.lookup(URL) == recovered


@pytest.mark.parametrize('page', [None, 2])
@pytest.mark.parametrize('query', ['', 'unextractable theorem'])
def test_image_only_pdf_is_returned_even_without_a_text_match(tmp_path, monkeypatch, page, query):
    pdf_http(monkeypatch)
    fake_poppler(monkeypatch, text_failure='empty')
    tools = LiteratureTools(ReferenceStore(tmp_path))
    result = tools.read_reference(URL, page=page, query=query)
    assert result['status'] == 'passage' and result['page_images'] and not result['text']
    assert result['matches'] == []
    if query:
        assert result['text_match_found'] is False
        assert 'No exact text match' in result['notice']
    if page:
        assert [p['page'] for p in result['page_images']] == [page]
    incoming = [json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': 'read_reference',
                                      'arguments': {'reference': URL, 'page': page, 'query': query}}})]
    outgoing = io.StringIO()
    serve(tools, incoming, outgoing)
    content = json.loads(outgoing.getvalue())['result']['content']
    images = [item for item in content if item['type'] == 'image']
    assert images
    with Image.open(io.BytesIO(base64.b64decode(images[0]['data']))) as image:
        assert image.format == 'PNG'


def test_partial_pdf_download_never_reaches_poppler(tmp_path, monkeypatch):
    pdf_http(monkeypatch, truncated=True)
    calls = fake_poppler(monkeypatch)
    record = ReferenceStore(tmp_path).fetch(URL)
    assert record['status'] == 'unresolved'
    assert record['retrievals'][0]['error_kind'] == 'truncated' and not calls


def test_alternate_source_does_not_inherit_failed_pdf_representation(tmp_path, monkeypatch):
    from test_literature_tools import fixture_http

    landing = 'https://example.org/article'
    tex = 'https://example.org/source.tex'
    fixture_http(monkeypatch, {
        landing: (f'<html><meta name="citation_pdf_url" content="{URL}"><meta name="citation_pdf_url" content="{tex}"></html>'.encode(), 'text/html'),
        URL: (b'%PDF-corrupt', 'application/pdf'),
        tex: (b'\\section{Theorem}\nAlternate original source.', 'application/x-tex'),
    })
    fake_poppler(monkeypatch, text_failure='nonzero', render_failure='nonzero')
    record = ReferenceStore(tmp_path).fetch(landing)
    assert record['status'] == 'available' and record['format'] == 'latex'
    assert 'pdf_path' not in record and not record.get('processing_errors')
    assert record['retrievals'][1]['processing_errors']
    assert record['retrievals'][1]['error_kind'] == 'pdf_processing'


def test_pdf_text_uses_explicit_utf8_on_non_utf8_hosts(tmp_path, monkeypatch):
    pdf_http(monkeypatch)
    fake_poppler(monkeypatch)
    read_text = Path.read_text

    def host_default(path, encoding=None, errors=None):
        return read_text(path, encoding=encoding or 'cp1252', errors=errors)

    monkeypatch.setattr(Path, 'read_text', host_default)
    result = LiteratureTools(ReferenceStore(tmp_path)).read_reference(URL, page=1)
    assert 'α ≤ β' in result['text']


@pytest.mark.skipif(not all(shutil.which(name) for name in ('pdftotext', 'pdftoppm')),
                    reason='real Poppler executables are required')
@pytest.mark.parametrize('name,quality', [('reference-text.pdf', 'text_extracted'),
                                         ('reference-scan.pdf', 'image_only')])
def test_real_pdf_bytes_through_poppler_and_mcp(tmp_path, monkeypatch, name, quality):
    data = (Path(__file__).parent / 'fixtures' / name).read_bytes()
    pdf_http(monkeypatch, data)
    store = ReferenceStore(tmp_path)
    record = store.fetch(URL)
    assert record['status'] == 'available' and record['content_quality'] == quality
    result = LiteratureTools(store).read_reference(URL, page=1)
    assert result['status'] == 'passage' and result['page_images']
    if quality == 'text_extracted':
        assert 'PDF pipeline fixture' in result['text']
    with Image.open(result['page_images'][0]['path']) as image:
        assert image.format == 'PNG' and max(image.size) == 1800
        assert image.convert('L').getextrema()[0] < 128  # Original visible ink, not an empty PNG.
    assert hashlib.sha256(data).hexdigest() in record['files'].values()
    assert read_json(tmp_path / hashlib.sha256(URL.encode()).hexdigest() / 'receipt.json')['status'] == 'available'


@pytest.mark.skipif(not all(shutil.which(name) for name in ('pdftotext', 'pdftoppm')),
                    reason='real Poppler executables are required')
def test_corrupt_pdf_preserves_errors_and_never_becomes_available(tmp_path, monkeypatch):
    pdf_http(monkeypatch, b'%PDF-1.7\nThis is not a valid PDF document.')
    record = ReferenceStore(tmp_path).fetch(URL)
    assert record['status'] == 'unresolved' and record['access_status'] == 'pdf_processing'
    assert len(record['processing_errors']) == 2
    assert all(e['kind'] == 'tool_failed' and e['message'] for e in record['processing_errors'])
    assert record['text_path'] is None and not record['page_images']


@pytest.mark.skipif(not all(shutil.which(name) for name in ('pdftotext', 'pdftoppm')),
                    reason='real Poppler executables are required')
def test_out_of_range_pdf_page_is_an_explicit_render_limitation(tmp_path, monkeypatch):
    pdf_http(monkeypatch, (Path(__file__).parent / 'fixtures/reference-text.pdf').read_bytes())
    tools = LiteratureTools(ReferenceStore(tmp_path))
    result = tools.read_reference(URL, page=999)
    assert result['status'] == 'passage_not_found' and not result['page_images']
    assert result['page_image_status'] == 'unavailable'
    assert result['processing_errors'][0]['page'] == 999
    assert tools.store.lookup(URL)['status'] == 'available'
