# Agent Orchestrator

Pilote une **équipe d'agents IA** (via des CLI de coding existants : OpenCode, puis Claude Code / Aider) pour transformer **une idée en logiciel testé, déployé et utilisable**.

## Pipeline

```
Idée → Product Manager → Architecte → Lead Manager → (Coder ↔ Tester)* → DevOps
```

| Agent | Rôle | Livrables |
|---|---|---|
| Product Manager | Idée → PRD validé | `07-prd-final.md`, `docs/PRD.md` |
| Architecte | Découpe la solution en modules, conçoit, scaffolde | `docs/MODULES.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/TECH_STACK.md` |
| Lead Manager | Modules → plan de travail sans chevauchement | `docs/TASKS.md`, `docs/TASKS.json`, `docs/BACKLOG.md` |
| Coder | Implémente une tâche dans son périmètre | code + `docs/reports/TASK-NNN.md` |
| Tester | Valide fonctionnellement (gate : `STATUT: PASS`) | `docs/reports/TASK-NNN-tests.md` |
| DevOps | Rends déployable et utilisable | `Dockerfile`, `docs/DEPLOYMENT.md`, `docs/CHANGELOG.md` |

**Boucle centrale** : Coder ↔ Tester tourne en boucle jusqu'à épuisement du backlog. Une tâche bloquée après 3 essais est marquée `BLOCKED`.

## Revue entre étapes

Après chaque agent réussi (et avant de passer la main au suivant), l'outil affiche :
1. le **résumé** rédigé par l'agent (quelques lignes : contexte, décisions clés, fichiers produits),
2. la liste **numérotée** des fichiers de documentation créés,
3. l'invitation à **voir un fichier** (numéro) — ouvert avec `$EDITOR` (ou `$VISUAL`) si défini, sinon `less`/`cat`,
4. la confirmation de **passer à l'étape suivante** (Entrée pour continuer).

Cela permet de vérifier le travail d'un agent avant qu'il ne transmette au suivant. Désactivable en répondant non à la confirmation finale.

## Installation

```bash
cd orchestrator
pip install -e .
```

Prérequis utilisateur : Python 3.11+, Git, et au moins un backend CLI installé/configuré (OpenCode, avec ses skills dans `~/.agents/skills/`).

## Utilisation

Toutes les commandes acceptent `<project_id>` en argument positionnel **ou** via l'option `--project-id` / `-p`.

### Référence des commandes

| Commande | Rôle |
|---|---|
| `start <idée>` | **Démarre un nouveau projet** à partir d'une idée brute. Propose des noms générés par IA (ou via `--name`), choisit le chemin (ou via `--path`), crée le projet puis lance le pipeline. |
| `advance <project_id>` | **Exécute une seule étape** du pipeline puis s'arrête. |
| `resume <project_id>` | **Reprend un projet** là où il s'est arrêté, en boucle interactive (s'arrête quand on le lui demande ou si une étape échoue). |
| `reset <project_id> --to <phase>` | **Réinitialise un projet à une phase précise** (supprime les livrables des phases suivantes). Utile pour re-tester une étape plusieurs fois sur le même projet. |
| `status <project_id>` | **Affiche l'état** d'un projet : phase, chemin, détail des tâches et du dernier run. |
| `list` / `projects` | **Liste les projets** gérés avec leur identifiant, phase et nom. |
| `rm <project_id>` | **Supprime un projet** (état SQLite + dossier projet). Demande confirmation sauf avec `--yes` / `-y`. |
| `config <set\|get\|show> [clé] [valeur]` | **Gère la configuration** (voir ci-dessous). |
| `agents` | **Liste les définitions d'agents** disponibles (nom, description, backend, livrables attendus). |

### Exemples

