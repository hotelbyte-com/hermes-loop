"""Credential-reference resolution without placing credentials in config.yaml."""

from __future__ import annotations

import os


class SecretResolutionError(RuntimeError):
    """A configured credential reference cannot be resolved."""


class EnvironmentSecretResolver:
    """Resolve ``secret://env/NAME`` from the process credential environment."""

    _PREFIX = "secret://env/"

    def resolve(self, reference: str) -> str:
        if not reference.startswith(self._PREFIX):
            raise SecretResolutionError(
                "only secret://env/NAME references are supported by this runtime"
            )
        name = reference.removeprefix(self._PREFIX)
        if not name or not name.replace("_", "").isalnum():
            raise SecretResolutionError("invalid environment secret reference")
        value = os.environ.get(name)
        if not value:
            raise SecretResolutionError(f"required secret {name!r} is unavailable")
        return value


def resolve_if_reference(value: str, resolver: EnvironmentSecretResolver) -> str:
    if value.startswith("secret://"):
        return resolver.resolve(value)
    return value


__all__ = [
    "EnvironmentSecretResolver",
    "SecretResolutionError",
    "resolve_if_reference",
]
