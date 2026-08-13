"""Classe centrale : crée les projets, exécute les étapes du pipeline, applique les gates."""
from __future__ import annotations

import json
import re
import sys
import threading
import time
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
    status: str  # succeeded | failed | blocked | completed | noop | questions
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


_ACTIVITY_TOOLS = {
    "websearch": "recherche en ligne",
    "webfetch": "lecture de page web",
    "read": "lecture de fichiers",
    "bash": "commande système",
    "grep": "recherche dans les fichiers",
    "glob": "recherche de fichiers",
    "task": "travail d'un sous-agent",
    "question": "question posée",
    "todowrite": "planification",
    "skill": "chargement d'un savoir-faire",
    "plan": "planification",
}

_ACTIVITY_FILES = {
    "01-idea.md": "rédaction de l'idée",
    "02-interview.md": "rédaction de l'entrevue",
    "03-structure.md": "rédaction de la structure",
    "04-research.md": "rédaction de la recherche",
    "05-prd.md": "rédaction du PRD",
    "06-review.md": "relecture du PRD",
    "07-prd-final.md": "rédaction du PRD final",
    "interview-answers.json": "lecture des réponses",
    "MODULES.md": "rédaction de l'architecture",
    "PRD.md": "rédaction du PRD final",
}


def _activity_label(part: dict) -> Optional[str]:
    """Traduit un événement tool_use du log JSON en libellé court et humain."""
    tool = part.get("tool", "")
    inp = (part.get("state") or {}).get("input") or {}
    if tool in ("write", "edit"):
        path = inp.get("filePath") or inp.get("file") or ""
        name = Path(path).name
        return _ACTIVITY_FILES.get(name) or "rédaction de fichiers"
    return _ACTIVITY_TOOLS.get(tool)


class _ActivityTail:
    """Lit en direct le log JSON d'un run et mémorise la dernière activité connue."""

    def __init__(self, log_path: str):
        self.log_path = Path(log_path)
        self._pos = 0
        self._label = ""

    def current(self) -> str:
        self._scan()
        return self._label

    def _scan(self) -> None:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                f.seek(self._pos)
                new = f.read()
                self._pos = f.tell()
        except OSError:
            return
        for line in new.splitlines():
            label = _line_label(line)
            if label:
                self._label = label


def _line_label(line: str) -> Optional[str]:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        evt = json.loads(line)
    except json.JSONDecodeError:
        return None
    part = evt.get("part") or {}
    if part.get("type") != "tool":
        return None
    return _activity_label(part)


