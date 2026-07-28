"""Strict, shadow-only production configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit

from issue_control.contracts import issue_key
from issue_control.secrets import SecretResolutionError, environment_secret_name


class ConfigError(ValueError):
    """Configuration is unsafe, ambiguous, or unsupported in Phase 1A."""


_CREDENTIAL_QUERY_FRAGMENTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "credential",
    "signature",
    "auth",
)


def _strict_keys(
    raw: Mapping[str, Any],
    allowed: set[str],
    *,
    scope: str,
) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unknown {scope} configuration keys: {', '.join(unknown)}")


def _validate_secret_reference(field: str, value: str) -> None:
    try:
        environment_secret_name(value)
    except SecretResolutionError as exc:
        raise ConfigError(f"{field} must use secret://env/NAME") from exc


def _validate_credential_free_url(
    field: str,
    value: str,
    *,
    allowed_schemes: set[str],
    allow_username: bool = False,
    allow_query: bool = False,
) -> None:
    try:
        parsed = urlsplit(value)
        _parsed_port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"{field} must be a valid credential-free URL") from exc
    if (
        parsed.scheme.casefold() not in allowed_schemes
        or not parsed.hostname
        or parsed.fragment
    ):
        raise ConfigError(f"{field} must be a supported credential-free URL")
    if parsed.password is not None or (
        not allow_username and parsed.username is not None
    ):
        raise ConfigError(f"{field} must not contain credentials in config.yaml")
    if parsed.query and not allow_query:
        raise ConfigError(f"{field} must not contain query parameters")
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = key.casefold().replace("-", "_")
        if any(fragment in normalized for fragment in _CREDENTIAL_QUERY_FRAGMENTS):
            raise ConfigError(f"{field} must not contain credentials in config.yaml")


@dataclass(frozen=True, slots=True)
class GitHubReadConfig:
    api_base_url: str
    read_token_secret_ref: str
    webhook_secret_ref: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> GitHubReadConfig:
        _strict_keys(
            raw,
            {
                "api_base_url",
                "read_token_secret_ref",
                "webhook_secret_ref",
            },
            scope="github",
        )
        for forbidden_fragment in ("write", "private_key", "app_key"):
            if any(forbidden_fragment in key.casefold() for key in raw):
                raise ConfigError(
                    "GitHub write credentials are forbidden in shadow mode"
                )
        try:
            config = cls(
                api_base_url=str(raw["api_base_url"]).rstrip("/"),
                read_token_secret_ref=str(raw["read_token_secret_ref"]),
                webhook_secret_ref=str(raw["webhook_secret_ref"]),
            )
        except KeyError as exc:
            raise ConfigError(f"missing github configuration: {exc.args[0]}") from exc
        _validate_credential_free_url(
            "github.api_base_url",
            config.api_base_url,
            allowed_schemes={"https"},
        )
        _validate_secret_reference(
            "github.read_token_secret_ref",
            config.read_token_secret_ref,
        )
        _validate_secret_reference(
            "github.webhook_secret_ref",
            config.webhook_secret_ref,
        )
        return config


@dataclass(frozen=True, slots=True)
class S3PayloadConfig:
    bucket: str
    prefix: str
    endpoint_url: str | None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> S3PayloadConfig:
        _strict_keys(
            raw,
            {"bucket", "prefix", "endpoint_url"},
            scope="payload_store",
        )
        try:
            config = cls(
                bucket=str(raw["bucket"]),
                prefix=str(raw.get("prefix", "issue-control")),
                endpoint_url=(
                    str(raw["endpoint_url"]).rstrip("/")
                    if raw.get("endpoint_url")
                    else None
                ),
            )
        except KeyError as exc:
            raise ConfigError(
                f"missing payload_store configuration: {exc.args[0]}"
            ) from exc
        if not config.bucket:
            raise ConfigError("payload_store.bucket is required")
        if config.endpoint_url is not None:
            _validate_credential_free_url(
                "payload_store.endpoint_url",
                config.endpoint_url,
                allowed_schemes={"http", "https"},
            )
        return config


@dataclass(frozen=True, slots=True)
class IssueControlConfig:
    mode: str
    postgres_dsn: str
    redis_url: str
    node_id: str
    primary_node: str
    standby_node: str
    authorized_repositories: tuple[str, ...]
    github: GitHubReadConfig
    payload_store: S3PayloadConfig
    renewal_interval_seconds: int = 10
    takeover_after_seconds: int = 60
    reconciliation_interval_seconds: int = 300
    internal_host: str = "127.0.0.1"
    internal_port: int = 8787

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> IssueControlConfig:
        _strict_keys(
            raw,
            {
                "mode",
                "postgres_dsn",
                "redis_url",
                "node_id",
                "primary_node",
                "standby_node",
                "authorized_repositories",
                "github",
                "payload_store",
                "renewal_interval_seconds",
                "takeover_after_seconds",
                "reconciliation_interval_seconds",
                "internal_host",
                "internal_port",
            },
            scope="issue_control",
        )
        mode = str(raw.get("mode", "shadow"))
        if mode != "shadow":
            raise ConfigError("shadow is the only enabled Issue Control Plane mode")
        try:
            repositories = tuple(
                _canonical_repository(str(repository))
                for repository in raw["authorized_repositories"]
            )
            config = cls(
                mode=mode,
                postgres_dsn=str(raw["postgres_dsn"]),
                redis_url=str(raw["redis_url"]),
                node_id=str(raw["node_id"]),
                primary_node=str(raw["primary_node"]),
                standby_node=str(raw["standby_node"]),
                authorized_repositories=repositories,
                github=GitHubReadConfig.from_mapping(raw["github"]),
                payload_store=S3PayloadConfig.from_mapping(raw["payload_store"]),
                renewal_interval_seconds=int(raw.get("renewal_interval_seconds", 10)),
                takeover_after_seconds=int(raw.get("takeover_after_seconds", 60)),
                reconciliation_interval_seconds=int(
                    raw.get("reconciliation_interval_seconds", 300)
                ),
                internal_host=str(raw.get("internal_host", "127.0.0.1")),
                internal_port=int(raw.get("internal_port", 8787)),
            )
        except KeyError as exc:
            raise ConfigError(
                f"missing issue_control configuration: {exc.args[0]}"
            ) from exc
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError(f"invalid issue_control configuration: {exc}") from exc
        if config.primary_node == config.standby_node:
            raise ConfigError("primary_node and standby_node must differ")
        if (config.primary_node, config.standby_node) != ("s3", "s5"):
            raise ConfigError("Phase 1A requires primary_node=s3 and standby_node=s5")
        if config.node_id not in (config.primary_node, config.standby_node):
            raise ConfigError("node_id must be the configured primary or standby")
        if config.renewal_interval_seconds != 10:
            raise ConfigError("Phase 1A requires renewal_interval_seconds=10")
        if config.takeover_after_seconds != 60:
            raise ConfigError("Phase 1A requires takeover_after_seconds=60")
        if config.reconciliation_interval_seconds <= 0:
            raise ConfigError("reconciliation_interval_seconds must be positive")
        if not repositories or len(set(repositories)) != len(repositories):
            raise ConfigError("authorized_repositories must be non-empty and unique")
        for field, value in (
            ("postgres_dsn", config.postgres_dsn),
            ("redis_url", config.redis_url),
        ):
            _validate_service_location(field, value)
        if not (1 <= config.internal_port <= 65535):
            raise ConfigError("internal_port is outside the valid TCP range")
        return config


def _validate_service_location(field: str, value: str) -> None:
    if value.startswith("secret://"):
        _validate_secret_reference(field, value)
        return
    allowed_schemes = (
        {"postgres", "postgresql"}
        if field == "postgres_dsn"
        else {"redis", "rediss"}
    )
    _validate_credential_free_url(
        field,
        value,
        allowed_schemes=allowed_schemes,
        allow_username=True,
        allow_query=True,
    )


def _canonical_repository(repository: str) -> str:
    canonical = issue_key(repository, 1).removesuffix("#1")
    if canonical != repository.strip().casefold():
        raise ConfigError("authorized repository names must be canonical")
    return canonical


__all__ = [
    "ConfigError",
    "GitHubReadConfig",
    "IssueControlConfig",
    "S3PayloadConfig",
]
