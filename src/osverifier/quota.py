"""Recognize explicit provider quota failures, without inspecting model/tool content."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from .storage import atomic_json

RESUME_MESSAGE = (
    "Codex quota exhausted. After quota recovers, rerun the same command and run directory."
)
QUOTA_CODES = {
    "usagelimitreached",
    "usagelimitexceeded",
    "insufficientquota",
    "quotaexceeded",
    "billinghardlimitreached",
}
QUOTA_MESSAGE = re.compile(
    r"\b(?:you(?:'ve| have)?\s+(?:hit|reached|exceeded)\s+your\s+(?:current\s+)?(?:usage\s+limit|quota)"
    r"|(?:usage\s+limit|quota)\s+(?:has\s+been\s+)?(?:exhausted|reached))\b",
    re.IGNORECASE,
)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class BackendStalled(subprocess.TimeoutExpired):
    """Operational inactivity deadline, not a mathematical verdict."""


class QuotaExhausted(RuntimeError):
    def __init__(self, detail: str, source: str = "provider"):
        super().__init__(RESUME_MESSAGE)
        self.detail, self.source = detail, source

    def record(self):
        return {
            "reason": "quota_exhausted",
            "message": RESUME_MESSAGE,
            "detail": self.detail,
            "source": self.source,
            "resume": "manual",
        }


def record_quota(directory: Path, error: QuotaExhausted, **context):
    atomic_json(directory / "interruption.json", {**context, **error.record()})


def _explicit_quota(value) -> str | None:
    if isinstance(value, str):
        normalized = re.sub(r"[^a-z]", "", value.lower())
        codes = re.split(r"[^a-zA-Z_]+", value)
        if (
            normalized in QUOTA_CODES
            or any(word.replace("_", "").lower() in QUOTA_CODES for word in codes)
            or QUOTA_MESSAGE.search(value.replace("’", "'"))
        ):
            return value[:2000]
    elif isinstance(value, dict):
        for key in ("code", "type", "kind", "codex_error_info", "message", "error"):
            if detail := _explicit_quota(value.get(key)):
                return detail
    return None


def quota_from_event(event) -> QuotaExhausted | None:
    # Agent text, tool failures, HTTP statuses, and reported usage percentages are
    # deliberately not provider quota signals.
    if not isinstance(event, dict) or event.get("type") not in ("error", "turn.failed"):
        return None
    if detail := _explicit_quota(event):
        return QuotaExhausted(detail, "events.jsonl")
    return None


class QuotaMonitor:
    def __init__(self, attempt: Path):
        self.attempt = attempt
        self.offset = 0
        self.pending = b""
        self.last_progress = time.monotonic()

    def poll(self, *, final=False) -> QuotaExhausted | None:
        with (self.attempt / "events.jsonl").open("rb") as stream:
            stream.seek(self.offset)
            self.pending += stream.read()
            self.offset = stream.tell()
        lines = self.pending.split(b"\n")
        self.pending = lines.pop()
        if final and self.pending:
            lines.append(self.pending)
            self.pending = b""
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(event, dict) and event.get("type") in ("item.started", "item.updated", "item.completed", "turn.completed"):
                self.last_progress = time.monotonic()
            if error := quota_from_event(event):
                error.source = str(self.attempt / "events.jsonl")
                return error
        return None

    def failed_stderr(self) -> QuotaExhausted | None:
        # Older CLI versions may only print a terminal ERROR line. Do not scan
        # arbitrary stderr prose or accept a bare 429 as exhausted quota.
        path = self.attempt / "stderr.log"
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = ANSI.sub("", line).strip()
                if line.startswith("ERROR:") or line.startswith("error:"):
                    if detail := _explicit_quota(line.split(":", 1)[1]):
                        return QuotaExhausted(detail, str(path))
        return None


def redacted_command(process) -> str:
    """Name only the executable. argv carries connection overrides that must not
    reach exception text, which callers persist (failure.json, trajectories)."""
    args = process.args
    program = args[0] if isinstance(args, (list, tuple)) and args else args
    return f"{Path(str(program)).name} (arguments omitted; see command.json)"


def communicate_with_quota_detection(process, prompt, timeout, attempt, on_quota=None, *, idle_timeout=None):
    """Watch provider errors while the CLI runs, including before its own retries."""
    monitor = QuotaMonitor(attempt)
    deadline = None if timeout is None else time.monotonic() + timeout
    input_text = prompt
    while True:
        try:
            interval = 0.2 if deadline is None else min(0.2, max(0, deadline - time.monotonic()))
            process.communicate(input=input_text, timeout=interval)
            finished = True
        except subprocess.TimeoutExpired:
            finished = False
        input_text = None
        error = monitor.poll(final=finished)
        if error is None and finished and process.returncode:
            error = monitor.failed_stderr()
        if error is not None:
            record_quota(attempt, error)
            if on_quota:
                on_quota(error)
            raise error
        if finished:
            return
        if idle_timeout is not None and time.monotonic() - monitor.last_progress >= idle_timeout:
            atomic_json(attempt / "stall.json", {"reason": "no_progress_timeout", "idle_seconds": idle_timeout})
            raise BackendStalled(redacted_command(process), idle_timeout)
        if deadline is not None and time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(redacted_command(process), timeout)
