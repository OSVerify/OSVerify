from __future__ import annotations

import io
import time

from osverifier_reports import reference_tools, references
from osverifier_reports.reference_tools import tool_config
from osverifier_reports.references import ReferenceBackend, ReferenceStore

URL = 'https://example.edu/slow.tex'
BODY = b'\\section{Theorem}\n' + b'Continuous source text.\n' * 20000


def stream(monkeypatch, *, stall=False):
    clock = [0.0]
    timeouts = []

    class Response(io.BytesIO):
        url = URL
        headers = {'Content-Type': 'application/x-tex'}

        def read(self, size=-1):
            raise AssertionError('A filling read can hide progress and the total deadline')

        def read1(self, size=-1):
            if stall:
                clock[0] += 20
                raise TimeoutError('socket read timed out')
            chunk = io.BytesIO.read(self, size)
            if chunk:
                clock[0] += 15  # Progress every 15s; complete source takes >75s.
            return chunk

    class Opener:
        def open(self, request, timeout):
            timeouts.append(timeout)
            return Response(BODY)

    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(references, 'public_url', lambda url: url)
    monkeypatch.setattr(references.urllib.request, 'build_opener', lambda *a: Opener())
    return clock, timeouts


def test_continuous_download_past_75_seconds_is_preserved(tmp_path, monkeypatch):
    clock, timeouts = stream(monkeypatch)
    store = ReferenceStore(tmp_path)
    result = store.fetch(URL)
    assert result['status'] == 'available' and clock[0] > 75
    assert result['timeout_policy'] == {'idle_seconds': 20, 'download_seconds': 300}
    assert result['retrievals'][0]['byte_count'] == len(BODY)
    assert timeouts == [20]


def test_stalled_download_still_stops_at_idle_timeout(tmp_path, monkeypatch):
    clock, _ = stream(monkeypatch, stall=True)
    result = ReferenceStore(tmp_path).fetch(URL)
    assert result['status'] == 'unresolved' and clock[0] == 20
    assert 'no progress within 20 seconds' in result['reason']
    assert result['retrievals'][0]['error_kind'] == 'timeout'


def test_total_budget_still_bounds_continuously_progressing_download(tmp_path, monkeypatch):
    clock, _ = stream(monkeypatch)
    result = ReferenceStore(tmp_path, download_timeout_seconds=45).fetch(URL)
    assert result['status'] == 'unresolved' and clock[0] == 45
    assert 'total download budget exceeded' in result['reason']
    assert result['retrievals'][0]['byte_count'] > 0


def test_role_mcp_server_and_wait_budget_receive_same_timeout_policy(tmp_path, monkeypatch):
    store = ReferenceStore(tmp_path / 'refs', idle_timeout_seconds=12, download_timeout_seconds=600)

    class Backend:
        identity = {'kind': 'fixture'}

        def generate_with_references(self, role, payload, schema, directory, *, reference_tools):
            return reference_tools

    wrapper = ReferenceBackend(Backend(), store)
    assert wrapper.identity['reference_timeouts'] == store.timeout_policy
    for role in references.REFERENCE_ROLES:
        descriptor = wrapper._generate(role, {}, {}, tmp_path / role)
        config = tool_config(descriptor)
        args = config['mcp_servers.osverify_literature.args']
        assert args[args.index('--idle-timeout-seconds') + 1] == '12.0'
        assert args[args.index('--download-timeout-seconds') + 1] == '600.0'
        assert config['mcp_servers.osverify_literature.tool_timeout_sec'] > 2 * store.wait_timeout_seconds
    captured = []
    monkeypatch.setattr(reference_tools, 'serve', lambda service, *args: captured.append(service.store.timeout_policy))
    reference_tools.main(['--store', str(tmp_path / 'server'), '--journal', str(tmp_path / 'journal'),
                          '--idle-timeout-seconds', '12', '--download-timeout-seconds', '600'])
    assert captured == [store.timeout_policy]