def _spinner(stop_event: threading.Event, label_fn) -> None:
    """Animation simple : indique que l'agent travaille (affiché en arrière-plan).

    label_fn : callable -> str, relu à chaque tick pour refléter l'activité réelle.
    """
    spin = "|/-\\"
    i = 0
    last = ""
    while not stop_event.is_set():
        label = label_fn() or ""
        text = f"\r{spin[i % len(spin)]} {label}"
        sys.stdout.write(text + " " * max(0, len(last) - len(text)))
        sys.stdout.flush()
        last = text
        i += 1
        time.sleep(0.12)
    sys.stdout.write("\r" + " " * len(last) + "\r")
    sys.stdout.flush()


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

    def start(self, idea: str, name: Optional[str] = None, custom_path: Optional[str] = None) -> ProjectState:
        name = name or title_from_idea(idea)
        project_id = self._fresh_id(name)
        
        # Utiliser le chemin personnalisé ou le chemin par défaut
        if custom_path:
            base_dir = Path(custom_path)
            base_dir.mkdir(parents=True, exist_ok=True)
            path = base_dir / project_id
        else:
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

        # Phase QUESTIONS : si un fichier de questions existe sans réponses, l'outil
        # doit les poser à l'utilisateur (statut "questions"). Sinon on relance le PM.
        if agent_key == AgentKey.PM and state.phase == Phase.QUESTIONS:
            pending = self._pending_questions_file(state)
            if pending:
                return StepResult(
                    status="questions",
                    agent=agent_key.value,
                    message=f"Le Product Manager attend tes réponses ({pending}).",
                )

        return self._run(state, agent_key, task)

    def _pending_questions_file(self, state: ProjectState) -> Optional[str]:
        """Renvoie le fichier de questions en attente de réponses, si présent."""
        docs = Path(state.path) / "docs"
        candidates = [
            "docs/clarify-questions.json",
            "docs/interview-questions.json",
            "docs/decisions-questions.json",
        ]
        for rel in candidates:
            q = docs / Path(rel).name
            a = docs / (Path(rel).name.replace("-questions.json", "-answers.json"))
            if q.exists() and not a.exists():
                return rel
        return None

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

    def _pm_mode(self, state: ProjectState) -> str:
        """Détermine le mode du PM selon les fichiers déjà produits.

        - "clarify": pose les questions de clarification (docs/clarify-questions.json)
        - "interview": pose les questions de l'entrevue (docs/interview-questions.json)
        - "research": structure + recherche marché + analyse comparative + boucle de décision (docs/decisions-questions.json)
        - "prd": rédige le PRD final à partir des réponses
        """
        docs = Path(state.path) / "docs"
        if not (docs / "clarify-questions.json").exists():
            return "clarify"
        if not (docs / "interview-questions.json").exists():
            return "interview"
        if not (docs / "decisions-questions.json").exists():
            return "research"
        return "prd"

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

        mode = None
        if agent_key == AgentKey.PM and agent.asks_questions:
            # Le PM écrit ses questions dans docs/*.json ; l'outil les pose à
            # l'utilisateur (voir _collect_answers) puis le PM livre le PRD.
            mode = self._pm_mode(state)
            if mode == "clarify":
                prompt = self.pipeline.render_prompt(agent_key, state, task, mode="clarify")
                expected = ["docs/clarify-questions.json"]
            elif mode == "interview":
                prompt = self.pipeline.render_prompt(agent_key, state, task, mode="idea_interview")
                expected = ["01-idea.md", "docs/interview-questions.json"]
            elif mode == "research":
                prompt = self.pipeline.render_prompt(agent_key, state, task, mode="research")
                expected = ["03-structure.md", "04-research.md", "docs/decisions-questions.json"]
            else:
                prompt = self.pipeline.render_prompt(agent_key, state, task, mode="prd")
                expected = agent.expected_outputs
        else:
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

        interactive = self._interactive(agent_key)
        timeout = self._timeout(agent_key)
        spec = RunSpec(
            agent_key=agent_key.value,
            prompt=prompt,
            cwd=state.path,
            interactive=interactive,
            timeout_seconds=timeout,
            auto_approve=bool(self.cfg.get("auto_approve")),
            expected_outputs=expected,
            capture=not interactive,
        )

        label = f"Agent '{agent.name}' ({agent_key.value}) réfléchit..."
        if not interactive:
            stop = threading.Event()
            tail = _ActivityTail(log_path)
            t = threading.Thread(target=_spinner, args=(stop, lambda: tail.current() or label), daemon=True)
            t.start()
            try:
                result = backend.run(spec, log_path)
            finally:
                stop.set()
                t.join()
        else:
            sys.stdout.write(f"\r{agent.name} : mode interactif — vous pouvez répondre à ses questions.\n")
            sys.stdout.flush()
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

        msg = self._message(agent_key, task, success, missing, mode)
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
        agent = self.agents[agent_key.value]
        if self._quiet():
            # En quiet : les agents tournent en arrière-plan (spinner + résultat).
            # Les questions du PM sont posées par l'outil (contrat de fichiers),
            # pas en mode interactif.
            return False
        return agent.interactive

    def _quiet(self) -> bool:
        val = self.cfg.get("quiet")
        return val is None or bool(val)

    def _timeout(self, agent_key: AgentKey) -> Optional[int]:
        """Timeout en secondes pour un agent (config timeouts.<agent>, défaut 1800s).

        Empêche un run bloqué (appel LLM qui ne répond plus) de rester 'running'
        indéfiniment : après le délai, le backend force l'échec et le pipeline
        peut être relancé proprement via advance/resume.
        """
        val = self.cfg.get(f"timeouts.{agent_key.value}")
        if val is None:
            val = self.cfg.get("timeout_seconds")
        if val is None:
            return 1800
        try:
            return max(1, int(val))
        except (TypeError, ValueError):
            return 1800

    def _message(self, agent_key: AgentKey, task, success: bool, missing: list[str], pm_mode: Optional[str] = None) -> str:
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
            if pm_mode:
                base += f" (mode {pm_mode})"
            if task:
                base += f" — {task.id} {task.title}"
            return base
        return f"{labels[agent_key]} : échec" + (f" — {missing}" if missing else "")
