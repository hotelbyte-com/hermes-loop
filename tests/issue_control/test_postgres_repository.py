from concurrent.futures import ThreadPoolExecutor
from collections.abc import Generator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import os
from typing import Any, NoReturn
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from issue_control.coordination import RedisAdvisoryCoordination
from issue_control.contracts import (
    ActorKind,
    IssueEvent,
    IssueState,
    RiskTier,
)
from issue_control.repository import (
    EventDisposition,
    MutationContext,
    PostgresIssueRepository,
    RepositoryConflict,
    StaleFenceError,
    _event_tiebreaker,
)
from issue_control.ingestion import IssueEventIngestor
from issue_control.state_machine import StaleTransition


POSTGRES_DSN = os.getenv("HERMES_ISSUE_CONTROL_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="set HERMES_ISSUE_CONTROL_TEST_POSTGRES_DSN for PostgreSQL contract tests",
)


@pytest.fixture
def repository() -> Generator[PostgresIssueRepository, None, None]:
    schema = f"issue_control_test_{uuid4().hex}"
    repo = PostgresIssueRepository(POSTGRES_DSN or "", schema=schema)
    repo.migrate()
    yield repo
    repo.drop_schema_for_test()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _leader(
    repository: PostgresIssueRepository,
    now: datetime,
    node_id: str = "s3",
) -> MutationContext:
    repository.bootstrap_cluster(
        primary_node="s3",
        standby_node="s5",
        now=now,
    )
    decision = repository.try_acquire_leadership(node_id=node_id, now=now)
    assert decision.is_leader
    return MutationContext(
        node_id=node_id,
        lease_epoch=decision.lease_epoch,
        run_id=f"test-{node_id}",
    )


def _event(
    *,
    event_id: str,
    github_version: int,
    occurred_at: datetime,
) -> IssueEvent:
    return IssueEvent(
        event_id=event_id,
        issue_key="hotelbyte-com/hotel-be#22338",
        github_version=github_version,
        event_type="issues.opened",
        actor_kind=ActorKind.HUMAN,
        occurred_at=occurred_at,
        sanitized_payload_ref=f"s3://issue-events/{event_id}.json",
    )


class _PayloadStore:
    def put(self, payload: Mapping[str, Any]) -> str:
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return f"s3://sanitized-payloads/{digest}.json"


def _webhook(
    *,
    delivery_id: str,
    updated_at: str,
) -> tuple[dict[str, str], bytes]:
    body = json.dumps({
        "action": "opened",
        "repository": {"full_name": "hotelbyte-com/hotel-be"},
        "issue": {
            "number": 22338,
            "title": "Issue control observation",
            "body": "",
            "state": "open",
            "labels": [{"name": "security"}],
            "user": {"login": "reporter"},
            "updated_at": updated_at,
            "html_url": "https://github.com/hotelbyte-com/hotel-be/issues/22338",
        },
        "sender": {"login": "reporter", "type": "User"},
    }).encode()
    signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    return (
        {
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": f"sha256={signature}",
        },
        body,
    )


def test_migration_installs_pgvector_ready_and_append_only_schema(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    context = _leader(repository, now)
    result = repository.observe_event(
        _event(event_id="delivery-1", github_version=1, occurred_at=now),
        candidate_session_id="session-1",
        risk_tier=RiskTier.HIGH,
        context=context,
        now=now,
    )

    assert result.disposition is EventDisposition.APPLIED
    assert repository.schema_capabilities() == {
        "append_only_events": True,
        "append_only_snapshots": True,
        "pgvector": True,
    }
    with pytest.raises(Exception, match="append-only"):
        repository.unsafe_update_event_for_test("delivery-1")


def test_concurrent_migration_startup_is_serialized() -> None:
    schema = f"issue_control_test_{uuid4().hex}"
    repositories = [
        PostgresIssueRepository(POSTGRES_DSN or "", schema=schema),
        PostgresIssueRepository(POSTGRES_DSN or "", schema=schema),
    ]
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda repo: repo.migrate(), repositories))

        assert repositories[0].schema_capabilities()["pgvector"] is True
    finally:
        repositories[0].drop_schema_for_test()


