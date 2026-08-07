"""Classe centrale : crée les projets, exécute les étapes du pipeline, applique les gates."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..backends.base import RunSpec
from ..backends.registry import BackendRegistry
from .config import Config
from .pipeline import Pipeline
from .state import AgentKey, AgentRun, Phase, ProjectState, RunStatus, TaskStatus, now
from .state_store import StateStore


@dataclass
class StepResult:
    status: str  # succeeded | failed | blocked | completed | noop
    agent: Optional[str] = None
    task_id: Optional[str] = None
    message: str = ""


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "projet"


def title_from_idea(idea: str) -> str:
    words = idea.strip().split()
    title = " ".join(words[:6])
    return title[:60].strip().rstrip(",;:") or "Projet"


class Orchestrator:
    def __init__(
        self,
        root: Path,
        cfg: Config,
        agents: dict,
        backends: BackendRegistry,
        prompts_dir: Path,
    ):
        self.root = Path(root)
        self.cfg = cfg
        self.agents = agents
        self.backends = backends
        self.projects_dir = self.root / "projects"
        self.store = StateStore(self.root)
        self.pipeline = Pipeline(agents, prompts_dir)
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    # ---- projets ----

    def start(self, idea: str, name: Optional[str] = None) -> ProjectState:
        name = name or title_from_idea(idea)
        project_id = self._fresh_id(name)
        path = self.projects_dir / project_id
        path.mkdir(parents=True, exist_ok=True)
        state = ProjectState(id=project_id, name=name, path=str(path), idea=idea)
        self.store.save(state)
        return state

    def _fresh_id(self, name: str) -> str:
        base = slugify(name)
        candidate = base
        n = 1
        while (self.projects_dir / candidate).exists():
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    # ---- exécution du pipeline ----

    def advance(self, project_id: str) -> StepResult:
        state = self.store.get(project_id)
        if state is None:
            return StepResult(status="noop", message=f"Projet inconnu : {project_id}")

        self._recover_stale_runs(state)

        blocked = [t for t in state.tasks if t.status == TaskStatus.BLOCKED]
        if blocked:
            t = blocked[0]
            return StepResult(
                status="blocked",
                task_id=t.id,
                message=f"Tâche bloquée après 3 essais : {t.title}",
            )

        agent_key, task = self.pipeline.next_step(state)
        if agent_key is None:
            if state.phase == Phase.COMPLETED:
                return StepResult(status="completed", message="Pipeline terminé, projet livré.")
            return StepResult(status="noop", message=f"Phase actuelle : {state.phase.value}")

        return self._run(state, agent_key, task)

    def _recover_stale_runs(self, state: ProjectState) -> None:
        """Réinitialise les runs 'running' orphelins (processus tué en cours de route)."""
        changed = False
        for run in state.runs:
            if run.status == RunStatus.RUNNING:
                run.status = RunStatus.FAILED
                run.error = "Interrompu (processus tué avant la fin)"
                run.finished_at = now()
                changed = True
        if changed:
            self.store.save(state)

    def _run(self, state: ProjectState, agent_key: AgentKey, task) -> StepResult:
        agent = self.agents[agent_key.value]
        backend_name = self.cfg.get(f"backends.{agent_key.value}") or agent.backend
        try:
            backend = self.backends.get(backend_name)
        except KeyError:
            return StepResult(
                status="failed", agent=agent_key.value,
                message=f"Backend inconnu : {backend_name} (config : backends.{agent_key.value})",
            )

        prompt = self.pipeline.render_prompt(agent_key, state, task)
        expected = [p.format(task_id=task.id if task else "") for p in agent.expected_outputs]

        run = AgentRun(
            agent=agent_key,
            phase=state.phase.value,
            status=RunStatus.RUNNING,
            backend=backend_name,
            started_at=now(),
        )
        state.runs.append(run)
        self.store.save(state)

        log_dir = Path(state.path) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(log_dir / f"{agent_key.value}-{len(state.runs):03d}.log")

        spec = RunSpec(
            agent_key=agent_key.value,
            prompt=prompt,
            cwd=state.path,
            interactive=self._interactive(agent_key),
            auto_approve=bool(self.cfg.get("auto_approve")),
            expected_outputs=expected,
        )
        result = backend.run(spec, log_path)

        run.finished_at = now()
        run.exit_code = result.exit_code
        run.output_path = log_path
        missing = [p for p in expected if not (Path(state.path) / p).exists()]
        success = result.success and not missing
        run.status = RunStatus.SUCCEEDED if success else RunStatus.FAILED
        run.error = result.error or (f"livrables manquants : {missing}" if missing else None)

        self.pipeline.apply_result(state, agent_key, task, success)
        self.store.save(state)

        msg = self._message(agent_key, task, success, missing)
        return StepResult(
            status="succeeded" if success else "failed",
            agent=agent_key.value,
            task_id=task.id if task else None,
            message=msg,
        )

    def _interactive(self, agent_key: AgentKey) -> bool:
        override = self.cfg.get(f"interactive.{agent_key.value}")
        if override is not None:
            return bool(override)
        return self.agents[agent_key.value].interactive

    def _message(self, agent_key: AgentKey, task, success: bool, missing: list[str]) -> str:
        labels = {
            AgentKey.PM: "PRD",
            AgentKey.ARCHITECT: "Modules + architecture + scaffolding",
            AgentKey.LEAD_MANAGER: "Plan de travail",
            AgentKey.CODER: "Implémentation",
            AgentKey.TESTER: "Validation",
            AgentKey.DEVOPS: "Déploiement",
        }
        if success:
            base = f"{labels[agent_key]} : OK"
            if task:
                base += f" — {task.id} {task.title}"
            return base
        return f"{labels[agent_key]} : échec" + (f" — {missing}" if missing else "")
