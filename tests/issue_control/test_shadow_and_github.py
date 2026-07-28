import json

import httpx
import pytest

from issue_control.config import ConfigError, IssueControlConfig
from issue_control.github import GitHubPermissionError, GitHubReadOnlyClient
from issue_control.payloads import PayloadSanitizer, S3SanitizedPayloadStore
from issue_control.secrets import EnvironmentSecretResolver, SecretResolutionError


def _valid_config() -> dict:
    return {
        "mode": "shadow",
        "postgres_dsn": "postgresql://issue-control@postgres/hermes",
        "redis_url": "redis://redis:6379/0",
        "node_id": "s3",
        "primary_node": "s3",
        "standby_node": "s5",
        "authorized_repositories": [
            "hotelbyte-com/hotel-be",
            "hotelbyte-com/hotel-fe",
        ],
        "github": {
            "api_base_url": "https://api.github.com",
            "read_token_secret_ref": "secret://env/ISSUE_AGENT_READ_TOKEN",
            "webhook_secret_ref": "secret://env/ISSUE_AGENT_WEBHOOK_SECRET",
        },
        "payload_store": {
            "bucket": "hermes-issue-events",
            "prefix": "phase-1a",
            "endpoint_url": "https://minio.internal",
        },
    }


def test_shadow_is_the_only_accepted_runtime_mode() -> None:
    config = IssueControlConfig.from_mapping(_valid_config())

    assert config.mode == "shadow"
    assert config.renewal_interval_seconds == 10
    assert config.takeover_after_seconds == 60
    assert config.reconciliation_interval_seconds == 300

    unsafe = _valid_config()
    unsafe["mode"] = "execute"
    with pytest.raises(ConfigError, match="shadow"):
        IssueControlConfig.from_mapping(unsafe)


def test_phase_1a_topology_cannot_reverse_s3_primary_and_s5_standby() -> None:
    reversed_topology = _valid_config()
    reversed_topology["primary_node"] = "s5"
    reversed_topology["standby_node"] = "s3"

    with pytest.raises(ConfigError, match="primary_node=s3"):
        IssueControlConfig.from_mapping(reversed_topology)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("renewal_interval_seconds", 30, "renewal_interval_seconds=10"),
        ("takeover_after_seconds", 31, "takeover_after_seconds=60"),
    ],
)
def test_phase_1a_failover_cadence_is_fixed(
    field: str,
    value: int,
    message: str,
) -> None:
    raw = _valid_config()
    raw[field] = value

    with pytest.raises(ConfigError, match=message):
        IssueControlConfig.from_mapping(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("postgres_dsn", "postgresql://user:password@postgres/hermes"),
        ("postgres_dsn", "host=postgres dbname=hermes password=secret"),
        ("postgres_dsn", "postgresql://postgres/hermes?sslpassword=secret"),
        ("redis_url", "redis://:password@redis:6379/0"),
        ("redis_url", "redis://redis:6379/0?token=secret"),
        ("redis_url", "unix:///run/redis.sock"),
        ("redis_url", "secret://vault/redis"),
        ("redis_url", "secret://env/"),
        ("redis_url", "secret://env/_"),
    ],
)
def test_service_locations_reject_credentials_and_unsupported_dsns(
    field: str,
    value: str,
) -> None:
    raw = _valid_config()
    raw[field] = value

    with pytest.raises(ConfigError, match="credential|supported|secret"):
        IssueControlConfig.from_mapping(raw)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("github", "api_base_url"), "https://user:password@api.github.com"),
        (("github", "api_base_url"), "https://api.github.com?token=secret"),
        (
            ("payload_store", "endpoint_url"),
            "https://access-key@minio.internal",
        ),
        (
            ("payload_store", "endpoint_url"),
            "https://minio.internal?X-Amz-Credential=secret",
        ),
    ],
)
def test_endpoint_urls_reject_userinfo_and_query_credentials(
    path: tuple[str, ...],
    value: str,
) -> None:
    raw = _valid_config()
    target = raw
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(ConfigError, match="credential|query"):
        IssueControlConfig.from_mapping(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("read_token_secret_ref", "secret://env/"),
        ("read_token_secret_ref", "secret://env/_"),
        ("read_token_secret_ref", "secret://env/TOKEN/extra"),
        ("webhook_secret_ref", "secret://env/"),
        ("webhook_secret_ref", "secret://env/_"),
        ("webhook_secret_ref", "secret://env/TOKEN/extra"),
    ],
)
def test_github_secret_references_require_exact_environment_reference(
    field: str,
    value: str,
) -> None:
    raw = _valid_config()
    raw["github"][field] = value

    with pytest.raises(ConfigError, match="secret://env/NAME"):
        IssueControlConfig.from_mapping(raw)


@pytest.mark.parametrize(
    "reference",
    [
        "secret://env/",
        "secret://env/_",
        "secret://env/TOKEN/extra",
        "secret://vault/TOKEN",
    ],
)
def test_runtime_resolver_uses_same_exact_reference_predicate(
    reference: str,
) -> None:
    with pytest.raises(SecretResolutionError):
        EnvironmentSecretResolver().resolve(reference)


