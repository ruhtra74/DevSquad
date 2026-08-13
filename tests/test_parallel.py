import json
from pathlib import Path

from orchestrator.agents.loader import load_agents
from orchestrator.backends.dry import DryBackend
from orchestrator.backends.registry import BackendRegistry
from orchestrator.core.config import Config
from orchestrator.core.orchestrator import Orchestrator
from orchestrator.core.pipeline import Pipeline
from orchestrator.core.state import Phase, ProjectState, Task, TaskStatus


class RecordingDry(DryBackend):
    def __init__(self):
        self.calls: list[dict] = []

    def run(self, spec, log_path):
        self.calls.append({"agent": spec.agent_key, "prompt": spec.prompt, "expected": list(spec.expected_outputs)})
        return super().run(spec, log_path)


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


def _run_to_planning_done(orch: Orchestrator, state) -> None:
    for _ in range(4):  # clarify, interview, research, prd
        assert orch.advance(state.id).status == "succeeded"
        _answer(orch, state)
    assert orch.store.get(state.id).phase.value == "prd_done"
    assert orch.advance(state.id).status == "succeeded"  # architect config
    assert orch.advance(state.id).status == "questions"
    _answer(orch, state)
    assert orch.advance(state.id).status == "succeeded"  # architect init
    assert orch.advance(state.id).status == "succeeded"  # lead manager
    assert orch.store.get(state.id).phase.value == "planning_done"


def test_next_batch_respects_dependencies_and_limit():
    agents = load_agents(str(Path("orchestrator/agents")))
    p = Pipeline(agents, Path("orchestrator/prompts"))
    tasks = [
        Task(id="TASK-001", title="A", target="x/"),
        Task(id="TASK-002", title="B", target="y/"),
        Task(id="TASK-003", title="C", target="z/", dependencies=["TASK-001"]),
        Task(id="TASK-004", title="D", target="w/"),
    ]
    state = ProjectState(id="p", name="P", path="/tmp/p", idea="i", phase=Phase.PLANNING_DONE, tasks=tasks)

    batch2 = p.next_batch(state, 2)
    assert [t.id for _, t in batch2] == ["TASK-001", "TASK-002"]

    batch5 = p.next_batch(state, 5)
    # TASK-003 exclue : dépendance TASK-001 pas encore done.
    assert [t.id for _, t in batch5] == ["TASK-001", "TASK-002", "TASK-004"]

    # Vague testeurs : on ne lance pas de Coder tant que des tâches sont à tester.
    tasks[0].status = TaskStatus.IN_TEST
    batch = p.next_batch(state, 5)
    assert len(batch) == 1
    assert batch[0][0].value == "tester" and batch[0][1].id == "TASK-001"


def test_plan_batch_respects_max_parallel_config(tmp_path):
    orch = _make_orchestrator(tmp_path)
    state = orch.start("app", "App", str(tmp_path / "p"))
    _run_to_planning_done(orch, state)

    # TASK-002 et TASK-003 dépendent de TASK-001 : seule TASK-001 est prête.
    orch.cfg.set("max_parallel", 2)
    plan = orch.plan_batch(state.id, orch.max_parallel())
    assert isinstance(plan, list) and len(plan) == 1

    # On passe TASK-001 au vert (coder puis tester) pour libérer TASK-002/003.
    plan = orch.plan_batch(state.id, 5)
    assert orch.run_batch(state.id, plan)[0].status == "succeeded"
    plan = orch.plan_batch(state.id, 5)
    assert orch.run_batch(state.id, plan)[0].status == "succeeded"
    assert orch.store.get(state.id).tasks[0].status.value == "done"

    # Avec max_parallel=1 : un seul implémenteur à la fois.
    orch.cfg.set("max_parallel", 1)
    plan = orch.plan_batch(state.id, orch.max_parallel())
    assert isinstance(plan, list) and len(plan) == 1

    # Avec max_parallel=5 : les deux implémenteurs prêts (coder + devops).
    orch.cfg.set("max_parallel", 5)
    plan = orch.plan_batch(state.id, orch.max_parallel())
    assert isinstance(plan, list) and len(plan) == 2


