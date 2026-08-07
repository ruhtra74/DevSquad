"""Interface commune des backends CLI (OpenCode, Claude Code, Aider...)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RunSpec:
    agent_key: str
    prompt: str
    cwd: str
    interactive: bool = True
    auto_approve: bool = False
    timeout_seconds: Optional[int] = 14400
    expected_outputs: list[str] = field(default_factory=list)  # contrat documentaire


@dataclass
class RunResult:
    exit_code: int
    success: bool
    error: Optional[str] = None
    log_path: Optional[str] = None


class Backend(ABC):
    name: str = ""

    @abstractmethod
    def run(self, spec: RunSpec, log_path: str) -> RunResult:
        ...
