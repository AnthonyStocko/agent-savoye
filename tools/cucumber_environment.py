"""
Environnement reconstruit par l'init du projet (points 9c, 9g, 9h), 0 token côté Python.

- Contrôles d'environnement en LECTURE SEULE ajoutés à l'init du client : déclarations de champs personnalisés,
  documents, valeurs d'énumération et paramétrages dont dépendent les tests. Sources : champ
  « environment_checks » du profil client, et champs personnalisés absents relevés dans les runs importés
  (« Unable to find the custom field »). Un seul step du projet les vérifie tous et échoue UNE fois, avec la
  liste de tout ce qui manque : un environnement incomplet produit un échec clair dans l'init.
- Corrections manuelles de l'environnement signalées (bilan, étape Bilan) : jamais une solution durable ; elles
  deviennent une recréation dans l'init du projet (step du projet idempotent, avant le contrôle).
Les steps du projet sont écrits par un appel Claude borné (cucumber_init_environnement.md), puis contrôlés
(check_steps, compilation et dry-run) ; s'ils ne sont pas livrables, leurs scénarios restent commentés
(TODO-ENVIRONNEMENT) dans l'init.
"""
import json
import os
import re

from tools.cucumber_tools import (_java_methods, _read_json, _read_text_file, java_class_suffix,
                                  java_package_segment, step_expressions)

TYPES = {"champ_personnalise": "déclaration de champ personnalisé", "document": "document",
         "enumeration": "valeur d'énumération", "parametrage": "paramétrage"}
TODO_MARKER = "TODO-ENVIRONNEMENT:"
_CUSTOM_FIELD = re.compile(r"Unable to find the custom field\s*[:'\"«(]*\s*([\w.\-]+)", re.I)
_SCENARIO_KEYWORD = "Scenario"


def check_expression(client_name: str) -> str:
    return f"INIT- {java_class_suffix(client_name)} contrôle de l'environnement"


def recreation_expression(client_name: str) -> str:
    return f"INIT- {java_class_suffix(client_name)} recréation des corrections manuelles"


def java_path(client_name: str) -> str:
    return (f"src/test/java/cucumber/features/{java_package_segment(client_name)}/"
            f"Init{java_class_suffix(client_name)}EnvironnementSteps.java")


def _key(item: dict) -> tuple:
    return item.get("type", ""), item.get("entite", ""), item.get("code", "")


def imported_dependencies(imports_dir: str) -> list:
    """Champs personnalisés introuvables dans les runs importés (messages d'échec des analyses)."""
    found = {}
    if not os.path.isdir(imports_dir):
        return []
    for name in sorted(os.listdir(imports_dir)):
        analysis = _read_json(os.path.join(imports_dir, name, "analyse.json"), {}) or {}
        for result in (analysis.get("resultats") or {}).values():
            for field in _CUSTOM_FIELD.findall(result.get("message", "")):
                found.setdefault(field, {"type": "champ_personnalise", "entite": "", "code": field,
                                         "source": f"run importé n° {analysis.get('numero', name)}"})
    return list(found.values())


def dependencies(profile: dict, imports_dir: str = "") -> list:
    """Dépendances d'environnement à contrôler : profil client, puis runs importés (sans doublon)."""
    out, seen = [], set()
    profile_items = [{**i, "source": "profil"} for i in profile.get("environment_checks") or []
                     if isinstance(i, dict) and i.get("type") in TYPES and str(i.get("code") or "").strip()]
    for item in profile_items + imported_dependencies(imports_dir):
        if _key(item) not in seen:
            seen.add(_key(item))
            out.append(item)
    return out


def _cell(value) -> str:
    return " ".join(str(value or "").replace("|", "/").split())


def scenario_lines(client_name: str, checks: list, recreations: list) -> list:
    """
    Scénarios ajoutés à la fin de chaque feature d'init : recréation des corrections manuelles (avant), puis
    contrôle de l'environnement en lecture seule.
    """
    suffix, lines = java_class_suffix(client_name), []
    if recreations:
        lines += ["", f"  {_SCENARIO_KEYWORD}: {suffix} - Recréation des corrections manuelles de l'environnement",
                  f"    Given {recreation_expression(client_name)}", "      | description |"]
        lines += [f"      | {_cell(r)} |" for r in recreations]
    if checks:
        lines += ["", f"  {_SCENARIO_KEYWORD}: {suffix} - Contrôle de l'environnement (lecture seule)",
                  f"    Then {check_expression(client_name)}", "      | type | entite | code |"]
        lines += [f"      | {i['type']} | {_cell(i.get('entite'))} | {_cell(i['code'])} |" for i in checks]
    return lines


def commented(lines: list, reason: str) -> list:
    """Scénarios non livrables : gardés en commentaire sous TODO-ENVIRONNEMENT, avec la raison."""
    out = [f"  # {TODO_MARKER} {reason}"] if lines else []
    return out + [f"  # {line.strip()}" if line.strip() else line for line in lines]


def expected_expressions(client_name: str, checks: list, recreations: list) -> list:
    return ([check_expression(client_name)] if checks else []) + (
        [recreation_expression(client_name)] if recreations else [])


def check_steps(worktree: str, client_name: str, checks: list, recreations: list, std_expressions) -> list:
    """
    Contrôles de la classe écrite par Claude : chaque expression attendue définie une fois dans le projet, absente
    du Standard ; méthode non vide, sans PendingException ; contrôle en lecture seule (aucun appel POST, PUT, PATCH
    ni DELETE dans sa méthode). Une erreur par écart.
    """
    expected = expected_expressions(client_name, checks, recreations)
    root = os.path.join(worktree, "src", "test", "java")
    texts = [_read_text_file(os.path.join(d, n)) for d, _, names in os.walk(root) for n in names
             if n.endswith(".java")] if os.path.isdir(root) else []
    defined = [e for text in texts for e in step_expressions(text)[0]]
    errors = []
    for expression in expected:
        if defined.count(expression) != 1:
            errors.append(f"step « {expression} » défini {defined.count(expression)} fois (attendu : une fois)")
        if expression in set(std_expressions):
            errors.append(f"step « {expression} » déjà défini par le Standard : nom à rendre unique")
    for text in texts:
        for method in _java_methods(text):
            if not set(method["steps"]) & set(expected):
                continue
            if "PendingException" in method["body"] or not method["masked"].strip("{} \n\r\t"):
                errors.append(f"méthode {method['name']} vide ou en attente (PendingException) : à implémenter")
            if check_expression(client_name) in method["steps"] and re.search(
                    r"\b(?:post|put|patch|delete)\s*\(|\bHttpMethod\.(?:POST|PUT|PATCH|DELETE)\b", method["body"]):
                errors.append(f"méthode {method['name']} : le contrôle de l'environnement doit être en lecture "
                              f"seule (aucune écriture)")
    return errors


def prompt_items(checks: list, recreations: list) -> tuple:
    """(contrôles, recréations) présentés au prompt d'écriture des steps."""
    return (json.dumps(checks, ensure_ascii=False, indent=1) if checks else "aucun",
            "\n".join(f"- {r}" for r in recreations) or "aucune")
