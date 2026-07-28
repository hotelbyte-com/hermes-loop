"""Credential-reference resolution without placing credentials in config.yaml."""

from __future__ import annotations

import os
from typing import Protocol


class SecretResolutionError(RuntimeError):
    """A configured credential reference cannot be resolved."""


class SecretResolver(Protocol):
    def resolve(self, reference: str) -> str: ...


_ENVIRONMENT_SECRET_PREFIX = "secret://env/"


def environment_secret_name(reference: str) -> str:
    if not reference.startswith(_ENVIRONMENT_SECRET_PREFIX):
        raise SecretResolutionError(
            "only secret://env/NAME references are supported by this runtime"
        )
    name = reference.removeprefix(_ENVIRONMENT_SECRET_PREFIX)
    if not name or not name.replace("_", "").isalnum():
        raise SecretResolutionError("invalid environment secret reference")
    return name


class EnvironmentSecretResolver:
    """Resolve ``secret://env/NAME`` from the process credential environment."""

    def resolve(self, reference: str) -> str:
        name = environment_secret_name(reference)
        value = os.environ.get(name)
        if not value:
            raise SecretResolutionError(f"required secret {name!r} is unavailable")
        return value


def resolve_if_reference(value: str, resolver: SecretResolver) -> str:
    if value.startswith("secret://"):
        return resolver.resolve(value)
    return value


__all__ = [
    "EnvironmentSecretResolver",
    "SecretResolutionError",
    "environment_secret_name",
    "resolve_if_reference",
]
