from dataclasses import fields
from datetime import UTC, datetime

import pytest

from issue_control.contracts import (
    ActorKind,
    IssueEvent,
    IssueSession,
    IssueState,
    RiskTier,
    issue_key,
)
from issue_control.state_machine import (
    InvalidTransition,
    StaleTransition,
    can_transition,
    transition_session,
)


def test_issue_key_is_stable_and_repository_names_are_case_insensitive() -> None:
    assert issue_key("HotelByte-Com/Hotel-Be", 22338) == "hotelbyte-com/hotel-be#22338"
    assert (
        issue_key(" hotelbyte-com/hotel-be ", 22338) == "hotelbyte-com/hotel-be#22338"
    )


@pytest.mark.parametrize(
    ("repository", "number"),
    [
        ("", 1),
        ("owner-only", 1),
        ("owner/repo", 0),
        ("owner/repo", -1),
        ("owner/repo", True),
    ],
)
def test_issue_key_rejects_ambiguous_identity(repository: str, number: int) -> None:
    with pytest.raises(ValueError):
        issue_key(repository, number)


def test_issue_event_public_contract_has_only_the_approved_fields() -> None:
    assert [field.name for field in fields(IssueEvent)] == [
        "event_id",
        "issue_key",
        "github_version",
        "event_type",
        "actor_kind",
        "occurred_at",
        "sanitized_payload_ref",
    ]

    event = IssueEvent(
        event_id="delivery-123",
        issue_key="hotelbyte-com/hotel-be#22338",
        github_version=42,
        event_type="issues.opened",
        actor_kind=ActorKind.HUMAN,
        occurred_at=datetime(2026, 7, 28, tzinfo=UTC),
        sanitized_payload_ref="s3://hermes-issue-events/sha256/abc",
    )

    assert event.actor_kind is ActorKind.HUMAN


def test_issue_session_public_contract_has_only_the_approved_fields() -> None:
    assert [field.name for field in fields(IssueSession)] == [
        "issue_key",
        "session_id",
        "state",
        "context_version",
        "task_graph_ref",
        "active_run_id",
        "risk_tier",
        "lease_epoch",
    ]

    session = IssueSession(
        issue_key="hotelbyte-com/hotel-be#22338",
        session_id="session-123",
        state=IssueState.DISCOVERED,
        context_version=1,
        task_graph_ref=None,
        active_run_id=None,
        risk_tier=RiskTier.HIGH,
        lease_epoch=7,
    )

    assert session.state is IssueState.DISCOVERED


def test_public_contracts_reject_runtime_type_confusion() -> None:
    with pytest.raises(ValueError, match="github_version"):
        IssueEvent(
            event_id="delivery-1",
            issue_key="hotelbyte-com/hotel-be#1",
            github_version="1",  # type: ignore[arg-type]
            event_type="issues.opened",
            actor_kind=ActorKind.HUMAN,
            occurred_at=datetime(2026, 7, 28, tzinfo=UTC),
            sanitized_payload_ref="s3://events/1",
        )
    with pytest.raises(ValueError, match="state"):
        IssueSession(
            issue_key="hotelbyte-com/hotel-be#1",
            session_id="session-1",
            state="discovered",  # type: ignore[arg-type]
            context_version=1,
            task_graph_ref=None,
            active_run_id=None,
            risk_tier=RiskTier.UNKNOWN,
            lease_epoch=1,
        )


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("discovered", "triaged"),
        ("triaged", "planned"),
        ("planned", "executing"),
        ("executing", "reviewing"),
        ("reviewing", "awaiting_human"),
        ("reviewing", "pr_open"),
        ("awaiting_human", "planned"),
        ("awaiting_human", "pr_open"),
        ("pr_open", "checks_green"),
        ("checks_green", "merged"),
        ("merged", "verified"),
        ("verified", "closed"),
        ("triaged", "failed_retryable"),
        ("planned", "failed_retryable"),
        ("executing", "failed_retryable"),
        ("reviewing", "failed_retryable"),
        ("pr_open", "failed_retryable"),
        ("checks_green", "failed_retryable"),
        ("merged", "failed_retryable"),
        ("verified", "failed_retryable"),
        ("failed_retryable", "triaged"),
        ("failed_retryable", "planned"),
        ("failed_retryable", "executing"),
        ("failed_retryable", "reviewing"),
        ("failed_retryable", "pr_open"),
        ("failed_retryable", "checks_green"),
        ("failed_retryable", "merged"),
        ("failed_retryable", "verified"),
        ("discovered", "quarantined"),
        ("triaged", "quarantined"),
        ("planned", "quarantined"),
        ("executing", "quarantined"),
        ("reviewing", "quarantined"),
        ("awaiting_human", "quarantined"),
        ("pr_open", "quarantined"),
        ("checks_green", "quarantined"),
        ("merged", "quarantined"),
        ("verified", "quarantined"),
        ("failed_retryable", "quarantined"),
        ("quarantined", "awaiting_human"),
    ],
)
def test_approved_state_transitions_are_explicit(source: str, target: str) -> None:
    session = IssueSession(
        issue_key="hotelbyte-com/hotel-be#1",
        session_id="session-1",
        state=IssueState(source),
        context_version=4,
        task_graph_ref=None,
        active_run_id=None,
        risk_tier=RiskTier.UNKNOWN,
        lease_epoch=3,
    )

    updated = transition_session(
        session,
        target=IssueState(target),
        expected_context_version=4,
        lease_epoch=3,
    )

    assert updated.state is IssueState(target)
    assert updated.context_version == 5


