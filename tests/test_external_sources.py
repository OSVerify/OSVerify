"""External sources can be readable without being full research articles."""
from __future__ import annotations

import io
import json
import socket
import ssl
import urllib.error

import pytest
from test_literature_tools import fixture_http

from osverifier_reports import references
from osverifier_reports.reference_tools import LiteratureTools
from osverifier_reports.references import ReferenceStore


@pytest.mark.parametrize('html', [
    '<html><main><h1>Alice Smith</h1><p>Research notes and software.</p><a href="https://github.com/example/project">Code</a></main></html>',
    '<html><main><h1>Alice Smith</h1><p>I study geometry and maintain the software used in this experiment.</p></main></html>',
    '<html><article><h1>Evaluation results</h1><p>Our model scored 83 on the stated benchmark. The evaluation used the following settings.</p></article></html>',
    '<html><body><h1>Project notes</h1><p>The implementation uses exact rational arithmetic throughout the computation.</p></body></html>',
    '<html><main><article class="markdown-body"><h1>Repository</h1><p>Install this package to reproduce the reported calculation.</p></article></main></html>',
])
def test_web_pages_are_readable_and_not_labeled_paper_fulltext(tmp_path, monkeypatch, html):
    url = 'https://example.org/result'
    fixture_http(monkeypatch, {url: (html.encode(), 'text/html')})
    tools = LiteratureTools(ReferenceStore(tmp_path), web_search='live')
    record = tools.resolve_reference(url)['retrieval']
    assert record['status'] == 'available'
    assert record['access_status'] == 'web_page'
    assert record['retrievals'][0]['http_status'] == 200
    result = tools.read_reference(url)
    assert result['status'] == 'passage'
    assert result['source_kind'] == 'web_page'
    assert 'not a full scholarly article' in result['content_quality']


@pytest.mark.parametrize('suffix,content_type,text', [
    ('README.md', 'text/plain; charset=utf-8', '# Result\n\nThe score was 83.\n'),
    ('notes.txt', 'text/plain', 'The independent calculation returns 83.\n'),
    ('compute.py', 'text/plain; charset=utf-8', 'print(83)\n'),
    ('README.md', 'application/octet-stream', '# Result\n\nThe score was 83.\n'),
    ('README.md', 'text/plain', '# Example\n```html\n<html><body>Hello</body></html>\n```\n'),
    ('template.py', 'text/plain', 'template = "<html><body>Hello</body></html>"\n'),
    ('README.md', 'text/plain', '# TeX example\n\\section{Results}\nThe score was 83.\n'),
])
def test_github_raw_text_is_available_with_exact_lines(tmp_path, monkeypatch, suffix, content_type, text):
    url = 'https://github.com/example/project/blob/main/' + suffix
    raw = 'https://raw.githubusercontent.com/example/project/main/' + suffix
    calls = fixture_http(monkeypatch, {raw: (text.encode(), content_type)})
    tools = LiteratureTools(ReferenceStore(tmp_path))
    record = tools.resolve_reference(url)['retrieval']
    assert record['status'] == 'available' and record['source_kind'] == 'text_file'
    assert record['resolved_url'] == raw
    assert calls == [raw]
    result = tools.read_reference(url)
    assert result['text'] == '\n'.join(f'{i}: {line}' for i, line in enumerate(text.splitlines(), 1))


@pytest.mark.parametrize('html', [
    '<html><title>Just a moment</title><main><p>Please enable JavaScript and cookies to continue.</p></main></html>',
    '<html><main><h1>Abstract</h1><p>This abstract describes a result without the proof or complete implementation.</p></main></html>',
    '<html><meta name="citation_title" content="A theorem"><main><p>Authors, abstract, and journal information only.</p></main></html>',
    '<html><main><h1>A theorem</h1><p><strong>Abstract:</strong> We describe a new method and its possible implications without giving its proof here.</p></main></html>',
    '<html><main id="issueTabContent"><h1>Contents of Volume 5</h1><p>Available journal articles.</p><a href="wrong.pdf">PDF</a></main></html>',
    '<html><main><h1>Contents</h1><a href="a">One</a><a href="b">Two</a></main></html>',
    '<html><body><div id="root"></div><script>renderArticle()</script></body></html>',
])
def test_access_challenges_and_metadata_stay_unresolved(tmp_path, monkeypatch, html):
    url = 'https://example.org/article'
    fixture_http(monkeypatch, {url: (html.encode(), 'text/html')})
    record = ReferenceStore(tmp_path).fetch(url)
    assert record['status'] == 'unresolved' and record['access_status'] != 'fulltext'


def test_generic_landing_still_follows_explicit_paper_download(tmp_path, monkeypatch):
    url, paper = 'https://example.org/work', 'https://example.org/paper.tex'
    html = b'<html><main><h1>Project page</h1><p>Our full argument is in the paper below.</p><a href="paper.tex" download>Download</a></main></html>'
    fixture_http(monkeypatch, {url: (html, 'text/html'), paper: (b'\\section{Proof}\nExact source.', 'application/x-tex')})
    record = ReferenceStore(tmp_path).fetch(url)
    assert record['status'] == 'available' and record['format'] == 'latex'
    assert record['resolved_url'] == paper


