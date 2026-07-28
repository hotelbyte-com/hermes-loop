from datetime import UTC, datetime
import logging

from fastapi.testclient import TestClient
from fastapi.routing import APIRoute

from issue_control.status import InternalStatusService, create_status_app
from issue_control.web import create_control_plane_app


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


class _Repository:
    def ping(self):
        return True

    def control_status(self, *, now, authorized_repositories):
        assert now == NOW
        assert authorized_repositories == (
            "hotelbyte-com/hotel-be",
            "hotelbyte-com/hotel-fe",
        )
        return {
            "leader": {
                "node_id": "s3",
                "lease_epoch": 12,
                "renewed_at": "2026-07-28T12:00:00+00:00",
                "eligible_for_takeover": False,
            },
            "nodes": [
                {
                    "node_id": "s3",
                    "role": "leader",
                    "configured_role": "primary",
                    "ready": True,
                },
                {
                    "node_id": "s5",
                    "role": "standby",
                    "configured_role": "standby",
                    "ready": True,
                },
            ],
            "reconciliation": [
                {
                    "repository": "hotelbyte-com/hotel-be",
                    "lag_seconds": 30,
                    "open_issue_count": 4,
                    "observed_issue_count": 4,
                    "classified": True,
                },
                {
                    "repository": "hotelbyte-com/hotel-fe",
                    "lag_seconds": 45,
                    "open_issue_count": 3,
                    "observed_issue_count": 3,
                    "classified": True,
                },
            ],
        }

    def issue_session_trace(self, *, issue_key=None, run_id=None, limit=100):
        if issue_key not in (None, "hotelbyte-com/hotel-be#22338"):
            raise ValueError("issue_key must have the form owner/repo#number")
        if run_id is not None and not run_id.strip():
            raise ValueError("run_id filter must be nonblank")
        assert run_id in (None, "reconcile-42")
        assert limit in (100, 5)
        return [
            {
                "issue_key": "hotelbyte-com/hotel-be#22338",
                "session_id": "session-1",
                "run_id": "reconcile-42",
                "lease_epoch": 12,
                "kind": "event_observed",
            }
        ]


class _GitHub:
    permissions_verified = True


class _Redis:
    available = False
    last_error = "redis unavailable"


def _service() -> InternalStatusService:
    return InternalStatusService(
        repository=_Repository(),
        github=_GitHub(),
        advisory=_Redis(),
        node_id="s3",
        mode="shadow",
        authorized_repositories=(
            "hotelbyte-com/hotel-be",
            "hotelbyte-com/hotel-fe",
        ),
        clock=lambda: NOW,
    )


def test_status_exposes_leader_standby_epoch_reconciliation_lag_and_classification() -> (
    None
):
    status = _service().status()

    assert status["mode"] == "shadow"
    assert status["leader"]["node_id"] == "s3"
    assert status["leader"]["lease_epoch"] == 12
    assert status["standby_ready"] is True
    assert status["all_authorized_open_issues_classified"] is True
    assert status["maximum_reconciliation_lag_seconds"] == 45
    assert status["redis"]["authoritative"] is False
    assert status["redis"]["available"] is False


def test_readiness_remains_true_during_redis_loss_when_postgres_and_read_scope_are_valid() -> (
    None
):
    readiness = _service().readiness()

    assert readiness == {
        "ready": True,
        "mode": "shadow",
        "postgres": True,
        "github_read_only": True,
        "redis": False,
    }


def test_readiness_logs_postgres_failure_and_remains_fail_closed(caplog) -> None:
    service = _service()

    def fail_ping():
        raise RuntimeError("postgres unavailable")

    service._repository.ping = fail_ping
    with caplog.at_level(logging.ERROR, logger="issue_control.status"):
        readiness = service.readiness()

    assert readiness["ready"] is False
    assert readiness["postgres"] is False
    assert "PostgreSQL readiness check failed" in caplog.text
    assert "postgres unavailable" in caplog.text


def test_internal_status_app_is_read_only_and_returns_distinct_health_and_ready() -> (
    None
):
    app = create_status_app(_service())
    client = TestClient(app)

    health = client.get("/internal/health")
    ready = client.get("/internal/ready")
    status = client.get("/internal/status")
    reconciliation = client.get("/internal/reconciliation")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert ready.status_code == 200
    assert ready.json()["ready"] is True
    assert status.json()["leader"]["lease_epoch"] == 12
    assert len(reconciliation.json()["repositories"]) == 2
    assert reconciliation.json()["issue_sessions"][0]["session_id"] == "session-1"
    public_methods = {
        method
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/internal/")
        for method in (route.methods or set())
    }
    assert public_methods <= {"GET", "HEAD"}


class _Runtime:
    def start(self):
        pass

    def stop(self):
        pass

    def ingest_webhook(self, *, headers, body):
        return {"size": len(body)}


class _Closeable:
    def __init__(self, name, events):
        self.name = name
        self.events = events

    def close(self):
        self.events.append(self.name)


def test_production_app_reuses_status_routes_and_bounds_webhook_body(
    monkeypatch,
) -> None:
    threadpool_calls = []

    async def run_in_threadpool(function, *args, **kwargs):
        threadpool_calls.append((function, args, kwargs))
        return function(*args, **kwargs)

    monkeypatch.setattr(
        "issue_control.web.run_in_threadpool",
        run_in_threadpool,
    )
    app = create_control_plane_app(
        status_service=_service(),
        runtime=_Runtime(),
    )
    with TestClient(app) as client:
        reconciliation = client.get(
            "/internal/reconciliation",
            params={
                "issue_key": "hotelbyte-com/hotel-be#22338",
                "run_id": "reconcile-42",
                "limit": 5,
            },
        )
        oversized = client.post(
            "/github/events",
            content=b"x" * (1_048_576 + 1),
        )
        accepted = client.post("/github/events", content=b"{}")

    assert reconciliation.status_code == 200
    assert reconciliation.json()["issue_sessions"][0]["run_id"] == "reconcile-42"
    assert oversized.status_code == 413
    assert accepted.status_code == 202
    assert len(threadpool_calls) == 1


def test_production_app_closes_owned_clients_in_reverse_order() -> None:
    events = []
    first = _Closeable("redis", events)
    second = _Closeable("github", events)
    third = _Closeable("s3", events)
    app = create_control_plane_app(
        status_service=_service(),
        runtime=_Runtime(),
        close_callbacks=(first.close, second.close, third.close),
    )

    with TestClient(app):
        assert events == []

    assert events == ["s3", "github", "redis"]


def test_reconciliation_trace_filters_reject_invalid_queries() -> None:
    client = TestClient(create_status_app(_service()))

    invalid_issue = client.get(
        "/internal/reconciliation",
        params={"issue_key": "not-an-issue"},
    )
    blank_run = client.get(
        "/internal/reconciliation",
        params={"run_id": " "},
    )

    assert invalid_issue.status_code == 422
    assert blank_run.status_code == 422
