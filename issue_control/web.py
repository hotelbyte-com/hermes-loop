"""HTTP ingress and internal read-only observability surface."""

from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status as http_status
from starlette.concurrency import run_in_threadpool

from issue_control.ingestion import GitHubWebhookError, MAX_WEBHOOK_BYTES
from issue_control.runtime import IssueControlRuntime, NotLeaderError
from issue_control.status import InternalStatusService, create_status_router


def create_control_plane_app(
    *,
    status_service: InternalStatusService,
    runtime: IssueControlRuntime,
    close_callbacks: tuple[Callable[[], None], ...] = (),
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with AsyncExitStack() as stack:
            for callback in close_callbacks:
                stack.callback(callback)
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

    app.include_router(create_status_router(status_service))

    @app.post("/github/events", status_code=http_status.HTTP_202_ACCEPTED)
    async def github_events(request: Request) -> dict[str, Any]:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise HTTPException(
                    status_code=http_status.HTTP_400_BAD_REQUEST,
                    detail="invalid Content-Length",
                ) from exc
            if declared_length > MAX_WEBHOOK_BYTES:
                raise HTTPException(
                    status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="webhook body exceeds the one-megabyte limit",
                )
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_WEBHOOK_BYTES:
                raise HTTPException(
                    status_code=http_status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="webhook body exceeds the one-megabyte limit",
                )
            body.extend(chunk)
        try:
            result = await run_in_threadpool(
                runtime.ingest_webhook,
                headers=dict(request.headers),
                body=bytes(body),
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
