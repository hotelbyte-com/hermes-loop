"""HTTP ingress and internal read-only observability surface."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status as http_status

from issue_control.ingestion import GitHubWebhookError
from issue_control.runtime import IssueControlRuntime, NotLeaderError
from issue_control.status import InternalStatusService


def create_control_plane_app(
    *,
    status_service: InternalStatusService,
    runtime: IssueControlRuntime,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runtime.start()
        try:
            yield
        finally:
            runtime.stop()

    app = FastAPI(
        title="Hermes Issue Control Plane",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/internal/health")
    def health() -> dict[str, Any]:
        return status_service.health()

    @app.get("/internal/ready")
    def ready(response: Response) -> dict[str, Any]:
        readiness = status_service.readiness()
        if not readiness["ready"]:
            response.status_code = http_status.HTTP_503_SERVICE_UNAVAILABLE
        return readiness

    @app.get("/internal/status")
    def status() -> dict[str, Any]:
        return status_service.status()

    @app.get("/internal/reconciliation")
    def reconciliation() -> dict[str, Any]:
        return status_service.reconciliation()

    @app.post("/github/events", status_code=http_status.HTTP_202_ACCEPTED)
    async def github_events(request: Request) -> dict[str, Any]:
        body = await request.body()
        try:
            result = runtime.ingest_webhook(
                headers=dict(request.headers),
                body=body,
            )
        except GitHubWebhookError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except NotLeaderError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        if is_dataclass(result):
            return asdict(result)
        if isinstance(result, dict):
            return result
        return {"status": "accepted"}

    return app


__all__ = ["create_control_plane_app"]
