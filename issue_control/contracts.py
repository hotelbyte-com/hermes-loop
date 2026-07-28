"""Stable public value contracts for the Issue Control Plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import re


_REPOSITORY_RE = re.compile(r"^[a-z0-9_.-]+/[a-z0-9_.-]+$")
_S3_REFERENCE_RE = re.compile(r"^s3://[^/\s]+/.+$")


class ActorKind(StrEnum):
    HUMAN = "human"
    BOT = "bot"
    HERMES = "hermes"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class RiskTier(StrEnum):
    UNKNOWN = "unknown"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class IssueState(StrEnum):
    DISCOVERED = "discovered"
    TRIAGED = "triaged"
    PLANNED = "planned"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    AWAITING_HUMAN = "awaiting_human"
    PR_OPEN = "pr_open"
    CHECKS_GREEN = "checks_green"
    MERGED = "merged"
    VERIFIED = "verified"
    CLOSED = "closed"
    FAILED_RETRYABLE = "failed_retryable"
    QUARANTINED = "quarantined"


def issue_key(repository: str, issue_number: int) -> str:
    """Return the case-insensitive stable identity ``owner/repo#number``."""
    if not isinstance(repository, str):
        raise ValueError("repository must be an owner/name string")
    normalized_repository = repository.strip().casefold()
    if not _REPOSITORY_RE.fullmatch(normalized_repository):
        raise ValueError("repository must have the form owner/name")
    if (
        isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number < 1
    ):
        raise ValueError("issue_number must be a positive integer")
    return f"{normalized_repository}#{issue_number}"


def _validate_issue_key(value: str) -> None:
    if not isinstance(value, str):
        raise ValueError("issue_key must be a string")
    repository, separator, number = value.rpartition("#")
    if separator != "#" or not number.isdecimal():
        raise ValueError("issue_key must have the form owner/repo#number")
    if issue_key(repository, int(number)) != value:
        raise ValueError("issue_key must be canonical")


@dataclass(frozen=True, slots=True)
class IssueEvent:
    event_id: str
    issue_key: str
    github_version: int
    event_type: str
    actor_kind: ActorKind
    occurred_at: datetime
    sanitized_payload_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError("event_id is required")
        _validate_issue_key(self.issue_key)
        if (
            isinstance(self.github_version, bool)
            or not isinstance(self.github_version, int)
            or self.github_version < 0
        ):
            raise ValueError("github_version must be a non-negative integer")
        if not isinstance(self.event_type, str) or not self.event_type.strip():
            raise ValueError("event_type is required")
        if not isinstance(self.actor_kind, ActorKind):
            raise ValueError("actor_kind must be an ActorKind")
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise ValueError("occurred_at must be timezone-aware")
        if not isinstance(
            self.sanitized_payload_ref, str
        ) or not _S3_REFERENCE_RE.fullmatch(self.sanitized_payload_ref):
            raise ValueError("sanitized_payload_ref must use s3://")
        if len(self.sanitized_payload_ref) > 2048:
            raise ValueError("sanitized_payload_ref exceeds 2048 characters")


@dataclass(frozen=True, slots=True)
class IssueSession:
    issue_key: str
    session_id: str
    state: IssueState
    context_version: int
    task_graph_ref: str | None
    active_run_id: str | None
    risk_tier: RiskTier
    lease_epoch: int

    def __post_init__(self) -> None:
        _validate_issue_key(self.issue_key)
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id is required")
        if not isinstance(self.state, IssueState):
            raise ValueError("state must be an IssueState")
        if (
            isinstance(self.context_version, bool)
            or not isinstance(self.context_version, int)
            or self.context_version < 1
        ):
            raise ValueError("context_version must be positive")
        if self.task_graph_ref is not None and not isinstance(self.task_graph_ref, str):
            raise ValueError("task_graph_ref must be a string or None")
        if self.active_run_id is not None and not isinstance(self.active_run_id, str):
            raise ValueError("active_run_id must be a string or None")
        if not isinstance(self.risk_tier, RiskTier):
            raise ValueError("risk_tier must be a RiskTier")
        if (
            isinstance(self.lease_epoch, bool)
            or not isinstance(self.lease_epoch, int)
            or self.lease_epoch < 1
        ):
            raise ValueError("lease_epoch must be positive")
