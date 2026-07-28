"""Bounded webhook sanitization and content-addressed S3 persistence."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Protocol


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(gh[pousr]_[a-z0-9_]{8,})\b"),
    re.compile(r"(?i)\b(token|password|secret|api[_-]?key)\s*[:=]\s*\S+"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S
    ),
)


class S3Client(Protocol):
    def put_object(self, **kwargs: Any) -> Any: ...


class PayloadSanitizer:
    """Allowlist the issue fields required for classification and audit."""

    def sanitize_issue_event(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        repository = _required_mapping(raw.get("repository"), "repository")
        issue = _required_mapping(raw.get("issue"), "issue")
        sender = _mapping_or_empty(raw.get("sender"), "sender")
        user = _mapping_or_empty(issue.get("user"), "issue.user")
        labels = issue.get("labels") or []
        if not isinstance(labels, list):
            raise ValueError("issue.labels must be an array")
        number = issue.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("issue.number must be a positive integer")
        return {
            "action": _bounded_text(raw.get("action"), 64),
            "repository": {
                "full_name": _bounded_text(repository.get("full_name"), 256).casefold(),
            },
            "issue": {
                "number": number,
                "title": _redact(_bounded_text(issue.get("title"), 512)),
                "body": _redact(_bounded_text(issue.get("body"), 4096)),
                "state": _bounded_text(issue.get("state"), 32),
                "labels": [
                    _redact(_bounded_text(label.get("name"), 128))
                    for label in labels[:50]
                    if isinstance(label, Mapping) and label.get("name")
                ],
                "user_login": _bounded_text(
                    user.get("login"),
                    128,
                ),
                "updated_at": _bounded_text(issue.get("updated_at"), 64),
                "html_url": _bounded_text(issue.get("html_url"), 512),
            },
            "sender": {
                "login": _bounded_text(sender.get("login"), 128),
                "type": _bounded_text(sender.get("type"), 32),
            },
        }


class S3SanitizedPayloadStore:
    def __init__(
        self,
        *,
        client: S3Client,
        bucket: str,
        prefix: str = "issue-control",
    ) -> None:
        if not bucket:
            raise ValueError("S3 bucket is required")
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    def put(self, sanitized_payload: Mapping[str, Any]) -> str:
        body = json.dumps(
            sanitized_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        prefix = f"{self._prefix}/" if self._prefix else ""
        key = f"{prefix}sha256/{digest[:2]}/{digest}.json"
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            ServerSideEncryption="AES256",
            Metadata={"sha256": digest, "sanitized": "true"},
        )
        return f"s3://{self._bucket}/{key}"


def _bounded_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def _required_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _mapping_or_empty(value: Any, field: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    return _required_mapping(value, field)


def _redact(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


__all__ = [
    "PayloadSanitizer",
    "S3Client",
    "S3SanitizedPayloadStore",
]
