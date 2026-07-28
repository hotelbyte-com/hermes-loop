"""Shadow-only runtime loops for leadership, ingestion, and reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
import logging
import threading
from typing import Any, Protocol
from uuid import uuid4

from issue_control.coordination import LeaderTick
from issue_control.repository import (
    LeadershipDecision,
    MutationContext,
    StaleFenceError,
)


logger = logging.getLogger(__name__)


class NotLeaderError(RuntimeError):
    """This node is standby and therefore cannot request a durable mutation."""


class Reconciler(Protocol):
    def run_once(
        self,
        *,
        context: MutationContext,
        now: datetime,
        run_id: str,
    ) -> Any: ...


class Ingestor(Protocol):
    def ingest_webhook(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
        context: MutationContext,
        now: datetime,
    ) -> Any: ...


class AdvisoryQueue(Protocol):
    def enqueue_event(self, event_id: str) -> bool: ...


class LeaderElector(Protocol):
    def tick(self, *, now: datetime) -> LeaderTick: ...


class IssueControlRuntime:
    """Owns only observation loops; it never dispatches or executes issue work."""

    def __init__(
        self,
        *,
        leader: LeaderElector,
        reconciler: Reconciler,
        ingestor: Ingestor,
        advisory: AdvisoryQueue,
        renewal_interval_seconds: int,
        reconciliation_interval_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        run_id_factory: Callable[[str], str] = lambda prefix: f"{prefix}-{uuid4()}",
    ) -> None:
        if renewal_interval_seconds <= 0:
            raise ValueError("renewal interval must be positive")
        if reconciliation_interval_seconds <= 0:
            raise ValueError("reconciliation interval must be positive")
        self._leader = leader
        self._reconciler = reconciler
        self._ingestor = ingestor
        self._advisory = advisory
        self._renewal_interval_seconds = renewal_interval_seconds
        self._reconciliation_interval_seconds = reconciliation_interval_seconds
        self._clock = clock
        self._run_id_factory = run_id_factory
        self._leadership_lock = threading.Lock()
        self._leadership: LeadershipDecision | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def renew_leadership(self) -> LeaderTick:
        tick = self._leader.tick(now=self._clock())
        with self._leadership_lock:
            self._leadership = tick.decision
        return tick

    def _mutation_context(self, run_id: str) -> MutationContext:
        with self._leadership_lock:
            decision = self._leadership
        if decision is None or not decision.is_leader:
            raise NotLeaderError("only the current PostgreSQL leader may mutate facts")
        return MutationContext(
            node_id=decision.node_id,
            lease_epoch=decision.lease_epoch,
            run_id=run_id,
        )

    def reconcile_once(self) -> Any:
        run_id = self._run_id_factory("reconciliation")
        context = self._mutation_context(run_id)
        try:
            return self._reconciler.run_once(
                context=context,
                now=self._clock(),
                run_id=run_id,
            )
        except StaleFenceError as exc:
            self._reject_stale_context(context)
            raise NotLeaderError(
                "PostgreSQL rejected this node's stale leadership fence"
            ) from exc

    def ingest_webhook(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> Any:
        delivery_id = next(
            (
                value
                for key, value in headers.items()
                if key.casefold() == "x-github-delivery"
            ),
            "unknown",
        )
        context = self._mutation_context(f"webhook-{delivery_id}")
        try:
            result = self._ingestor.ingest_webhook(
                headers=headers,
                body=body,
                context=context,
                now=self._clock(),
            )
        except StaleFenceError as exc:
            self._reject_stale_context(context)
            raise NotLeaderError(
                "PostgreSQL rejected this node's stale leadership fence"
            ) from exc
        event_id = (
            result.get("event_id")
            if isinstance(result, dict)
            else getattr(result, "event_id", None)
        )
        if event_id:
            self._advisory.enqueue_event(str(event_id))
        return result

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        self.renew_leadership()
        self._threads = [
            threading.Thread(
                target=self._renewal_loop,
                name="issue-control-leader-renewal",
                daemon=True,
            ),
            threading.Thread(
                target=self._reconciliation_loop,
                name="issue-control-reconciliation",
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join()
        self._threads.clear()

    def _reject_stale_context(self, context: MutationContext) -> None:
        with self._leadership_lock:
            decision = self._leadership
            if (
                decision is not None
                and decision.node_id == context.node_id
                and decision.lease_epoch == context.lease_epoch
            ):
                self._leadership = None

    def _renewal_loop(self) -> None:
        while not self._stop.wait(self._renewal_interval_seconds):
            try:
                self.renew_leadership()
            except Exception:
                with self._leadership_lock:
                    self._leadership = None
                logger.exception("issue-control leadership renewal failed closed")

    def _reconciliation_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile_once()
            except NotLeaderError:
                pass
            except Exception:
                logger.exception("issue-control reconciliation failed")
            if self._stop.wait(self._reconciliation_interval_seconds):
                break


__all__ = [
    "IssueControlRuntime",
    "LeaderElector",
    "NotLeaderError",
]
