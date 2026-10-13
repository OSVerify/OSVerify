"""Shared test helpers: a scripted fake Codex CLI and a polling helper."""

from __future__ import annotations

import json
import stat
import time
import urllib.error
import urllib.request

FAKE_CODEX = """#!/bin/sh
# Scripted stand-in: version/login answer immediately; exec blocks until interrupted.
case "$1" in
  --version) echo "codex-cli 0.162.0-fake"; exit 0 ;;
  login)
    if [ "$2" = "status" ]; then echo "Logged in using ChatGPT"; exit 0; fi
    echo "Starting local login server on http://localhost:1455."
    echo "navigate to this URL: https://auth.example/oauth?state=abc"
    sleep 60 ;;
  exec) sleep 60; exit 1 ;;
esac
exit 1
"""


def fake_codex(tmp_path):
    path = tmp_path / "bin" / "codex"
    path.parent.mkdir()
    path.write_text(FAKE_CODEX)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def call(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())




def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("condition not met in time")