def test_run_batch_executes_implementers_in_parallel_then_testers(tmp_path):
    backend = RecordingDry()
    orch = _make_orchestrator(tmp_path, backend)
    orch.cfg.set("max_parallel", 5)

    state = orch.start("app", "App", str(tmp_path / "p"))
    _run_to_planning_done(orch, state)

    # TASK-002 et TASK-003 dépendent de TASK-001 : seule TASK-001 est prête.
    plan = orch.plan_batch(state.id, orch.max_parallel())
    assert isinstance(plan, list) and len(plan) == 1
    assert plan[0][0].value == "coder" and plan[0][1].id == "TASK-001"
    assert orch.run_batch(state.id, plan)[0].status == "succeeded"

    # Vague de testers pour TASK-001.
    plan = orch.plan_batch(state.id, 5)
    assert isinstance(plan, list) and len(plan) == 1
    assert plan[0][0].value == "tester" and plan[0][1].id == "TASK-001"
    assert orch.run_batch(state.id, plan)[0].status == "succeeded"

    # TASK-001 done → TASK-002 (coder) et TASK-003 (devops) sont prêtes en
    # même temps : les deux doivent partir dans le même batch.
    plan = orch.plan_batch(state.id, orch.max_parallel())
    assert isinstance(plan, list) and len(plan) == 2
    assert {a.value for a, _ in plan} == {"coder", "devops"}

    steps = orch.run_batch(state.id, plan)
    assert len(steps) == 2 and all(s.status == "succeeded" for s in steps)

    coder_calls = [c for c in backend.calls if c["agent"] == "coder"]
    devops_calls = [c for c in backend.calls if c["agent"] == "devops"]
    assert len(coder_calls) == 2   # TASK-001 + TASK-002
    assert len(devops_calls) == 1  # TASK-003

    st = orch.store.get(state.id)
    assert st.tasks[0].status.value == "done"
    assert st.tasks[1].status.value == "in_test"
    assert st.tasks[2].status.value == "in_test"
    assert st.phase.value == "development"

    # Étape suivante : vague de testers (pas de nouveaux implémenteurs tant
    # qu'il y a des tâches à valider).
    plan2 = orch.plan_batch(state.id, 5)
    assert isinstance(plan2, list) and len(plan2) == 2
    assert all(a.value == "tester" for a, _ in plan2)


def test_extract_commit_message_from_report(tmp_path):
    from orchestrator.cli import _extract_commit_message
    from orchestrator.core.orchestrator import StepResult

    report = tmp_path / "TASK-001.md"
    report.write_text("Résumé\nCOMMIT: TASK-001: Créer la table produits\n", encoding="utf-8")
    step = StepResult(status="succeeded", agent="coder", task_id="TASK-001", message="Implémentation : OK")
    assert _extract_commit_message(report, None, step) == "TASK-001: Créer la table produits"


def test_extract_commit_message_fallback(tmp_path):
    from orchestrator.cli import _extract_commit_message
    from orchestrator.core.orchestrator import StepResult

    class Task:
        id = "TASK-002"
        title = "Créer l'API clients"

    step = StepResult(status="succeeded", agent="coder", task_id="TASK-002")
    assert _extract_commit_message(tmp_path / "absent.md", Task(), step) == "TASK-002: Créer l'API clients"


def test_run_commit_calls_git(monkeypatch, tmp_path):
    from orchestrator.cli import _run_commit

    calls = []

    class R:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(cmd, cwd=None, check=None, capture_output=None):
        calls.append((cmd, cwd))
        return R()

    monkeypatch.setattr("orchestrator.cli.subprocess.run", fake_run)
    _run_commit(tmp_path, "TASK-001: x", push=True)

    assert calls[0][0] == ["git", "add", "-A"]
    assert calls[1][0][:3] == ["git", "commit", "-m"]
    assert calls[1][0][3] == "TASK-001: x"
    assert calls[2][0] == ["git", "push"]
    assert calls[0][1] == tmp_path


def test_propose_commit_skip(monkeypatch, tmp_path):
    from orchestrator.cli import _propose_commit
    from orchestrator.core.orchestrator import StepResult

    class FakeState:
        path = str(tmp_path)
        tasks = []

    class FakeStore:
        def get(self, project_id):
            return FakeState()

    class FakeOrch:
        store = FakeStore()

    (tmp_path / "docs").mkdir(parents=True)
    (tmp_path / "docs" / "reports").mkdir()
    (tmp_path / "docs" / "reports" / "TASK-001.md").write_text("COMMIT: TASK-001: x\n")

    calls = []
    monkeypatch.setattr("orchestrator.cli.typer.prompt", lambda msg, default="": "n")
    monkeypatch.setattr("orchestrator.cli._run_commit", lambda path, msg, push: calls.append((msg, push)))

    _propose_commit(FakeOrch(), "p1", StepResult(status="succeeded", agent="coder", task_id="TASK-001"))
    assert calls == []  # pas de commit


def test_propose_commit_uses_proposed_message(monkeypatch, tmp_path):
    from orchestrator.cli import _propose_commit
    from orchestrator.core.orchestrator import StepResult

    class FakeState:
        path = str(tmp_path)
        tasks = []

    class FakeStore:
        def get(self, project_id):
            return FakeState()

    class FakeOrch:
        store = FakeStore()

    (tmp_path / "docs" / "reports").mkdir(parents=True)
    (tmp_path / "docs" / "reports" / "TASK-001.md").write_text("COMMIT: TASK-001: Créer la table\n")

    answers = iter(["u", "y"])
    monkeypatch.setattr("orchestrator.cli.typer.prompt", lambda msg, default="": next(answers))
    monkeypatch.setattr("orchestrator.cli.typer.confirm", lambda msg, default=False: True)

    commits = []
    monkeypatch.setattr("orchestrator.cli._run_commit", lambda path, msg, push: commits.append((msg, push)))

    _propose_commit(FakeOrch(), "p1", StepResult(status="succeeded", agent="coder", task_id="TASK-001"))
    assert commits == [("TASK-001: Créer la table", True)]