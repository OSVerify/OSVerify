from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

from osverifier.backend import ROLE_SKILLS, CodexBackend
from osverifier.storage import FrozenRunMismatch, atomic_json, read_json
from osverifier_reports import references
from osverifier_reports.fields import AIPaperBackend
from osverifier_reports.reference_html import extract_html
from osverifier_reports.reference_tools import LiteratureTools
from osverifier_reports.references import (
    ReferenceBackend,
    ReferenceStore,
    checked_references,
    reference_limits,
)

HTML = '<html><title>Lemma</title><main id="proof"><h1>Lemma 1</h1><p>Let R be Noetherian.</p><math alttext="x^2=1"><mi>x</mi></math><p>Proof. ' + 'A coherent sheaf has the stated property. ' * 20 + '</p></main></html>'
URL = 'https://stacks.math.columbia.edu/tag/01YS'


def fixture_http(monkeypatch, responses):
    calls = []
    class Response(io.BytesIO):
        def __init__(self, data, url, content_type):
            super().__init__(data)
            self.url = url
            self.headers = {'Content-Type': content_type}
    class Opener:
        def open(self, request, **kwargs):
            calls.append(request.full_url)
            data, kind = responses[request.full_url]
            return Response(data, request.full_url, kind)
    monkeypatch.setattr(references, 'public_url', lambda url: url)
    monkeypatch.setattr(references.urllib.request, 'build_opener', lambda *a: Opener())
    return calls


def test_html_fulltext_preserves_tex_mathml_and_excludes_scripts():
    doc = extract_html(HTML + '<script>do_evil()</script>', URL)
    assert doc['kind'] == 'fulltext' and '$x^2=1$' in doc['text'] and 'do_evil' not in doc['text']
    doc = extract_html(HTML.replace('<math alttext="x^2=1"><mi>x</mi></math>', '<math><mfrac><mi>x</mi><msup><mi>y</mi><mn>2</mn></msup></mfrac></math>'), URL)
    assert r'\frac{x}{{y}^{2}}' in doc['text']


def test_abstract_and_security_challenge_are_not_fulltext():
    for markup, url in [('<html><title>Just a moment</title><main>' + HTML + '</main></html>', URL),
                        (HTML, 'https://arxiv.org/abs/1234.56789')]:
        assert extract_html(markup, url)['kind'] != 'fulltext'
    assert extract_html('<html><article><h1>Abstract</h1><p>Results ' + 'summary ' * 400 + '</p></article></html>', 'https://example.org/paper')['kind'] == 'landing_page'


def test_landing_download_and_read_original_passage(tmp_path, monkeypatch):
    landing = b'<html><head><meta name="citation_pdf_url" content="/paper.tex"></head><body>Abstract only</body></html>'
    tex = b'\\documentclass{article}\n\\section{Theorem 2}\nEvery finite group satisfies the stated conclusion.\nProof...'
    calls = fixture_http(monkeypatch, {'https://example.org/article': (landing, 'text/html'), 'https://example.org/paper.tex': (tex, 'application/x-tex')})
    service = LiteratureTools(ReferenceStore(tmp_path/'refs'), tmp_path/'journal.jsonl')
    result = service.call('resolve_reference', {'reference': 'https://example.org/article'})
    assert result['retrieval']['status'] == 'available'
    read = service.call('read_reference', {'reference': result['reference_id'], 'query': 'Theorem 2'})
    assert read['status'] == 'passage' and 'Every finite group' in read['text']
    assert len(calls) == 2 and len((tmp_path/'journal.jsonl').read_text().splitlines()) == 2
    assert service.read_reference(result['reference_id'], query='missing theorem')['status'] == 'passage_not_found'
    Path(result['retrieval']['text_path']).write_text('tampered')
    with pytest.raises(FrozenRunMismatch):
        service.read_reference(result['reference_id'])


def test_arxiv_abs_keeps_version_and_bibliography_links_are_not_downloads():
    doc = extract_html('<html><a href="cited.pdf">Smith (2000)</a></html>', 'https://arxiv.org/abs/2310.17965v1')
    assert doc['candidates'] == ['https://arxiv.org/pdf/2310.17965v1']


