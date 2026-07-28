"""Fail-closed lifecycle state transitions."""

from __future__ import annotations

from dataclasses import replace

from issue_control.contracts import IssueSession, IssueState


class TransitionError(RuntimeError):
    """Base error for a rejected session transition."""


class InvalidTransition(TransitionError):
    """The lifecycle graph does not allow this state edge."""


class StaleTransition(TransitionError):
    """The caller observed an old context version or fencing epoch."""


_ALLOWED: dict[IssueState, frozenset[IssueState]] = {
    IssueState.DISCOVERED: frozenset({IssueState.TRIAGED, IssueState.QUARANTINED}),
    IssueState.TRIAGED: frozenset({
        IssueState.PLANNED,
        IssueState.FAILED_RETRYABLE,
        IssueState.QUARANTINED,
    }),
    IssueState.PLANNED: frozenset({
        IssueState.EXECUTING,
        IssueState.FAILED_RETRYABLE,
        IssueState.QUARANTINED,
    }),
    IssueState.EXECUTING: frozenset({
        IssueState.REVIEWING,
        IssueState.FAILED_RETRYABLE,
        IssueState.QUARANTINED,
    }),
    IssueState.REVIEWING: frozenset({
        IssueState.AWAITING_HUMAN,
        IssueState.PR_OPEN,
        IssueState.FAILED_RETRYABLE,
        IssueState.QUARANTINED,
    }),
    IssueState.AWAITING_HUMAN: frozenset({
        IssueState.PLANNED,
        IssueState.PR_OPEN,
        IssueState.QUARANTINED,
    }),
    IssueState.PR_OPEN: frozenset({
        IssueState.CHECKS_GREEN,
        IssueState.FAILED_RETRYABLE,
        IssueState.QUARANTINED,
    }),
    IssueState.CHECKS_GREEN: frozenset({
        IssueState.MERGED,
        IssueState.FAILED_RETRYABLE,
        IssueState.QUARANTINED,
    }),
    IssueState.MERGED: frozenset({
        IssueState.VERIFIED,
        IssueState.FAILED_RETRYABLE,
        IssueState.QUARANTINED,
    }),
    IssueState.VERIFIED: frozenset({
        IssueState.CLOSED,
        IssueState.FAILED_RETRYABLE,
        IssueState.QUARANTINED,
    }),
    IssueState.CLOSED: frozenset(),
    IssueState.FAILED_RETRYABLE: frozenset({
        IssueState.TRIAGED,
        IssueState.PLANNED,
        IssueState.EXECUTING,
        IssueState.REVIEWING,
        IssueState.PR_OPEN,
        IssueState.CHECKS_GREEN,
        IssueState.MERGED,
        IssueState.VERIFIED,
        IssueState.QUARANTINED,
    }),
    IssueState.QUARANTINED: frozenset({IssueState.AWAITING_HUMAN}),
}


def can_transition(source: IssueState, target: IssueState) -> bool:
    return target in _ALLOWED[source]


def transition_session(
    session: IssueSession,
    *,
    target: IssueState,
    expected_context_version: int,
    lease_epoch: int,
) -> IssueSession:
    """Apply one lifecycle edge after context and fencing CAS checks."""
    if (
        expected_context_version != session.context_version
        or lease_epoch != session.lease_epoch
    ):
        raise StaleTransition(
            "session context version or fencing epoch no longer matches"
        )
    if not can_transition(session.state, target):
        raise InvalidTransition(
            f"{session.state.value} -> {target.value} is not allowed"
        )
    return replace(
        session,
        state=target,
        context_version=session.context_version + 1,
    )


__all__ = [
    "InvalidTransition",
    "StaleTransition",
    "TransitionError",
    "can_transition",
    "transition_session",
]
