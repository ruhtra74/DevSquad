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


def test_reset_to_prd_done_removes_architect_deliverables(tmp_path):
    orch = _make_orchestrator(tmp_path)
    state = orch.start("app de stock", "StockPro", str(tmp_path / "p"))
    _run_to_architecture_done(orch, state)

    # Des livrables d'architecte existent.
    assert (Path(state.path) / "docs" / "MODULES.md").exists()
    assert (Path(state.path) / "docs" / "architect-questions.json").exists()
    assert (Path(state.path) / "07-prd-final.md").exists()

    step = orch.reset(state.id, "prd_done")
    assert step.status == "succeeded"

    st = orch.store.get(state.id)
    assert st.phase.value == "prd_done"
    assert (Path(state.path) / "07-prd-final.md").exists()  # PRD conservé
    assert not (Path(state.path) / "docs" / "MODULES.md").exists()
    assert not (Path(state.path) / "docs" / "architect-questions.json").exists()

    # L'architecte peut être rejoué : un nouveau run config repart de zéro.
    assert orch.advance(state.id).status == "succeeded"
    assert (Path(state.path) / "docs" / "architect-questions.json").exists()


def test_reset_to_idea_removes_everything(tmp_path):
    orch = _make_orchestrator(tmp_path)
    state = orch.start("app de stock", "StockPro", str(tmp_path / "p"))
    _run_to_architecture_done(orch, state)

    step = orch.reset(state.id, "idea")
    assert step.status == "succeeded"

    st = orch.store.get(state.id)
    assert st.phase.value == "idea"
    assert not (Path(state.path) / "07-prd-final.md").exists()
    assert not (Path(state.path) / "docs" / "PRD.md").exists()
    assert not (Path(state.path) / "docs" / "MODULES.md").exists()
    assert st.tasks == []

    # Le PM reprend depuis le début.
    assert orch.advance(state.id).status == "succeeded"
    assert (Path(state.path) / "docs" / "clarify-questions.json").exists()


def test_reset_to_architecture_done_removes_lead_deliverables(tmp_path):
    orch = _make_orchestrator(tmp_path)
    state = orch.start("app de stock", "StockPro", str(tmp_path / "p"))
    _run_to_architecture_done(orch, state)

    # Lead Manager -> planning_done avec TASKS.json.
    assert orch.advance(state.id).status == "succeeded"
    assert orch.store.get(state.id).phase.value == "planning_done"
    assert (Path(state.path) / "docs" / "TASKS.json").exists()

    step = orch.reset(state.id, "architecture_done")
    assert step.status == "succeeded"

    st = orch.store.get(state.id)
    assert st.phase.value == "architecture_done"
    assert not (Path(state.path) / "docs" / "TASKS.json").exists()
    assert (Path(state.path) / "docs" / "MODULES.md").exists()  # architecture conservée


def test_reset_invalid_target(tmp_path):
    orch = _make_orchestrator(tmp_path)
    state = orch.start("app", "App", str(tmp_path / "p"))
    step = orch.reset(state.id, "nimporte_quoi")
    assert step.status == "failed"
    assert orch.store.get(state.id).phase.value == "idea"
