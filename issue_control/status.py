"""Internal read-only health, readiness, and reconciliation projections."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import logging
from typing import Any, Protocol

from fastapi import (
    APIRouter,
    FastAPI,
    HTTPException,
    Query,
    Response,
    status as http_status,
)

_LOGGER = logging.getLogger(__name__)


class StatusRepository(Protocol):
    def ping(self) -> bool: ...

    def control_status(
        self,
        *,
        now: datetime,
        authorized_repositories: tuple[str, ...],
    ) -> dict[str, Any]: ...

    def issue_session_trace(
        self,
        *,
        issue_key: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...


class GitHubPermissionStatus(Protocol):
    @property
    def permissions_verified(self) -> bool: ...


class RedisStatus(Protocol):
    available: bool
    last_error: str | None


class InternalStatusService:
    def __init__(
        self,
        *,
        repository: StatusRepository,
        github: GitHubPermissionStatus,
        advisory: RedisStatus,
        node_id: str,
        mode: str,
        authorized_repositories: tuple[str, ...],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if mode != "shadow":
            raise ValueError("internal status can only represent shadow mode")
        self._repository = repository
        self._github = github
        self._advisory = advisory
        self._node_id = node_id
        self._mode = mode
        self._authorized_repositories = authorized_repositories
        self._clock = clock

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": self._mode,
            "node_id": self._node_id,
        }

    def readiness(self) -> dict[str, Any]:
        try:
            postgres_ready = bool(self._repository.ping())
        except Exception:
            _LOGGER.exception("PostgreSQL readiness check failed")
            postgres_ready = False
        github_read_only = bool(self._github.permissions_verified)
        return {
            "ready": postgres_ready and github_read_only,
            "mode": self._mode,
            "postgres": postgres_ready,
            "github_read_only": github_read_only,
            "redis": bool(self._advisory.available),
        }

    def status(self) -> dict[str, Any]:
        projection = self._repository.control_status(
            now=self._clock(),
            authorized_repositories=self._authorized_repositories,
        )
        reconciliation = projection["reconciliation"]
        reconciled_repositories = {
            item["repository"] for item in reconciliation if item.get("classified")
        }
        all_classified = reconciled_repositories == set(self._authorized_repositories)
        lags = [
            item["lag_seconds"]
            for item in reconciliation
            if item.get("lag_seconds") is not None
        ]
        standby_ready = any(
            node.get("role") == "standby" and node.get("ready") is True
            for node in projection["nodes"]
        )
        return {
            "mode": self._mode,
            "node_id": self._node_id,
            "leader": projection["leader"],
            "nodes": projection["nodes"],
            "standby_ready": standby_ready,
            "reconciliation": reconciliation,
            "all_authorized_open_issues_classified": all_classified,
            "maximum_reconciliation_lag_seconds": max(lags) if lags else None,
            "redis": {
                "available": bool(self._advisory.available),
                "authoritative": False,
                "last_error": self._advisory.last_error,
            },
        }

    def reconciliation(
        self,
        *,
        issue_key: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        status = self.status()
        return {
            "all_authorized_open_issues_classified": status[
                "all_authorized_open_issues_classified"
            ],
            "maximum_lag_seconds": status["maximum_reconciliation_lag_seconds"],
            "repositories": status["reconciliation"],
            "issue_sessions": self._repository.issue_session_trace(
                issue_key=issue_key,
                run_id=run_id,
                limit=limit,
            ),
        }


def create_status_router(service: InternalStatusService) -> APIRouter:
    router = APIRouter()

    @router.get("/internal/health")
    def health() -> dict[str, Any]:
        return service.health()

    @router.get("/internal/ready")
    def ready(response: Response) -> dict[str, Any]:
        readiness = service.readiness()
        if not readiness["ready"]:
            response.status_code = http_status.HTTP_503_SERVICE_UNAVAILABLE
        return readiness

    @router.get("/internal/status")
    def control_status() -> dict[str, Any]:
        return service.status()

    @router.get("/internal/reconciliation")
    def reconciliation(
        issue_key: str | None = None,
        run_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        try:
            return service.reconciliation(
                issue_key=issue_key,
                run_id=run_id,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

    return router


def create_status_app(service: InternalStatusService) -> FastAPI:
    app = FastAPI(
        title="Hermes Issue Control Plane Internal Status",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(create_status_router(service))

    return app


__all__ = [
    "InternalStatusService",
    "create_status_app",
    "create_status_router",
]
