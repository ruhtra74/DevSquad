import json
from pathlib import Path

from orchestrator.agents.loader import load_agents
from orchestrator.backends.base import Backend, RunResult, RunSpec
from orchestrator.backends.registry import BackendRegistry
from orchestrator.core.config import Config
from orchestrator.core.orchestrator import Orchestrator


class SpyBackend(Backend):
    name = "spy"

    def __init__(self):
        self.calls: list[RunSpec] = []
        self.dry = False

    def run(self, spec: RunSpec, log_path: str) -> RunResult:
        self.calls.append(spec)
        for rel in spec.expected_outputs:
            target = Path(spec.cwd) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if rel.endswith("-questions.json"):
                target.write_text(json.dumps({
                    "questions": [
                        {
                            "header": "Test",
                            "question": "Question de test ?",
                            "options": [{"label": "Option A", "description": ""}],
                            "multiple": False,
                        }
                    ]
                }, indent=2))
            else:
                target.write_text(f"# stub ({spec.agent_key})\n")
        return RunResult(exit_code=0, success=True, log_path=log_path)


def _make_orchestrator(tmpdir: Path, backend: SpyBackend) -> Orchestrator:
    cfg_path = tmpdir / "config.json"
    cfg_path.write_text(json.dumps({"backends": {k: "spy" for k in ("pm", "architect", "lead_manager", "coder", "tester", "devops")}}))
    cfg = Config(cfg_path)
    agents = load_agents(str(Path("orchestrator/agents")))
    backends = BackendRegistry()
    backends.register(backend)
    return Orchestrator(
        root=tmpdir / "orch",
        cfg=cfg,
        agents=agents,
        backends=backends,
        prompts_dir=Path("orchestrator/prompts"),
    )


def test_pm_questions_then_delivers(tmp_path):
    backend = SpyBackend()
    orch = _make_orchestrator(tmp_path, backend)
    state = orch.start("une app de livraison", "Depeche Express", str(tmp_path / "custom"))

    # Run 1 : clarification -> docs/clarify-questions.json, phase QUESTIONS
    step1 = orch.advance(state.id)
    assert step1.status == "succeeded"
    assert "docs/clarify-questions.json" in backend.calls[0].expected_outputs
    assert (Path(state.path) / "docs" / "clarify-questions.json").exists()
    assert orch.store.get(state.id).phase.value == "questions"

    # L'outil pose les questions de clarification -> réponses
    step_pending = orch.advance(state.id)
    assert step_pending.status == "questions"
    _write_answers(orch, state.path, "docs/clarify-questions.json")

    # Run 2 : entrevue -> 01-idea.md + docs/interview-questions.json
    step2 = orch.advance(state.id)
    assert step2.status == "succeeded"
    assert "docs/interview-questions.json" in backend.calls[1].expected_outputs
    assert "01-idea.md" in backend.calls[1].expected_outputs

    # L'outil pose les questions de l'entrevue -> réponses
    step_pending2 = orch.advance(state.id)
    assert step_pending2.status == "questions"
    _write_answers(orch, state.path, "docs/interview-questions.json")

    # Run 3 : recherche marché + analyse comparative + boucle de décision
    # -> 03-structure.md + 04-research.md + docs/decisions-questions.json
    step3 = orch.advance(state.id)
    assert step3.status == "succeeded"
    assert "docs/decisions-questions.json" in backend.calls[2].expected_outputs
    assert (Path(state.path) / "03-structure.md").exists()
    assert (Path(state.path) / "04-research.md").exists()

    # L'outil pose les questions de décision -> réponses
    step_pending3 = orch.advance(state.id)
    assert step_pending3.status == "questions"
    _write_answers(orch, state.path, "docs/decisions-questions.json")

    # Run 4 : livraison du PRD -> 07-prd-final.md + docs/PRD.md, phase prd_done
    step4 = orch.advance(state.id)
    assert step4.status == "succeeded"
    assert "07-prd-final.md" in backend.calls[3].expected_outputs
    assert (Path(state.path) / "07-prd-final.md").exists()
    assert orch.store.get(state.id).phase.value == "prd_done"


def _write_answers(orch: Orchestrator, project_path: str, questions_rel: str) -> None:
    qpath = Path(project_path) / questions_rel
    data = json.loads(qpath.read_text())
    answers = []
    for q in data["questions"]:
        opt = q["options"][0]
        answers.append({
            "header": q.get("header", ""),
            "question": q.get("question", ""),
            "answer": opt["label"] if isinstance(opt, dict) else str(opt),
        })
    apath = Path(project_path) / questions_rel.replace("-questions.json", "-answers.json")
    apath.write_text(json.dumps({"answers": answers}, indent=2, ensure_ascii=False))


def test_pm_modes_follow_files(tmp_path):
    backend = SpyBackend()
    orch = _make_orchestrator(tmp_path, backend)
    state = orch.start("une idée", "Mon Projet", str(tmp_path / "p"))

    assert orch._pm_mode(state) == "clarify"
    assert orch._pending_questions_file(state) is None

    orch.advance(state.id)  # run clarify -> produit docs/clarify-questions.json
    assert orch._pending_questions_file(state) == "docs/clarify-questions.json"

    q = Path(state.path) / "docs" / "clarify-questions.json"
    a = Path(state.path) / "docs" / "clarify-answers.json"
    a.write_text('{"answers": [{"answer": "Option A"}]}')

    assert orch._pending_questions_file(state) is None
    assert orch._pm_mode(state) == "interview"

    # entrevue -> docs/interview-questions.json (spy écrit aussi 01-idea.md)
    orch.advance(state.id)
    assert orch._pending_questions_file(state) == "docs/interview-questions.json"
    (Path(state.path) / "docs" / "interview-answers.json").write_text('{"answers": [{"answer": "Option A"}]}')

    assert orch._pending_questions_file(state) is None
    assert orch._pm_mode(state) == "research"

    # recherche -> docs/decisions-questions.json
    orch.advance(state.id)
    assert orch._pending_questions_file(state) == "docs/decisions-questions.json"
    (Path(state.path) / "docs" / "decisions-answers.json").write_text('{"answers": [{"answer": "Garder mon choix"}]}')

    assert orch._pending_questions_file(state) is None
    assert orch._pm_mode(state) == "prd"


def test_resolve_single_and_multiple(monkeypatch):
    from orchestrator.cli import _resolve_multiple, _resolve_single

    monkeypatch.setattr("orchestrator.cli.typer.prompt", lambda msg, default="": "2")
    assert _resolve_single(["A", "B", "C"]) == "B"

    monkeypatch.setattr("orchestrator.cli.typer.prompt", lambda msg, default="": "1,3")
    assert _resolve_multiple(["A", "B", "C"]) == "A, C"

    monkeypatch.setattr("orchestrator.cli.typer.prompt", lambda msg, default="": "mon propre choix")
    assert _resolve_single(["A", "B", "C"]) == "mon propre choix"