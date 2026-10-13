"""Import transport configuration without importing user agent behavior."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path

# Provider tables are retained whole so provider-specific transport extensions work.
CONNECTION_KEYS = (
    "model_provider",
    "openai_base_url",
    "chatgpt_base_url",
    "cli_auth_credentials_store",
    "forced_login_method",
    "forced_chatgpt_workspace_id",
)
REDACTED = "<redacted connection config>"


def toml_value(value):
    if isinstance(value, dict):
        return (
            "{ " + ", ".join(f"{json.dumps(k)} = {toml_value(v)}" for k, v in value.items()) + " }"
        )
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, (str, bool, int, float)):
        return json.dumps(value, ensure_ascii=True, allow_nan=False)
    raise ValueError("Unsupported connection configuration value type")


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


class ConnectionConfig:
    def __init__(self, codex_home=None):
        self.home = (
            Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
            .expanduser()
            .resolve()
        )
        self.path = self.home / "config.toml"
        try:
            config = tomllib.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (OSError, ValueError):
            # TOML parser messages can include credential-bearing source lines.
            raise ValueError(f"Cannot read Codex configuration at {self.path}") from None
        profile = config.get("profile")
        if profile:
            profiles = config.get("profiles", {})
            if profile not in profiles:
                raise ValueError("Selected Codex profile is missing from profiles")
            config = {**config, **profiles[profile]}
        self.values = {key: config[key] for key in CONNECTION_KEYS if key in config}
        provider = self.values.get("model_provider", "openai")
        providers = config.get("model_providers", {})
        if provider in providers:
            self.values["model_providers"] = {provider: providers[provider]}
        self.arguments = []
        for key, value in self.values.items():
            self.arguments += ["-c", f"{key}={toml_value(value)}"]
        # Hash only selected configuration, not auth.json or environment credentials.
        digest = hashlib.sha256(json.dumps(self.values, sort_keys=True).encode()).hexdigest()
        self.identity = {
            "source": str(self.path),
            "profile": profile,
            "keys": sorted(self.values),
            "sha256": digest,
        }

    def redact(self, argv):
        values = set(self.arguments[1::2])
        return [arg.split("=", 1)[0] + "=" + REDACTED if arg in values else arg for arg in argv]

    def redact_text(self, text: str) -> str:
        """Defensively remove selected values from free text such as exception messages.

        Whole overrides are matched raw and in repr/JSON escaping; individual values of
        eight or more characters (URLs, headers, tokens) are matched as well.
        """
        candidates = set(self.arguments[1::2])
        candidates |= {value for value in _strings(self.values) if len(value) >= 8}
        needles = set()
        for value in candidates:
            needles |= {value, repr(value)[1:-1], json.dumps(value)[1:-1],
                        json.dumps(value, ensure_ascii=False)[1:-1]}
        for needle in sorted(needles, key=len, reverse=True):
            if needle:
                text = text.replace(needle, REDACTED)
        return text


def redact_backend_text(backend, text: str) -> str:
    """Apply a backend's connection redaction, if it has selected connection settings."""
    redact = getattr(getattr(backend, "connection", None), "redact_text", None)
    return redact(text) if redact else text
