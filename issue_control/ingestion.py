"""Authenticated GitHub event ingestion into durable issue observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
from typing import Any, Mapping, Protocol
from uuid import NAMESPACE_URL, uuid5

from issue_control.contracts import (
    ActorKind,
    IssueEvent,
    IssueSession,
    IssueState,
    RiskTier,
    issue_key,
)
from issue_control.payloads import PayloadSanitizer
from issue_control.repository import (
    EventDisposition,
    MutationContext,
    ObservationResult,
)


MAX_WEBHOOK_BYTES = 1_048_576
SUPPORTED_GITHUB_EVENTS = frozenset({"issues", "issue_comment"})


class GitHubWebhookError(ValueError):
    """Webhook authentication or shape failed before persistence."""


class PayloadStore(Protocol):
    def put(self, payload: Mapping[str, Any]) -> str: ...


class EventRepository(Protocol):
    def observe_event(
        self,
        event: IssueEvent,
        *,
        candidate_session_id: str,
        risk_tier: RiskTier,
        context: MutationContext,
        now: datetime,
    ) -> ObservationResult: ...

    def ensure_session_triaged(
        self,
        *,
        issue_key: str,
        expected_session_id: str,
        context: MutationContext,
        now: datetime,
    ) -> IssueSession: ...


@dataclass(frozen=True, slots=True)
class IngestionResult:
    disposition: EventDisposition
    session: IssueSession
    event_id: str


class RiskClassifier:
    """Conservative deterministic Phase 1A classification."""

    _HIGH_LABELS = frozenset({
        "security",
        "authorization",
        "auth",
        "payments",
        "pricing",
        "ddl",
        "backfill",
        "production",
        "destructive",
    })
    _LOW_LABELS = frozenset({"documentation", "docs", "typo", "low-risk"})

    def classify(self, sanitized_payload: Mapping[str, Any]) -> RiskTier:
        issue = sanitized_payload.get("issue") or {}
        labels = {
            str(label).strip().casefold()
            for label in issue.get("labels", [])
            if str(label).strip()
        }
        if labels & self._HIGH_LABELS:
            return RiskTier.HIGH
        if labels and labels <= self._LOW_LABELS:
            return RiskTier.LOW
        return RiskTier.MEDIUM


class IssueEventIngestor:
    def __init__(
        self,
        *,
        repository: EventRepository,
        payload_store: PayloadStore,
        webhook_secret: str,
        authorized_repositories: tuple[str, ...],
        sanitizer: PayloadSanitizer | None = None,
        classifier: RiskClassifier | None = None,
    ) -> None:
        if not webhook_secret:
            raise ValueError("GitHub webhook secret is required")
        self._repository = repository
        self._payload_store = payload_store
        self._webhook_secret = webhook_secret.encode("utf-8")
        self._authorized_repositories = frozenset(authorized_repositories)
        self._sanitizer = sanitizer or PayloadSanitizer()
        self._classifier = classifier or RiskClassifier()

    def ingest_webhook(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
        context: MutationContext,
        now: datetime,
    ) -> IngestionResult:
        if len(body) > MAX_WEBHOOK_BYTES:
            raise GitHubWebhookError("webhook body exceeds the one-megabyte limit")
        normalized_headers = {key.casefold(): value for key, value in headers.items()}
        delivery_id = normalized_headers.get("x-github-delivery", "").strip()
        event_name = normalized_headers.get("x-github-event", "").strip()
        signature = normalized_headers.get("x-hub-signature-256", "").strip()
        if not delivery_id:
            raise GitHubWebhookError("X-GitHub-Delivery is required")
        if event_name not in SUPPORTED_GITHUB_EVENTS:
            raise GitHubWebhookError(f"unsupported GitHub event {event_name!r}")
        expected = (
            "sha256="
            + hmac.new(
                self._webhook_secret,
                body,
                hashlib.sha256,
            ).hexdigest()
        )
        if not hmac.compare_digest(signature, expected):
            raise GitHubWebhookError("invalid GitHub webhook signature")
        try:
            raw = json.loads(body)
        except (TypeError, json.JSONDecodeError) as exc:
            raise GitHubWebhookError("webhook body is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise GitHubWebhookError("webhook body must be a JSON object")
        sanitized = self._sanitize_and_authorize(raw)
        action = sanitized["action"] or "unknown"
        return self._observe(
            sanitized=sanitized,
            event_id=f"github:{delivery_id}",
            event_type=f"{event_name}.{action}",
            actor_kind=_actor_kind(sanitized),
            context=context,
            now=now,
        )

    def ingest_reconciliation_issue(
        self,
        *,
        repository: str,
        issue: Mapping[str, Any],
        context: MutationContext,
        now: datetime,
    ) -> IngestionResult:
        raw = {
            "action": "reconciled",
            "repository": {"full_name": repository},
            "issue": dict(issue),
            "sender": {"login": "hermes-reconciler", "type": "System"},
        }
        sanitized = self._sanitize_and_authorize(raw)
        key = issue_key(repository, int(issue["number"]))
        github_version = _github_version(sanitized["issue"]["updated_at"])
        identity = f"{key}:{github_version}:issues.reconciled"
        event_id = "reconcile:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self._observe(
            sanitized=sanitized,
            event_id=event_id,
            event_type="issues.reconciled",
            actor_kind=ActorKind.SYSTEM,
            context=context,
            now=now,
        )

    def _sanitize_and_authorize(
        self,
        raw: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            sanitized = self._sanitizer.sanitize_issue_event(raw)
            repository = sanitized["repository"]["full_name"]
            issue_key(repository, sanitized["issue"]["number"])
            _parse_github_time(sanitized["issue"]["updated_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubWebhookError(f"invalid GitHub issue payload: {exc}") from exc
        if repository not in self._authorized_repositories:
            raise GitHubWebhookError(f"repository {repository!r} is not authorized")
        return sanitized

    def _observe(
        self,
        *,
        sanitized: Mapping[str, Any],
        event_id: str,
        event_type: str,
        actor_kind: ActorKind,
        context: MutationContext,
        now: datetime,
    ) -> IngestionResult:
        repository_name = sanitized["repository"]["full_name"]
        issue_number = sanitized["issue"]["number"]
        stable_issue_key = issue_key(repository_name, issue_number)
        occurred_at = _parse_github_time(sanitized["issue"]["updated_at"])
        payload_ref = self._payload_store.put(sanitized)
        event = IssueEvent(
            event_id=event_id,
            issue_key=stable_issue_key,
            github_version=_datetime_version(occurred_at),
            event_type=event_type,
            actor_kind=actor_kind,
            occurred_at=occurred_at,
            sanitized_payload_ref=payload_ref,
        )
        risk_tier = self._classifier.classify(sanitized)
        candidate_session_id = "issue:" + str(uuid5(NAMESPACE_URL, stable_issue_key))
        observation = self._repository.observe_event(
            event,
            candidate_session_id=candidate_session_id,
            risk_tier=risk_tier,
            context=context,
            now=now,
        )
        session = observation.session
        if session.state is IssueState.DISCOVERED:
            session = self._repository.ensure_session_triaged(
                issue_key=stable_issue_key,
                expected_session_id=session.session_id,
                context=context,
                now=now,
            )
        return IngestionResult(observation.disposition, session, event.event_id)


def _parse_github_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("GitHub updated_at must be timezone-aware")
    return parsed.astimezone(UTC)


def _datetime_version(value: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = value.astimezone(UTC) - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _github_version(value: str) -> int:
    return _datetime_version(_parse_github_time(value))


def _actor_kind(sanitized: Mapping[str, Any]) -> ActorKind:
    sender = sanitized.get("sender") or {}
    actor_type = str(sender.get("type", "")).casefold()
    login = str(sender.get("login", "")).casefold()
    if login.startswith("hermes"):
        return ActorKind.HERMES
    if actor_type == "bot":
        return ActorKind.BOT
    if actor_type == "system":
        return ActorKind.SYSTEM
    if actor_type in {"user", "organization"}:
        return ActorKind.HUMAN
    return ActorKind.UNKNOWN


__all__ = [
    "GitHubWebhookError",
    "IngestionResult",
    "IssueEventIngestor",
    "MAX_WEBHOOK_BYTES",
    "RiskClassifier",
    "SUPPORTED_GITHUB_EVENTS",
]
