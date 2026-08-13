import json
from pathlib import Path

from orchestrator.agents.loader import load_agents
from orchestrator.backends.dry import DryBackend
from orchestrator.backends.registry import BackendRegistry
from orchestrator.core.config import Config
from orchestrator.core.orchestrator import Orchestrator


class RecordingDry(DryBackend):
    """DryBackend qui enregistre les appels (agent, prompt, livrables attendus)."""

    def __init__(self):
        self.calls: list[dict] = []

    def run(self, spec, log_path):
        self.calls.append({
            "agent": spec.agent_key,
            "prompt": spec.prompt,
            "expected": list(spec.expected_outputs),
        })
        return super().run(spec, log_path)


class RogueBackend(DryBackend):
    """DryBackend qui peut écrire un TASKS.json personnalisé (Lead Manager) ou
    créer des fichiers interdits (Coder qui déborde sur la zone DevOps)."""

    def __init__(self):
        self.tasks_json = None
        self.coder_extra: list[str] = []

    def run(self, spec, log_path):
        result = super().run(spec, log_path)
        if spec.agent_key == "lead_manager" and self.tasks_json is not None:
            (Path(spec.cwd) / "docs" / "TASKS.json").write_text(json.dumps(self.tasks_json, indent=2))
        if spec.agent_key == "coder":
            for rel in self.coder_extra:
                (Path(spec.cwd) / rel).write_text("# créé par le Coder (interdit)\n")
        return result


def _make_orchestrator(tmp_path: Path, backend=None) -> Orchestrator:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"backends": {k: "dry" for k in ("pm", "architect", "lead_manager", "coder", "tester", "devops")}}))
    cfg = Config(cfg_path)
    agents = load_agents(str(Path("orchestrator/agents")))
    backends = BackendRegistry()
    backends.register(backend or DryBackend())
    return Orchestrator(
        root=tmp_path / "orch",
        cfg=cfg,
        agents=agents,
        backends=backends,
        prompts_dir=Path("orchestrator/prompts"),
    )


def _answer(orch: Orchestrator, state) -> None:
    rel = orch._pending_questions_file(state)
    if not rel:
        return
    qpath = Path(state.path) / rel
    data = json.loads(qpath.read_text())
    answers = []
    for q in data["questions"]:
        opt = q["options"][0]
        answers.append({
            "header": q.get("header", ""),
            "question": q.get("question", ""),
            "answer": opt["label"] if isinstance(opt, dict) else str(opt),
        })
    apath = Path(state.path) / rel.replace("-questions.json", "-answers.json")
    apath.write_text(json.dumps({"answers": answers}, indent=2, ensure_ascii=False))


def _run_to_architecture_done(orch: Orchestrator, state) -> None:
    for _ in range(4):  # clarify, interview, research, prd
        assert orch.advance(state.id).status == "succeeded"
        _answer(orch, state)
    assert orch.store.get(state.id).phase.value == "prd_done"

    assert orch.advance(state.id).status == "succeeded"  # architect config
    assert orch.store.get(state.id).phase.value == "arch_questions"
    assert orch.advance(state.id).status == "questions"
    _answer(orch, state)

    assert orch.advance(state.id).status == "succeeded"  # architect init
    assert orch.store.get(state.id).phase.value == "architecture_done"


