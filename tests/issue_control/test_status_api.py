from datetime import UTC, datetime

from fastapi.testclient import TestClient
from fastapi.routing import APIRoute

from issue_control.status import InternalStatusService, create_status_app


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
    public_methods = {
        method
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/internal/")
        for method in (route.methods or set())
    }
    assert public_methods <= {"GET", "HEAD"}
