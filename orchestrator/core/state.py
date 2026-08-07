"""Modèles d'état du pipeline (source de vérité technique de l'orchestrateur)."""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(timezone.utc)


class AgentKey(str, enum.Enum):
    PM = "pm"
    ARCHITECT = "architect"
    LEAD_MANAGER = "lead_manager"
    CODER = "coder"
    TESTER = "tester"
    DEVOPS = "devops"


class Phase(str, enum.Enum):
    IDEA = "idea"
    PRD_DONE = "prd_done"
    ARCHITECTURE_DONE = "architecture_done"
    PLANNING_DONE = "planning_done"
    DEVELOPMENT = "development"
    DEPLOYMENT = "deployment"
    COMPLETED = "completed"


class TaskStatus(str, enum.Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_TEST = "in_test"
    DONE = "done"
    BLOCKED = "blocked"


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Task(BaseModel):
    id: str
    title: str
    module: str = ""
    target: Optional[str] = None
    dependencies: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.TODO
    attempts: int = 0
    report: Optional[str] = None
    updated_at: datetime = Field(default_factory=now)


class AgentRun(BaseModel):
    agent: AgentKey
    phase: str
    status: RunStatus = RunStatus.PENDING
    backend: str = "opencode"
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    exit_code: Optional[int] = None
    output_path: Optional[str] = None
    error: Optional[str] = None


class ProjectState(BaseModel):
    id: str
    name: str
    path: str
    idea: str
    phase: Phase = Phase.IDEA
    prd_path: Optional[str] = None
    modules_path: Optional[str] = None
    tasks: list[Task] = Field(default_factory=list)
    runs: list[AgentRun] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
