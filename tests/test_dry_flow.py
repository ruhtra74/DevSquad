import json
from pathlib import Path

from orchestrator.agents.loader import load_agents
from orchestrator.backends.dry import DryBackend
from orchestrator.backends.registry import BackendRegistry
from orchestrator.core.config import Config
from orchestrator.core.orchestrator import Orchestrator


def _make_orchestrator(tmp_path: Path) -> Orchestrator:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"backends": {k: "dry" for k in ("pm", "architect", "lead_manager", "coder", "tester", "devops")}}))
    cfg = Config(cfg_path)
    agents = load_agents(str(Path("orchestrator/agents")))
    backends = BackendRegistry()
    backends.register(DryBackend())
    return Orchestrator(
        root=tmp_path / "orch",
        cfg=cfg,
        agents=agents,
        backends=backends,
        prompts_dir=Path("orchestrator/prompts"),
    )


def _answer(orch: Orchestrator, state) -> None:
    """Simule l'outil : écrit les réponses pour le fichier de questions en attente."""
    rel = orch._pending_questions_file(state)
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


def test_dry_pipeline_full_pm_flow(tmp_path):
    orch = _make_orchestrator(tmp_path)
    state = orch.start("app de livraison", "Depeche Express", str(tmp_path / "p"))

    # 1. clarify
    s = orch.advance(state.id)
    assert s.status == "succeeded"
    assert (Path(state.path) / "docs" / "clarify-questions.json").exists()
    _answer(orch, state)

    # 2. interview
    s = orch.advance(state.id)
    assert s.status == "succeeded"
    assert (Path(state.path) / "01-idea.md").exists()
    assert (Path(state.path) / "docs" / "interview-questions.json").exists()
    _answer(orch, state)

    # 3. research + décisions
    s = orch.advance(state.id)
    assert s.status == "succeeded"
    assert (Path(state.path) / "03-structure.md").exists()
    assert (Path(state.path) / "04-research.md").exists()
    assert (Path(state.path) / "docs" / "decisions-questions.json").exists()
    _answer(orch, state)

    # 4. prd
    s = orch.advance(state.id)
    assert s.status == "succeeded"
    assert (Path(state.path) / "07-prd-final.md").exists()
    assert (Path(state.path) / "docs" / "PRD.md").exists()
    assert orch.store.get(state.id).phase.value == "prd_done"

    # 5. architecte — mode config : questions de configuration, phase ARCH_QUESTIONS
    s = orch.advance(state.id)
    assert s.status == "succeeded"
    assert (Path(state.path) / "docs" / "architect-questions.json").exists()
    assert orch.store.get(state.id).phase.value == "arch_questions"

    # L'outil pose les questions de l'architecte -> réponses
    s = orch.advance(state.id)
    assert s.status == "questions"
    assert orch._pending_questions_file(state) == "docs/architect-questions.json"
    _answer(orch, state)

    # 6. architecte — mode init : livrables d'architecture, phase architecture_done
    s = orch.advance(state.id)
    assert s.status == "succeeded"
    assert (Path(state.path) / "docs" / "MODULES.md").exists()
    assert (Path(state.path) / "docs" / "ARCHITECTURE.md").exists()
    assert orch.store.get(state.id).phase.value == "architecture_done"