def test_tex_served_as_plain_text_keeps_source_format(tmp_path, monkeypatch):
    url = 'https://example.org/paper.tex'
    fixture_http(monkeypatch, {url: (b'\\section{Proof}\nExact source.', 'text/plain')})
    record = ReferenceStore(tmp_path).fetch(url)
    assert record['status'] == 'available' and record['format'] == 'latex'


def test_html_challenge_at_markdown_url_is_not_treated_as_text(tmp_path, monkeypatch):
    url = 'https://example.org/README.md'
    fixture_http(monkeypatch, {url: (b'<html><title>Just a moment</title><body>Please enable JavaScript and cookies.</body></html>', 'text/html')})
    record = ReferenceStore(tmp_path).fetch(url)
    assert record['status'] == 'unresolved' and record['access_status'] == 'access_challenge'


@pytest.mark.parametrize('data,content_type', [
    (b'\x89PNG\x00\x01\xff', 'text/plain'),
    (b'PK\x03\x04\x00binary\x00', 'application/octet-stream'),
    (b'\x00\x00invalid markdown', 'text/markdown'),
])
def test_binary_disguised_as_text_is_not_released(tmp_path, monkeypatch, data, content_type):
    url = 'https://example.org/README.md'
    fixture_http(monkeypatch, {url: (data, content_type)})
    record = ReferenceStore(tmp_path).fetch(url)
    assert record['status'] == 'unresolved'
    assert record['retrievals'][0]['error_kind'] == 'unsupported_format'
    assert record['retrievals'][0]['http_status'] == 200


@pytest.mark.parametrize('underlying,kind', [
    (socket.gaierror(-2, 'Name not known'), 'dns'),
    (ssl.SSLCertVerificationError('CERTIFICATE_VERIFY_FAILED'), 'tls_certificate'),
    (TimeoutError('timed out'), 'timeout'),
])
def test_urlerror_keeps_actual_network_failure_kind(tmp_path, monkeypatch, underlying, kind):
    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.URLError(underlying)
    monkeypatch.setattr(references, 'public_url', lambda url: url)
    monkeypatch.setattr(references.urllib.request, 'build_opener', lambda *a: Opener())
    record = ReferenceStore(tmp_path).fetch('https://example.org/paper')
    assert record['status'] == 'unresolved'
    assert record['retrievals'][0]['error_kind'] == kind


def test_mcp_roundtrip_preserves_web_source_kind(tmp_path, monkeypatch):
    from osverifier_reports.reference_tools import serve
    url = 'https://example.org/results'
    html = b'<html><main><h1>Results</h1><p>The benchmark score is 83 using the described evaluation procedure.</p></main></html>'
    fixture_http(monkeypatch, {url: (html, 'text/html')})
    requests = [{'jsonrpc': '2.0', 'id': i, 'method': 'tools/call',
                 'params': {'name': name, 'arguments': {'reference': url}}}
                for i, name in enumerate(['resolve_reference', 'read_reference'], 1)]
    out = io.StringIO()
    serve(LiteratureTools(ReferenceStore(tmp_path)), (json.dumps(r)+'\n' for r in requests), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert all(not r['result']['isError'] for r in replies)
    read = json.loads(replies[-1]['result']['content'][0]['text'])
    assert read['source_kind'] == 'web_page' and 'benchmark score is 83' in read['text']


@pytest.mark.parametrize('classes', ['toc-visible:md:grid-cols-10', 'toc-collision-target', 'contents'])
def test_layout_classes_do_not_discard_real_article_body(tmp_path, monkeypatch, classes):
    # Shape observed on the public OpenAI result page; prose is synthetic.
    url = 'https://example.org/results'
    html = f'<html><main><article><div class="{classes}"><h1>Evaluation</h1><p>The benchmark score is 83. We provide the exact evaluation setup here.</p></div></article></main></html>'
    fixture_http(monkeypatch, {url: (html.encode(), 'text/html')})
    tools = LiteratureTools(ReferenceStore(tmp_path))
    assert tools.resolve_reference(url)['retrieval']['status'] == 'available'
    assert 'benchmark score is 83' in tools.read_reference(url)['text']


@pytest.mark.parametrize('attribute', ['class="table-of-contents"', 'class="article-abstract"',
                                      'class="ltx_bibliography"', 'id="contents"'])
def test_semantic_metadata_containers_are_still_excluded(tmp_path, monkeypatch, attribute):
    url = 'https://example.org/paper'
    html = f'<html><main><div {attribute}><h1>Research</h1><p>The alleged result appears here only in metadata, without its actual supporting argument.</p></div></main></html>'
    fixture_http(monkeypatch, {url: (html.encode(), 'text/html')})
    assert ReferenceStore(tmp_path).fetch(url)['status'] == 'unresolved'
