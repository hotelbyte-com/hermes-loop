"""Machine-readable readiness checks for installers and bootstrap clients.

This module deliberately keeps the readiness contract smaller than the human
``hermes doctor`` report.  It reuses the same configuration and provider
helpers, but never serializes config, environment, auth, or exception text.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from typing import Any

from hermes_cli.config import get_config_path, get_env_path, load_config, validate_config_structure


READINESS_SCHEMA_VERSION = 1
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "ready",
        "hermes_version",
        "python",
        "provider",
        "gateway",
        "checks",
        "next_command",
    }
)
_CHECK_FIELDS = frozenset({"name", "status", "detail"})
_CHECK_STATUSES = {
    "config": frozenset({"ok", "incomplete", "failure"}),
    "runtime": frozenset({"ok", "failure"}),
    "provider": frozenset({"ok", "incomplete", "failure"}),
    "gateway": frozenset({"configured", "optional_absent", "optional_unavailable"}),
}
_APPROVED_NEXT_COMMANDS = frozenset(
    {
        "hermes setup",
        "hermes update",
        "hermes doctor",
    }
)
_EXPECTED_CHECK_NAMES = frozenset(_CHECK_STATUSES)
_SECRET_TEXT_RE = re.compile(
    r"(?:"
    r"(?:sk|rk|pk|ghp|gho|ghs|ghr|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}"
    r"|(?:bearer|token|api[_ -]?key|password|secret|authorization|credential)s?\s*[:=]\s*\S+"
    r")",
    re.IGNORECASE,
)
_REQUIRED_IMPORTS = ("openai", "rich", "dotenv", "yaml", "httpx")
_GATEWAY_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "WHATSAPP_ENABLED",
)
_PLACEHOLDER_VALUES = {"", "your-token-here", "your_api_key_here", "changeme"}
_OAUTH_RUNTIME_RESOLVERS = {
    "nous": "resolve_nous_runtime_credentials",
    "openai-codex": "resolve_codex_runtime_credentials",
    "xai-oauth": "resolve_xai_oauth_runtime_credentials",
    "qwen-oauth": "resolve_qwen_runtime_credentials",
    "minimax-oauth": "resolve_minimax_oauth_runtime_credentials",
}


def _check(name: str, status: str, detail: str) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail}


def _env_values() -> dict[str, str]:
    """Read only key names and presence from the user env file.

    Values stay in this process and are reduced to booleans by callers.  This
    avoids making the receipt depend on dotenv's parser output or exposing a
    credential in a subprocess error.
    """
    values = dict(os.environ)
    try:
        for raw in get_env_path().read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in values:
                values[key] = value
    except (OSError, UnicodeError):
        pass
    return values


def _has_usable_value(value: Any) -> bool:
    return str(value or "").strip().lower() not in _PLACEHOLDER_VALUES


def _gateway_configured(values: dict[str, str]) -> bool:
    return any(_has_usable_value(values.get(key)) for key in _GATEWAY_ENV_KEYS)


def _gateway_status(configured: bool) -> str:
    if not configured:
        return "optional_absent"
    try:
        from gateway.status import get_runtime_status_running_pid, read_runtime_status

        state = read_runtime_status()
        if (
            isinstance(state, dict)
            and state.get("gateway_state") == "running"
            and get_runtime_status_running_pid(state) is not None
        ):
            return "configured"
    except Exception:
        pass
    return "optional_unavailable"


def _provider_access_error(provider: str, config: dict[str, Any]) -> str | None:
    """Prove that the selected provider can answer an authenticated request.

    Resolve through the same runtime owner used by the agent before probing.
    This is important for profile-local credential pools, configured endpoint
    overrides, SDK-backed auth (Bedrock), and callable Entra credentials.  It
    never includes a credential or response body in the returned error code or
    in the readiness receipt.
    """
    try:
        model = config.get("model")
        model = model if isinstance(model, dict) else {}
        requested_provider = str(model.get("provider") or provider).strip() or provider
        configured_base_url = str(model.get("base_url") or "").strip()

        # Keep the ACP subprocess handshake on its existing auth seam.  The
        # provider is not an HTTP endpoint and the resolver is deliberately
        # external-process based.
        if provider == "copilot-acp":
            from hermes_cli.auth import resolve_external_process_provider_credentials
            from agent.copilot_acp_client import CopilotACPClient

            credentials = resolve_external_process_provider_credentials(provider)
            client = CopilotACPClient(
                acp_command=str(credentials["command"]),
                acp_args=list(credentials.get("args") or []),
            )
            client.probe(timeout_seconds=10.0)
            return None

        explicit_api_key: str | None = None
        if provider == "openrouter":
            from hermes_cli.config import get_env_value

            explicit_api_key = str(
                get_env_value("OPENROUTER_API_KEY")
                or get_env_value("OPENAI_API_KEY")
                or ""
            ).strip() or None
        else:
            from hermes_cli.auth import PROVIDER_REGISTRY, get_anthropic_key
            from hermes_cli.config import get_env_value

            pconfig = PROVIDER_REGISTRY.get(provider)
            if pconfig is not None and pconfig.auth_type == "api_key":
                if provider == "anthropic":
                    explicit_api_key = get_anthropic_key() or None
                else:
                    for env_name in pconfig.api_key_env_vars:
                        value = str(get_env_value(env_name) or "").strip()
                        if value:
                            explicit_api_key = value
                            break

        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(
            requested=requested_provider,
            explicit_api_key=explicit_api_key,
            explicit_base_url=configured_base_url or None,
            target_model=str(model.get("default") or model.get("model") or "").strip() or None,
        )
        runtime_provider = str(runtime.get("provider") or provider).strip().lower()
        runtime_base_url = str(runtime.get("base_url") or "").strip()
        # The runtime resolver owns the endpoint actually used by the agent.
        # In particular, a credential-pool entry may carry an inference URL
        # that differs from the provider catalog.  Keep the explicit config
        # value only as a compatibility fallback for older resolvers that did
        # not return model.base_url at all.
        base_url = runtime_base_url or configured_base_url

        if runtime_provider == "bedrock" or runtime.get("api_mode") == "bedrock_converse":
            try:
                from agent.bedrock_adapter import resolve_bedrock_region
                import boto3
                from botocore.config import Config as BotoConfig

                bedrock_config = config.get("bedrock")
                bedrock_config = bedrock_config if isinstance(bedrock_config, dict) else {}
                region = str(runtime.get("region") or bedrock_config.get("region") or "").strip()
                region = region or resolve_bedrock_region()
                client = boto3.client(
                    "bedrock",
                    region_name=region,
                    config=BotoConfig(
                        connect_timeout=5,
                        read_timeout=10,
                        retries={"max_attempts": 1},
                    ),
                )
                client.list_foundation_models()
                return None
            except ImportError:
                return "provider_auth_unverified"
            except Exception:
                return "provider_unreachable"

        if not base_url:
            return "custom_endpoint_missing" if runtime_provider == "custom" else "provider_endpoint_missing"

        api_key = runtime.get("api_key")
        try:
            token = api_key() if callable(api_key) else str(api_key or "").strip()
        except Exception:
            return "provider_auth_unverified"
        if callable(api_key):
            token = str(token or "").strip()
        if not token and runtime_provider not in {"custom", "lmstudio"}:
            return "credentials_missing"

        from providers import get_provider_profile
        from hermes_cli.providers import determine_api_mode

        profile = get_provider_profile(runtime_provider)
        if profile is not None and not getattr(profile, "supports_health_check", True):
            return None

        api_mode = str(runtime.get("api_mode") or determine_api_mode(runtime_provider, base_url))

        effective_base = base_url.rstrip("/")
        if api_mode == "anthropic_messages" and not effective_base.endswith("/v1"):
            effective_base += "/v1"
        elif (
            "api.kimi.com/coding" in effective_base.lower()
            and not effective_base.endswith("/v1")
        ):
            effective_base += "/v1"
        models_url = effective_base + "/models"
        urls = [models_url]
        if not configured_base_url and base_url and models_url == base_url.rstrip("/") + "/models":
            alternate_base = (
                base_url.rstrip("/")[:-3].rstrip("/")
                if base_url.rstrip("/").endswith("/v1")
                else base_url.rstrip("/") + "/v1"
            )
            alternate_url = alternate_base + "/models"
            if alternate_url not in urls:
                urls.append(alternate_url)

        headers: dict[str, str] = {"User-Agent": "hermes-readiness"}
        if profile is not None:
            headers.update(getattr(profile, "default_headers", {}) or {})
        anthropic_oauth = False
        if token and api_mode == "anthropic_messages":
            from agent.anthropic_adapter import (
                _OAUTH_ONLY_BETAS,
                _common_betas_for_base_url,
                _get_claude_code_version,
                _is_oauth_token,
            )

            anthropic_oauth = _is_oauth_token(token)
            if anthropic_oauth:
                headers["Authorization"] = f"Bearer {token}"
                headers["anthropic-version"] = "2023-06-01"
                common_betas = _common_betas_for_base_url(base_url)
                headers["anthropic-beta"] = ",".join(common_betas + _OAUTH_ONLY_BETAS)
                headers["user-agent"] = f"claude-cli/{_get_claude_code_version()} (external, cli)"
                headers["x-app"] = "cli"
        if token and not anthropic_oauth and (callable(api_key) or runtime.get("auth_mode") == "entra_id"):
            headers["Authorization"] = f"Bearer {token}"
        elif token and not anthropic_oauth and api_mode == "anthropic_messages":
            headers["x-api-key"] = token
            headers["anthropic-version"] = "2023-06-01"
        elif token and not anthropic_oauth:
            headers["Authorization"] = f"Bearer {token}"
        if token and base_url.lower().find("generativelanguage.googleapis.com") >= 0:
            headers.pop("Authorization", None)
            headers["x-goog-api-key"] = token

        import httpx

        last_status: int | None = None
        for url in urls:
            response = httpx.get(url, headers=headers, timeout=10.0, follow_redirects=True)
            last_status = response.status_code
            if 200 <= last_status < 300:
                return None
            if last_status in {401, 403}:
                return "provider_auth_rejected"
            if last_status != 404:
                break
        if last_status == 404:
            return "provider_endpoint_invalid"
        return "provider_unreachable"
    except Exception:
        return "provider_unreachable"


def _contains_secret_text(value: str) -> bool:
    return bool(_SECRET_TEXT_RE.search(value))


def validate_readiness_receipt(receipt: Any, exit_code: int) -> tuple[bool, str | None, str | None]:
    """Validate the installer receipt without returning untrusted text.

    The two public installers call this exact entry point through the installed
    Python runtime.  Keep this allowlist deliberately small: a receipt that is
    merely JSON-shaped is not sufficient evidence to launch Hermes.
    """
    if not isinstance(receipt, dict) or set(receipt) - _RECEIPT_FIELDS:
        return False, None, None

    def walk(value: Any) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in {
                    "api_key", "token", "access_token", "refresh_token", "secret",
                    "password", "auth", "authorization", "credential", "credentials",
                }:
                    return False
                if not walk(child):
                    return False
            return True
        if isinstance(value, list):
            return all(walk(child) for child in value)
        return not isinstance(value, str) or not _contains_secret_text(value)

    if not walk(receipt):
        return False, None, None
    if (
        type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != READINESS_SCHEMA_VERSION
        or not isinstance(receipt.get("status"), str)
        or receipt["status"] not in {"ready", "incomplete_setup", "failure"}
        or type(receipt.get("ready")) is not bool
        or not isinstance(receipt.get("checks"), list)
        or not isinstance(receipt.get("gateway"), dict)
    ):
        return False, None, None
    for metadata_key in ("hermes_version", "python"):
        if metadata_key in receipt and not isinstance(receipt[metadata_key], str):
            return False, None, None
    if receipt.get("provider") is not None and not isinstance(receipt.get("provider"), str):
        return False, None, None
    if set(receipt["gateway"]) != {"status", "optional"}:
        return False, None, None
    gateway_status = receipt["gateway"].get("status")
    if gateway_status not in _CHECK_STATUSES["gateway"] or receipt["gateway"].get("optional") is not True:
        return False, None, None

    checks_by_name: dict[str, dict[str, Any]] = {}
    for check in receipt["checks"]:
        if not isinstance(check, dict) or set(check) - _CHECK_FIELDS:
            return False, None, None
        if (
            not isinstance(check.get("name"), str)
            or check["name"] not in _EXPECTED_CHECK_NAMES
            or check["name"] in checks_by_name
            or not isinstance(check.get("status"), str)
            or check["status"] not in _CHECK_STATUSES[check["name"]]
            or not isinstance(check.get("detail"), str)
        ):
            return False, None, None
        checks_by_name[check["name"]] = check
    if set(checks_by_name) != _EXPECTED_CHECK_NAMES:
        return False, None, None
    if checks_by_name["gateway"]["status"] != gateway_status:
        return False, None, None

    status = receipt["status"]
    next_command = receipt.get("next_command")
    # Command text is trusted only in the single, top-level recovery field.
    # Keep the value closed over the installer-owned command contract so a
    # syntactically valid receipt cannot turn either installer into a shell.
    if "next_command" in receipt and next_command not in _APPROVED_NEXT_COMMANDS:
        return False, None, None
    if status == "ready":
        valid = (
            receipt["ready"]
            and exit_code == 0
            and "next_command" not in receipt
            and all(checks_by_name[name]["status"] == "ok" for name in ("config", "runtime", "provider"))
        )
    elif status == "incomplete_setup":
        valid = (
            not receipt["ready"]
            and exit_code == 2
            and isinstance(next_command, str)
            and bool(next_command.strip())
            and any(check["status"] == "incomplete" for check in checks_by_name.values())
            and not any(check["status"] == "failure" for check in checks_by_name.values())
        )
    else:
        valid = (
            not receipt["ready"]
            and exit_code == 1
            and isinstance(next_command, str)
            and bool(next_command.strip())
            and any(check["status"] == "failure" for check in checks_by_name.values())
        )
    return bool(valid), status if valid else None, next_command if isinstance(next_command, str) else None


def _provider_state(config: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return provider id, failure code, and recovery command."""
    model = config.get("model")
    model = model if isinstance(model, dict) else {}
    requested = str(model.get("provider") or "").strip().lower()

    try:
        from hermes_cli.auth import get_active_provider, resolve_provider

        active = (get_active_provider() or "").strip().lower()
        if not requested and active:
            requested = active
        if not requested:
            return None, "provider_missing", "hermes setup"
        provider = resolve_provider(requested)
    except Exception:
        return None, "provider_invalid", "hermes setup"

    if provider == "custom":
        base_url = str(model.get("base_url") or "").strip()
        custom = config.get("custom_providers")
        if not base_url and isinstance(custom, list):
            for entry in custom:
                if isinstance(entry, dict) and str(entry.get("name") or "").strip():
                    base_url = str(entry.get("base_url") or "").strip()
                    if base_url:
                        break
        if not base_url:
            return provider, "custom_endpoint_missing", "hermes setup"
        access_error = _provider_access_error(provider, config)
        return provider, access_error, "hermes setup" if access_error else None

    try:
        from hermes_cli.auth import PROVIDER_REGISTRY, get_auth_status, has_usable_secret
        from hermes_cli.config import get_env_value

        if provider == "openrouter":
            configured = any(
                has_usable_secret(str(get_env_value(key) or ""))
                for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY")
            )
            if not configured:
                try:
                    from agent.credential_pool import load_pool

                    configured = bool(load_pool(provider).has_credentials())
                except Exception:
                    configured = False
            if not configured:
                return provider, "credentials_missing", "hermes setup"
            access_error = _provider_access_error(provider, config)
            return provider, access_error, "hermes setup" if access_error else None
        else:
            provider_config = PROVIDER_REGISTRY.get(provider)
            if provider_config is None:
                return provider, "provider_unknown", "hermes setup"
            state = get_auth_status(provider) or {}
            if provider_config.auth_type == "api_key":
                # get_auth_status() delegates to the shared API-key resolver,
                # which checks env/.env and the profile-aware credential pool.
                configured = bool(state.get("configured") or state.get("logged_in"))
            else:
                configured = bool(state.get("logged_in"))
    except Exception:
        return provider, "provider_check_failed", "hermes setup"

    if not configured:
        return provider, "credentials_missing", "hermes setup"
    access_error = _provider_access_error(provider, config)
    if access_error:
        return provider, access_error, "hermes setup"
    return provider, None, None