```bash
# Démarrer un projet à partir d'une idée (propose un nom + chemin, puis lance le pipeline)
orchestrator start "Une app de gestion de stock pour petit commerce"

# Démarrer sans interactivité pour le nom/chemin + en backend simulé (test du pipeline)
orchestrator start "Mon app" --name MonApp --path /tmp/monprojet --dry

# Exécuter une seule étape du pipeline
orchestrator advance <project_id>

# Reprendre un projet (boucle interactive jusqu'à l'arrêt)
orchestrator resume <project_id>

# Réinitialiser un projet à une phase précise (pour re-tester une étape)
orchestrator reset <project_id> --to prd_done     # rejouer l'architecte
orchestrator reset <project_id> --to idea          # rejouer le PM
orchestrator reset <project_id> --to architecture_done  # rejouer le Lead Manager
orchestrator reset <project_id> --to prd_done --yes      # sans confirmation

# État d'avancement
orchestrator status <project_id>

# Lister / supprimer des projets
orchestrator list
orchestrator rm <project_id> --yes

# Configurer le backend d'un agent
orchestrator config set coder.backend dry     # backend simulé (test pipeline)
orchestrator config set coder.backend opencode

# Configurer le mode silencieux (actif par défaut) / consulter la config
orchestrator config set quiet true            # agents en arrière-plan, pas de stream (sauf ceux qui posent des questions)
orchestrator config set quiet false           # tous les agents interactifs (stream + questions)
orchestrator config show
```

### Options notables

- `start --dry` : backend simulé pour valider le pipeline de bout en bout sans coût ni réseau.
- `rm --yes` / `-y` : supprime sans demander confirmation.
- `reset --yes` / `-y` : réinitialise sans demander confirmation. Phases cibles : `idea`, `prd_done`, `arch_questions`, `architecture_done`, `planning_done`, `development`.
- `--project-id <id>` / `-p <id>` : passer l'identifiant de projet en option (ex : `advance -p monapp`) au lieu d'en argument positionnel.

### Clés de configuration (`orchestrator config`)

| Clé | Valeur par défaut | Rôle |
|---|---|---|
| `root` | `~/.config/orchestrator` | Répertoire de stockage de l'état (DB + projets) |
| `max_parallel` | `1` | Nombre maximal d'agents en parallèle |
| `auto_approve` | `false` | Auto-approuve les actions des agents |
| `quiet` | `true` | `true` : agents en arrière-plan (indicateur + résultat seul), **sauf** les agents qui posent des questions (`asks_questions: true` dans leur définition, ex. le PM) qui restent interactifs ; `false` : tous les agents en interactif |
| `backends.<agent>` | `opencode` | Backend à utiliser par agent (`pm`, `architect`, `lead_manager`, `coder`, `tester`, `devops`) |
| `interactive.<agent>` | selon agent | Force le mode interactif pour un agent donné |
| `timeouts.<agent>` | `1800` | Timeout (s) d'un run par agent. Un run bloqué (ex. appel LLM qui ne répond plus) passe en échec après ce délai et peut être relancé via `resume`. Ex : `orchestrator config set timeouts.pm 900` |
| `agents_dir` / `prompts_dir` | interne | Répertoires personnalisés des définitions / prompts |

## Contrats entre agents

- **Le Lead Manager ne re-découpe jamais les modules** : il travaille sur `docs/MODULES.md` de l'Architecte.
- **Une tâche = un module + un livrable** : `docs/TASKS.json` garantit l'absence de chevauchement (chaque tâche pointe une `target` unique).
- **Le Tester est un gate** : rien n'est `done` sans `STATUT: PASS` dans son rapport.
- **L'interactivité est optionnelle** : par défaut (`quiet: true`) les agents tournent en arrière-plan et seul le résultat est affiché, **sauf** les agents qui posent des questions (`asks_questions: true`, ex. le PM) qui restent interactifs pour recueillir tes réponses. Avec `config set quiet false`, tous les agents sont relancés en `opencode run --interactive` et peuvent poser des questions via l'outil `question`.

## Structure

```
orchestrator/
├── agents/          # définitions des agents (YAML) — personnalisables
├── backends/        # adaptateurs CLI (interface commune) : opencode, dry
├── core/
│   ├── state.py         # modèles Pydantic (ProjectState, Task, AgentRun)
│   ├── state_store.py   # persistance SQLite (reprise fiable)
│   ├── config.py        # configuration globale (JSON)
│   ├── pipeline.py      # machine d'état PM→Architecte→Lead→Coder↔Tester→DevOps
│   ├── scheduler.py     # helpers git worktree (parallélisme v2)
│   └── orchestrator.py  # classe centrale
├── prompts/         # templates Jinja2 par agent — personnalisables
└── cli.py           # start, advance, resume, status, list/rm, config, agents
```

L'état est stocké en **hybride** : SQLite (`orchestrator.db`) pour l'état technique + documents `.md`/`.json` dans `projects/<project_id>/` pour le contrat documentaire. On peut reprendre n'importe quand avec `resume`.

## À venir

- Backends Claude Code et Aider
- Parallélisme (plusieurs Coder/Tester) via git worktrees
- Multi-projets
