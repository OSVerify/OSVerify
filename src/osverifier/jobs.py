"""Bounded, resumable jobs. Only locally validated results enter the cache."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from threading import Lock

from pydantic import BaseModel

from .backend import Backend, BackendCancelled
from .connection_config import redact_backend_text
from .quota import QuotaExhausted, record_quota
from .storage import FrozenRunMismatch, atomic_json, digest, package_fingerprint, read_json


class JobFailed(RuntimeError):
    pass


class JobRunner:
    def __init__(
        self,
        backend: Backend,
        run_dir: Path,
        max_attempts: int,
        package_sha256: str,
        event_sink=None,
    ):
        self.backend = backend
        self.run_dir = run_dir
        self.max_attempts = max_attempts
        self.package_sha256 = package_sha256
        self.event_sink = event_sink
        self._quota_lock = Lock()
        self._quota_error = None

    @property
    def quota_error(self):
        with self._quota_lock:
            return self._quota_error

    def check_quota(self):
        with self._quota_lock:
            if self._quota_error is not None:
                raise self._quota_error

    def halt_for_quota(self, error, **context):
        with self._quota_lock:
            if self._quota_error is not None:
                return
            self._quota_error = error
            record_quota(self.run_dir, error, **context)
            self.emit("quota_exhausted", **context, reason="quota_exhausted")
        halt = getattr(self.backend, "halt_for_quota", None)
        if halt:
            halt(error)

    def emit(self, event, **fields):
        if self.event_sink:
            self.event_sink(event, **fields)

    def call(
        self,
        job_key: str,
        role: str,
        payload: dict,
        output_type: type[BaseModel],
        validate_links: Callable[[BaseModel], None],
    ):
        if package_fingerprint() != self.package_sha256:
            raise FrozenRunMismatch("package or skills changed during the run")
        directory = self.run_dir / "jobs" / job_key
        cache = directory / "result.json"
        # Exact, verified cache hits remain available after quota exhaustion; the
        # latch blocks only new jobs and backend launches.
        if not cache.exists():
            self.check_quota()
        directory.mkdir(parents=True, exist_ok=True)
        schema = output_type.model_json_schema()
        fingerprint = digest({"role": role, "payload": payload, "schema": schema})
        frozen_path = directory / "input.json"
        if frozen_path.exists():
            frozen = read_json(frozen_path)
            if (
                frozen["fingerprint"] != fingerprint
                or digest(
                    {
                        "role": frozen["role"],
                        "payload": frozen["payload"],
                        "schema": schema,
                    }
                )
                != fingerprint
            ):
                raise FrozenRunMismatch(f"job inputs changed: {job_key}")
        else:
            atomic_json(frozen_path, {"fingerprint": fingerprint, "role": role, "payload": payload})
        if cache.exists():
            saved = read_json(cache)
            if (
                saved["fingerprint"] != fingerprint
                or digest(saved["output"]) != saved["output_sha256"]
            ):
                raise FrozenRunMismatch(f"cached artifact integrity check failed: {job_key}")
            parsed = output_type.model_validate(saved["output"])
            validate_links(parsed)
            for relative, expected in saved.get("auxiliary_artifacts", {}).items():
                path = (directory / relative).resolve()
                if not path.is_relative_to(directory.resolve()) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise FrozenRunMismatch(f"cached auxiliary artifact changed: {job_key}")
            self.emit(
                "job_cache_hit",
                job=job_key,
                role=role,
                artifact=str(cache.relative_to(self.run_dir)),
            )
            return parsed

        for _ in range(self.max_attempts):
            self.check_quota()
            previous = [
                int(p.name.split("-")[-1]) for p in directory.glob("attempt-*") if p.is_dir()
            ]
            attempt = directory / f"attempt-{max(previous, default=0) + 1:04d}"
            attempt.mkdir()
            self.emit(
                "attempt_started",
                job=job_key,
                role=role,
                attempt=attempt.name,
                input_artifact=str(frozen_path.relative_to(self.run_dir)),
            )
            try:
                raw = self.backend.generate(role, payload, schema, attempt)
                atomic_json(attempt / "response.json", raw)
                parsed = output_type.model_validate(raw)
                validate_links(parsed)
                if package_fingerprint() != self.package_sha256:
                    raise FrozenRunMismatch("package or skills changed during a model call")
            except FrozenRunMismatch:
                raise
            except QuotaExhausted as exc:
                self.halt_for_quota(exc, job=job_key, role=role, attempt=attempt.name)
                record_quota(attempt, exc)
                self.emit("attempt_quota_exhausted", job=job_key, role=role, attempt=attempt.name)
                raise
            except BackendCancelled as exc:
                atomic_json(
                    attempt / "cancelled.json", {"reason": redact_backend_text(self.backend, str(exc))}
                )
                self.emit("attempt_cancelled", job=job_key, role=role, attempt=attempt.name)
                raise
            except Exception as exc:
                # Keep full contract errors internally. The public report references only the job.
                # Exception text is persisted, so strip any connection override it carries.
                atomic_json(
                    attempt / "failure.json",
                    {"type": type(exc).__name__, "error": redact_backend_text(self.backend, str(exc))},
                )
                self.emit(
                    "attempt_failed",
                    job=job_key,
                    role=role,
                    attempt=attempt.name,
                    error_type=type(exc).__name__,
                )
                self.check_quota()
                continue
            except BaseException as exc:
                atomic_json(attempt / "interrupted.json", {"type": type(exc).__name__})
                self.emit("attempt_interrupted", job=job_key, role=role, attempt=attempt.name)
                raise
            output = parsed.model_dump(mode="json")
            artifacts = getattr(self.backend, "attempt_artifacts", None)
            auxiliary = artifacts(attempt) if artifacts else {}
            atomic_json(
                cache,
                {
                    "fingerprint": fingerprint,
                    "output_sha256": digest(output),
                    "output": output,
                    "attempt": attempt.name,
                    **({"auxiliary_artifacts": {f"{attempt.name}/{p}": sha for p, sha in auxiliary.items()}} if auxiliary else {}),
                },
            )
            self.emit(
                "job_completed",
                job=job_key,
                role=role,
                attempt=attempt.name,
                artifact=str(cache.relative_to(self.run_dir)),
                output_sha256=digest(output),
            )
            return parsed
        raise JobFailed(
            f"{job_key}: no valid result after {self.max_attempts} attempts this invocation"
        )
