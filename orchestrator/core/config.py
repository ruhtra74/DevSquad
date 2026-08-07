"""Configuration globale de l'orchestrateur (fichier JSON, éditable via CLI)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "root": None,  # résolu vers ~/.config/orchestrator si non défini
    "max_parallel": 1,
    "auto_approve": False,
    "backends": {
        "pm": "opencode",
        "architect": "opencode",
        "lead_manager": "opencode",
        "coder": "opencode",
        "tester": "opencode",
        "devops": "opencode",
    },
    "agents_dir": None,
    "prompts_dir": None,
}


def default_root() -> Path:
    return Path.home() / ".config" / "orchestrator"


class Config:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = dict(DEFAULTS)
        if path.exists():
            self.data.update(json.loads(path.read_text()))

    @classmethod
    def load(cls) -> "Config":
        return cls(default_root() / "config.json")

    def get(self, key: str) -> Any:
        node: Any = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def set(self, key: str, value: Any) -> None:
        value = self._coerce(value)
        parts = key.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        self.save()

    @staticmethod
    def _coerce(value: Any) -> Any:
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off"):
                return False
        return value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")
