"""
Analyse d'une épopée : l'agent rédige un manifeste de découpage ('<ÉPOPÉE>-decoupage.json') et un
fichier d'analyse par tâche ; ce module crée les tâches enfants dans Jira à partir de ce manifeste.

Une fois créée, chaque tâche possède son '<CLÉ>-cadrage.md' : l'étape patch habituelle s'y applique telle quelle.
"""
import json
import os

from tools.jira_tools import create_jira_issue, resolve_epic_task_type, set_jira_description_markdown
from tools.memory_tools import save_cadrage_memory


def epic_manifest_path(epic_key: str) -> str:
    return f"{epic_key}-decoupage.json"


def load_epic_manifest(epic_key: str) -> dict:
    """Manifeste de découpage de l'épopée ({} s'il est absent ou illisible)."""
    try:
        with open(epic_manifest_path(epic_key), "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(manifest, dict):
        return {}
    tasks = manifest.get("tasks")
    manifest["tasks"] = [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []
    return manifest


def save_epic_manifest(epic_key: str, manifest: dict) -> None:
    with open(epic_manifest_path(epic_key), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _restore_keys(manifest: dict, previous: dict) -> None:
    """Remet la clé Jira des tâches déjà créées si l'agent l'a perdue en réécrivant le manifeste."""
    by_file = {t.get("file"): t.get("key") for t in previous.get("tasks", []) if t.get("key")}
    by_summary = {(t.get("summary") or "").strip().lower(): t.get("key") for t in previous.get("tasks", []) if t.get("key")}
    used = {t.get("key") for t in manifest["tasks"] if t.get("key")}
    for task in manifest["tasks"]:
        if task.get("key"):
            continue
        key = by_file.get(task.get("file")) or by_summary.get((task.get("summary") or "").strip().lower())
        if key and key not in used:
            task["key"] = key
            used.add(key)


def sync_epic_tasks(epic_key: str, project_key: str, previous_manifest: dict = None,
                    component: str = "", issue_type: str = "", epic_components: list = None) -> bool:
    """
    Crée dans Jira les tâches du manifeste qui n'ont pas encore de clé (rattachées à l'épopée), met à jour
    la description des autres, et range chaque analyse dans '<CLÉ>-cadrage.md'. Renvoie False si une tâche a échoué.
    """
    manifest = load_epic_manifest(epic_key)
    if not manifest.get("tasks"):
        print(f"[ÉPOPÉE] ❌ Manifeste '{epic_manifest_path(epic_key)}' absent, illisible ou sans tâche.")
        return False
    previous_manifest = previous_manifest or {}
    _restore_keys(manifest, previous_manifest)

    dropped = {t.get("key") for t in previous_manifest.get("tasks", []) if t.get("key")} \
        - {t.get("key") for t in manifest["tasks"]}
    if dropped:
        print(f"[ÉPOPÉE] ⚠️ Tâches retirées du découpage mais conservées dans Jira (à clôturer à la main) : "
              f"{', '.join(sorted(dropped))}")

    wanted_type = issue_type
    issue_type = resolve_epic_task_type(project_key, issue_type)
    if wanted_type and issue_type != wanted_type:
        print(f"[ÉPOPÉE] ⚠️ Type '{wanted_type}' absent du projet {project_key} : type '{issue_type}' utilisé.")

    ok = True
    for index, task in enumerate(manifest["tasks"], 1):
        summary = (task.get("summary") or "").strip()
        analysis = _read(task.get("file") or "").strip()
        if not summary or not analysis:
            print(f"[ÉPOPÉE] ❌ Tâche {index} : titre ou fichier d'analyse '{task.get('file')}' manquant, tâche ignorée.")
            ok = False
            continue
        try:
            if task.get("key"):
                set_jira_description_markdown(task["key"], analysis)
                print(f"[ÉPOPÉE] 🔄 {task['key']} mise à jour : {summary}")
            else:
                try:
                    task["key"] = create_jira_issue(project_key, summary, analysis, component, [],
                                                    issue_type, parent_key=epic_key)
                except ValueError as e:
                    # Équipe / composant non résolu : la tâche est créée quand même, rattachée à l'épopée
                    print(f"[ÉPOPÉE] ⚠️ {e} Tâche créée sans équipe.")
                    task["key"] = create_jira_issue(project_key, summary, analysis, "", [],
                                                    issue_type, parent_key=epic_key)
                print(f"[ÉPOPÉE] ✅ {task['key']} créée sous {epic_key} : {summary}")
        except Exception as e:
            print(f"[ÉPOPÉE] ❌ Tâche {index} « {summary} » : {e}")
            ok = False
            continue

        cadrage_file = f"{task['key']}-cadrage.md"
        if task["file"] != cadrage_file:
            os.replace(task["file"], cadrage_file)
            task["file"] = cadrage_file
        # Enregistré après chaque tâche : une relance ne recrée pas celles déjà créées
        save_epic_manifest(epic_key, manifest)
        try:
            save_cadrage_memory(task["key"], project_key, cadrage_text=analysis, jira_context={
                "summary": summary, "components": epic_components or [], "description": analysis})
        except Exception as e:
            print(f"[MÉMOIRE] ⚠️ Capitalisation impossible pour {task['key']} ({e})")

    keys = [t["key"] for t in manifest["tasks"] if t.get("key")]
    print(f"EPIC_TASKS={','.join(keys)}")
    return ok