def test_concurrent_claims_converge_to_one_active_session(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    context = _leader(repository, now)

    def claim(candidate: int) -> str:
        session, _created = repository.claim_session(
            issue_key="hotelbyte-com/hotel-be#22338",
            candidate_session_id=f"session-{candidate}",
            risk_tier=RiskTier.UNKNOWN,
            context=context,
            now=now,
        )
        return session.session_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        claimed_ids = list(executor.map(claim, range(8)))

    assert len(set(claimed_ids)) == 1
    assert repository.count_active_sessions("hotelbyte-com/hotel-be#22338") == 1


def test_duplicate_and_reordered_events_converge_without_state_regression(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    context = _leader(repository, now)
    current = _event(event_id="delivery-current", github_version=20, occurred_at=now)
    old = _event(
        event_id="delivery-old",
        github_version=10,
        occurred_at=now - timedelta(minutes=10),
    )

    first = repository.observe_event(
        current,
        candidate_session_id="session-current",
        risk_tier=RiskTier.MEDIUM,
        context=context,
        now=now,
    )
    duplicate = repository.observe_event(
        current,
        candidate_session_id="session-duplicate",
        risk_tier=RiskTier.LOW,
        context=context,
        now=now,
    )
    reordered = repository.observe_event(
        old,
        candidate_session_id="session-old",
        risk_tier=RiskTier.LOW,
        context=context,
        now=now,
    )

    assert first.disposition is EventDisposition.APPLIED
    assert duplicate.disposition is EventDisposition.DUPLICATE
    assert reordered.disposition is EventDisposition.STALE
    assert first.session.session_id == duplicate.session.session_id
    assert first.session.session_id == reordered.session.session_id
    assert repository.event_count(current.issue_key) == 2
    assert repository.latest_github_version(current.issue_key) == 20
    assert repository.get_session(current.issue_key).risk_tier is RiskTier.MEDIUM


def test_same_version_events_converge_independently_of_delivery_order(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    context = _leader(repository, now)
    candidate_a = _event(
        event_id="delivery-a",
        github_version=20,
        occurred_at=now,
    )
    candidate_z = IssueEvent(
        event_id="delivery-z",
        issue_key=candidate_a.issue_key,
        github_version=candidate_a.github_version,
        event_type=candidate_a.event_type,
        actor_kind=candidate_a.actor_kind,
        occurred_at=candidate_a.occurred_at,
        sanitized_payload_ref="s3://issue-events/z.json",
    )
    smaller, larger = sorted((candidate_a, candidate_z), key=_event_tiebreaker)
    other_smaller = replace(
        smaller,
        event_id="delivery-a-2",
        issue_key="hotelbyte-com/hotel-be#22339",
    )
    other_larger = replace(
        larger,
        event_id="delivery-z-2",
        issue_key=other_smaller.issue_key,
    )

    larger_first = repository.observe_event(
        larger,
        candidate_session_id="session-same-version",
        risk_tier=RiskTier.HIGH,
        context=context,
        now=now,
    )
    stale = repository.observe_event(
        smaller,
        candidate_session_id="session-same-version",
        risk_tier=RiskTier.LOW,
        context=context,
        now=now,
    )
    smaller_first = repository.observe_event(
        other_smaller,
        candidate_session_id="session-same-version-other",
        risk_tier=RiskTier.LOW,
        context=context,
        now=now,
    )
    applied = repository.observe_event(
        other_larger,
        candidate_session_id="session-same-version-other",
        risk_tier=RiskTier.HIGH,
        context=context,
        now=now,
    )

    assert stale.disposition is EventDisposition.STALE
    assert applied.disposition is EventDisposition.APPLIED
    assert repository.event_count(smaller.issue_key) == 2
    assert repository.event_count(other_smaller.issue_key) == 2
    assert repository.get_session(smaller.issue_key).risk_tier is RiskTier.HIGH
    assert repository.get_session(other_smaller.issue_key).risk_tier is RiskTier.HIGH
    assert stale.session.context_version == larger_first.session.context_version
    assert applied.session.context_version != smaller_first.session.context_version
    assert [
        mutation.kind
        for mutation in repository.list_session_mutations(smaller.issue_key)
    ] == [
        mutation.kind
        for mutation in repository.list_session_mutations(other_smaller.issue_key)
    ]


def test_duplicate_and_reordered_webhooks_converge_through_real_repository(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    context = _leader(repository, now)
    ingestor = IssueEventIngestor(
        repository=repository,
        payload_store=_PayloadStore(),
        webhook_secret="webhook-secret",
        authorized_repositories=("hotelbyte-com/hotel-be",),
    )
    current_headers, current_body = _webhook(
        delivery_id="delivery-current",
        updated_at="2026-07-28T12:00:00Z",
    )
    old_headers, old_body = _webhook(
        delivery_id="delivery-old",
        updated_at="2026-07-28T11:50:00Z",
    )

    first = ingestor.ingest_webhook(
        headers=current_headers,
        body=current_body,
        context=context,
        now=now,
    )
    duplicate = ingestor.ingest_webhook(
        headers=current_headers,
        body=current_body,
        context=context,
        now=now,
    )
    reordered = ingestor.ingest_webhook(
        headers=old_headers,
        body=old_body,
        context=context,
        now=now,
    )

    assert first.disposition is EventDisposition.APPLIED
    assert duplicate.disposition is EventDisposition.DUPLICATE
    assert reordered.disposition is EventDisposition.STALE
    assert first.session.session_id == duplicate.session.session_id
    assert first.session.session_id == reordered.session.session_id
    assert repository.count_active_sessions(first.session.issue_key) == 1
    assert repository.event_count(first.session.issue_key) == 2
    assert repository.get_session(first.session.issue_key).state is IssueState.TRIAGED


def test_concurrent_duplicate_events_converge_to_one_fact(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    context = _leader(repository, now)
    event = _event(
        event_id="delivery-concurrent-duplicate",
        github_version=21,
        occurred_at=now,
    )

    def observe(_attempt: int):
        return repository.observe_event(
            event,
            candidate_session_id="session-concurrent-duplicate",
            risk_tier=RiskTier.HIGH,
            context=context,
            now=now,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(observe, range(2)))

    assert {result.disposition for result in results} == {
        EventDisposition.APPLIED,
        EventDisposition.DUPLICATE,
    }
    assert repository.event_count(event.issue_key) == 1


def test_concurrent_duplicate_webhooks_converge_through_initial_triage(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    context = _leader(repository, now)
    ingestor = IssueEventIngestor(
        repository=repository,
        payload_store=_PayloadStore(),
        webhook_secret="webhook-secret",
        authorized_repositories=("hotelbyte-com/hotel-be",),
    )
    headers, body = _webhook(
        delivery_id="delivery-concurrent-webhook",
        updated_at="2026-07-28T12:00:00Z",
    )

    def ingest(_attempt: int):
        return ingestor.ingest_webhook(
            headers=headers,
            body=body,
            context=context,
            now=now,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(ingest, range(2)))

    assert {result.disposition for result in results} == {
        EventDisposition.APPLIED,
        EventDisposition.DUPLICATE,
    }
    assert all(result.session.state is IssueState.TRIAGED for result in results)


def test_initial_triage_is_idempotent_and_rejects_later_states(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    context = _leader(repository, now)
    observed = repository.observe_event(
        _event(
            event_id="delivery-idempotent-triage", github_version=1, occurred_at=now
        ),
        candidate_session_id="session-idempotent-triage",
        risk_tier=RiskTier.LOW,
        context=context,
        now=now,
    )

    triaged = repository.ensure_session_triaged(
        issue_key=observed.session.issue_key,
        expected_session_id=observed.session.session_id,
        context=context,
        now=now,
    )
    repeated = repository.ensure_session_triaged(
        issue_key=observed.session.issue_key,
        expected_session_id=observed.session.session_id,
        context=context,
        now=now,
    )
    planned = repository.transition_session(
        issue_key=observed.session.issue_key,
        expected_session_id=observed.session.session_id,
        target=IssueState.PLANNED,
        expected_context_version=triaged.context_version,
        context=context,
        now=now,
    )

    assert repeated == triaged
    assert planned.state is IssueState.PLANNED
    with pytest.raises(RepositoryConflict, match="initial triage"):
        repository.ensure_session_triaged(
            issue_key=observed.session.issue_key,
            expected_session_id=observed.session.session_id,
            context=context,
            now=now,
        )


def test_concurrent_reordered_webhooks_converge_through_initial_triage(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    context = _leader(repository, now)
    ingestor = IssueEventIngestor(
        repository=repository,
        payload_store=_PayloadStore(),
        webhook_secret="webhook-secret",
        authorized_repositories=("hotelbyte-com/hotel-be",),
    )
    old_headers, old_body = _webhook(
        delivery_id="delivery-concurrent-old",
        updated_at="2026-07-28T11:59:00Z",
    )
    new_headers, new_body = _webhook(
        delivery_id="delivery-concurrent-new",
        updated_at="2026-07-28T12:00:00Z",
    )

    def ingest(request):
        headers, body = request
        return ingestor.ingest_webhook(
            headers=headers,
            body=body,
            context=context,
            now=now,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                ingest,
                ((old_headers, old_body), (new_headers, new_body)),
            )
        )

    assert all(result.session.state is IssueState.TRIAGED for result in results)
    assert (
        repository.get_session(results[0].session.issue_key).state is IssueState.TRIAGED
    )
    assert repository.event_count(results[0].session.issue_key) == 2


def test_reconciliation_replay_converges_through_real_repository(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    context = _leader(repository, now)
    ingestor = IssueEventIngestor(
        repository=repository,
        payload_store=_PayloadStore(),
        webhook_secret="webhook-secret",
        authorized_repositories=("hotelbyte-com/hotel-be",),
    )
    issue = {
        "number": 22338,
        "title": "Issue control observation",
        "body": "",
        "state": "open",
        "labels": [],
        "user": {"login": "reporter"},
        "updated_at": "2026-07-28T12:00:00Z",
        "html_url": "https://github.com/hotelbyte-com/hotel-be/issues/22338",
    }

    first = ingestor.ingest_reconciliation_issue(
        repository="hotelbyte-com/hotel-be",
        issue=issue,
        context=context,
        now=now,
    )
    replay = ingestor.ingest_reconciliation_issue(
        repository="hotelbyte-com/hotel-be",
        issue=issue,
        context=context,
        now=now + timedelta(minutes=5),
    )

    assert first.event_id == replay.event_id
    assert first.disposition is EventDisposition.APPLIED
    assert replay.disposition is EventDisposition.DUPLICATE
    assert first.session.session_id == replay.session.session_id
    assert repository.count_active_sessions(first.session.issue_key) == 1
    assert repository.event_count(first.session.issue_key) == 1


def test_repository_restart_recovers_postgresql_truth_when_process_state_is_lost(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    context = _leader(repository, now)
    observed = repository.observe_event(
        _event(event_id="delivery-restart", github_version=4, occurred_at=now),
        candidate_session_id="session-restart",
        risk_tier=RiskTier.LOW,
        context=context,
        now=now,
    )

    restarted = PostgresIssueRepository(
        POSTGRES_DSN or "",
        schema=repository.schema,
    )

    assert restarted.get_session(observed.session.issue_key) == observed.session
    assert restarted.event_count(observed.session.issue_key) == 1


def test_closed_issue_reopens_with_a_new_lifecycle_session(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    context = _leader(repository, now)
    first = repository.observe_event(
        _event(event_id="delivery-first-lifecycle", github_version=1, occurred_at=now),
        candidate_session_id="stable-issue-session",
        risk_tier=RiskTier.LOW,
        context=context,
        now=now,
    ).session
    transition_path = (
        IssueState.TRIAGED,
        IssueState.PLANNED,
        IssueState.EXECUTING,
        IssueState.REVIEWING,
        IssueState.PR_OPEN,
        IssueState.CHECKS_GREEN,
        IssueState.MERGED,
        IssueState.VERIFIED,
        IssueState.CLOSED,
    )
    for target in transition_path:
        first = repository.transition_session(
            issue_key=first.issue_key,
            expected_session_id=first.session_id,
            target=target,
            expected_context_version=first.context_version,
            context=context,
            now=now,
        )

    reopened = repository.observe_event(
        _event(
            event_id="delivery-second-lifecycle",
            github_version=2,
            occurred_at=now + timedelta(minutes=1),
        ),
        candidate_session_id="stable-issue-session",
        risk_tier=RiskTier.MEDIUM,
        context=context,
        now=now,
    ).session

    assert reopened.session_id == "stable-issue-session:2"
    assert reopened.session_id != first.session_id
    assert repository.count_active_sessions(reopened.issue_key) == 1


def test_stale_transition_cannot_cross_lifecycle_session_identity(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    context = _leader(repository, now)
    first, _created = repository.claim_session(
        issue_key="hotelbyte-com/hotel-be#22338",
        candidate_session_id="session-lifecycle-cas",
        risk_tier=RiskTier.LOW,
        context=context,
        now=now,
    )
    stale_context_version = first.context_version
    for target in (
        IssueState.TRIAGED,
        IssueState.PLANNED,
        IssueState.EXECUTING,
        IssueState.REVIEWING,
        IssueState.PR_OPEN,
        IssueState.CHECKS_GREEN,
        IssueState.MERGED,
        IssueState.VERIFIED,
        IssueState.CLOSED,
    ):
        first = repository.transition_session(
            issue_key=first.issue_key,
            expected_session_id=first.session_id,
            target=target,
            expected_context_version=first.context_version,
            context=context,
            now=now,
        )
    second, _created = repository.claim_session(
        issue_key=first.issue_key,
        candidate_session_id="session-lifecycle-cas",
        risk_tier=RiskTier.LOW,
        context=context,
        now=now,
    )

    assert second.context_version == stale_context_version
    with pytest.raises(RepositoryConflict, match="is not active"):
        repository.transition_session(
            issue_key=second.issue_key,
            expected_session_id=first.session_id,
            target=IssueState.TRIAGED,
            expected_context_version=stale_context_version,
            context=context,
            now=now,
        )
    with pytest.raises(RepositoryConflict, match="is not active"):
        repository.ensure_session_triaged(
            issue_key=second.issue_key,
            expected_session_id=first.session_id,
            context=context,
            now=now,
        )


def test_redis_loss_and_restart_leave_postgresql_event_and_session_truth_intact(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    class FailedRedis:
        @staticmethod
        def _fail() -> NoReturn:
            raise RedisConnectionError("redis lost")

        def set(self, key: str, value: str, *, ex: int | None = None) -> NoReturn:
            self._fail()

        def rpush(self, key: str, value: str) -> NoReturn:
            self._fail()

        def incr(self, key: str) -> NoReturn:
            self._fail()

        def expire(self, key: str, seconds: int) -> NoReturn:
            self._fail()

        def ping(self) -> NoReturn:
            self._fail()

    context = _leader(repository, now)
    observed = repository.observe_event(
        _event(event_id="delivery-redis-loss", github_version=5, occurred_at=now),
        candidate_session_id="session-redis-loss",
        risk_tier=RiskTier.MEDIUM,
        context=context,
        now=now,
    )
    advisory = RedisAdvisoryCoordination(
        client=FailedRedis(),
        cluster_name="issue-control-test",
    )

    assert not advisory.enqueue_event("delivery-redis-loss")

    restarted = PostgresIssueRepository(
        POSTGRES_DSN or "",
        schema=repository.schema,
    )
    assert restarted.get_session(observed.session.issue_key) == observed.session
    assert restarted.event_count(observed.session.issue_key) == 1


def test_s3_to_s5_failover_fences_old_leader_and_preserves_session(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    s3 = _leader(repository, now)
    session, _created = repository.claim_session(
        issue_key="hotelbyte-com/hotel-be#22338",
        candidate_session_id="session-failover",
        risk_tier=RiskTier.HIGH,
        context=s3,
        now=now,
    )

    too_early = repository.try_acquire_leadership(
        node_id="s5",
        now=now + timedelta(days=365),
    )
    repository.age_lease_for_test(timedelta(seconds=60))
    takeover = repository.try_acquire_leadership(
        node_id="s5",
        now=now - timedelta(days=365),
    )

    assert not too_early.is_leader
    assert takeover.is_leader
    assert takeover.lease_epoch == s3.lease_epoch + 1

    with pytest.raises(StaleFenceError):
        repository.transition_session(
            issue_key=session.issue_key,
            expected_session_id=session.session_id,
            target=IssueState.TRIAGED,
            expected_context_version=session.context_version,
            context=s3,
            now=now + timedelta(seconds=60),
        )

    s5 = MutationContext(
        node_id="s5",
        lease_epoch=takeover.lease_epoch,
        run_id="test-s5",
    )
    adopted, created = repository.claim_session(
        issue_key=session.issue_key,
        candidate_session_id="must-not-replace-active-session",
        risk_tier=RiskTier.LOW,
        context=s5,
        now=now + timedelta(seconds=60),
    )
    transitioned = repository.transition_session(
        issue_key=session.issue_key,
        expected_session_id=adopted.session_id,
        target=IssueState.TRIAGED,
        expected_context_version=adopted.context_version,
        context=s5,
        now=now + timedelta(seconds=60),
    )
    recovered_s3 = repository.try_acquire_leadership(
        node_id="s3",
        now=now + timedelta(seconds=61),
    )
    repository.report_node_status(
        node_id="s5",
        ready=True,
        observed_epoch=takeover.lease_epoch,
        now=now + timedelta(seconds=60),
        detail={"role": "leader"},
    )
    repository.report_node_status(
        node_id="s3",
        ready=True,
        observed_epoch=recovered_s3.lease_epoch,
        now=now + timedelta(seconds=61),
        detail={"role": "standby"},
    )
    status = repository.control_status(
        now=now + timedelta(seconds=61),
        authorized_repositories=("hotelbyte-com/hotel-be",),
    )

    assert not created
    assert adopted.session_id == session.session_id
    assert adopted.lease_epoch == takeover.lease_epoch
    assert transitioned.state is IssueState.TRIAGED
    assert not recovered_s3.is_leader
    assert recovered_s3.role == "standby"
    assert {node["node_id"]: node["role"] for node in status["nodes"]} == {
        "s3": "standby",
        "s5": "leader",
    }


def test_duplicate_replay_after_failover_adopts_epoch_and_triages(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    s3 = _leader(repository, now)
    payload_store = _PayloadStore()
    headers, body = _webhook(
        delivery_id="delivery-failover-replay",
        updated_at="2026-07-28T12:00:00Z",
    )

    class CrashBeforeTriage:
        def observe_event(self, *args, **kwargs):
            return repository.observe_event(*args, **kwargs)

        def ensure_session_triaged(self, **kwargs):
            raise RuntimeError("simulated leader loss before triage")

    interrupted = IssueEventIngestor(
        repository=CrashBeforeTriage(),
        payload_store=payload_store,
        webhook_secret="webhook-secret",
        authorized_repositories=("hotelbyte-com/hotel-be",),
    )
    with pytest.raises(RuntimeError, match="leader loss"):
        interrupted.ingest_webhook(
            headers=headers,
            body=body,
            context=s3,
            now=now,
        )

    repository.age_lease_for_test(timedelta(seconds=60))
    takeover = repository.try_acquire_leadership(node_id="s5", now=now)
    assert takeover.is_leader
    s5 = MutationContext(
        node_id="s5",
        lease_epoch=takeover.lease_epoch,
        run_id="test-failover-replay",
    )
    replay = IssueEventIngestor(
        repository=repository,
        payload_store=payload_store,
        webhook_secret="webhook-secret",
        authorized_repositories=("hotelbyte-com/hotel-be",),
    ).ingest_webhook(
        headers=headers,
        body=body,
        context=s5,
        now=now,
    )

    assert replay.disposition is EventDisposition.DUPLICATE
    assert replay.session.state is IssueState.TRIAGED
    assert replay.session.lease_epoch == takeover.lease_epoch


def test_expired_leader_cannot_renew_itself_after_takeover_window(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    s3 = _leader(repository, now)
    repository.age_lease_for_test(timedelta(seconds=60))

    expired_renewal = repository.try_acquire_leadership(
        node_id="s3",
        now=now - timedelta(days=365),
    )

    assert not expired_renewal.is_leader
    assert expired_renewal.lease_epoch == s3.lease_epoch


def test_fence_freshness_uses_postgresql_clock_not_caller_timestamp(
    repository: PostgresIssueRepository,
) -> None:
    caller_time = datetime.now(UTC) + timedelta(days=365)
    context = _leader(repository, caller_time)
    repository.age_lease_for_test(timedelta(seconds=61))

    with pytest.raises(StaleFenceError):
        repository.observe_event(
            _event(
                event_id="delivery-stale-caller-clock",
                github_version=1,
                occurred_at=caller_time,
            ),
            candidate_session_id="session-stale-caller-clock",
            risk_tier=RiskTier.LOW,
            context=context,
            now=caller_time,
        )


def test_new_event_projection_invalidates_stale_transition_context(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    context = _leader(repository, now)
    initial = repository.observe_event(
        _event(event_id="delivery-context-v1", github_version=1, occurred_at=now),
        candidate_session_id="session-context-cas",
        risk_tier=RiskTier.LOW,
        context=context,
        now=now,
    ).session
    projected = repository.observe_event(
        _event(
            event_id="delivery-context-v2",
            github_version=2,
            occurred_at=now + timedelta(seconds=1),
        ),
        candidate_session_id="session-context-cas",
        risk_tier=RiskTier.HIGH,
        context=context,
        now=now,
    ).session

    assert projected.context_version != initial.context_version
    with pytest.raises(StaleTransition):
        repository.transition_session(
            issue_key=initial.issue_key,
            expected_session_id=initial.session_id,
            target=IssueState.TRIAGED,
            expected_context_version=initial.context_version,
            context=context,
            now=now,
        )


def test_every_session_mutation_is_traceable_by_session_run_and_epoch(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    leader = _leader(repository, now)
    context = MutationContext(
        node_id=leader.node_id,
        lease_epoch=leader.lease_epoch,
        run_id="run-42",
    )
    observed = repository.observe_event(
        _event(event_id="delivery-trace", github_version=9, occurred_at=now),
        candidate_session_id="session-trace",
        risk_tier=RiskTier.HIGH,
        context=context,
        now=now,
    )

    mutations = repository.list_session_mutations(observed.session.issue_key)

    assert [mutation.kind for mutation in mutations] == ["claimed"]
    assert all(mutation.session_id == "session-trace" for mutation in mutations)
    assert all(mutation.run_id == "run-42" for mutation in mutations)
    assert all(mutation.lease_epoch == leader.lease_epoch for mutation in mutations)
    trace = repository.issue_session_trace(run_id="run-42", limit=10)
    assert {item["issue_key"] for item in trace} == {observed.session.issue_key}
    assert {item["session_id"] for item in trace} == {"session-trace"}
    assert {item["lease_epoch"] for item in trace} == {leader.lease_epoch}


def test_mutation_context_requires_nonblank_run_trace() -> None:
    with pytest.raises(ValueError, match="run_id"):
        MutationContext(node_id="s3", lease_epoch=1, run_id="")


def test_reconciliation_status_measures_lag_and_classification_coverage(
    repository: PostgresIssueRepository,
    now: datetime,
) -> None:
    context = _leader(repository, now)
    repository.record_reconciliation_started(
        "hotelbyte-com/hotel-be",
        "reconcile-42",
        context,
        now,
    )
    repository.record_reconciliation_completed(
        "hotelbyte-com/hotel-be",
        "reconcile-42",
        open_issue_count=3,
        observed_issue_count=3,
        newest_github_updated_at=now - timedelta(minutes=1),
        context=context,
        now=now,
    )
    repository.age_reconciliation_for_test(
        "hotelbyte-com/hotel-be",
        timedelta(seconds=25),
    )

    status = repository.control_status(
        now=now + timedelta(days=365),
        authorized_repositories=(
            "hotelbyte-com/hotel-be",
            "hotelbyte-com/hotel-fe",
        ),
    )

    assert status["reconciliation"][0]["classified"] is True
    assert 25 <= status["reconciliation"][0]["lag_seconds"] < 27
    assert status["reconciliation"][1]["classified"] is False
    assert status["reconciliation"][1]["lag_seconds"] is None
