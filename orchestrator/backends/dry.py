"""Backend "dry" : simule les agents sans lancer de CLI externe.

Utile pour valider le pipeline de bout en bout (gates, transitions,
persistance, boucle Coder↔Tester) sans coût ni réseau. Il produit des
livrables réalistes : un TASKS.json avec des tâches d'exemple pour le
Lead Manager, des rapports avec verdict PASS pour le Tester, et des stubs
pour les autres livrables.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import Backend, RunResult, RunSpec

_SAMPLE_TASKS = [
    {
        "id": "TASK-001",
        "title": "Créer la table produits",
        "module": "Produits",
        "target": "backend/src/modules/produits/",
        "dependencies": [],
        "priority": "P0",
    },
    {
        "id": "TASK-002",
        "title": "Créer l'API clients",
        "module": "Clients",
        "target": "backend/src/modules/clients/",
        "dependencies": ["TASK-001"],
        "priority": "P0",
    },
]


class DryBackend(Backend):
    name = "dry"

    def run(self, spec: RunSpec, log_path: str) -> RunResult:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(spec.prompt)

        for rel in spec.expected_outputs:
            target = Path(spec.cwd) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                continue
            if rel.endswith("docs/TASKS.json"):
                target.write_text(json.dumps({"tasks": _SAMPLE_TASKS}, indent=2))
            elif rel.endswith("-tests.md"):
                target.write_text("STATUT: PASS\n\nTous les tests passent.\n")
            else:
                target.write_text(f"# Stub généré par le backend dry ({spec.agent_key})\n")

        return RunResult(exit_code=0, success=True, log_path=log_path)
