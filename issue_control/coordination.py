"""Redis advisory coordination and PostgreSQL-authoritative leadership."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Protocol

from redis.exceptions import RedisError

from issue_control.repository import LeadershipDecision


class RedisClient(Protocol):
    def set(self, key: str, value: str, *, ex: int | None = None) -> Any: ...
    def rpush(self, key: str, value: str) -> Any: ...
    def incr(self, key: str) -> int: ...
    def expire(self, key: str, seconds: int) -> Any: ...
    def ping(self) -> Any: ...


class LeaderRepository(Protocol):
    def try_acquire_leadership(
        self,
        *,
        node_id: str,
        now: datetime,
    ) -> LeadershipDecision: ...

    def report_node_status(
        self,
        *,
        node_id: str,
        ready: bool,
        observed_epoch: int,
        now: datetime,
        detail: dict[str, Any],
    ) -> None: ...


class RedisAdvisoryCoordination:
    """Best-effort queues, rate limits, and lease cache.

    Failure changes availability diagnostics only. It cannot authorize a
    durable mutation or alter PostgreSQL session/event truth.
    """

    def __init__(
        self,
        *,
        client: RedisClient,
        cluster_name: str,
        leader_ttl_seconds: int = 60,
    ) -> None:
        self._client = client
        self._prefix = cluster_name
        self._leader_ttl_seconds = leader_ttl_seconds
        self.available = True
        self.last_error: str | None = None

    def _success(self) -> None:
        self.available = True
        self.last_error = None

    def _failure(self, exc: RedisError) -> None:
        self.available = False
        self.last_error = str(exc)

    def ping(self) -> bool:
        try:
            self._client.ping()
        except RedisError as exc:
            self._failure(exc)
            return False
        self._success()
        return True

    def publish_leadership(self, decision: LeadershipDecision) -> bool:
        payload = json.dumps(
            {
                "node_id": decision.node_id,
                "is_leader": decision.is_leader,
                "leader_node": decision.leader_node,
                "lease_epoch": decision.lease_epoch,
                "renewed_at": decision.renewed_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            self._client.set(
                f"{self._prefix}:leader",
                payload,
                ex=self._leader_ttl_seconds,
            )
        except RedisError as exc:
            self._failure(exc)
            return False
        self._success()
        return True

    def enqueue_event(self, event_id: str) -> bool:
        try:
            self._client.rpush(f"{self._prefix}:events", event_id)
        except RedisError as exc:
            self._failure(exc)
            return False
        self._success()
        return True

    def allow(self, name: str, *, limit: int, window_seconds: int) -> bool:
        if limit < 1 or window_seconds < 1:
            raise ValueError("rate limit and window must be positive")
        key = f"{self._prefix}:rate:{name}"
        try:
            count = self._client.incr(key)
            if count == 1:
                self._client.expire(key, window_seconds)
        except RedisError as exc:
            self._failure(exc)
            return False
        self._success()
        return count <= limit


@dataclass(frozen=True, slots=True)
class LeaderTick:
    decision: LeadershipDecision
    redis_available: bool


class LeaderCoordinator:
    def __init__(
        self,
        *,
        repository: LeaderRepository,
        advisory: RedisAdvisoryCoordination,
        node_id: str,
    ) -> None:
        self._repository = repository
        self._advisory = advisory
        self._node_id = node_id

    def tick(self, *, now: datetime) -> LeaderTick:
        decision = self._repository.try_acquire_leadership(
            node_id=self._node_id,
            now=now,
        )
        redis_available = self._advisory.publish_leadership(decision)
        self._repository.report_node_status(
            node_id=self._node_id,
            ready=True,
            observed_epoch=decision.lease_epoch,
            now=now,
            detail={
                "role": decision.role,
                "redis_available": redis_available,
                "redis_error": self._advisory.last_error,
            },
        )
        return LeaderTick(
            decision=decision,
            redis_available=redis_available,
        )


__all__ = [
    "LeaderCoordinator",
    "LeaderTick",
    "RedisAdvisoryCoordination",
]
