import json
import tomllib

import pytest

from osverifier.connection_config import ConnectionConfig


def test_connection_selection_and_redaction(tmp_path):
    text = """model_provider = "proxy"
model = "user-model"
developer_instructions = "do something else"
cli_auth_credentials_store = "keyring"
[model_providers.proxy]
name = "Proxy"
base_url = "https://example.invalid/v1"
wire_api = "responses"
env_key = "PROXY_KEY"
http_headers = { Authorization = "secret-marker" }
query_params = { "api-version" = "2025-01-01" }
request_max_retries = 3
[model_providers.unused]
name = "Unused"
[mcp_servers.personal]
command = "do-not-run"
[features]
hooks = true
"""
    (tmp_path / "config.toml").write_text(text)
    config = ConnectionConfig(tmp_path)
    effective = {}
    for argument in config.arguments[1::2]:
        effective.update(tomllib.loads(argument))
    assert set(effective) == {"model_provider", "model_providers", "cli_auth_credentials_store"}
    assert set(effective["model_providers"]) == {"proxy"}
    assert effective["model_providers"]["proxy"]["http_headers"]["Authorization"] == "secret-marker"
    assert "secret-marker" not in json.dumps(config.identity)
    assert "secret-marker" not in json.dumps(config.redact(config.arguments))
    assert (tmp_path / "config.toml").read_text() == text
    (tmp_path / "config.toml").write_text(
        text.replace("do something else", "different instruction")
    )
    assert ConnectionConfig(tmp_path).identity == config.identity
    (tmp_path / "config.toml").write_text(text.replace("example.invalid", "other.invalid"))
    assert ConnectionConfig(tmp_path).identity != config.identity


def test_profile_and_home_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("""profile = "work"
model_provider = "openai"
[profiles.work]
model_provider = "proxy"
approval_policy = "untrusted"
[model_providers.proxy]
name = "Proxy"
env_key = "PROXY_KEY"
""")
    config = ConnectionConfig()
    assert config.values["model_provider"] == "proxy"
    assert "approval_policy" not in config.values
    assert ConnectionConfig(tmp_path / "separate").values == {}


def test_bad_configuration_fails_without_exposing_source(tmp_path):
    (tmp_path / "config.toml").write_text('secret = "credential-marker')
    with pytest.raises(ValueError) as error:
        ConnectionConfig(tmp_path)
    assert "credential-marker" not in str(error.value)


def test_free_text_redaction_covers_escaped_and_partial_values(tmp_path):
    import subprocess

    (tmp_path / "config.toml").write_text("""model_provider = "proxy"
[model_providers.proxy]
name = "Proxy"
base_url = "https://private-gateway.invalid/v1"
http_headers = { Authorization = "it's-secret-marker" }
""")
    config = ConnectionConfig(tmp_path)
    leaked = str(subprocess.TimeoutExpired(["codex", "exec", *config.arguments], 5))
    assert "secret-marker" in leaked
    for text in (leaked, json.dumps(config.arguments), "header it's-secret-marker rejected"):
        redacted = config.redact_text(text)
        assert "secret-marker" not in redacted and "private-gateway" not in redacted
    assert config.redact_text("Codex exited 1") == "Codex exited 1"