def test_state_transition_contract_has_no_implicit_edges() -> None:
    approved = {
        (IssueState(source), IssueState(target))
        for source, target in [
            ("discovered", "triaged"),
            ("triaged", "planned"),
            ("planned", "executing"),
            ("executing", "reviewing"),
            ("reviewing", "awaiting_human"),
            ("reviewing", "pr_open"),
            ("awaiting_human", "planned"),
            ("awaiting_human", "pr_open"),
            ("pr_open", "checks_green"),
            ("checks_green", "merged"),
            ("merged", "verified"),
            ("verified", "closed"),
            ("triaged", "failed_retryable"),
            ("planned", "failed_retryable"),
            ("executing", "failed_retryable"),
            ("reviewing", "failed_retryable"),
            ("pr_open", "failed_retryable"),
            ("checks_green", "failed_retryable"),
            ("merged", "failed_retryable"),
            ("verified", "failed_retryable"),
            ("failed_retryable", "triaged"),
            ("failed_retryable", "planned"),
            ("failed_retryable", "executing"),
            ("failed_retryable", "reviewing"),
            ("failed_retryable", "pr_open"),
            ("failed_retryable", "checks_green"),
            ("failed_retryable", "merged"),
            ("failed_retryable", "verified"),
            ("discovered", "quarantined"),
            ("triaged", "quarantined"),
            ("planned", "quarantined"),
            ("executing", "quarantined"),
            ("reviewing", "quarantined"),
            ("awaiting_human", "quarantined"),
            ("pr_open", "quarantined"),
            ("checks_green", "quarantined"),
            ("merged", "quarantined"),
            ("verified", "quarantined"),
            ("failed_retryable", "quarantined"),
            ("quarantined", "awaiting_human"),
        ]
    }

    actual = {
        (source, target)
        for source in IssueState
        for target in IssueState
        if can_transition(source, target)
    }

    assert actual == approved


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("discovered", "executing"),
        ("triaged", "merged"),
        ("planned", "checks_green"),
        ("closed", "discovered"),
        ("quarantined", "executing"),
    ],
)
def test_invalid_transitions_fail_closed(source: str, target: str) -> None:
    session = IssueSession(
        issue_key="hotelbyte-com/hotel-be#1",
        session_id="session-1",
        state=IssueState(source),
        context_version=4,
        task_graph_ref=None,
        active_run_id=None,
        risk_tier=RiskTier.UNKNOWN,
        lease_epoch=3,
    )

    with pytest.raises(InvalidTransition):
        transition_session(
            session,
            target=IssueState(target),
            expected_context_version=4,
            lease_epoch=3,
        )


@pytest.mark.parametrize(
    ("expected_context_version", "lease_epoch"),
    [
        (3, 3),
        (5, 3),
        (4, 2),
        (4, 4),
    ],
)
def test_stale_context_or_epoch_never_masquerades_as_success(
    expected_context_version: int,
    lease_epoch: int,
) -> None:
    session = IssueSession(
        issue_key="hotelbyte-com/hotel-be#1",
        session_id="session-1",
        state=IssueState.DISCOVERED,
        context_version=4,
        task_graph_ref=None,
        active_run_id=None,
        risk_tier=RiskTier.UNKNOWN,
        lease_epoch=3,
    )

    with pytest.raises(StaleTransition):
        transition_session(
            session,
            target=IssueState.TRIAGED,
            expected_context_version=expected_context_version,
            lease_epoch=lease_epoch,
        )
