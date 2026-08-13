"""Classe centrale : crée les projets, exécute les étapes du pipeline, applique les gates."""
from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..backends.base import RunResult, RunSpec
from ..backends.registry import BackendRegistry
from .config import Config
from .pipeline import DEVOPS_RESERVED_PATHS, Pipeline
from .state import AgentKey, AgentRun, Phase, ProjectState, RunStatus, TaskStatus, now
from .state_store import StateStore


@dataclass
class StepResult:
    status: str  # succeeded | failed | blocked | completed | noop | questions
    agent: Optional[str] = None
    task_id: Optional[str] = None
    message: str = ""
    summary: str = ""  # résumé rédigé par l'agent (dernière réponse)
    files: list = None  # fichiers produits par le run (livrables documentaires)


@dataclass
class RunPlan:
    """Ce qu'il faut pour lancer un agent : backend, prompt, livrables, mode."""
    agent_key: AgentKey
    agent: object
    backend: object
    backend_name: str
    prompt: str
    mode: Optional[str]
    expected: list
    interactive: bool
    timeout: Optional[int]


@dataclass
class PreparedRun:
    """Run planifié : spec, log, run d'état et gate Coder prêt à exécuter."""
    plan: RunPlan
    task: object
    run: AgentRun
    log_path: str
    spec: RunSpec
    reserved_before: Optional[list] = None


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


