"""Machine d'état du pipeline : détermine l'agent suivant et applique les résultats.

Séquence : PM → Architecte → Lead Manager → (Coder ↔ Tester)* → DevOps
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Tuple

from jinja2 import Environment, FileSystemLoader

from .state import AgentKey, Phase, ProjectState, Task, TaskStatus, now


class Pipeline:
    def __init__(self, agents: dict, prompts_dir: Path):
        self.agents = agents
        self.env = Environment(loader=FileSystemLoader(str(prompts_dir)))

    def render_prompt(self, agent_key: AgentKey, state: ProjectState, task: Optional[Task],
                      mode: Optional[str] = None) -> str:
        tpl = self.env.get_template(f"{agent_key.value}.j2")
        return tpl.render(
            project_name=state.name,
            project_path=state.path,
            idea=state.idea,
            prd_path=state.prd_path or "docs/PRD.md",
            task=task,
            mode=mode,
        )

    def next_step(self, state: ProjectState) -> Tuple[Optional[AgentKey], Optional[Task]]:
        phase = state.phase
        if phase == Phase.IDEA:
            return AgentKey.PM, None
        if phase == Phase.QUESTIONS:
            # Le PM poursuit : soit il pose de nouvelles questions (entrevue),
            # soit il livre le PRD. Le mode exact est déterminé par les fichiers
            # présents dans _run (voir _pm_mode). L'outil intercepte la phase
            # QUESTIONS pour poser les questions à l'utilisateur quand nécessaire.
            return AgentKey.PM, None
        if phase == Phase.PRD_DONE:
            return AgentKey.ARCHITECT, None
        if phase == Phase.ARCHITECTURE_DONE:
            return AgentKey.LEAD_MANAGER, None
        if phase in (Phase.PLANNING_DONE, Phase.DEVELOPMENT):
            in_flight = [t for t in state.tasks if t.status in (TaskStatus.IN_PROGRESS, TaskStatus.IN_TEST)]
            if in_flight:
                t = in_flight[0]
                return (AgentKey.TESTER if t.status == TaskStatus.IN_TEST else AgentKey.CODER), t
            task = self._next_task(state)
            if task:
                return AgentKey.CODER, task
            if state.tasks and all(t.status == TaskStatus.DONE for t in state.tasks):
                return AgentKey.DEVOPS, None
            return None, None
        if phase == Phase.DEPLOYMENT:
            return AgentKey.DEVOPS, None
        return None, None

    def _next_task(self, state: ProjectState) -> Optional[Task]:
        done = {t.id for t in state.tasks if t.status == TaskStatus.DONE}
        for t in state.tasks:
            if t.status == TaskStatus.TODO and all(d in done for d in t.dependencies):
                return t
        return None

    def apply_result(self, state: ProjectState, agent_key: AgentKey, task: Optional[Task], success: bool) -> None:
        if agent_key == AgentKey.PM:
            if not success:
                state.updated_at = now()
                return
            # Entre vue terminée (02-interview.md) mais PRD pas encore livré :
            # on passe en QUESTIONS pour que l'utilisateur confirme, puis le PM
            # relancera en livraison. Si le PRD existe déjà : PRD_DONE.
            if (Path(state.path) / "07-prd-final.md").exists():
                state.phase = Phase.PRD_DONE
                state.prd_path = "07-prd-final.md"
            else:
                state.phase = Phase.QUESTIONS
        elif agent_key == AgentKey.ARCHITECT:
            if success:
                state.phase = Phase.ARCHITECTURE_DONE
                state.modules_path = "docs/MODULES.md"
        elif agent_key == AgentKey.LEAD_MANAGER:
            if success:
                state.phase = Phase.PLANNING_DONE
                self._load_tasks(state)
        elif agent_key == AgentKey.CODER and task:
            task.attempts += 1
            if success:
                task.status = TaskStatus.IN_TEST
                task.report = f"docs/reports/{task.id}.md"
            else:
                task.status = TaskStatus.BLOCKED if task.attempts >= 3 else TaskStatus.IN_PROGRESS
            if state.phase != Phase.DEVELOPMENT:
                state.phase = Phase.DEVELOPMENT
        elif agent_key == AgentKey.TESTER and task:
            verdict = self._verdict(state, task)
            if verdict == "PASS":
                task.status = TaskStatus.DONE
            elif verdict == "FAIL":
                task.status = TaskStatus.IN_PROGRESS
            else:
                task.status = TaskStatus.BLOCKED
            if state.tasks and all(t.status == TaskStatus.DONE for t in state.tasks):
                state.phase = Phase.DEPLOYMENT
        elif agent_key == AgentKey.DEVOPS and success:
            state.phase = Phase.COMPLETED
        state.updated_at = now()

    def _load_tasks(self, state: ProjectState) -> None:
        path = Path(state.path) / "docs" / "TASKS.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            return
        state.tasks = [
            Task(
                id=item["id"],
                title=item.get("title", ""),
                module=item.get("module", ""),
                target=item.get("target"),
                dependencies=item.get("dependencies", []),
            )
            for item in data.get("tasks", [])
        ]

    def _verdict(self, state: ProjectState, task: Task) -> Optional[str]:
        report = Path(state.path) / "docs" / "reports" / f"{task.id}-tests.md"
        if not report.exists():
            return None
        for line in report.read_text().splitlines():
            if line.startswith("STATUT:"):
                return line.split(":", 1)[1].strip().upper()
        return None