def test_environment_reference_with_alphanumeric_name_is_shared_and_valid(
    monkeypatch,
) -> None:
    raw = _valid_config()
    raw["github"]["read_token_secret_ref"] = "secret://env/_TOKEN_"
    config = IssueControlConfig.from_mapping(raw)
    monkeypatch.setenv("_TOKEN_", "resolved")

    assert (
        EnvironmentSecretResolver().resolve(config.github.read_token_secret_ref)
        == "resolved"
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("github", "token"), "ghp_write"),
        (("github", "write_token"), "github_pat_write"),
        (("github", "private_key"), "-----BEGIN PRIVATE KEY-----"),
        (("github", "app_private_key_ref"), "secret://env/GITHUB_APP_KEY"),
        (("github_write_token",), "secret://env/GITHUB_WRITE_TOKEN"),
    ],
)
def test_config_rejects_every_github_write_credential_surface(
    path: tuple[str, ...],
    value: str,
) -> None:
    raw = _valid_config()
    target = raw
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(ConfigError, match="unknown|write|credential"):
        IssueControlConfig.from_mapping(raw)


def test_github_client_rejects_installations_with_any_write_permission() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={"permissions": {"issues": "write", "metadata": "read"}},
        )

    client = GitHubReadOnlyClient(
        base_url="https://api.github.test",
        token="installation-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(GitHubPermissionError, match="issues=write"):
        client.assert_read_only_permissions()


def test_github_client_has_only_get_paths_and_filters_pull_requests() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == "/installation":
            return httpx.Response(
                200,
                json={"permissions": {"issues": "read", "metadata": "read"}},
            )
        assert request.url.path == "/repos/hotelbyte-com/hotel-be/issues"
        return httpx.Response(
            200,
            json=[
                {"number": 1, "title": "Issue", "updated_at": "2026-07-28T01:02:03Z"},
                {
                    "number": 2,
                    "title": "PR",
                    "updated_at": "2026-07-28T01:02:03Z",
                    "pull_request": {},
                },
            ],
        )

    client = GitHubReadOnlyClient(
        base_url="https://api.github.test",
        token="installation-token",
        transport=httpx.MockTransport(handler),
    )

    client.assert_read_only_permissions()
    issues = client.list_open_issues("hotelbyte-com/hotel-be")

    assert [issue["number"] for issue in issues] == [1]
    assert methods == ["GET", "GET"]
    assert not hasattr(client, "post")
    assert not hasattr(client, "patch")
    assert not hasattr(client, "delete")


class _RecordingS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict] = {}

    def put_object(self, **kwargs) -> None:
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs


def test_payload_store_is_bounded_sanitized_content_addressed_and_encrypted() -> None:
    raw = {
        "action": "opened",
        "repository": {"full_name": "HotelByte-Com/Hotel-Be", "private": True},
        "issue": {
            "number": 22338,
            "title": "x" * 2_000,
            "body": "token=ghp_secret_value\n" + ("b" * 10_000),
            "state": "open",
            "labels": [{"name": "security"}, {"name": "customer"}],
            "user": {"login": "alice", "email": "alice@example.com"},
            "updated_at": "2026-07-28T01:02:03Z",
            "html_url": "https://github.com/hotelbyte-com/hotel-be/issues/22338",
        },
        "sender": {"login": "alice", "email": "alice@example.com"},
        "installation": {"id": 999, "token": "must-not-persist"},
    }
    sanitizer = PayloadSanitizer()
    sanitized = sanitizer.sanitize_issue_event(raw)
    s3 = _RecordingS3()
    store = S3SanitizedPayloadStore(
        client=s3,
        bucket="hermes-issue-events",
        prefix="phase-1a",
    )

    first_ref = store.put(sanitized)
    second_ref = store.put(sanitized)

    assert first_ref == second_ref
    assert first_ref.startswith("s3://hermes-issue-events/phase-1a/sha256/")
    assert len(s3.objects) == 1
    stored = next(iter(s3.objects.values()))
    body = json.loads(stored["Body"])
    serialized = stored["Body"].decode()
    assert stored["ServerSideEncryption"] == "AES256"
    assert len(body["issue"]["title"]) == 512
    assert len(body["issue"]["body"]) <= 4096
    assert "[REDACTED]" in body["issue"]["body"]
    assert "must-not-persist" not in serialized
    assert "alice@example.com" not in serialized


def test_payload_redacts_complete_private_key_before_bounding() -> None:
    private_key = (
        "-----BEGIN PRIVATE KEY-----\n"
        + ("sensitive-key-material\n" * 300)
        + "-----END PRIVATE KEY-----"
    )
    raw = {
        "action": "opened",
        "repository": {"full_name": "hotelbyte-com/hotel-be"},
        "issue": {
            "number": 22338,
            "title": "Security report",
            "body": private_key,
            "state": "open",
            "labels": [],
            "user": {"login": "reporter"},
            "updated_at": "2026-07-28T01:02:03Z",
            "html_url": "https://github.com/hotelbyte-com/hotel-be/issues/22338",
        },
        "sender": {"login": "reporter", "type": "User"},
    }

    sanitized = PayloadSanitizer().sanitize_issue_event(raw)

    assert sanitized["issue"]["body"] == "[REDACTED]"
    assert "sensitive-key-material" not in json.dumps(sanitized)