def test_full_pipeline_routes_devops_tasks_and_reviews_deployment(tmp_path):
    backend = RecordingDry()
    orch = _make_orchestrator(tmp_path, backend)
    state = orch.start("app de stock", "StockPro", str(tmp_path / "p"))
    _run_to_architecture_done(orch, state)

    # Lead Manager : plan avec une tâche coder (001), une coder (002) et une devops (003).
    assert orch.advance(state.id).status == "succeeded"
    assert orch.store.get(state.id).phase.value == "planning_done"

    # TASK-001 (coder) : le Coder implémente, le Tester valide.
    assert orch.advance(state.id).status == "succeeded"
    assert backend.calls[-1]["agent"] == "coder"
    assert orch.advance(state.id).status == "succeeded"
    assert backend.calls[-1]["agent"] == "tester"

    # TASK-002 (coder) : idem.
    assert orch.advance(state.id).status == "succeeded"
    assert backend.calls[-1]["agent"] == "coder"
    assert orch.advance(state.id).status == "succeeded"
    assert backend.calls[-1]["agent"] == "tester"

    # TASK-003 (devops) : exécutée par DevOps, livrable = sa cible (Dockerfile) + rapport.
    step = orch.advance(state.id)
    assert step.status == "succeeded"
    assert backend.calls[-1]["agent"] == "devops"
    devops_call = backend.calls[-1]
    assert "Dockerfile" in devops_call["expected"]
    assert "docs/reports/TASK-003.md" in devops_call["expected"]

    # Le Tester valide le travail de DevOps en mode "devops".
    step = orch.advance(state.id)
    assert step.status == "succeeded"
    assert backend.calls[-1]["agent"] == "tester"
    assert "validation d'une tâche DevOps" in backend.calls[-1]["prompt"]

    st = orch.store.get(state.id)
    assert all(t.status.value == "done" for t in st.tasks)
    assert st.phase.value == "deployment"

    # Run terminal de déploiement (DevOps) puis revue finale (Tester).
    assert orch.advance(state.id).status == "succeeded"
    assert orch.store.get(state.id).phase.value == "deployment_review"
    assert orch.advance(state.id).status == "succeeded"
    assert orch.store.get(state.id).phase.value == "completed"


def test_lead_manager_rejected_when_coder_targets_devops_zone(tmp_path):
    backend = RogueBackend()
    orch = _make_orchestrator(tmp_path, backend)
    state = orch.start("app", "App", str(tmp_path / "p"))
    _run_to_architecture_done(orch, state)

    # Le Lead Manager produit un plan où une tâche coder cible un Dockerfile.
    backend.tasks_json = {
        "tasks": [
            {
                "id": "TASK-001",
                "title": "Dockeriser le backend",
                "module": "Déploiement",
                "target": "Dockerfile",
                "dependencies": [],
                "priority": "P0",
                "assignee": "coder",
            }
        ]
    }

    step = orch.advance(state.id)
    assert step.status == "failed"
    assert "invalide" in step.message
    st = orch.store.get(state.id)
    assert st.phase.value == "architecture_done"  # pas de passage en planning
    assert (Path(state.path) / "docs" / "TASKS-validation-errors.md").exists()
    errors = (Path(state.path) / "docs" / "TASKS-validation-errors.md").read_text()
    assert "zone DevOps réservée" in errors

    # Une fois le plan corrigé (assignee devops), le Lead Manager est accepté.
    backend.tasks_json = {
        "tasks": [
            {
                "id": "TASK-001",
                "title": "Dockeriser le backend",
                "module": "Déploiement",
                "target": "Dockerfile",
                "dependencies": [],
                "priority": "P0",
                "assignee": "devops",
            }
        ]
    }
    assert orch.advance(state.id).status == "succeeded"
    assert orch.store.get(state.id).phase.value == "planning_done"
    st = orch.store.get(state.id)
    assert st.tasks[0].assignee == "devops"


def test_coder_creating_devops_file_fails_run(tmp_path):
    backend = RogueBackend()
    orch = _make_orchestrator(tmp_path, backend)
    state = orch.start("app", "App", str(tmp_path / "p"))
    _run_to_architecture_done(orch, state)

    # Plan valide (sample : TASK-001 coder, TASK-002 coder, TASK-003 devops).
    assert orch.advance(state.id).status == "succeeded"
    assert orch.store.get(state.id).phase.value == "planning_done"

    # Le Coder désobéit et crée un Dockerfile pendant sa tâche -> gate.
    backend.coder_extra = ["Dockerfile"]
    step = orch.advance(state.id)
    assert step.status == "failed"
    assert "zone DevOps" in step.message

    st = orch.store.get(state.id)
    assert st.runs[-1].error and "Dockerfile" in st.runs[-1].error
    assert st.tasks[0].status.value == "in_progress"  # la tâche reste à faire