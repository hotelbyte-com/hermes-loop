from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from issue_control.coordination import LeaderTick
from issue_control.repository import LeadershipDecision
from issue_control.runtime import IssueControlRuntime, NotLeaderError


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


class _Leader:
    def __init__(self, *, is_leader: bool) -> None:
        self.is_leader = is_leader

    def tick(self, *, now: datetime) -> LeaderTick:
        return LeaderTick(
            decision=LeadershipDecision(
                node_id="s3",
                is_leader=self.is_leader,
                lease_epoch=17,
                role="leader" if self.is_leader else "standby",
                leader_node="s3" if self.is_leader else "s5",
                renewed_at=now,
            ),
            redis_available=False,
        )


class _Reconciler:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def run_once(self, *, context: Any, now: datetime, run_id: str) -> dict[str, str]:
        self.calls.append((context, now, run_id))
        return {"run_id": run_id}


class _Ingestor:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def ingest_webhook(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
        context: Any,
        now: datetime,
    ) -> dict[str, str]:
        self.calls.append((headers, body, context, now))
        return {"event_id": headers["X-GitHub-Delivery"]}


class _Advisory:
    def __init__(self) -> None:
        self.events: list[str] = []

    def enqueue_event(self, event_id: str) -> bool:
        self.events.append(event_id)
        return False


def test_runtime_reconciles_and_ingests_only_under_current_postgres_leadership() -> (
    None
):
    reconciler = _Reconciler()
    ingestor = _Ingestor()
    advisory = _Advisory()
    runtime = IssueControlRuntime(
        leader=_Leader(is_leader=True),
        reconciler=reconciler,
        ingestor=ingestor,
        advisory=advisory,
        renewal_interval_seconds=10,
        reconciliation_interval_seconds=300,
        clock=lambda: NOW,
        run_id_factory=lambda prefix: f"{prefix}-deterministic",
    )

    tick = runtime.renew_leadership()
    summary = runtime.reconcile_once()
    result = runtime.ingest_webhook(
        headers={"X-GitHub-Delivery": "delivery-1"},
        body=b"{}",
    )

    assert tick.decision.lease_epoch == 17
    assert reconciler.calls[0][0].lease_epoch == 17
    assert reconciler.calls[0][2] == "reconciliation-deterministic"
    assert ingestor.calls[0][2].run_id == "webhook-delivery-1"
    assert result == {"event_id": "delivery-1"}
    assert advisory.events == ["delivery-1"]
    assert summary == {"run_id": "reconciliation-deterministic"}


def test_standby_runtime_cannot_ingest_or_reconcile() -> None:
    runtime = IssueControlRuntime(
        leader=_Leader(is_leader=False),
        reconciler=_Reconciler(),
        ingestor=_Ingestor(),
        advisory=_Advisory(),
        renewal_interval_seconds=10,
        reconciliation_interval_seconds=300,
        clock=lambda: NOW,
    )
    runtime.renew_leadership()

    with pytest.raises(NotLeaderError):
        runtime.reconcile_once()
    with pytest.raises(NotLeaderError):
        runtime.ingest_webhook(
            headers={"X-GitHub-Delivery": "delivery-1"},
            body=b"{}",
        )
