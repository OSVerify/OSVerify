"""Append-only, thread-safe observable execution trajectory."""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .storage import atomic_text


class TrajectoryCorrupted(ValueError):
    pass


def parse_events(data: bytes):
    """Return events and the byte offset of a malformed final record, if any.

    An interrupted append can leave only the last record incomplete; a malformed
    record anywhere earlier is corruption that must not be dropped silently.
    """
    lines = data.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    events, offset = [], 0
    for number, line in enumerate(lines, 1):
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("sequence"), int):
                raise ValueError("not a trajectory event")
        except ValueError:
            if number == len(lines):
                return events, offset
            raise TrajectoryCorrupted(
                f"trajectory.jsonl record {number} is malformed; repair it manually"
            ) from None
        events.append(event)
        offset += len(line) + 1
    return events, None


class Trajectory:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "trajectory.jsonl"
        self.lock = threading.Lock()
        self.sequence = 0
        if self.path.exists():
            # Callers hold the run lock, so no other writer is appending.
            data = self.path.read_bytes()
            events, torn = parse_events(data)
            self.sequence = max((event["sequence"] for event in events), default=0)
            if torn is not None:
                fragment = data[torn:]
                kept = directory / f"trajectory-discarded-tail-{self.sequence:06d}.bin"
                kept.write_bytes(fragment)
                os.truncate(self.path, torn)
                self.record(
                    "trajectory_tail_discarded",
                    reason="malformed_final_record",
                    bytes=len(fragment),
                    sha256=hashlib.sha256(fragment).hexdigest(),
                    artifact=kept.name,
                )
            elif data and not data.endswith(b"\n"):
                with self.path.open("ab") as stream:
                    stream.write(b"\n")

    def record(self, event, **fields):
        with self.lock:
            self.sequence += 1
            value = {
                "sequence": self.sequence,
                "at": datetime.now(timezone.utc).isoformat(),
                "event": event,
                **fields,
            }
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(value, ensure_ascii=False) + "\n")
                stream.flush()

    def render(self):
        with self.lock:
            events, _ = parse_events(self.path.read_bytes())
        lines = [
            "# Execution trajectory",
            "",
            "Internal execution records. Mathematical prose here is not released feedback.",
            "Role inputs, schemas, skills, responses, and CLI-visible tool traces remain in jobs/.",
            "CLI logs expose only what the CLI emits; this does not claim access to hidden reasoning.",
            "",
            "| # | UTC | Event | Details |",
            "| --- | --- | --- | --- |",
        ]
        for item in events:
            details = (
                json.dumps(
                    {k: v for k, v in item.items() if k not in ("sequence", "at", "event")},
                    ensure_ascii=False,
                )
                .replace("|", "\\|")
                .replace("\n", " ")
            )
            lines.append(f"| {item['sequence']} | {item['at']} | {item['event']} | {details} |")
        atomic_text(self.directory / "trajectory.md", "\n".join(lines) + "\n")
