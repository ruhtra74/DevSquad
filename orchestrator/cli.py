"""Interface CLI : start, advance, resume, status, list, config, agents."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import shutil

import typer

from .agents.loader import load_agents
from .backends.dry import DryBackend
from .backends.opencode import OpenCodeBackend
from .backends.registry import BackendRegistry
from .core.config import Config, default_root
from .core.orchestrator import Orchestrator, StepResult

PACKAGE_DIR = Path(__file__).resolve().parent
PACKAGE_AGENTS_DIR = PACKAGE_DIR / "agents"
PACKAGE_PROMPTS_DIR = PACKAGE_DIR / "prompts"

app = typer.Typer(add_completion=False)


def build_orchestrator() -> Orchestrator:
    cfg = Config.load()
    root = Path(cfg.get("root")) if cfg.get("root") else default_root()
    agents_dir = Path(cfg.get("agents_dir")) if cfg.get("agents_dir") else PACKAGE_AGENTS_DIR
    prompts_dir = Path(cfg.get("prompts_dir")) if cfg.get("prompts_dir") else PACKAGE_PROMPTS_DIR

    agents = load_agents(agents_dir)
    backends = BackendRegistry()
    backends.register(OpenCodeBackend())
    backends.register(DryBackend())
    return Orchestrator(root=root, cfg=cfg, agents=agents, backends=backends, prompts_dir=prompts_dir)


def _report(step: StepResult) -> None:
    if step.status == "succeeded":
        if step.agent:
            typer.secho(f"[OK] {step.agent} terminé.", fg="green", bold=True)
        else:
            typer.secho("[OK]", fg="green", bold=True)
    elif step.status == "failed":
        typer.secho(f"[ÉCHEC] {step.agent}.", fg="red", bold=True)
    elif step.status == "blocked":
        typer.secho("[BLOQUÉ]", fg="yellow", bold=True)
    elif step.status == "questions":
        typer.secho("[QUESTIONS] L'agent attend tes réponses.", fg="cyan", bold=True)
    elif step.status == "completed":
        typer.secho("[TERMINÉ] Pipeline complet, projet livré.", fg="green", bold=True)
    else:
        typer.secho("[RAS]", fg="bright_black", bold=True)
    if step.task_id:
        typer.echo(f"    Tâche : {step.task_id}")
    if step.message:
        typer.echo(f"    {step.message}")


def _loop(orch: Orchestrator, project_id: str) -> None:
    while True:
        plan = orch.plan_batch(project_id, orch.max_parallel())
        if isinstance(plan, StepResult):
            _report(plan)
            if plan.status == "questions":
                _collect_answers(orch, project_id)
                continue
            return

        # Proposition : si plusieurs tâches sont prêtes, on propose de les
        # lancer en parallèle (l'utilisateur peut refuser → séquentiel).
        if len(plan) > 1 and not _propose_parallel(orch, plan, orch.max_parallel()):
            plan = plan[:1]

        steps = orch.run_batch(project_id, plan)
        stop = False
        for step in steps:
            _report(step)
            if step.status == "succeeded":
                # Les agents ne committent jamais : ils proposent, l'utilisateur valide.
                if step.agent in ("coder", "devops") and step.task_id:
                    _propose_commit(orch, project_id, step)
                _review_step(orch, project_id, step)
            else:
                stop = True
        if stop:
            return
        if not typer.confirm("Continuer avec les prochaines étapes ?", default=True):
            return


def _propose_parallel(orch: Orchestrator, plan, max_parallel: int) -> bool:
    """Présente les tâches prêtes et propose de les lancer en parallèle."""
    typer.secho("")
    typer.secho(f"Tâches prêtes à être lancées en parallèle (max {max_parallel}) :", fg="cyan", bold=True)
    for agent_key, task in plan:
        if task:
            typer.echo(f"  - {task.id} ({task.assignee}) : {task.title}")
        else:
            typer.echo(f"  - {agent_key}")
    return typer.confirm("Lancer ces tâches en parallèle ?", default=True)


def _propose_commit(orch: Orchestrator, project_id: str, step: StepResult) -> None:
    """Les agents ne committent jamais eux-mêmes : ils proposent un message de
    commit dans leur rapport et c'est l'utilisateur qui valide — message proposé
    ou personnalisé, commit réel ou non, push ou non."""
    state = orch.store.get(project_id)
    if state is None:
        return
    task = next((t for t in state.tasks if t.id == step.task_id), None)
    report = Path(state.path) / "docs" / "reports" / f"{step.task_id}.md"
    proposed = _extract_commit_message(report, task, step)

    typer.secho("")
    typer.secho(f"Message de commit proposé par l'agent ({step.task_id}) :", fg="cyan", bold=True)
    typer.echo(f"  {proposed}")
    choice = typer.prompt("Utiliser / personnaliser / ne pas committer ? [u/p/n]", default="u").strip().lower()
    if choice.startswith("n"):
        typer.secho("Pas de commit.", fg="bright_black")
        return
    if choice.startswith("p"):
        custom = typer.prompt("Message de commit personnalisé", default=proposed).strip()
        if not custom:
            typer.secho("Commit annulé (message vide).", fg="yellow")
            return
        proposed = custom
    do_push = typer.confirm("Pousser (git push) ?", default=False)
    _run_commit(Path(state.path), proposed, do_push)


def _extract_commit_message(report: Path, task, step: StepResult) -> str:
    """Message de commit proposé par l'agent : ligne `COMMIT:` du rapport, sinon
    un fallback à partir de la tâche."""
    if report.exists():
        for line in report.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("COMMIT:"):
                msg = line.split(":", 1)[1].strip()
                if msg:
                    return msg
    if task:
        return f"{task.id}: {task.title}"
    fallback = step.message or ""
    return f"{step.task_id}: {fallback}".strip() if fallback else str(step.task_id or "travail")


def _run_commit(path: Path, message: str, push: bool) -> None:
    try:
        subprocess.run(["git", "add", "-A"], cwd=path, check=False, capture_output=True)
        res = subprocess.run(["git", "commit", "-m", message], cwd=path, check=False, capture_output=True)
        if res.returncode != 0:
            err = (res.stderr or res.stdout or b"").decode(errors="replace").strip()
            typer.secho(f"Commit impossible : {err}", fg="yellow")
            return
        typer.secho(f"Commit créé : {message}", fg="green")
        if push:
            rp = subprocess.run(["git", "push"], cwd=path, check=False, capture_output=True)
            if rp.returncode == 0:
                typer.secho("Push effectué.", fg="green")
            else:
                err = (rp.stderr or rp.stdout or b"").decode(errors="replace").strip()
                typer.secho(f"Push impossible : {err}", fg="yellow")
    except OSError as e:
        typer.secho(f"Git indisponible : {e}", fg="yellow")


def _review_step(orch: Orchestrator, project_id: str, step: StepResult) -> None:
    """Entre deux agents : affiche le résumé de l'agent, les fichiers produits
    (numérotés) et propose de les consulter avant de passer à la suite."""
    typer.secho("")
    typer.secho(f"Résumé de l'agent ({step.agent}):", fg="cyan", bold=True)
    summary = (step.summary or step.message or "").strip()
    if summary:
        typer.echo("  " + summary.replace("\n", "\n  "))
    files = step.files or []
    if files:
        typer.secho("")
        typer.secho("Fichiers de documentation créés :", fg="cyan", bold=True)
        for i, f in enumerate(files, 1):
            typer.echo(f"  {i}. {f}")
        typer.echo("")
        while True:
            raw = typer.prompt("Voir un fichier (numéro), ou Entrée pour continuer", default="").strip()
            if not raw:
                break
            if raw.isdigit() and 1 <= int(raw) <= len(files):
                state = orch.store.get(project_id)
                if state:
                    _view_file(Path(state.path) / files[int(raw) - 1])
            else:
                typer.secho(f"Numéro invalide (1-{len(files)}).", fg="yellow")
    typer.secho("")


def _view_file(path: Path) -> None:
    """Affiche un fichier : éditeur par défaut ($EDITOR) sinon less/cat."""
    if not path.exists():
        typer.secho(f"Fichier introuvable : {path}", fg="yellow")
        return
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    try:
        if editor:
            subprocess.run([editor, str(path)], check=False)
        else:
            pager = "less" if shutil.which("less") else "cat"
            subprocess.run([pager, str(path)], check=False)
    except (OSError, ValueError):
        try:
            typer.echo(path.read_text())
        except OSError as e:
            typer.secho(f"Lecture impossible : {e}", fg="yellow")


def _collect_answers(orch: Orchestrator, project_id: str) -> None:
    """Pose les questions du PM (fichier docs/*-questions.json) et enregistre les réponses.

    Chaque question affiche les propositions avec leurs descriptions et permet
    un champ libre. Les réponses sont écrites dans docs/*-answers.json.
    """
    state = orch.store.get(project_id)
    if state is None:
        return
    rel = orch._pending_questions_file(state)
    if not rel:
        typer.secho("Aucune question en attente.", fg="yellow")
        return
    questions_path = Path(state.path) / rel
    answers_path = Path(state.path) / rel.replace("-questions.json", "-answers.json")

    try:
        data = json.loads(questions_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        typer.secho(f"Fichier de questions illisible : {e}", fg="red")
        return

    questions = data.get("questions", []) if isinstance(data, dict) else data
    if not isinstance(questions, list) or not questions:
        # Liste vide : l'agent n'a rien à demander, on débloque la phase.
        answers_path.parent.mkdir(parents=True, exist_ok=True)
        answers_path.write_text(json.dumps({"answers": []}, indent=2, ensure_ascii=False))
        typer.secho("Aucune question à poser, reprise du pipeline.", fg="green")
        return

    typer.secho("")
    who = "L'Architecte" if "architect" in rel else "Le Product Manager"
    typer.secho(f"{who} te pose quelques questions :", fg="cyan", bold=True)
    answers: list[dict] = []
    for i, q in enumerate(questions, 1):
        header = q.get("header", "Question")
        question = q.get("question", "")
        options = q.get("options", []) or []
        multiple = bool(q.get("multiple"))
        typer.secho("")
        typer.secho(f"[{i}/{len(questions)}] {header}", fg="bright_black", bold=True)
        typer.echo(f"  {question}")
        labels = []
        for j, opt in enumerate(options, 1):
            if isinstance(opt, dict):
                label = opt.get("label", "")
                desc = opt.get("description", "")
            else:
                label = str(opt)
                desc = ""
            labels.append(label)
            typer.echo(f"   {j}. {label}" + (f" — {desc}" if desc else ""))
        typer.echo("   libre. taper directement votre réponse")

        if multiple:
            answer = _resolve_multiple(labels)
        else:
            answer = _resolve_single(labels)
        answers.append({
            "header": header,
            "question": question,
            "answer": answer,
        })

    answers_path.parent.mkdir(parents=True, exist_ok=True)
    answers_path.write_text(json.dumps({"answers": answers}, indent=2, ensure_ascii=False))
    typer.secho("")
    typer.secho(f"Réponses enregistrées ({len(answers)}).", fg="green", bold=True)


def _resolve_single(labels: list[str]) -> str:
    """Une seule réponse : un numéro de proposition ou un champ libre."""
    while True:
        raw = typer.prompt("Ta réponse", default="").strip()
        if not raw:
            typer.secho("Réponse vide, merci de répondre.", fg="yellow")
            continue
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(labels):
                return labels[idx - 1]
            typer.secho(f"Numéro invalide (1-{len(labels)}).", fg="yellow")
            continue
        return raw


def _resolve_multiple(labels: list[str]) -> str:
    """Plusieurs réponses : numéros séparés par des virgules ou champ libre."""
    while True:
        raw = typer.prompt("Ta réponse (numéros séparés par des virgules, ou texte libre)", default="").strip()
        if not raw:
            typer.secho("Réponse vide, merci de répondre.", fg="yellow")
            continue
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if all(p.isdigit() for p in parts):
            idxs = [int(p) for p in parts]
            if all(1 <= i <= len(labels) for i in idxs):
                return ", ".join(labels[i - 1] for i in idxs)
            typer.secho(f"Numéros invalides (1-{len(labels)}).", fg="yellow")
            continue
        return raw


def _resolve_project_id(project_id: Optional[str], project_id_option: Optional[str]) -> str:
    resolved = project_id_option or project_id
    if not resolved:
        raise typer.BadParameter("Veuillez fournir un identifiant de projet via l'argument ou --project-id.")
    return resolved


def _generate_name_suggestions(idea: str, orch: Orchestrator) -> list[str]:
    """Génère plusieurs suggestions de noms via le backend configuré."""
    from .backends.base import RunSpec

    backend_name = orch.cfg.get("backends.pm") or orch.agents["pm"].backend

    try:
        backend = orch.backends.get(backend_name)
    except KeyError:
        typer.secho(f"⚠️  Backend '{backend_name}' non disponible. Mode manuel.", fg="yellow")
        return []

    if backend_name == "dry":
        typer.secho("Mode 'dry' détecté. Proposez directement le nom du projet.", fg="bright_black")
        return []

    prompt = f"""Tu dois générer exactement 4 suggestions de noms courts et créatifs pour un projet logiciel.

Idée du projet : {idea}

Consignes :
- Génère 4 noms différents, créatifs et mémorables
- Chaque nom doit être court (2-4 mots maximum)
- Les noms doivent refléter l'idée du projet
- Format : réponds avec SEULEMENT les 4 noms, un par ligne, sans numérotation ni explication

Génère les suggestions :"""

    typer.echo("")
    typer.secho(f"L'IA ({backend_name}) génère des suggestions de noms...", fg="cyan", bold=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        spec = RunSpec(
            agent_key="project_naming",
            prompt=prompt,
            cwd=tmpdir,
            interactive=False,
            auto_approve=False,
            expected_outputs=[],
            capture=True,
        )
        try:
            result = backend.run(spec, str(Path(tmpdir) / "naming.log"))
        except Exception as e:
            typer.secho(f"⚠️  Erreur lors de l'appel au backend : {e}", fg="yellow")
            typer.secho("Mode manuel activé.", fg="yellow")
            return []

    if not result.success:
        typer.secho(f"⚠️  Le backend n'a pas répondu ({result.error or 'erreur inconnue'}). Mode manuel.", fg="yellow")
        return []

    names = _parse_suggestions(result.output)
    if not names:
        typer.secho("⚠️  Aucun nom exploitable dans la réponse de l'IA. Mode manuel.", fg="yellow")
        return []

    return names


def _parse_suggestions(output: str) -> list[str]:
    """Extrait les noms proposés depuis la sortie du backend (JSON/SSE ou texte brut)."""
    text = (output or "").strip()
    if not text:
        return []

    candidates: list[str] = []
    saw_json = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("data:"):
            stripped = stripped[len("data:"):].strip()
        if not stripped.startswith("{"):
            continue
        saw_json = True
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        payload = (obj.get("part") or {}).get("text")
        if not payload:
            payload = (obj.get("message") or {}).get("content")
        if payload:
            payload = str(payload).strip()
            if payload:
                candidates.extend(_split_names(payload))

    if not candidates and not saw_json:
        candidates.extend(_split_names(text))

    cleaned: list[str] = []
    seen = set()
    for name in candidates:
        if name and name.lower() not in seen:
            seen.add(name.lower())
            cleaned.append(name)
    return cleaned


def _split_names(payload: str) -> list[str]:
    """Sépare les noms sur les sauts de ligne / virgules / puces, sans numérotation."""
    raw = re.split(r"[\n,;]+", payload)
    names = []
    for item in raw:
        name = item.strip()
        name = re.sub(r"^[-*•\d.)\s]+", "", name).strip()
        name = name.rstrip(".")
        if name and len(name) <= 60:
            names.append(name)
    return names[:4]


@app.command()
def start(
    idea: str = typer.Argument(..., help="Idée brute du projet, entre guillemets."),
    name: str = typer.Option(None, "--name", "-n", help="Nom du projet (si non fourni, choix interactif)."),
    path: str = typer.Option(None, "--path", "-p", help="Chemin où stocker le projet (si non fourni, choix interactif)."),
    dry: bool = typer.Option(False, "--dry", help="Backend simulé (aucun agent réel, pour tester le pipeline)."),
):
    """Démarre un nouveau projet : crée le projet puis lance le Product Manager."""
    orch = build_orchestrator()
    if dry:
        for agent in ("pm", "architect", "lead_manager", "coder", "tester", "devops"):
            orch.cfg.set(f"backends.{agent}", "dry")
    
    # Étape 1 : Afficher l'idée
    typer.secho("Idée du projet :", fg="cyan", bold=True)
    typer.echo(f"  {idea}")
    typer.echo("")
    
    # Étape 2 : Choix du nom via le backend configuré
    if not name:
        use_ai = not dry  # N'utilise l'IA que si on n'est pas en mode dry
        suggestions = _generate_name_suggestions(idea, orch) if use_ai else []
        
        if suggestions:
            typer.secho("Suggestions de noms proposées par l'IA :", fg="cyan", bold=True)
            for i, suggestion in enumerate(suggestions, 1):
                typer.echo(f"  {i}. {suggestion}")
            typer.echo(f"  {len(suggestions) + 1}. Proposer un autre nom")
            typer.echo("")

            choice_raw = typer.prompt(
                "Choisissez un numéro, ou tapez directement votre nom",
                default="1"
            )
            try:
                choice = int(choice_raw)
            except ValueError:
                name = choice_raw.strip()
            else:
                if 1 <= choice <= len(suggestions):
                    name = suggestions[choice - 1]
                else:
                    name = typer.prompt("Entrez le nom du projet")
        else:
            # Fallback : demander directement le nom
            name = typer.prompt("Entrez le nom du projet")
    
    typer.secho(f"Nom du projet : {name}", fg="green")
    typer.echo("")
    
    # Étape 3 : Choix du chemin
    if not path:
        default_path = str(orch.projects_dir)
        typer.secho(f"Chemin de destination par défaut : {default_path}", fg="bright_black")
        custom_path = typer.prompt(
            "Voulez-vous un chemin personnalisé ? (appuyez sur Entrée pour le chemin par défaut)",
            default=""
        )
        path = os.path.expanduser(custom_path) if custom_path else default_path
    
    typer.secho(f"Chemin : {path}", fg="green")
    typer.echo("")
    
    # Créer le projet avec les paramètres
    state = orch.start(idea, name, path)
    typer.secho(f"Projet créé : {state.id}", fg="green", bold=True)
    typer.echo(f"  Nom   : {state.name}")
    typer.echo(f"  Chemin: {state.path}")
    typer.echo("")
    _loop(orch, state.id)


@app.command()
def advance(
    project_id: Optional[str] = typer.Argument(None, help="Identifiant du projet."),
    project_id_option: Optional[str] = typer.Option(None, "--project-id", "-p", help="Identifiant du projet."),
):
    """Exécute une seule étape du pipeline."""
    orch = build_orchestrator()
    resolved_project_id = _resolve_project_id(project_id, project_id_option)
    step = orch.advance(resolved_project_id)
    _report(step)
    if step.status == "succeeded" and step.agent in ("coder", "devops") and step.task_id:
        _propose_commit(orch, resolved_project_id, step)


@app.command()
def reset(
    project_id: Optional[str] = typer.Argument(None, help="Identifiant du projet."),
    project_id_option: Optional[str] = typer.Option(None, "--project-id", "-p", help="Identifiant du projet."),
    to: str = typer.Option(..., "--to", "-t", help="Phase cible : idea, prd_done, arch_questions, architecture_done, planning_done, development."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Ne pas demander confirmation."),
):
    """Réinitialise un projet à une phase précise (supprime les livrables des phases suivantes).

    Permet de re-tester une étape du pipeline plusieurs fois sur le même projet.
    Exemples :
      orchestrator reset stockpro --to prd_done     # rejouer l'architecte
      orchestrator reset stockpro --to idea          # rejouer le PM
      orchestrator reset stockpro --to architecture_done   # rejouer le Lead Manager
    """
    orch = build_orchestrator()
    resolved_project_id = _resolve_project_id(project_id, project_id_option)
    state = orch.store.get(resolved_project_id)
    if state is None:
        raise typer.BadParameter(f"Projet inconnu : {resolved_project_id}")
    if not yes:
        current = state.phase.value
        confirm = typer.confirm(
            f"Réinitialiser '{state.name}' ({resolved_project_id}) de '{current}' vers '{to}' ? "
            "Les livrables des phases suivantes seront supprimés.",
            default=False,
        )
        if not confirm:
            typer.echo("Annulé.")
            return
    _report(orch.reset(resolved_project_id, to))


@app.command()
def resume(
    project_id: Optional[str] = typer.Argument(None, help="Identifiant du projet."),
    project_id_option: Optional[str] = typer.Option(None, "--project-id", "-p", help="Identifiant du projet."),
):
    """Reprend un projet là où il s'est arrêté (boucle interactive)."""
    resolved_project_id = _resolve_project_id(project_id, project_id_option)
    _loop(build_orchestrator(), resolved_project_id)


@app.command()
def status(
    project_id: Optional[str] = typer.Argument(None, help="Identifiant du projet."),
    project_id_option: Optional[str] = typer.Option(None, "--project-id", "-p", help="Identifiant du projet."),
):
    """Affiche l'état d'un projet."""
    orch = build_orchestrator()
    resolved_project_id = _resolve_project_id(project_id, project_id_option)
    state = orch.store.get(resolved_project_id)
    if state is None:
        raise typer.BadParameter(f"Projet inconnu : {resolved_project_id}")
    typer.secho(f"{state.name}", bold=True)
    typer.echo(f"  ID     : {state.id}")
    typer.echo(f"  Phase  : {state.phase.value}")
    typer.echo(f"  Chemin : {state.path}")
    if state.tasks:
        typer.echo("")
        typer.secho("Tâches :", bold=True)
        for t in state.tasks:
            mark = {"todo": "⬜", "in_progress": "🔄", "in_test": "🧪", "done": "✅", "blocked": "❌"}.get(
                t.status.value, "•"
            )
            typer.echo(f"  {mark} {t.status.value:12} {t.id} [{t.module or '-'}] {t.title}")
    if state.runs:
        last = state.runs[-1]
        typer.echo("")
        typer.echo(f"Dernier run : {last.agent.value} → {last.status.value} (backend {last.backend})")
        if last.error:
            typer.secho(f"  Erreur : {last.error}", fg="yellow")


@app.command("projects")
@app.command("list")
def list_projects():
    """Liste les projets gérés avec leurs identifiants."""
    orch = build_orchestrator()
    rows = orch.store.list()
    if not rows:
        typer.echo("Aucun projet enregistré.")
        return
    typer.echo("Projets enregistrés :")
    for r in rows:
        typer.echo(f"- {r['id']} | {r['phase']} | {r['name']}")


@app.command("rm")
def remove_project(
    project_id: Optional[str] = typer.Argument(None, help="Identifiant du projet."),
    project_id_option: Optional[str] = typer.Option(None, "--project-id", "-p", help="Identifiant du projet."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Ne pas demander confirmation."),
):
    """Supprime un projet (état SQLite + dossier projet)."""
    orch = build_orchestrator()
    resolved_project_id = _resolve_project_id(project_id, project_id_option)
    state = orch.store.get(resolved_project_id)
    if state is None:
        raise typer.BadParameter(f"Projet inconnu : {resolved_project_id}")
    if not yes and not typer.confirm(f"Supprimer définitivement '{state.name}' ({state.id}) ?", default=False):
        typer.echo("Annulé.")
        return
    orch.store.delete(resolved_project_id)
    path = Path(state.path)
    if path.exists():
        shutil.rmtree(path)
    typer.echo(f"Projet supprimé : {resolved_project_id}")


@app.command()
def config(
    action: str = typer.Argument(..., help="set | get | show"),
    key: str = typer.Argument(None, help="Clé, ex : coder.backend"),
    value: str = typer.Argument(None, help="Valeur (pour set)"),
):
    """Lit ou modifie la configuration (ex : orchestrator config set coder.backend dry)."""
    cfg = Config.load()
    if action == "set":
        if not key or value is None:
            raise typer.BadParameter("Usage : orchestrator config set <clé> <valeur>")
        cfg.set(key, value)
        typer.echo(f"{key} = {value}")
    elif action == "get":
        typer.echo(cfg.get(key))
    elif action == "show":
        typer.echo(json.dumps(cfg.data, indent=2, ensure_ascii=False))
    else:
        raise typer.BadParameter("Action inconnue : set | get | show")


@app.command("agents")
def agents_list():
    """Liste les définitions d'agents disponibles."""
    orch = build_orchestrator()
    for key, agent in orch.agents.items():
        typer.echo(f"{key:14} {agent.name:20} {agent.description}")
        typer.echo(f"{'':14} {'backend:':10} {agent.backend}  livrables: {', '.join(agent.expected_outputs) or '-'}")


if __name__ == "__main__":
    app()
