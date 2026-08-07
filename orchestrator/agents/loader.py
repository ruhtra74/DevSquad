"""Chargement des définitions d'agents depuis les YAML (agents/)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class AgentDefinition:
    key: str
    name: str
    description: str
    backend: str = "opencode"
    interactive: bool = True
    inputs: list[str] = field(default_factory=list)
    expected_outputs: list[str] = field(default_factory=list)


def load_agents(*dirs: Path) -> dict[str, AgentDefinition]:
    agents: dict[str, AgentDefinition] = {}
    for directory in dirs:
        if not directory or not Path(directory).exists():
            continue
        for path in sorted(Path(directory).glob("*.yaml")):
            data = yaml.safe_load(path.read_text())
            agent = AgentDefinition(**data)
            agents[agent.key] = agent
    return agents
