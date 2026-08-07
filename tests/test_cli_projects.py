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
