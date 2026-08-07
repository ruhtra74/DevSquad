"""Registre des backends disponibles, adressés par nom (ex: "opencode")."""
from __future__ import annotations

from .base import Backend


class BackendRegistry:
    def __init__(self) -> None:
        self._backends: dict[str, Backend] = {}

    def register(self, backend: Backend) -> None:
        self._backends[backend.name] = backend

    def get(self, name: str) -> Backend:
        if name not in self._backends:
            raise KeyError(f"Backend inconnu: {name}")
        return self._backends[name]

    def names(self) -> list[str]:
        return list(self._backends)
