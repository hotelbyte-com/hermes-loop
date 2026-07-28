import pytest

from issue_control.config import IssueControlConfig


def _valid_config() -> dict:
    return {
        "mode": "shadow",
        "postgres_dsn": "postgresql://issue-control@postgres/hermes",
        "redis_url": "redis://redis:6379/0",
        "node_id": "s3",
        "primary_node": "s3",
        "standby_node": "s5",
        "authorized_repositories": ["hotelbyte-com/hotel-be"],
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


@pytest.mark.parametrize(
    ("failure_stage", "expected_cleanup"),
    [
        ("permission", ["github", "redis"]),
        ("s3", ["github", "redis"]),
        ("webhook_secret", ["s3", "github", "redis"]),
    ],
)
def test_application_startup_failure_closes_constructed_clients(
    monkeypatch,
    failure_stage: str,
    expected_cleanup: list[str],
) -> None:
    pytest.importorskip("boto3")
    pytest.importorskip("redis")
    pytest.importorskip("psycopg")
    from issue_control.cli import build_application
    from issue_control.github import GitHubPermissionError

    cleanup: list[str] = []

    class Repository:
        cluster_name = "issue-control-test"

        def __init__(self, *args, **kwargs):
            pass

        def migrate(self):
            pass

        def bootstrap_cluster(self, **kwargs):
            pass

    class RedisClient:
        def close(self):
            cleanup.append("redis")

    class RedisFactory:
        @staticmethod
        def from_url(*args, **kwargs):
            return RedisClient()

    class GitHub:
        def __init__(self, **kwargs):
            pass

        def assert_read_only_permissions(self):
            if failure_stage == "permission":
                raise GitHubPermissionError("unsafe permission")

        def close(self):
            cleanup.append("github")

    class S3:
        def close(self):
            cleanup.append("s3")

    class Resolver:
        def resolve(self, reference):
            if failure_stage == "webhook_secret" and reference.endswith(
                "WEBHOOK_SECRET"
            ):
                raise RuntimeError("webhook secret unavailable")
            return "resolved-secret"

    def create_s3(*args, **kwargs):
        if failure_stage == "s3":
            raise RuntimeError("s3 construction failed")
        return S3()

    monkeypatch.setattr("issue_control.cli.PostgresIssueRepository", Repository)
    monkeypatch.setattr("issue_control.cli.Redis", RedisFactory)
    monkeypatch.setattr("issue_control.cli.GitHubReadOnlyClient", GitHub)
    monkeypatch.setattr("issue_control.cli.boto3.client", create_s3)

    with pytest.raises(
        (GitHubPermissionError, RuntimeError),
        match="unsafe permission|s3 construction failed|webhook secret unavailable",
    ):
        build_application(
            IssueControlConfig.from_mapping(_valid_config()),
            resolver=Resolver(),
        )

    assert cleanup == expected_cleanup
