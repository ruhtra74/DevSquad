"""Machine d'état du pipeline : détermine l'agent suivant et applique les résultats.

Séquence : PM → Architecte → Lead Manager → (Coder ↔ Tester)* → DevOps
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Tuple

from jinja2 import Environment, FileSystemLoader

from .state import AgentKey, Phase, ProjectState, Task, TaskStatus, now


# Chemins réservés à l'agent DevOps : le Coder ne doit jamais les créer/modifier.
DEVOPS_RESERVED_PATHS = [
    "Dockerfile", ".dockerignore",
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    ".github", ".gitlab-ci.yml", "Jenkinsfile",
    "docs/DEPLOYMENT.md", "docs/CHANGELOG.md",
    "k8s", "helm", "deploy", "infra",
]


def in_devops_zone(rel: str) -> bool:
    """Vrai si le chemin relatif tombe dans une zone réservée à DevOps."""
    rel = (rel or "").strip().rstrip("/")
    if not rel:
        return False
    for p in DEVOPS_RESERVED_PATHS:
        pn = p.rstrip("/")
        if rel == pn or rel.startswith(pn + "/"):
            return True
    return False


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
        items = self.next_batch(state, max_parallel=1)
        if not items:
            return None, None
        return items[0]

    def next_batch(self, state: ProjectState, max_parallel: int = 1) -> list[Tuple[AgentKey, Optional[Task]]]:
        """Retourne la liste des (agent, tâche) à lancer maintenant.

        - Phases séquentielles (PM, architecte, lead, déploiement) : 1 élément.
        - Phase développement : une **vague** d'implémenteurs (Coder/DevOps)
          jusqu'à `max_parallel`, OU la vague des Testers des tâches livrées
          (jamais les deux : on ne teste pas pendant qu'un Coder écrit).
        - Toutes tâches done : run de déploiement terminal (DevOps).
        """
        phase = state.phase
        if phase == Phase.IDEA:
            return [(AgentKey.PM, None)]
        if phase == Phase.QUESTIONS:
            # Le PM poursuit : soit il pose de nouvelles questions (entrevue),
            # soit il livre le PRD. L'outil intercepte la phase QUESTIONS pour
            # poser les questions à l'utilisateur quand nécessaire.
            return [(AgentKey.PM, None)]
        if phase == Phase.PRD_DONE:
            return [(AgentKey.ARCHITECT, None)]
        if phase == Phase.ARCH_QUESTIONS:
            return [(AgentKey.ARCHITECT, None)]
        if phase == Phase.ARCHITECTURE_DONE:
            return [(AgentKey.LEAD_MANAGER, None)]
        if phase == Phase.DEPLOYMENT:
            return [(AgentKey.DEVOPS, None)]
        if phase == Phase.DEPLOYMENT_REVIEW:
            # Le Tester relit le déploiement terminal de DevOps avant livraison.
            return [(AgentKey.TESTER, None)]
        if phase in (Phase.PLANNING_DONE, Phase.DEVELOPMENT):
            in_test = [t for t in state.tasks if t.status == TaskStatus.IN_TEST]
            if in_test:
                # Vague de validation : les implémenteurs ont fini, on teste.
                return [(AgentKey.TESTER, t) for t in in_test[:max_parallel]]
            # Vague d'implémenteurs : d'abord les reprises (IN_PROGRESS), puis
            # les nouvelles tâches dont les dépendances sont satisfaites.
            implementers = [t for t in state.tasks if t.status == TaskStatus.IN_PROGRESS]
            implementers += [t for t in state.tasks if t.status == TaskStatus.TODO and self._deps_done(state, t)]
            if implementers:
                batch = implementers[:max_parallel]
                return [(AgentKey.DEVOPS if t.assignee == "devops" else AgentKey.CODER, t) for t in batch]
            if state.tasks and all(t.status == TaskStatus.DONE for t in state.tasks):
                return [(AgentKey.DEVOPS, None)]
        return []

    def _deps_done(self, state: ProjectState, task: Task) -> bool:
        done = {t.id for t in state.tasks if t.status == TaskStatus.DONE}
        return all(d in done for d in task.dependencies)

    def apply_result(self, state: ProjectState, agent_key: AgentKey, task: Optional[Task], success: bool) -> Optional[str]:
        """Applique le résultat d'un run.

        Retourne un message d'erreur si le run doit être considéré en échec
        malgré la réussite de l'agent (ex : TASKS.json invalide produit par le
        Lead Manager), None sinon.
        """
        if agent_key == AgentKey.PM:
            if not success:
                state.updated_at = now()
                return None
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
                # Après un run en mode config : si des questions ont été posées
                # (fichier présent) mais pas encore répondues, on passe en
                # ARCH_QUESTIONS pour que l'utilisateur réponde, puis l'architecte
                # reprendra en mode init. Sinon (réponses déjà là ou aucune
                # question nécessaire) : ARCHITECTURE_DONE.
                questions = Path(state.path) / "docs" / "architect-questions.json"
                answers = Path(state.path) / "docs" / "architect-answers.json"
                if questions.exists() and not answers.exists():
                    state.phase = Phase.ARCH_QUESTIONS
                else:
                    state.phase = Phase.ARCHITECTURE_DONE
                    state.modules_path = "docs/MODULES.md"
        elif agent_key == AgentKey.LEAD_MANAGER:
            if success:
                self._load_tasks(state)
                violations = self.validate_tasks(state)
                if violations:
                    self._write_validation_errors(state, violations)
                    # On ne passe pas en PLANNING_DONE : le Lead Manager sera
                    # relancé et corrigera en lisant le fichier d'erreurs.
                    state.phase = Phase.ARCHITECTURE_DONE
                    state.updated_at = now()
                    return f"Plan de travail invalide ({len(violations)} problème(s)) — voir docs/TASKS-validation-errors.md"
                state.phase = Phase.PLANNING_DONE
        elif agent_key in (AgentKey.CODER, AgentKey.DEVOPS) and task:
            # Tâche exécutée par son assignee (Coder ou DevOps) : même boucle,
            # le Tester fait le gate pour les deux.
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
        elif agent_key == AgentKey.DEVOPS:
            # Run terminal de déploiement (plus de tâches en attente).
            if success:
                state.phase = Phase.DEPLOYMENT_REVIEW
            else:
                state.deployment_attempts += 1
        elif agent_key == AgentKey.TESTER:
            # Revue finale du déploiement terminal par le Tester.
            verdict = self._deployment_verdict(state)
            if verdict == "PASS":
                state.phase = Phase.COMPLETED
            else:
                state.phase = Phase.DEPLOYMENT
                state.deployment_attempts += 1
        state.updated_at = now()
        return None

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
                assignee=item.get("assignee", "coder"),
            )
            for item in data.get("tasks", [])
        ]

    def validate_tasks(self, state: ProjectState) -> list[str]:
        """Vérifie les contraintes du plan de travail produit par le Lead Manager.

        Retourne la liste des violations (vide si le plan est valide) :
        - ids uniques et non vides
        - assignee ∈ {coder, devops}
        - une tâche Coder ne cible jamais une zone réservée à DevOps
        - les dépendances référencent des tâches existantes
        """
        violations: list[str] = []
        ids = {t.id for t in state.tasks}
        seen: set[str] = set()
        for t in state.tasks:
            if not t.id or t.id in seen:
                violations.append(f"id manquant ou dupliqué : {t.id!r}")
            seen.add(t.id)
            if t.assignee not in ("coder", "devops"):
                violations.append(f"{t.id}: assignee invalide {t.assignee!r} (valeurs : coder, devops)")
            if t.assignee == "coder" and in_devops_zone(t.target or ""):
                violations.append(f"{t.id}: tâche Coder ciblant la zone DevOps réservée ({t.target}) — assigne-la à 'devops'")
            for dep in t.dependencies:
                if dep not in ids:
                    violations.append(f"{t.id}: dépendance inconnue {dep!r}")
        return violations

    def _write_validation_errors(self, state: ProjectState, violations: list[str]) -> None:
        docs = Path(state.path) / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        content = [
            "# Erreurs de validation du plan de travail",
            "",
            "Le plan de travail produit ne respecte pas les contrats. Corrige",
            "docs/TASKS.json puis réécris docs/TASKS.md et docs/BACKLOG.md en conséquence.",
            "",
            "## Violations",
            "",
        ] + [f"- {v}" for v in violations]
        (docs / "TASKS-validation-errors.md").write_text("\n".join(content) + "\n", encoding="utf-8")

    def _verdict(self, state: ProjectState, task: Task) -> Optional[str]:
        report = Path(state.path) / "docs" / "reports" / f"{task.id}-tests.md"
        return self._read_verdict(report)

    def _deployment_verdict(self, state: ProjectState) -> Optional[str]:
        report = Path(state.path) / "docs" / "reports" / "deployment-tests.md"
        return self._read_verdict(report)

    @staticmethod
    def _read_verdict(report: Path) -> Optional[str]:
        if not report.exists():
            return None
        for line in report.read_text().splitlines():
            if line.startswith("STATUT:"):
                return line.split(":", 1)[1].strip().upper()
        return None
