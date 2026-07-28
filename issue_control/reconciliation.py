"""Periodic full reconciliation of all authorized open issues."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Any, Mapping, Protocol

from issue_control.ingestion import IssueEventIngestor, _parse_github_time
from issue_control.repository import MutationContext


DEFAULT_RECONCILIATION_INTERVAL_SECONDS = 300
logger = logging.getLogger(__name__)


class GitHubIssueReader(Protocol):
    def list_open_issues(self, repository: str) -> list[dict[str, Any]]: ...


class ReconciliationRepository(Protocol):
    def record_reconciliation_started(
        self,
        repository: str,
        run_id: str,
        context: MutationContext,
        now: datetime,
    ) -> None: ...

    def record_reconciliation_completed(
        self,
        repository: str,
        run_id: str,
        *,
        open_issue_count: int,
        observed_issue_count: int,
        newest_github_updated_at: datetime | None,
        context: MutationContext,
        now: datetime,
    ) -> None: ...

    def record_reconciliation_failed(
        self,
        repository: str,
        run_id: str,
        error: str,
        context: MutationContext,
        now: datetime,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ReconciliationSummary:
    run_id: str
    repository_count: int
    open_issue_count: int
    observed_issue_count: int
    failures: Mapping[str, str]
    completed_at: datetime


class ReconciliationService:
    def __init__(
        self,
        *,
        repository: ReconciliationRepository,
        github: GitHubIssueReader,
        ingestor: IssueEventIngestor,
        authorized_repositories: tuple[str, ...],
        interval_seconds: int = DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("reconciliation interval must be positive")
        self._repository = repository
        self._github = github
        self._ingestor = ingestor
        self._authorized_repositories = authorized_repositories
        self.interval_seconds = interval_seconds

    def run_once(
        self,
        *,
        context: MutationContext,
        now: datetime,
        run_id: str,
    ) -> ReconciliationSummary:
        open_count = 0
        observed_count = 0
        failures: dict[str, str] = {}
        for repository_name in self._authorized_repositories:
            self._repository.record_reconciliation_started(
                repository_name,
                run_id,
                context,
                now,
            )
            try:
                issues = self._github.list_open_issues(repository_name)
                open_count += len(issues)
                newest: datetime | None = None
                repository_observed = 0
                for issue in issues:
                    self._ingestor.ingest_reconciliation_issue(
                        repository=repository_name,
                        issue=issue,
                        context=context,
                        now=now,
                    )
                    repository_observed += 1
                    updated_at = _parse_github_time(str(issue["updated_at"]))
                    newest = max(newest, updated_at) if newest else updated_at
                observed_count += repository_observed
                self._repository.record_reconciliation_completed(
                    repository_name,
                    run_id,
                    open_issue_count=len(issues),
                    observed_issue_count=repository_observed,
                    newest_github_updated_at=newest,
                    context=context,
                    now=now,
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:1024]
                logger.exception(
                    "issue-control reconciliation failed for %s",
                    repository_name,
                )
                failures[repository_name] = error
                self._repository.record_reconciliation_failed(
                    repository_name,
                    run_id,
                    error,
                    context,
                    now,
                )
        return ReconciliationSummary(
            run_id=run_id,
            repository_count=len(self._authorized_repositories),
            open_issue_count=open_count,
            observed_issue_count=observed_count,
            failures=failures,
            completed_at=now.astimezone(UTC),
        )


__all__ = [
    "DEFAULT_RECONCILIATION_INTERVAL_SECONDS",
    "GitHubIssueReader",
    "ReconciliationService",
    "ReconciliationSummary",
]