def test_legacy_store_is_readable_but_not_mutated(tmp_path):
    folder = tmp_path / hashlib.sha256(URL.encode()).hexdigest()
    old = {'url': URL, 'status': 'unresolved', 'files': {}, 'paths': []}
    atomic_json(folder/'receipt.json', old)
    before = (folder/'receipt.json').read_bytes()
    store = ReferenceStore(tmp_path)
    assert store.lookup(URL) == old
    with pytest.raises(FrozenRunMismatch, match='Historical'):
        store.fetch(URL)
    assert (folder/'receipt.json').read_bytes() == before
    assert not (tmp_path/'store-version.json').exists()


def test_concurrent_instances_share_one_fetch(tmp_path, monkeypatch):
    calls = fixture_http(monkeypatch, {URL: (HTML.encode(), 'text/html')})
    with ThreadPoolExecutor(max_workers=6) as pool:
        records = list(pool.map(lambda _: ReferenceStore(tmp_path).fetch(URL), range(6)))
    assert len(calls) == 1 and all(r['status'] == 'available' for r in records)


def test_private_redirect_rejected(monkeypatch):
    import socket
    import urllib.request
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a: [(2,1,6,'',('127.0.0.1',80))])
    with pytest.raises(ValueError, match='Non-public'):
        references.PublicRedirect().redirect_request(urllib.request.Request('https://example.org'), None, 302, '', {}, 'http://localhost/private')


def check(status, url='https://example.org/broken', dep='paper:L20:compactness'):
    return {'url': url, 'title': 'External theorem', 'dependency': 'Compactness at L20',
            'dependency_id': dep, 'status': status, 'note': 'Checked alternate edition and assumptions' if status == 'verified' else 'Source unavailable', 'pages': [],
            'source_evidence': [{'url': URL, 'locator': 'Lemma 1, proof', 'version': '2026 snapshot'}] if status == 'verified' else []}


def observation(directory, job, role, checks):
    path = directory/'jobs'/job/'attempt-0001/reference-observations.json'
    atomic_json(path, {'role': role, 'scope': {'scope_id': 'root'}, 'checks': checks, 'catalogue': []})
    atomic_json(path.parent.parent/'result.json', {'auxiliary_artifacts': {'attempt-0001/reference-observations.json': hashlib.sha256(path.read_bytes()).hexdigest()}})


def test_alternate_source_resolves_dependency_not_another_role(tmp_path):
    observation(tmp_path, 'root-proposer', 'proposer', [check('unresolved'), check('verified', URL)])
    observation(tmp_path, 'issue-validator', 'issue_validator', [check('unresolved')])
    limitations = reference_limits(tmp_path)
    assert len(limitations) == 1 and limitations[0]['role'] == 'issue_validator'
    unsupported = check('verified')
    unsupported['source_evidence'] = []
    assert checked_references([unsupported])[0]['status'] == 'unresolved'
    different = check('verified', dep='paper:L99:different-obligation')
    assert len(references.unresolved_checks([check('unresolved'), different])) == 1


@pytest.mark.parametrize('role', ['proposer', 'issue_validator', 'feedback_auditor'])
def test_every_role_can_request_download_and_reread(tmp_path, monkeypatch, role):
    fixture_http(monkeypatch, {URL: (HTML.encode(), 'text/html')})
    class Backend:
        identity = {'kind': 'fixture'}
        calls = []
        def generate_with_references(self, role, payload, schema, directory, *, reference_tools):
            assert 'reference_checks' in schema['required']
            assert reference_tools['store'] == str((tmp_path/'refs').resolve())
            self.calls.append(deepcopy(payload))
            c = check('verified' if len(self.calls) == 2 else 'unresolved', URL)
            return {'reference_checks': [c], 'result': 'role-specific judgment'}
    backend = Backend()
    wrapper = ReferenceBackend(backend, ReferenceStore(tmp_path/'refs'))
    result = wrapper.generate(role, {'scope': {'scope_id': 'root'}}, {'properties': {}, 'required': []}, tmp_path/'attempt')
    assert result == {'result': 'role-specific judgment'} and len(backend.calls) == 2
    row = read_json(tmp_path/'attempt/reference-observations.json')
    assert row['role'] == role and row['checks'][0]['status'] == 'verified'


