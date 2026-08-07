"""Interface CLI : start, advance, resume, status, list, config, agents."""
from __future__ import annotations

import json
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
        typer.secho(f"[OK] {step.agent} terminé.", fg="green", bold=True)
    elif step.status == "failed":
        typer.secho(f"[ÉCHEC] {step.agent}.", fg="red", bold=True)
    elif step.status == "blocked":
        typer.secho("[BLOQUÉ]", fg="yellow", bold=True)
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
        step = orch.advance(project_id)
        _report(step)
        if step.status != "succeeded":
            return
        if not typer.confirm("Continuer avec l'étape suivante ?", default=True):
            return


def _resolve_project_id(project_id: Optional[str], project_id_option: Optional[str]) -> str:
    resolved = project_id_option or project_id
    if not resolved:
        raise typer.BadParameter("Veuillez fournir un identifiant de projet via l'argument ou --project-id.")
    return resolved


@app.command()
def start(
    idea: str = typer.Argument(..., help="Idée brute du projet, entre guillemets."),
    name: str = typer.Option(None, "--name", "-n", help="Nom du projet (défaut : extrait de l'idée)."),
    dry: bool = typer.Option(False, "--dry", help="Backend simulé (aucun agent réel, pour tester le pipeline)."),
):
    """Démarre un nouveau projet : crée le projet puis lance le Product Manager."""
    orch = build_orchestrator()
    if dry:
        for agent in ("pm", "architect", "lead_manager", "coder", "tester", "devops"):
            orch.cfg.set(f"backends.{agent}", "dry")
    state = orch.start(idea, name)
    typer.echo(f"Projet créé : {state.id}")
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
    _report(orch.advance(resolved_project_id))


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
