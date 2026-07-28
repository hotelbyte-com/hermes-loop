from datetime import UTC, datetime
from typing import NoReturn

from redis.exceptions import ConnectionError as RedisConnectionError

from issue_control.coordination import (
    LeaderCoordinator,
    RedisAdvisoryCoordination,
)
from issue_control.repository import LeadershipDecision


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str | int] = {}
        self.expirations: dict[str, int] = {}
        self.lists: dict[str, list[str]] = {}

    def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        self.values[key] = value
        if ex is not None:
            self.expirations[key] = ex
        return True

    def rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def incr(self, key: str) -> int:
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value

    def expire(self, key: str, seconds: int) -> bool:
        self.expirations[key] = seconds
        return True

    def ping(self) -> bool:
        return True


class _FailedRedis:
    @staticmethod
    def _fail() -> NoReturn:
        raise RedisConnectionError("redis unavailable")

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


def test_redis_is_advisory_for_queue_rate_limit_and_leader_cache() -> None:
    redis = _Redis()
    coordination = RedisAdvisoryCoordination(
        client=redis,
        cluster_name="issue-control",
    )
    decision = LeadershipDecision(
        node_id="s3",
        is_leader=True,
        lease_epoch=4,
        role="leader",
        leader_node="s3",
        renewed_at=NOW,
    )

    assert coordination.publish_leadership(decision)
    assert coordination.enqueue_event("github:delivery-1")
    assert coordination.allow("github-read", limit=2, window_seconds=60)
    assert coordination.allow("github-read", limit=2, window_seconds=60)
    assert not coordination.allow("github-read", limit=2, window_seconds=60)
    assert coordination.available
    assert redis.expirations["issue-control:leader"] == 60
    assert redis.lists["issue-control:events"] == ["github:delivery-1"]


def test_redis_loss_is_reported_but_does_not_invent_or_erase_leadership() -> None:
    coordination = RedisAdvisoryCoordination(
        client=_FailedRedis(),
        cluster_name="issue-control",
    )

    assert not coordination.enqueue_event("github:delivery-1")
    assert not coordination.allow("github-read", limit=1, window_seconds=60)
    assert not coordination.available
    assert coordination.last_error == "redis unavailable"


class _LeaderRepository:
    def __init__(self) -> None:
        self.status_reports: list[dict] = []

    def try_acquire_leadership(self, *, node_id, now):
        return LeadershipDecision(
            node_id=node_id,
            is_leader=True,
            lease_epoch=11,
            role="leader",
            leader_node=node_id,
            renewed_at=now,
        )

    def report_node_status(
        self,
        *,
        node_id,
        ready,
        observed_epoch,
        now,
        detail,
    ):
        self.status_reports.append({
            "node_id": node_id,
            "ready": ready,
            "observed_epoch": observed_epoch,
            "now": now,
            "detail": detail,
        })


def test_leader_coordinator_uses_postgresql_decision_when_redis_is_down() -> None:
    repository = _LeaderRepository()
    coordinator = LeaderCoordinator(
        repository=repository,
        advisory=RedisAdvisoryCoordination(
            client=_FailedRedis(),
            cluster_name="issue-control",
        ),
        node_id="s3",
    )

    tick = coordinator.tick(now=NOW)

    assert tick.decision.is_leader
    assert tick.decision.lease_epoch == 11
    assert not tick.redis_available
    assert repository.status_reports[0]["ready"]
    assert repository.status_reports[0]["detail"]["redis_available"] is False