def test_stdio_mcp_subprocess_roundtrip(tmp_path, monkeypatch):
    fixture_http(monkeypatch, {URL: (HTML.encode(), 'text/html')})
    store = ReferenceStore(tmp_path/'refs')
    store.fetch(URL)
    requests = [ {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
                 {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                 {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                 {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'read_reference', 'arguments': {'reference': URL, 'query': 'Noetherian'}}} ]
    result = subprocess.run([sys.executable, '-m', 'osverifier_reports.reference_tools', '--store', str(store.directory), '--journal', str(tmp_path/'journal')], input='\n'.join(map(json.dumps, requests))+'\n', text=True, capture_output=True, check=True)
    responses = list(map(json.loads, result.stdout.splitlines()))
    assert len(responses) == 3 and len(responses[1]['result']['tools']) == 3
    assert 'Noetherian' in responses[2]['result']['content'][0]['text']
    assert not responses[2]['result']['isError']


@pytest.mark.parametrize('role', ['proposer', 'issue_validator', 'feedback_auditor'])
@pytest.mark.parametrize('field', ['math', 'ai'])
def test_actual_command_wires_mcp_only_for_paper_roles(tmp_path, monkeypatch, role, field):
    # Capture the real transport command without using a model or a user account.
    from test_codex_transport import fake_codex as fixture
    binary = fixture.__wrapped__(tmp_path)
    binary.write_text(binary.read_text().replace('audit-verifier-feedback', ROLE_SKILLS[role]).replace('output = {', 'output = {"evidence_limits": [], "reference_checks": [], '))
    backend = CodexBackend(model='fixture', codex_bin=str(binary), codex_home=tmp_path/'home')
    selected = AIPaperBackend(backend) if field == 'ai' else backend
    wrapper = ReferenceBackend(selected, ReferenceStore(tmp_path/'refs'))
    wrapper.generate(role, {}, {'type': 'object', 'properties': {}, 'required': []}, tmp_path/'attempt')
    argv = read_json(tmp_path/'attempt/command.json')['argv']
    config = ' '.join(argv)
    assert 'mcp_servers.osverify_literature.command=' in config
    assert 'osverifier_reports.reference_tools' in config and '--store' in config
    assert argv[argv.index('--sandbox')+1] == 'read-only'
    assert '--ignore-user-config' in argv


@pytest.mark.parametrize('wrapper', ['<body><h1>Proof of the theorem</h1>{}</body>', '<div class="entry"><h1>Lemma</h1>{}</div>', '<div class="main-text"><h1>Theorem</h1>{}</div>'])
def test_legacy_mathematical_html_is_readable(wrapper):
    content = '<p>Theorem. ' + 'Assume the stated hypotheses. ' * 30 + '</p>'
    content += '<p>Proof. \\(x\\) \\(y\\) \\(z\\) imply the claim.</p>'
    doc = extract_html('<html>' + wrapper.format(content) + '</html>', 'https://example.edu/notes/lemma.html')
    assert doc['kind'] == 'fulltext' and 'hypotheses' in doc['text']


def test_cached_pdf_can_add_requested_pages_without_changing_earlier_files(tmp_path, monkeypatch):
    from types import SimpleNamespace
    calls = fixture_http(monkeypatch, {'https://example.org/paper.pdf': (b'%PDF-fixture', 'application/pdf')})
    def poppler(args, **kwargs):
        if args[0] == 'pdftotext':
            Path(args[-1]).write_text('First page\fSecond page theorem\f')
        else:
            Path(args[-1] + '.png').write_bytes(b'page fixture')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(references.subprocess, 'run', poppler)
    store = ReferenceStore(tmp_path/'refs')
    first = store.fetch('https://example.org/paper.pdf')
    hashes = dict(first['files'])
    later = store.fetch('https://example.org/paper.pdf', [2])
    assert len(calls) == 1 and later['page_images'][0]['page'] == 2
    assert all(later['files'][k] == v for k, v in hashes.items())
    assert not first.get('page_images')
    assert LiteratureTools(store).read_reference('https://example.org/paper.pdf', page=2)['text'] == '1: Second page theorem'


def test_new_dependency_identity_does_not_change_legacy_records(tmp_path):
    legacy = check('unresolved')
    del legacy['dependency_id']
    del legacy['source_evidence']
    other_url = dict(legacy, url=URL, status='verified')
    observation(tmp_path, 'proposer', 'proposer', [legacy, other_url])
    assert len(reference_limits(tmp_path)) == 1


def test_embedded_pdf_and_labeled_downloads_are_discovered():
    doc = extract_html('<html><iframe src="/article.pdf"></iframe><a href="/paper.pdf">Full article (PDF)</a></html>', 'https://example.org/article')
    assert set(doc['candidates']) == {'https://example.org/article.pdf', 'https://example.org/paper.pdf'}


def test_resolved_dependency_clears_stale_progress_but_preserves_other_limits():
    from types import SimpleNamespace

    from osverifier_reports.paper import refresh_reference_progress
    from osverifier_reports.progress import PaperProgress
    progress = PaperProgress(execution='completed', search='completed', evidence='limited',
                             evidence_limits=['Unresolved reference: Old link', 'Unreadable figure'])
    report = SimpleNamespace(progress=progress, reference_limitations=[])
    refresh_reference_progress(report)
    assert progress.evidence_limits == ['Unreadable figure'] and progress.evidence == 'limited'
    progress.evidence_limits = ['Unresolved reference: Old link']
    refresh_reference_progress(report)
    assert progress.evidence_limits == [] and progress.evidence == 'no_reported_limits'


def test_versioned_arxiv_landing_never_falls_back_to_latest_pdf():
    raw = '<html><meta name="citation_pdf_url" content="https://arxiv.org/pdf/2310.17965"><a href="/pdf/2310.17965v2">PDF</a></html>'
    assert extract_html(raw, 'https://arxiv.org/abs/2310.17965v1')['candidates'] == ['https://arxiv.org/pdf/2310.17965v1']


@pytest.mark.parametrize('content', [
    '<section class="abstract">{prose}</section><nav class="toc">Introduction Proof Theorem</nav>',
    '<h2>Abstract</h2>{prose}<h2>Table of contents</h2><p>Introduction Proof Theorem</p>',
    '<div id="productDetailMainWrapper">{prose}</div>',
    '<div id="issueTabContent">Contents of Volume 40{prose}<a href="unrelated.pdf">PDF</a></div>',
    '<div class="table-of-contents">{prose}</div>',
])
def test_metadata_cannot_satisfy_body_evidence(content):
    prose = '<p>Introduction. Theorem. Proof. ' + ('A statement with assumptions $x$ $y$ $z$. ' * 80) + '</p>'
    doc = extract_html('<html><main><h1>Research</h1>' + content.format(prose=prose) + '</main></html>', 'https://example.org/article')
    assert doc['kind'] == 'landing_page'
    assert not doc['candidates']  # In particular, never follow the journal issue's PDF.


def test_real_body_after_abstract_and_toc_is_still_readable():
    raw = '<html><main><h1>Paper</h1><h2>Abstract</h2><p>Summary.</p><h2>Contents</h2><a href="#intro">Introduction</a><h2>Introduction</h2>'
    raw += '<p>Theorem. ' + 'Assume the hypotheses and consider the following construction. ' * 30 + '</p><h2>Proof</h2><p>Thus the result holds.</p></main></html>'
    doc = extract_html(raw, 'https://example.org/paper')
    assert doc['kind'] == 'fulltext'
    assert 'metadata_heading_section' in doc['classification']['excluded_regions']


def test_catalogue_does_not_download_unrelated_current_articles(tmp_path, monkeypatch):
    url = 'https://example.org/old-paper.pdf'
    raw = b'<html><main id="issueTabContent"><h1>Journal</h1><p>Contents of Volume 50</p><a href="other.pdf">PDF</a></main></html>'
    calls = fixture_http(monkeypatch, {url: (raw, 'text/html')})
    result = LiteratureTools(ReferenceStore(tmp_path/'refs'), web_search='live').resolve_reference(url, search_query='Original title Author 1989')
    assert result['retrieval']['status'] == 'unresolved'
    assert result['retrieval']['access_status'] == 'metadata_only'
    assert result['fallback']['query'] == 'Original title Author 1989'
    assert calls == [url]


def test_search_failure_has_fallback_and_web_candidates_keep_provenance(tmp_path, monkeypatch):
    from osverifier_reports import reference_tools
    def failed(url):
        raise OSError('HTTP 429')
    monkeypatch.setattr(reference_tools, 'metadata_json', failed)
    service = LiteratureTools(ReferenceStore(tmp_path/'refs'), tmp_path/'calls.jsonl', web_search='live')
    result = service.call('search_literature', {'query': 'Original title'})
    assert result['status'] == 'search_failed'
    assert result['fallback']['status'] == 'search_required'
    fixture_http(monkeypatch, {URL: (HTML.encode(), 'text/html')})
    candidate = service.call('search_literature', {'query': 'Original title author PDF', 'web_results': [{'title': 'Original title', 'url': URL}]})
    assert candidate['status'] == 'metadata_only' and not service.store.lookup(URL)
    resolved = service.call('resolve_reference', {'reference': URL, 'search_id': candidate['search_id']})
    assert resolved['discovery']['query'] == 'Original title author PDF'
    assert resolved['retrieval']['status'] == 'available'
    with pytest.raises(ValueError, match='not a candidate'):
        service.resolve_reference('https://example.org/another.pdf', search_id=candidate['search_id'])
    path = Path(candidate['search_receipt'])
    value = read_json(path)
    value['query'] = 'changed'
    atomic_json(path, value)
    with pytest.raises(ValueError, match='receipt changed'):
        service.resolve_reference(URL, search_id=candidate['search_id'])


@pytest.mark.parametrize('mode', ['disabled', 'unavailable', 'cached', 'live'])
@pytest.mark.parametrize('role', ['proposer', 'issue_validator', 'feedback_auditor'])
def test_fallback_mode_reaches_every_role(tmp_path, mode, role):
    from osverifier_reports.reference_tools import tool_config
    class Backend:
        identity = {'kind': 'fixture', 'web_search': mode}
        def generate_with_references(self, selected_role, payload, schema, directory, *, reference_tools):
            assert selected_role == role
            assert reference_tools['web_search'] == mode
            args = tool_config(reference_tools)['mcp_servers.osverify_literature.args']
            assert args[args.index('--web-search') + 1] == mode
            service = LiteratureTools(ReferenceStore(reference_tools['store']), web_search=mode)
            assert service._fallback('Title', 'failed')['status'] == ('search_required' if mode in {'live', 'cached'} else mode)
            return {'reference_checks': []}
    ReferenceBackend(Backend(), ReferenceStore(tmp_path/'refs')).generate(role, {}, {'properties': {}, 'required': []}, tmp_path/'attempt')


def test_previous_version_store_is_not_reused_by_new_classifier(tmp_path):
    atomic_json(tmp_path/'store-version.json', {'version': 2})
    with pytest.raises(FrozenRunMismatch, match='version changed'):
        ReferenceStore(tmp_path).fetch(URL)
    assert read_json(tmp_path/'store-version.json') == {'version': 2}


def test_deeply_nested_legacy_html_is_extracted_without_recursion():
    # latex2html-style pages often open thousands of <P> paragraphs without closing them.
    body = ''.join(f'<P>Theorem {i}. The proof uses the stated lemma and its conclusion.\n' for i in range(5000))
    doc = extract_html('<html><head><title>Notes</title></head><body><h1>Lecture</h1>' + body + '</body></html>', 'https://example.edu/notes.html')
    assert 'Theorem 4999.' in doc['text'] and doc['classification']['body_char_count'] > 300000


def test_unusable_source_is_a_receipt_not_a_crash(tmp_path, monkeypatch):
    import http.client
    import io

    from osverifier_reports.reference_tools import serve

    class Opener:
        def open(self, request, **kwargs):
            if ' ' in request.full_url:
                raise http.client.InvalidURL("URL can't contain control characters")
            raise http.client.IncompleteRead(b'partial', 100)
    monkeypatch.setattr(references, 'public_url', lambda url: url)
    monkeypatch.setattr(references.urllib.request, 'build_opener', lambda *a: Opener())
    store = ReferenceStore(tmp_path/'refs')
    record = store.fetch('https://example.org/Smith 2019.pdf')
    assert record['status'] == 'unresolved' and record['retrievals'][0]['error_kind'] == 'invalid_url'
    assert store.lookup('https://example.org/Smith 2019.pdf')['reason'].startswith('InvalidURL')
    assert store.fetch('https://example.org/cut.pdf')['retrievals'][0]['error_kind'] == 'truncated'

    # The MCP server answers every request, including failing tools and malformed parameters.
    tools = LiteratureTools(store, tmp_path/'journal.jsonl')
    monkeypatch.setattr(tools, 'search_literature', lambda **kw: (_ for _ in ()).throw(RecursionError('deep')))
    lines = [{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'resolve_reference', 'arguments': {'reference': 'https://example.org/a b.pdf'}}},
             {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'search_literature', 'arguments': {'query': 'x'}}},
             {'jsonrpc': '2.0', 'id': 3, 'method': 'initialize', 'params': ['not', 'an', 'object']},
             {'jsonrpc': '2.0', 'id': 4, 'method': 'ping'}]
    out = io.StringIO()
    serve(tools, (json.dumps(line) + '\n' for line in lines), out)
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r['id'] for r in responses] == [1, 2, 3, 4]
    assert not responses[0]['result']['isError'] and responses[0]['result']['content'][0]['text']
    assert responses[1]['result']['isError'] and 'RecursionError' in responses[1]['result']['content'][0]['text']
    assert 'error' in responses[2] and responses[3]['result'] == {}

    # A completed role result survives a failing post-call download and records the failure.
    class Backend:
        identity = {'kind': 'fixture'}
        def generate(self, role, payload, schema, directory):
            return {'reference_checks': [check('unresolved', 'https://example.org/notes.pdf')], 'result': 'judgment'}
    def broken(self, url, pages=()):
        raise RuntimeError('cache directory unavailable')
    monkeypatch.setattr(ReferenceStore, 'fetch', broken)
    wrapper = ReferenceBackend(Backend(), store)
    assert wrapper.generate('proposer', {'scope': {'scope_id': 'root'}}, {'properties': {}, 'required': []}, tmp_path/'attempt') == {'result': 'judgment'}
    row = read_json(tmp_path/'attempt/reference-observations.json')
    assert row['fetch_errors'] == [{'url': 'https://example.org/notes.pdf', 'error': 'RuntimeError: cache directory unavailable'}]


