"""Scheduler : exécution des agents (séquentielle en v1) et isolation git worktree.

v1 : exécution séquentielle — un seul agent à la fois, le Coder et le Tester
d'une tâche se succèdent. Les helpers de worktree sont fournis pour le
parallélisme prévu en v2 (plusieurs Coder/Tester sur des tâches indépendantes).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def create_worktree(project_path: Path, branch: str) -> Path:
    """Crée un worktree git isolé pour une tâche (parallélisme v2)."""
    project = Path(project_path)
    worktree = project.parent / f"{project.name}-{branch}"
    run(["git", "worktree", "add", "-b", branch, str(worktree)], project)
    return worktree


def remove_worktree(project_path: Path, branch: str) -> None:
    project = Path(project_path)
    worktree = project.parent / f"{project.name}-{branch}"
    run(["git", "worktree", "remove", str(worktree)], project)
    run(["git", "branch", "-d", branch], project)


def is_clean(project_path: Path) -> bool:
    result = run(["git", "status", "--porcelain"], Path(project_path))
    return result.stdout.strip() == ""
