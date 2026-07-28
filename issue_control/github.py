"""Read-only GitHub installation client.

There is intentionally no generic request method and no mutation method.
Runtime readiness requires an installation-permission audit before use.
"""

from __future__ import annotations

from typing import Any

import httpx


class GitHubReadError(RuntimeError):
    """A read request failed or returned an invalid contract."""


class GitHubPermissionError(GitHubReadError):
    """The supplied installation token is not provably read-only."""


class GitHubReadOnlyClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        if not token:
            raise ValueError("GitHub installation read token is required")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "hermes-issue-control-shadow",
            },
            timeout=timeout_seconds,
            transport=transport,
        )
        self._permissions_verified = False

    def _get(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        response = self._client.get(path, params=params)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GitHubReadError(
                f"GitHub GET {path} failed with HTTP {response.status_code}"
            ) from exc
        return response

    def assert_read_only_permissions(self) -> None:
        body = self._get("/installation").json()
        permissions = body.get("permissions")
        if not isinstance(permissions, dict):
            raise GitHubPermissionError("installation permissions are missing")
        unsafe = sorted(
            f"{name}={level}"
            for name, level in permissions.items()
            if str(level).casefold() not in {"read", "none"}
        )
        if unsafe:
            raise GitHubPermissionError(
                "GitHub installation has non-read permissions: " + ", ".join(unsafe)
            )
        if permissions.get("issues") != "read":
            raise GitHubPermissionError("GitHub installation must grant issues=read")
        self._permissions_verified = True

    @property
    def permissions_verified(self) -> bool:
        return self._permissions_verified

    def list_open_issues(self, repository: str) -> list[dict[str, Any]]:
        if not self._permissions_verified:
            raise GitHubPermissionError(
                "read-only installation permissions must be verified before reconciliation"
            )
        issues: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self._get(
                f"/repos/{repository}/issues",
                params={"state": "open", "per_page": 100, "page": page},
            )
            batch = response.json()
            if not isinstance(batch, list):
                raise GitHubReadError("GitHub issue list response must be an array")
            issues.extend(issue for issue in batch if "pull_request" not in issue)
            if len(batch) < 100:
                break
            page += 1
        return issues

    def close(self) -> None:
        self._client.close()


__all__ = [
    "GitHubPermissionError",
    "GitHubReadError",
    "GitHubReadOnlyClient",
]
