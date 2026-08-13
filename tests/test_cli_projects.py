from typer.testing import CliRunner

from orchestrator.cli import app


class DummyStore:
    def list(self):
        return [
            {"id": "proj-demo", "name": "Demo app", "phase": "development", "updated_at": "2026-01-01"}
        ]

    def get(self, project_id):
        return None


class DummyOrchestrator:
    def __init__(self):
        self.store = DummyStore()
        self.last_project_id = None

    def advance(self, project_id):
        self.last_project_id = project_id
        return type("Step", (), {"status": "noop", "agent": None, "task_id": None, "message": ""})()


def test_projects_command_lists_known_projects(monkeypatch):
    monkeypatch.setattr("orchestrator.cli.build_orchestrator", lambda: DummyOrchestrator())

    runner = CliRunner()
    result = runner.invoke(app, ["projects"])

    assert result.exit_code == 0
    assert "proj-demo" in result.stdout
    assert "Demo app" in result.stdout


def test_advance_accepts_project_id_option(monkeypatch):
    orch = DummyOrchestrator()
    monkeypatch.setattr("orchestrator.cli.build_orchestrator", lambda: orch)

    runner = CliRunner()
    result = runner.invoke(app, ["advance", "--project-id", "proj-demo"])

    assert result.exit_code == 0
    assert orch.last_project_id == "proj-demo"


def test_review_step_shows_summary_and_files(monkeypatch, tmp_path):
    from orchestrator.cli import _review_step
    from orchestrator.core.orchestrator import StepResult

    class FakeStore:
        def get(self, project_id):
            class FakeState:
                path = str(tmp_path)
            return FakeState()

    class FakeOrch:
        store = FakeStore()

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "MODULES.md").write_text("# Modules\ncontenu")

    step = StepResult(
        status="succeeded", agent="architect", message="OK",
        summary="Résumé : 3 modules découpés.",
        files=["docs/MODULES.md"],
    )
    captured = []
    answers = iter(["1", ""])
    monkeypatch.setattr("orchestrator.cli.typer.prompt", lambda msg, default="": next(answers))
    monkeypatch.setattr("orchestrator.cli._view_file", lambda path: captured.append(path))

    _review_step(FakeOrch(), "p1", step)

    assert captured and captured[0] == tmp_path / "docs" / "MODULES.md"


def test_review_step_skip_with_enter(monkeypatch, tmp_path):
    from orchestrator.cli import _review_step
    from orchestrator.core.orchestrator import StepResult

    class FakeStore:
        def get(self, project_id):
            class FakeState:
                path = str(tmp_path)
            return FakeState()

    class FakeOrch:
        store = FakeStore()

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "MODULES.md").write_text("# Modules")

    step = StepResult(status="succeeded", agent="architect", files=["docs/MODULES.md"])
    monkeypatch.setattr("orchestrator.cli.typer.prompt", lambda msg, default="": "")
    monkeypatch.setattr("orchestrator.cli._view_file", lambda path: (_ for _ in ()).throw(AssertionError("ne doit pas être appelé")))

    _review_step(FakeOrch(), "p1", step)  # doit terminer sans erreur
