from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
from typing import Any, Mapping

import pytest

from issue_control.contracts import IssueSession, IssueState, RiskTier
from issue_control.ingestion import (
    GitHubWebhookError,
    IssueEventIngestor,
)
from issue_control.repository import (
    EventDisposition,
    MutationContext,
    ObservationResult,
)
from issue_control.reconciliation import ReconciliationService
from issue_control.state_machine import StaleTransition


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


class _PayloadStore:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def put(self, payload: Mapping[str, Any]) -> str:
        self.payloads.append(dict(payload))
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return f"s3://events/{digest}.json"


class _Repository:
    def __init__(self) -> None:
        self.events = []
        self.sessions: dict[str, IssueSession] = {}
        self.reconciliation_calls: list[tuple] = []

    def observe_event(
        self,
        event,
        *,
        candidate_session_id,
        risk_tier,
        context,
        now,
    ):
        self.events.append((event, context, now))
        session = self.sessions.get(event.issue_key)
        if session is None:
            session = IssueSession(
                issue_key=event.issue_key,
                session_id=candidate_session_id,
                state=IssueState.DISCOVERED,
                context_version=2,
                task_graph_ref=None,
                active_run_id=None,
                risk_tier=risk_tier,
                lease_epoch=context.lease_epoch,
            )
            self.sessions[event.issue_key] = session
            disposition = EventDisposition.APPLIED
        else:
            disposition = EventDisposition.DUPLICATE
        return ObservationResult(disposition, session)

    def transition_session(
        self,
        *,
        issue_key,
        target,
        expected_context_version,
        context,
        now,
    ):
        current = self.sessions[issue_key]
        assert expected_context_version == current.context_version
        transitioned = replace(
            current,
            state=target,
            context_version=current.context_version + 1,
        )
        self.sessions[issue_key] = transitioned
        return transitioned

    def get_session(self, issue_key):
        return self.sessions[issue_key]

    def record_reconciliation_started(self, repository, run_id, context, now):
        self.reconciliation_calls.append(("started", repository, run_id, context, now))

    def record_reconciliation_completed(
        self,
        repository,
        run_id,
        *,
        open_issue_count,
        observed_issue_count,
        newest_github_updated_at,
        context,
        now,
    ):
        self.reconciliation_calls.append((
            "completed",
            repository,
            run_id,
            open_issue_count,
            observed_issue_count,
            newest_github_updated_at,
            context,
            now,
        ))

    def record_reconciliation_failed(self, repository, run_id, error, context, now):
        self.reconciliation_calls.append((
            "failed",
            repository,
            run_id,
            error,
            context,
            now,
        ))


def _webhook_body(*, updated_at: str = "2026-07-28T12:00:00Z") -> bytes:
    return json.dumps({
        "action": "opened",
        "repository": {"full_name": "hotelbyte-com/hotel-be"},
        "issue": {
            "number": 22338,
            "title": "Authorization bypass",
            "body": "The actor can bypass the ownership check",
            "state": "open",
            "labels": [{"name": "security"}],
            "user": {"login": "reporter"},
            "updated_at": updated_at,
            "html_url": "https://github.com/hotelbyte-com/hotel-be/issues/22338",
        },
        "sender": {"login": "reporter", "type": "User"},
    }).encode()


def _headers(body: bytes, *, delivery: str = "delivery-1") -> dict[str, str]:
    signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": f"sha256={signature}",
    }


def test_signed_webhook_is_sanitized_observed_and_classified() -> None:
    repository = _Repository()
    store = _PayloadStore()
    ingestor = IssueEventIngestor(
        repository=repository,
        payload_store=store,
        webhook_secret="webhook-secret",
        authorized_repositories=("hotelbyte-com/hotel-be",),
    )
    body = _webhook_body()

    result = ingestor.ingest_webhook(
        headers=_headers(body),
        body=body,
        context=MutationContext(node_id="s3", lease_epoch=7, run_id="run-1"),
        now=NOW,
    )

    assert result.session.state is IssueState.TRIAGED
    assert result.session.risk_tier is RiskTier.HIGH
    assert result.disposition is EventDisposition.APPLIED
    event = repository.events[0][0]
    assert event.issue_key == "hotelbyte-com/hotel-be#22338"
    assert event.event_type == "issues.opened"
    assert event.event_id == "github:delivery-1"
    assert event.sanitized_payload_ref.startswith("s3://events/")
    assert store.payloads[0]["issue"]["title"] == "Authorization bypass"


def test_concurrent_initial_triage_accepts_already_achieved_state() -> None:
    class RacingRepository(_Repository):
        def transition_session(self, **kwargs):
            current = self.sessions[kwargs["issue_key"]]
            self.sessions[kwargs["issue_key"]] = replace(
                current,
                state=IssueState.TRIAGED,
                context_version=current.context_version + 1,
            )
            raise StaleTransition("concurrent triage won the context CAS")

    repository = RacingRepository()
    ingestor = IssueEventIngestor(
        repository=repository,
        payload_store=_PayloadStore(),
        webhook_secret="webhook-secret",
        authorized_repositories=("hotelbyte-com/hotel-be",),
    )
    body = _webhook_body()

    result = ingestor.ingest_webhook(
        headers=_headers(body),
        body=body,
        context=MutationContext(node_id="s3", lease_epoch=7, run_id="run-race"),
        now=NOW,
    )

    assert result.disposition is EventDisposition.APPLIED
    assert result.session.state is IssueState.TRIAGED


