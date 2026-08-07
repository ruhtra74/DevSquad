"""Persistance des états de projet dans SQLite (reprise fiable à tout moment)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from .state import ProjectState


class StateStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.db_path = self.root / "orchestrator.db"
        self.root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS projects (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    phase       TEXT NOT NULL,
                    state_json  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )"""
            )

    def save(self, state: ProjectState) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO projects (id, name, phase, state_json, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name,
                       phase=excluded.phase,
                       state_json=excluded.state_json,
                       updated_at=excluded.updated_at""",
                (
                    state.id,
                    state.name,
                    state.phase.value,
                    state.model_dump_json(),
                    state.updated_at.isoformat(),
                ),
            )

    def get(self, project_id: str) -> Optional[ProjectState]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        if not row:
            return None
        return ProjectState.model_validate_json(row["state_json"])

    def list(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, phase, updated_at FROM projects ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, project_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