def http_response(raw, url):
    """A real http.client response over captured bytes (no socket)."""
    import http.client

    class Capture:
        def makefile(self, *args, **kwargs):
            return io.BytesIO(raw)
    response = http.client.HTTPResponse(Capture())
    response.begin()
    response.url = url
    return response


@pytest.mark.parametrize('framing', ['content-length', 'chunked'])
def test_truncated_body_is_not_recorded_as_fulltext(tmp_path, monkeypatch, framing):
    from osverifier_reports import reference_tools

    page = HTML.encode()
    half = page[:len(page) // 2]
    if framing == 'chunked':
        raw = b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nTransfer-Encoding: chunked\r\n\r\n' + b'%x\r\n' % len(page) + half
    else:
        raw = b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n\r\n' % len(page) + half

    class Opener:
        def open(self, request, **kwargs):
            return http_response(raw, request.full_url)
    monkeypatch.setattr(references, 'public_url', lambda url: url)
    monkeypatch.setattr(references.urllib.request, 'build_opener', lambda *a: Opener())
    monkeypatch.setattr(reference_tools, 'public_url', lambda url: url)
    monkeypatch.setattr(reference_tools.urllib.request, 'build_opener', lambda *a: Opener())
    record = ReferenceStore(tmp_path/'refs').fetch(URL)
    assert record['status'] == 'unresolved' and record['access_status'] != 'fulltext'
    assert record['retrievals'][0]['error_kind'] == 'truncated' and 'TruncatedDownload' in record['reason']
    with pytest.raises(references.TruncatedDownload):
        reference_tools.metadata_json('https://api.crossref.org/works?query=x')
    complete = b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n\r\n' % len(page) + page
    monkeypatch.setattr(Opener, 'open', lambda self, request, **kwargs: http_response(complete, request.full_url))
    assert ReferenceStore(tmp_path/'complete').fetch(URL)['status'] == 'available'