def build_readiness_receipt() -> dict[str, Any]:
    """Build a bounded, credential-free readiness receipt."""
    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    incomplete: list[str] = []

    try:
        config_path = get_config_path()
        if not config_path.exists():
            checks.append(_check("config", "incomplete", "config.yaml is not configured"))
            incomplete.append("config")
            config: dict[str, Any] = {}
        else:
            try:
                import yaml

                with config_path.open(encoding="utf-8") as handle:
                    raw_config = yaml.safe_load(handle)
                if raw_config is not None and not isinstance(raw_config, dict):
                    raise ValueError("config root must be a mapping")
                config = load_config()
                config_issues = validate_config_structure(config)
            except Exception:
                checks.append(_check("config", "failure", "config.yaml could not be read safely"))
                failures.append("config")
                config = {}
                config_issues = []
            if config_path.exists() and "config" not in failures:
                structural_errors = [issue for issue in config_issues if issue.severity == "error"]
                if structural_errors:
                    checks.append(_check("config", "failure", "config.yaml has invalid structure"))
                    failures.append("config")
                else:
                    checks.append(_check("config", "ok", "configuration is readable"))
    except Exception:
        checks.append(_check("config", "failure", "configuration path is not usable"))
        failures.append("config")
        config = {}

    try:
        missing_imports = []
        for module in _REQUIRED_IMPORTS:
            try:
                importlib.import_module(module)
            except Exception:
                missing_imports.append(module)
        if missing_imports:
            checks.append(_check("runtime", "failure", "required runtime dependencies are unavailable"))
            failures.append("runtime")
        else:
            checks.append(_check("runtime", "ok", "required runtime dependencies are importable"))
    except Exception:
        checks.append(_check("runtime", "failure", "runtime dependency check failed"))
        failures.append("runtime")

    provider: str | None = None
    provider_command: str | None = None
    if "config" not in failures:
        provider, provider_error, provider_command = _provider_state(config)
        if provider_error:
            provider_status = (
                "failure"
                if provider_error
                in {
                    "provider_check_failed",
                    "provider_unknown",
                    "provider_auth_rejected",
                    "provider_endpoint_invalid",
                    "provider_unreachable",
                    "provider_auth_unverified",
                }
                else "incomplete"
            )
            detail = "provider or credentials are not ready"
            checks.append(_check("provider", provider_status, detail))
            (failures if provider_status == "failure" else incomplete).append("provider")
        else:
            checks.append(_check("provider", "ok", "provider credentials are available"))
    else:
        # Keep the receipt shape stable even when the config cannot be loaded.
        # The installer validator requires a typed provider check so it can
        # distinguish an actionable setup failure from a malformed receipt;
        # do not attempt provider resolution against an untrusted config.
        checks.append(_check("provider", "failure", "provider configuration could not be checked"))
        failures.append("provider")

    values = _env_values()
    gateway_configured = _gateway_configured(values)
    gateway_status = _gateway_status(gateway_configured)
    checks.append(
        _check(
            "gateway",
            gateway_status,
            "messaging gateway is optional"
            if not gateway_configured
            else (
                "messaging gateway is running"
                if gateway_status == "configured"
                else "messaging gateway is configured but unavailable"
            ),
        )
    )

    if failures:
        status = "failure"
        failure_name = failures[0]
        fallback_commands = {
            "config": "hermes setup",
            "runtime": "hermes update",
            "provider": "hermes setup",
        }
        next_command = fallback_commands.get(failure_name, "hermes doctor")
        if failure_name == "provider" and provider_command in _APPROVED_NEXT_COMMANDS:
            next_command = provider_command
    elif incomplete:
        status = "incomplete_setup"
        next_command = "hermes setup"
    else:
        status = "ready"
        next_command = None

    try:
        from hermes_cli import __version__
        version = str(__version__)
    except Exception:
        version = "unknown"

    receipt: dict[str, Any] = {
        "schema_version": READINESS_SCHEMA_VERSION,
        "status": status,
        "ready": status == "ready",
        "hermes_version": version,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "provider": provider or None,
        "gateway": {"status": gateway_status, "optional": True},
        "checks": checks,
    }
    if next_command:
        receipt["next_command"] = next_command
    return receipt


def print_readiness_receipt() -> int:
    """Print exactly one JSON receipt and return its installer exit code."""
    receipt = build_readiness_receipt()
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if receipt["ready"] else (2 if receipt["status"] == "incomplete_setup" else 1)


def _main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--exit-code", type=int, default=0)
    args, _unknown = parser.parse_known_args()
    if not args.validate:
        return print_readiness_receipt()
    try:
        receipt = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeError):
        return 1
    valid, status, next_command = validate_readiness_receipt(receipt, args.exit_code)
    if not valid or status is None:
        return 1
    print(status)
    print(next_command or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