@pytest.mark.parametrize(
    ("headers_mutation", "body"),
    [
        ({"X-Hub-Signature-256": "sha256=bad"}, _webhook_body()),
        ({"X-GitHub-Event": "push"}, _webhook_body()),
        ({"X-GitHub-Delivery": ""}, _webhook_body()),
        ({}, b"{" + (b"x" * 1_048_576)),
    ],
)
def test_invalid_webhook_contracts_fail_before_persistence(
    headers_mutation: dict[str, str],
    body: bytes,
) -> None:
    repository = _Repository()
    store = _PayloadStore()
    ingestor = IssueEventIngestor(
        repository=repository,
        payload_store=store,
        webhook_secret="webhook-secret",
        authorized_repositories=("hotelbyte-com/hotel-be",),
    )
    headers = _headers(body)
    headers.update(headers_mutation)

    with pytest.raises(GitHubWebhookError):
        ingestor.ingest_webhook(
            headers=headers,
            body=body,
            context=MutationContext(
                node_id="s3",
                lease_epoch=7,
                run_id="invalid-webhook",
            ),
            now=NOW,
        )

    assert not repository.events
    assert not store.payloads


def test_malformed_signed_github_shape_fails_closed_before_persistence() -> None:
    repository = _Repository()
    store = _PayloadStore()
    ingestor = IssueEventIngestor(
        repository=repository,
        payload_store=store,
        webhook_secret="webhook-secret",
        authorized_repositories=("hotelbyte-com/hotel-be",),
    )
    malformed = json.loads(_webhook_body())
    malformed["issue"]["labels"] = "security"
    body = json.dumps(malformed).encode()

    with pytest.raises(GitHubWebhookError, match="issue.labels"):
        ingestor.ingest_webhook(
            headers=_headers(body),
            body=body,
            context=MutationContext(
                node_id="s3",
                lease_epoch=7,
                run_id="malformed-webhook",
            ),
            now=NOW,
        )

    assert not repository.events
    assert not store.payloads


def test_unauthorized_repository_fails_closed() -> None:
    repository = _Repository()
    ingestor = IssueEventIngestor(
        repository=repository,
        payload_store=_PayloadStore(),
        webhook_secret="webhook-secret",
        authorized_repositories=("hotelbyte-com/hotel-fe",),
    )
    body = _webhook_body()

    with pytest.raises(GitHubWebhookError, match="not authorized"):
        ingestor.ingest_webhook(
            headers=_headers(body),
            body=body,
            context=MutationContext(
                node_id="s3",
                lease_epoch=7,
                run_id="oversized-webhook",
            ),
            now=NOW,
        )


class _GitHub:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def list_open_issues(self, repository: str) -> list[dict]:
        self.requested.append(repository)
        issue_number = 1 if repository.endswith("hotel-be") else 2
        return [
            {
                "number": issue_number,
                "title": f"Issue {issue_number}",
                "body": "",
                "state": "open",
                "labels": [],
                "user": {"login": "reporter"},
                "updated_at": f"2026-07-28T11:5{issue_number}:00Z",
                "html_url": f"https://github.com/{repository}/issues/{issue_number}",
            }
        ]


def test_full_reconciliation_discovers_and_classifies_every_authorized_open_issue() -> (
    None
):
    repository = _Repository()
    github = _GitHub()
    ingestor = IssueEventIngestor(
        repository=repository,
        payload_store=_PayloadStore(),
        webhook_secret="webhook-secret",
        authorized_repositories=(
            "hotelbyte-com/hotel-be",
            "hotelbyte-com/hotel-fe",
        ),
    )
    service = ReconciliationService(
        repository=repository,
        github=github,
        ingestor=ingestor,
        authorized_repositories=(
            "hotelbyte-com/hotel-be",
            "hotelbyte-com/hotel-fe",
        ),
    )

    summary = service.run_once(
        context=MutationContext(node_id="s3", lease_epoch=8, run_id="reconcile-1"),
        now=NOW,
        run_id="reconcile-1",
    )

    assert github.requested == [
        "hotelbyte-com/hotel-be",
        "hotelbyte-com/hotel-fe",
    ]
    assert summary.open_issue_count == 2
    assert summary.observed_issue_count == 2
    assert all(
        session.state is IssueState.TRIAGED for session in repository.sessions.values()
    )
    assert [call[0] for call in repository.reconciliation_calls] == [
        "started",
        "completed",
        "started",
        "completed",
    ]


def test_reconciliation_event_identity_is_deterministic_across_replay() -> None:
    repository = _Repository()
    ingestor = IssueEventIngestor(
        repository=repository,
        payload_store=_PayloadStore(),
        webhook_secret="webhook-secret",
        authorized_repositories=("hotelbyte-com/hotel-be",),
    )
    issue = _GitHub().list_open_issues("hotelbyte-com/hotel-be")[0]
    context = MutationContext(
        node_id="s3",
        lease_epoch=9,
        run_id="reconciliation-replay",
    )

    ingestor.ingest_reconciliation_issue(
        repository="hotelbyte-com/hotel-be",
        issue=issue,
        context=context,
        now=NOW,
    )
    ingestor.ingest_reconciliation_issue(
        repository="hotelbyte-com/hotel-be",
        issue=issue,
        context=context,
        now=NOW + timedelta(minutes=5),
    )

    assert repository.events[0][0].event_id == repository.events[1][0].event_id
    assert (
        repository.events[0][0].github_version == repository.events[1][0].github_version
    )
