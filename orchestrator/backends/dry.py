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
        "assignee": "coder",
    },
    {
        "id": "TASK-002",
        "title": "Créer l'API clients",
        "module": "Clients",
        "target": "backend/src/modules/clients/",
        "dependencies": ["TASK-001"],
        "priority": "P0",
        "assignee": "coder",
    },
    {
        "id": "TASK-003",
        "title": "Dockeriser le backend",
        "module": "Déploiement",
        "target": "Dockerfile",
        "dependencies": ["TASK-001"],
        "priority": "P0",
        "assignee": "devops",
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
            if rel == "docs/clarify-questions.json":
                target.write_text(json.dumps({
                    "questions": [
                        {
                            "header": "Cible",
                            "question": "Qui est l'utilisateur principal au départ ?",
                            "options": [
                                {"label": "Particuliers", "description": "Le grand public"},
                                {"label": "Entreprises", "description": "B2B"},
                                {"label": "Mixte", "description": "Les deux"},
                            ],
                            "multiple": False,
                        }
                    ]
                }, indent=2))
            elif rel == "docs/interview-questions.json":
                target.write_text(json.dumps({
                    "questions": [
                        {
                            "header": "Périmètre",
                            "question": "Faut-il prévoir une version mobile ?",
                            "options": [
                                {"label": "Oui, natif", "description": "iOS + Android"},
                                {"label": "Oui, web mobile", "description": "Responsive"},
                                {"label": "Non", "description": "Desktop uniquement"},
                            ],
                            "multiple": False,
                        },
                        {
                            "header": "Monétisation",
                            "question": "Quel modèle économique ?",
                            "options": [
                                {"label": "Abonnement", "description": "Récurrent"},
                                {"label": "Commission", "description": "Par transaction"},
                                {"label": "Freemium", "description": "Gratuit + options"},
                            ],
                            "multiple": False,
                        },
                    ]
                }, indent=2))
            elif rel == "docs/architect-questions.json":
                target.write_text(json.dumps({
                    "questions": [
                        {
                            "header": "Scaffolding",
                            "question": "Veux-tu lancer le scaffolding réel ou seulement créer l'arborescence manuellement ?",
                            "options": [
                                {"label": "Oui, lancer le scaffolding", "description": "Exécute les commandes de scaffolding"},
                                {"label": "Non, juste l'arborescence", "description": "Seulement les dossiers et fichiers de base"},
                            ],
                            "multiple": False,
                        },
                        {
                            "header": "Architecture",
                            "question": "Quelle architecture pour le code ?",
                            "options": [
                                {"label": "Hexagonale (ports & adapters)", "description": "Architecture hexagonale"},
                                {"label": "En couches (layered)", "description": "Controller/service/repository"},
                                {"label": "Aucune", "description": "Structure par défaut"},
                            ],
                            "multiple": False,
                        },
                    ]
                }, indent=2))
            elif rel == "docs/decisions-questions.json":
                target.write_text(json.dumps({
                    "questions": [
                        {
                            "header": "Contradiction marché",
                            "question": "L'étude recommande X plutôt que Y car les concurrents leaders utilisent X. Gardes-tu ton choix ou adoptes-tu la recommandation ?",
                            "options": [
                                {"label": "Garder mon choix", "description": "Je maintiens ma décision"},
                                {"label": "Adopter la recommandation", "description": "Je suis l'étude de marché"},
                                {"label": "Compromis", "description": "Je combine les deux"},
                            ],
                            "multiple": False,
                        }
                    ]
                }, indent=2))
            elif rel.endswith("docs/TASKS.json"):
                target.write_text(json.dumps({"tasks": _SAMPLE_TASKS}, indent=2))
            elif rel.endswith("-tests.md"):
                target.write_text("STATUT: PASS\n\nTous les tests passent.\n")
            else:
                target.write_text(f"# Stub généré par le backend dry ({spec.agent_key})\n")

        return RunResult(exit_code=0, success=True, log_path=log_path)
