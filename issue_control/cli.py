"""Production entry point for the Phase 1A shadow control plane."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import boto3
from redis import Redis
import uvicorn
import yaml

from hermes_constants import get_hermes_home
from issue_control.config import IssueControlConfig
from issue_control.coordination import (
    LeaderCoordinator,
    RedisAdvisoryCoordination,
    RedisClient,
)
from issue_control.github import GitHubReadOnlyClient
from issue_control.ingestion import IssueEventIngestor, PayloadStore
from issue_control.payloads import S3SanitizedPayloadStore
from issue_control.reconciliation import ReconciliationService
from issue_control.repository import PostgresIssueRepository
from issue_control.runtime import IssueControlRuntime
from issue_control.secrets import (
    EnvironmentSecretResolver,
    SecretResolver,
    resolve_if_reference,
)
from issue_control.status import InternalStatusService
from issue_control.web import create_control_plane_app


def load_config(path: Path) -> IssueControlConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("issue_control"), dict):
        raise ValueError("config.yaml must contain an issue_control mapping")
    return IssueControlConfig.from_mapping(raw["issue_control"])


def build_application(
    config: IssueControlConfig,
    *,
    resolver: SecretResolver | None = None,
):
    secret_resolver = resolver or EnvironmentSecretResolver()
    postgres_dsn = resolve_if_reference(config.postgres_dsn, secret_resolver)
    redis_url = resolve_if_reference(config.redis_url, secret_resolver)
    repository = PostgresIssueRepository(
        postgres_dsn,
        renewal_interval=timedelta(seconds=config.renewal_interval_seconds),
        takeover_after=timedelta(seconds=config.takeover_after_seconds),
    )
    now = datetime.now(UTC)
    repository.migrate()
    repository.bootstrap_cluster(
        primary_node=config.primary_node,
        standby_node=config.standby_node,
        now=now,
    )

    with ExitStack() as startup_cleanup:
        redis_client = Redis.from_url(redis_url, decode_responses=True)
        startup_cleanup.callback(redis_client.close)
        advisory = RedisAdvisoryCoordination(
            client=cast(RedisClient, redis_client),
            cluster_name=repository.cluster_name,
            leader_ttl_seconds=config.takeover_after_seconds,
        )
        github = GitHubReadOnlyClient(
            base_url=config.github.api_base_url,
            token=secret_resolver.resolve(config.github.read_token_secret_ref),
        )
        startup_cleanup.callback(github.close)
        github.assert_read_only_permissions()
        s3_client = boto3.client(
            "s3",
            endpoint_url=config.payload_store.endpoint_url,
        )
        startup_cleanup.callback(s3_client.close)
        payload_store = S3SanitizedPayloadStore(
            client=s3_client,
            bucket=config.payload_store.bucket,
            prefix=config.payload_store.prefix,
        )
        ingestor = IssueEventIngestor(
            repository=repository,
            payload_store=cast(PayloadStore, payload_store),
            webhook_secret=secret_resolver.resolve(config.github.webhook_secret_ref),
            authorized_repositories=config.authorized_repositories,
        )
        reconciliation = ReconciliationService(
            repository=repository,
            github=github,
            ingestor=ingestor,
            authorized_repositories=config.authorized_repositories,
            interval_seconds=config.reconciliation_interval_seconds,
        )
        leader = LeaderCoordinator(
            repository=repository,
            advisory=advisory,
            node_id=config.node_id,
        )
        runtime = IssueControlRuntime(
            leader=leader,
            reconciler=reconciliation,
            ingestor=ingestor,
            advisory=advisory,
            renewal_interval_seconds=config.renewal_interval_seconds,
            reconciliation_interval_seconds=config.reconciliation_interval_seconds,
        )
        status_service = InternalStatusService(
            repository=repository,
            github=github,
            advisory=advisory,
            node_id=config.node_id,
            mode=config.mode,
            authorized_repositories=config.authorized_repositories,
        )
        app = create_control_plane_app(
            status_service=status_service,
            runtime=runtime,
            close_callbacks=(
                redis_client.close,
                github.close,
                s3_client.close,
            ),
        )
        startup_cleanup.pop_all()
        return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run the read-only Hermes Issue Control Plane",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=get_hermes_home() / "config.yaml",
        help="Hermes config.yaml containing issue_control",
    )
    args = parser.parse_args(argv)
    config = load_config(args.config)
    app = build_application(config)
    uvicorn.run(
        app,
        host=config.internal_host,
        port=config.internal_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()


__all__ = [
    "build_application",
    "load_config",
    "main",
]