def _summary_from_log(log_path: str) -> str:
    """Extrait la dernière réponse texte de l'agent depuis le log JSON d'un run.

    Le log OpenCode (format JSON) contient une séquence d'événements ; la
    dernière part de type "text" de l'assistant est son message final, que
    l'on affiche comme résumé. Retourne "" si rien d'exploitable (backend dry).
    """
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    last = ""
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = evt.get("part") or {}
        if part.get("type") != "text":
            continue
        text = (part.get("text") or part.get("content") or "").strip()
        if text:
            last = text
    return last


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
        """Exécute une seule étape du pipeline (un seul agent)."""
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

        items = self.pipeline.next_batch(state, 1)
        if not items:
            if state.phase == Phase.COMPLETED:
                return StepResult(status="completed", message="Pipeline terminé, projet livré.")
            return StepResult(status="noop", message=f"Phase actuelle : {state.phase.value}")

        agent_key, task = items[0]

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

        # Phase ARCH_QUESTIONS : idem pour l'architecte (questions de configuration).
        if agent_key == AgentKey.ARCHITECT and state.phase == Phase.ARCH_QUESTIONS:
            pending = self._pending_questions_file(state)
            if pending:
                return StepResult(
                    status="questions",
                    agent=agent_key.value,
                    message=f"L'architecte attend tes réponses ({pending}).",
                )

        return self._run(state, agent_key, task)

    def plan_batch(self, project_id: str, max_parallel: int = 1):
        """Calcule la prochaine liste d'actions (agent, tâche) à lancer.

        Retourne une liste de couples (agent_key, task) si des travaux sont à
        exécuter, ou un StepResult (noop / completed / blocked / questions) si
        le pipeline est bloqué ou attend quelque chose.
        """
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

        items = self.pipeline.next_batch(state, max_parallel)
        if not items:
            if state.phase == Phase.COMPLETED:
                return StepResult(status="completed", message="Pipeline terminé, projet livré.")
            return StepResult(status="noop", message=f"Phase actuelle : {state.phase.value}")

        # Phase QUESTIONS : si un fichier de questions existe sans réponses, l'outil
        # doit les poser à l'utilisateur (statut "questions"). Sinon on relance le PM.
        if len(items) == 1 and items[0][0] == AgentKey.PM and state.phase == Phase.QUESTIONS:
            pending = self._pending_questions_file(state)
            if pending:
                return StepResult(
                    status="questions",
                    agent=AgentKey.PM.value,
                    message=f"Le Product Manager attend tes réponses ({pending}).",
                )

        # Phase ARCH_QUESTIONS : idem pour l'architecte (questions de configuration).
        if len(items) == 1 and items[0][0] == AgentKey.ARCHITECT and state.phase == Phase.ARCH_QUESTIONS:
            pending = self._pending_questions_file(state)
            if pending:
                return StepResult(
                    status="questions",
                    agent=AgentKey.ARCHITECT.value,
                    message=f"L'architecte attend tes réponses ({pending}).",
                )

        return items

    def run_batch(self, project_id: str, items) -> list[StepResult]:
        """Exécute une liste d'actions (agent, tâche) en parallèle puis applique
        les résultats. Les agents écrivent dans des fichiers disjoints (contrat
        TASKS.json) et le commit est géré par l'utilisateur, pas par les agents."""
        state = self.store.get(project_id)
        if state is None:
            return [StepResult(status="noop", message=f"Projet inconnu : {project_id}")]
        self._recover_stale_runs(state)

        parallel = len(items) > 1
        prepared: list[tuple[Optional[PreparedRun], Optional[StepResult]]] = []
        for agent_key, task in items:
            if task is not None:
                # Les tâches viennent de l'état chargé par plan_batch : on les
                # re-résout dans l'état frais pour muter les bons objets.
                fresh = next((t for t in state.tasks if t.id == task.id), None)
                if fresh is None:
                    prepared.append((None, StepResult(status="failed", agent=agent_key.value,
                                                      message=f"Tâche introuvable : {task.id}")))
                    continue
                task = fresh
            plan = self._plan(state, agent_key, task)
            if isinstance(plan, StepResult):
                prepared.append((None, plan))
                continue
            if parallel:
                # En parallèle les agents tournent en arrière-plan (pas de
                # question posée en direct ni de spinner concurrent).
                plan.interactive = False
                # En batch, les fichiers de zone DevOps sont légitimement
                # créés par d'autres agents du même lot : le gate Coder
                # (instantané avant / après) donnerait de faux positifs.
                # La portée est déjà garantie par validate_tasks + prompts.
            prep = self._prepare(state, plan, task)
            if parallel and prep.reserved_before is not None:
                prep.reserved_before = None
            prepared.append((prep, None))

        runnables = [p for p, _ in prepared if p is not None]
        if parallel and runnables:
            sys.stdout.write(f"\r→ {len(runnables)} agent(s) lancé(s) en parallèle...\n")
            sys.stdout.flush()

        results: dict[int, RunResult] = {}
        if runnables:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=len(runnables)) as pool:
                futures = {pool.submit(self._execute, p, spinner=False): p for p in runnables}
                for fut in futures:
                    p = futures[fut]
                    results[id(p)] = fut.result()

        steps: list[StepResult] = []
        for p, err in prepared:
            if err is not None:
                steps.append(err)
            else:
                steps.append(self._finalize(state, p, results[id(p)]))
        return steps

    def max_parallel(self) -> int:
        val = self.cfg.get("max_parallel")
        try:
            return max(1, int(val))
        except (TypeError, ValueError):
            return 1

    def _pending_questions_file(self, state: ProjectState) -> Optional[str]:
        """Renvoie le fichier de questions en attente de réponses, si présent."""
        docs = Path(state.path) / "docs"
        candidates = [
            "docs/clarify-questions.json",
            "docs/interview-questions.json",
            "docs/decisions-questions.json",
            "docs/architect-questions.json",
        ]
        for rel in candidates:
            q = docs / Path(rel).name
            a = docs / (Path(rel).name.replace("-questions.json", "-answers.json"))
            if q.exists() and not a.exists():
                return rel
        return None

    def _reserved_snapshot(self, state: ProjectState) -> list[str]:
        """Chemins réservés à DevOps existants actuellement (gate Coder)."""
        base = Path(state.path)
        return [p for p in DEVOPS_RESERVED_PATHS if (base / p).exists()]

    def _new_devops_zone_files(self, state: ProjectState, before: list[str]) -> list[str]:
        """Chemins réservés à DevOps apparus depuis l'instantané (créés par le Coder)."""
        base = Path(state.path)
        return [p for p in DEVOPS_RESERVED_PATHS if (base / p).exists() and p not in before]

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

    def _architect_mode(self, state: ProjectState) -> str:
        """Détermine le mode de l'architecte selon les fichiers déjà produits.

        - "config": pose les questions de configuration du projet
          (docs/architect-questions.json) — première passe, l'architecte
          extrait du PRD ce qui est déjà décidé et ne demande que le reste.
        - "init": lit les réponses (docs/architect-answers.json), découpe en
          modules, conçoit puis scaffold le projet.
        """
        docs = Path(state.path) / "docs"
        if not (docs / "architect-questions.json").exists():
            return "config"
        return "init"

    # ---- réinitialisation ----

    _RESET_TARGETS = ("idea", "prd_done", "arch_questions", "architecture_done",
                      "planning_done", "development")

    def reset(self, project_id: str, to: str) -> StepResult:
        """Réinitialise un projet à une phase précise : les livrables produits
        APRÈS cette phase sont supprimés (fichiers et dossiers), l'état technique
        (phase, prd_path, modules_path, tasks) est ramené à ce qu'il était à
        cette phase. Permet de re-tester une étape plusieurs fois sur le même
        projet (ex: revenir à prd_done pour rejouer l'architecte).

        Phases acceptées : idea, prd_done, arch_questions, architecture_done,
        planning_done, development.
        """
        state = self.store.get(project_id)
        if state is None:
            return StepResult(status="noop", message=f"Projet inconnu : {project_id}")
        if to not in self._RESET_TARGETS:
            return StepResult(
                status="failed",
                message=f"Phase cible invalide : {to} (choix : {', '.join(self._RESET_TARGETS)})",
            )

        base = Path(state.path)
        removed: list[str] = []

        def _rm(rel: str) -> None:
            p = base / rel
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                removed.append(rel + "/")
            elif p.exists():
                p.unlink()
                removed.append(rel)

        # Livrables du PM : tous les fichiers de la phase PRD, réponses incluses.
        pm_docs = [
            "01-idea.md", "02-interview.md", "03-structure.md", "04-research.md",
            "05-prd.md", "06-review.md", "07-prd-final.md",
            "docs/PRD.md",
            "docs/clarify-questions.json", "docs/clarify-answers.json",
            "docs/interview-questions.json", "docs/interview-answers.json",
            "docs/decisions-questions.json", "docs/decisions-answers.json",
        ]
        # Livrables de l'architecte : questions/réponses + architecture + scaffolding.
        arch_docs = [
            "docs/architect-questions.json", "docs/architect-answers.json",
            "docs/MODULES.md", "docs/TECH_STACK.md", "docs/ARCHITECTURE.md",
            "docs/DECISIONS.md", "docs/QUESTIONS.md",
        ]
        arch_dirs = ["backend", "frontend", "diagrams", "research", "references"]
        # Livrables du Lead Manager.
        lead_docs = ["docs/TASKS.md", "docs/TASKS.json", "docs/BACKLOG.md"]
        # Rapports coder/tester + livrables DevOps.
        reports_dir = "docs/reports"
        devops_docs = ["Dockerfile", "docs/DEPLOYMENT.md", "docs/CHANGELOG.md"]

        if to == "idea":
            for rel in pm_docs + arch_docs + lead_docs + devops_docs:
                _rm(rel)
            for d in arch_dirs:
                _rm(d)
            _rm(reports_dir)
            _rm(".git")
            _rm("README.md")
            state.phase = Phase.IDEA
            state.prd_path = None
            state.modules_path = None
            state.tasks = []
        elif to == "prd_done":
            for rel in arch_docs + lead_docs + devops_docs:
                _rm(rel)
            for d in arch_dirs:
                _rm(d)
            _rm(reports_dir)
            _rm(".git")
            _rm("README.md")
            state.phase = Phase.PRD_DONE
            state.prd_path = "07-prd-final.md"
            state.modules_path = None
            state.tasks = []
        elif to == "arch_questions":
            # On garde les questions posées, on supprime les réponses et tout le reste.
            for rel in arch_docs + lead_docs + devops_docs:
                if rel != "docs/architect-questions.json":
                    _rm(rel)
            for d in arch_dirs:
                _rm(d)
            _rm(reports_dir)
            _rm(".git")
            _rm("README.md")
            state.phase = Phase.ARCH_QUESTIONS
            state.modules_path = None
            state.tasks = []
        elif to == "architecture_done":
            for rel in lead_docs + devops_docs:
                _rm(rel)
            _rm(reports_dir)
            state.phase = Phase.ARCHITECTURE_DONE
            state.modules_path = "docs/MODULES.md"
            state.tasks = []
        elif to == "planning_done":
            for rel in devops_docs:
                _rm(rel)
            _rm(reports_dir)
            state.phase = Phase.PLANNING_DONE
            state.tasks = []
        elif to == "development":
            for rel in devops_docs:
                _rm(rel)
            _rm(reports_dir)
            state.phase = Phase.DEVELOPMENT
            for t in state.tasks:
                t.status = TaskStatus.TODO
                t.attempts = 0
                t.report = None

        state.updated_at = now()
        state.deployment_attempts = 0  # un reset revient toujours en arrière
        self.store.save(state)
        msg = f"Projet réinitialisé à la phase '{to}'. Supprimé : {', '.join(removed) or 'aucun fichier'}."
        return StepResult(status="succeeded", message=msg)

    def _run(self, state: ProjectState, agent_key: AgentKey, task) -> StepResult:
        plan = self._plan(state, agent_key, task)
        if isinstance(plan, StepResult):
            return plan
        prep = self._prepare(state, plan, task)
        result = self._execute(prep, spinner=True)
        return self._finalize(state, prep, result)

    def _plan(self, state: ProjectState, agent_key: AgentKey, task):
        """Construit le plan d'un run : agent, backend, prompt, mode, livrables
        attendus. Retourne un RunPlan, ou un StepResult en cas d'erreur."""
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
        elif agent_key == AgentKey.ARCHITECT and agent.asks_questions:
            # L'architecte écrit ses questions de configuration dans
            # docs/architect-questions.json ; l'outil les pose à l'utilisateur
            # puis l'architecte relit les réponses et livre l'architecture
            # (mode init).
            mode = self._architect_mode(state)
            if mode == "config":
                prompt = self.pipeline.render_prompt(agent_key, state, task, mode="config")
                expected = ["docs/architect-questions.json"]
            else:
                prompt = self.pipeline.render_prompt(agent_key, state, task, mode="init")
                expected = agent.expected_outputs
        elif agent_key == AgentKey.TESTER:
            # Le Tester est le gate : mode "devops" pour les tâches DevOps,
            # mode "deployment" pour la revue du déploiement terminal.
            if task and task.assignee == "devops":
                mode = "devops"
            elif task is None:
                mode = "deployment"
            prompt = self.pipeline.render_prompt(agent_key, state, task, mode=mode)
            if task:
                expected = [f"docs/reports/{task.id}-tests.md"]
            else:
                expected = ["docs/reports/deployment-tests.md"]
        else:
            prompt = self.pipeline.render_prompt(agent_key, state, task)
            if task and agent_key == AgentKey.DEVOPS:
                # Tâche DevOps pilotée : le livrable est la cible de la tâche
                # (+ son rapport, pour que le Tester puisse le relire).
                expected = [f"docs/reports/{task.id}.md"]
                if task.target:
                    expected.insert(0, task.target)
            elif task and agent_key == AgentKey.CODER:
                expected = [f"docs/reports/{task.id}.md"]
                if task.target:
                    expected.append(task.target)
            else:
                expected = [p.format(task_id=task.id if task else "") for p in agent.expected_outputs]

        interactive = self._interactive(agent_key)
        timeout = self._timeout(agent_key)
        return RunPlan(
            agent_key=agent_key,
            agent=agent,
            backend=backend,
            backend_name=backend_name,
            prompt=prompt,
            mode=mode,
            expected=expected,
            interactive=interactive,
            timeout=timeout,
        )

    def _prepare(self, state: ProjectState, plan: "RunPlan", task) -> "PreparedRun":
        run = AgentRun(
            agent=plan.agent_key,
            phase=state.phase.value,
            status=RunStatus.RUNNING,
            backend=plan.backend_name,
            started_at=now(),
        )
        state.runs.append(run)
        self.store.save(state)

        log_dir = Path(state.path) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(log_dir / f"{plan.agent_key.value}-{len(state.runs):03d}.log")

        spec = RunSpec(
            agent_key=plan.agent_key.value,
            prompt=plan.prompt,
            cwd=state.path,
            interactive=plan.interactive,
            timeout_seconds=plan.timeout,
            auto_approve=bool(self.cfg.get("auto_approve")),
            expected_outputs=plan.expected,
            capture=not plan.interactive,
        )

        # Gate Coder : instantané des chemins réservés à DevOps avant le run ;
        # si le Coder en crée un pendant son travail, le run échouera.
        reserved_before = self._reserved_snapshot(state) if (plan.agent_key == AgentKey.CODER and task) else None

        return PreparedRun(
            plan=plan, task=task, run=run, log_path=log_path, spec=spec, reserved_before=reserved_before,
        )

    def _execute(self, prep: "PreparedRun", spinner: bool = True) -> RunResult:
        plan = prep.plan
        if plan.interactive:
            sys.stdout.write(f"\r{plan.agent.name} : mode interactif — vous pouvez répondre à ses questions.\n")
            sys.stdout.flush()
            return plan.backend.run(prep.spec, prep.log_path)
        if spinner:
            label = f"Agent '{plan.agent.name}' ({plan.agent_key.value}) réfléchit..."
            stop = threading.Event()
            tail = _ActivityTail(prep.log_path)
            t = threading.Thread(target=_spinner, args=(stop, lambda: tail.current() or label), daemon=True)
            t.start()
            try:
                return plan.backend.run(prep.spec, prep.log_path)
            finally:
                stop.set()
                t.join()
        return plan.backend.run(prep.spec, prep.log_path)

    def _finalize(self, state: ProjectState, prep: "PreparedRun", result: RunResult) -> StepResult:
        plan = prep.plan
        task = prep.task
        run = prep.run
        agent_key = plan.agent_key

        run.finished_at = now()
        run.exit_code = result.exit_code
        run.output_path = prep.log_path
        missing = [p for p in plan.expected if not (Path(state.path) / p).exists()]
        success = result.success and not missing
        zone_touched = []
        if success and prep.reserved_before is not None:
            zone_touched = self._new_devops_zone_files(state, prep.reserved_before)
            if zone_touched:
                success = False
        run.status = RunStatus.SUCCEEDED if success else RunStatus.FAILED
        run.error = (
            result.error
            or (f"livrables manquants : {missing}" if missing else None)
            or (f"fichiers de zone DevOps créés par le Coder : {', '.join(zone_touched)}" if zone_touched else None)
        )

        pipeline_error = self.pipeline.apply_result(state, agent_key, task, success)
        if success and pipeline_error:
            # Ex : plan de travail invalide produit par le Lead Manager — on
            # force l'échec pour que l'étape soit visible et relancée.
            success = False
            run.error = pipeline_error
            run.status = RunStatus.FAILED
        self.store.save(state)

        msg = self._message(agent_key, task, success, missing, plan.mode)
        if not success and run.error:
            msg = f"{msg} — {run.error}"
        produced = [p for p in plan.expected if (Path(state.path) / p).exists()] if success else []
        return StepResult(
            status="succeeded" if success else "failed",
            agent=agent_key.value,
            task_id=task.id if task else None,
            message=msg,
            summary=_summary_from_log(prep.log_path) if success else "",
            files=produced,
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

    def _message(self, agent_key: AgentKey, task, success: bool, missing: list[str], mode: Optional[str] = None) -> str:
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
            if mode:
                base += f" (mode {mode})"
            if task:
                base += f" — {task.id} {task.title}"
            return base
        return f"{labels[agent_key]} : échec" + (f" — {missing}" if missing else "")
