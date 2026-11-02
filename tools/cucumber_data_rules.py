"""
Données des features (0 token), contrôlées sur TOUTES les features exécutées par la suite du client
(cucumber_checks.suite_features), pas seulement les nouvelles :

- Données d'une exécution à l'autre : toute donnée créée par un test fonctionne à CHAQUE exécution. Un code fixe
  créé par un step qui refuse un doublon (« Client existe déjà avec code … ») sans mécanisme d'idempotence est une
  erreur de lot. Mécanismes reconnus : suppression préalable du même code dans le même déroulé (Background puis
  scénario), création dans l'init du client (une fois par run), code unique par exécution (valeur calculée,
  ${…} ou <…>, par un step projet qui respecte la longueur maximale du modèle). Création dans le Background : elle
  est rejouée avant CHAQUE scénario, le 2e scénario échoue déjà.
  Créations qui refusent un doublon, sur preuve : brouillons intégrés par l'écran dont l'événement a été refusé en
  doublon (« Client existe déjà » : save-customer) et steps sous lesquels une console importée montre « existe
  déjà » / « already exists » ; relevés à chaque import (etapes_non_idempotentes.json du client).
- Données créées avant d'être utilisées : chaque objet désigné par un code propre à un ticket (UE-SPEC-1047-1,
  CUST-TOW-554-1, OP TOW-554-1…) est créé par la feature (Background, fichier de données qu'elle intègre, step de
  création) ou par l'init du client. Sinon, erreur de lot : la feature dépend d'une autre ou d'un état laissé.
"""
import json
import os
import re

from tools.cucumber_checks import (FEATURES_ROOT, _cells, _is_data_file, data_references, repo_files,
                                   resolved_path)
from tools.cucumber_init_data import init_paths, read_rows
from tools.cucumber_tools import _read_text_file, cache_dir

_STEP = re.compile(r"^\s*(Given|When|Then|And|But|\*|Soit|Sachant que|[EÉ]tant donn[ée]e?s?|Quand|Lorsque|Alors|Et|"
                   r"Mais)\s+(.+?)\s*$")
_BLOCK = re.compile(r"^\s*(Background|Contexte|Scenario Outline|Scenario Template|Scenario|Sc[ée]nario|"
                    r"Plan du sc[ée]nario|Example|Exemple)\s*:\s*(.*)$")
# Code de donnée : segments en majuscules séparés par des tirets, au moins un chiffre (UE-SPEC-1047-1, TOW-554-1)
_DATA_CODE = re.compile(r"(?<![\w-])(?:[A-Z][A-Z0-9]*-)+[A-Z0-9]*\d[A-Z0-9]*(?:-[A-Z0-9]+)*(?![\w-])")
_TICKET_TAG = re.compile(r"@([A-Z][A-Z0-9]+)-\d+\b")
_VARIABLE = re.compile(r"[$<{]")
# Code volontairement inexistant d'un test négatif : il ne doit pas être créé
_ABSENT_ON_PURPOSE = re.compile(r"(?:^|-)(?:INVALIDE?|INCONNUE?|UNKNOWN|INEXISTANTE?|ABSENTE?|FAUX|FAKE|WRONG|"
                                r"NOTFOUND)(?:-|$)")
# Step qui crée (ou intègre) des données : ses cellules et ses fichiers créent les codes qu'ils portent
_CREATION = re.compile(r"cr[ée]ation|cr[ée]er|int[ée]gration|int[ée]grer|\bajout|d[ée]claration|\ble stock suivant|"
                       r"suivante?s?\s*$", re.I)
# Step qui supprime ou purge : mécanisme d'idempotence s'il précède la création du même code
_DELETION = re.compile(r"suppr|purge|nettoy|effacer|\bdelete\b", re.I)
# Brouillon intégré par l'écran : son événement (première ligne du fichier) dit s'il refuse un doublon
SCREEN_DRAFT_STEP = "INIT- Intégration des fichiers via l'écran"
# Console : message de doublon puis step du Standard qui l'a levé (at ✽.<step>(…))
_DUPLICATE = re.compile(r"existe d[ée]j[àa]|already exists?|duplicate key|d[ée]j[àa] existant", re.I)
_STEP_AT = re.compile(r"at ✽\.(.+?)\((?:file:|classpath:)")
NOT_IDEMPOTENT_FILE = "etapes_non_idempotentes.json"


# ---------------------------------------------------------------------------
# Lecture d'une feature : steps dans l'ordre, avec leur bloc, leurs arguments et leurs tables
# ---------------------------------------------------------------------------

def parse_steps(text: str) -> list:
    """[{ligne, bloc (titre du Background ou du scénario), background, step, args, colonnes, lignes}] (doc strings
    et commentaires ignorés)."""
    steps, block, background, in_doc, current = [], "", False, False, None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(('"""', "```")):
            in_doc = not in_doc
            continue
        if in_doc or not stripped or stripped.startswith(("#", "@")):
            continue
        match = _BLOCK.match(line)
        if match:
            background = match.group(1) in ("Background", "Contexte")
            block, current = ("Background" if background else match.group(2).strip()), None
            continue
        match = _STEP.match(line)
        if match:
            step = match.group(2)
            current = {"ligne": number, "bloc": block, "background": background, "step": step,
                       "args": [a or b for a, b in re.findall(r"\"([^\"]*)\"|'([^']*)'", step)],
                       "colonnes": None, "lignes": []}
            steps.append(current)
            continue
        if stripped.startswith("|") and current is not None:
            cells = _cells(stripped)
            if current["colonnes"] is None:
                current["colonnes"] = cells
            else:
                current["lignes"].append(dict(zip(current["colonnes"], cells)))
        elif not stripped.startswith("|"):
            current = None
    return steps


def _codes(values) -> set:
    """Codes de données des valeurs (noms de fichiers et valeurs calculées exclus)."""
    out = set()
    for value in values:
        value = str(value or "")
        if value and not _is_data_file(value) and not _VARIABLE.search(value):
            out.update(_DATA_CODE.findall(value))
    return out


def _file_codes(text: str) -> set:
    """Codes de données du contenu d'un fichier (balises XML et séparateurs CSV compris)."""
    return set(_DATA_CODE.findall(text or ""))


def step_values(step: dict) -> list:
    """Arguments et cellules d'un step, hors colonne fileName (fichier lu, pas une donnée)."""
    cells = [v for row in step["lignes"] for k, v in row.items() if k.lower() != "filename"]
    return step["args"] + cells


def _file_text(worktree: str, rel: str) -> str:
    path = os.path.join(worktree, *rel.split("/"))
    if rel.lower().endswith((".xlsx", ".xlsm")):
        try:
            return "\n".join(";".join(row) for row in read_rows(path))
        except (OSError, ValueError):
            return ""
    return _read_text_file(path) if os.path.exists(path) else ""


def step_files(worktree: str, rel: str, step: dict, present: set) -> list:
    """Fichiers de données (présents) intégrés par un step de la feature `rel`."""
    text = "Feature: x\n  Scenario: x\n    Given " + step["step"] + "\n" + "".join(
        "      | " + " | ".join(step["colonnes"]) + " |\n" + "".join(
            "      | " + " | ".join(row.get(c, "") for c in step["colonnes"]) + " |\n" for row in step["lignes"])
        for _ in [0] if step["colonnes"])
    return [p for p in (resolved_path(rel, r) for r in data_references(text)) if p in present]


def ticket_keys(worktree: str) -> set:
    """Clés de projet des tickets cités en tag dans les features du dépôt (SPEC, TOW…)."""
    keys = set()
    for rel in repo_files(worktree):
        if rel.endswith(".feature") and os.path.exists(os.path.join(worktree, *rel.split("/"))):
            keys.update(_TICKET_TAG.findall(_read_text_file(os.path.join(worktree, *rel.split("/")))))
    return keys


def _ticket_code(code: str, keys: set) -> bool:
    return any(re.search(rf"(?<![A-Z0-9]){re.escape(k)}-\d", code) for k in keys)


# ---------------------------------------------------------------------------
# Créations qui refusent un doublon : relevées dans les consoles importées (preuve), plus la base connue
# ---------------------------------------------------------------------------

# Événements de brouillon refusant un doublon relevés sur un run réel (build 241 : « Client existe déjà avec code »)
KNOWN_EVENTS = ("ref-organization-event-save-customer-request",)
_DRAFT_FILE = re.compile(r"([\w.-]+\.(?:csv|xml|xlsm|xlsx|json))\s*/\s*INTEGRATED_WITH_ERROR", re.I)


def learned_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), NOT_IDEMPOTENT_FILE)


def not_idempotent(client_name: str = "") -> dict:
    """{"steps": [...], "evenements": [...]} des créations qui refusent un doublon (base + relevés du client)."""
    learned = {}
    if client_name and os.path.exists(learned_path(client_name)):
        with open(learned_path(client_name), encoding="utf-8") as f:
            learned = json.load(f)
    return {"steps": list(dict.fromkeys(learned.get("steps") or [])),
            "evenements": list(dict.fromkeys(list(KNOWN_EVENTS) + (learned.get("evenements") or [])))}


def draft_event(text: str) -> str:
    """Événement d'un brouillon intégré par l'écran : première cellule de sa première ligne."""
    first = (text or "").lstrip("﻿").split("\n", 1)[0]
    return re.split(r"[;,\t]", first, maxsplit=1)[0].strip()


def duplicate_refusals(console: str) -> dict:
    """
    Refus de doublon montrés par la console : {"steps": steps du Standard qui les lèvent, "fichiers": brouillons
    intégrés en erreur « existe déjà »}.
    """
    lines, steps, files = console.splitlines(), [], []
    for index, line in enumerate(lines):
        if not _DUPLICATE.search(line):
            continue
        draft = _DRAFT_FILE.search(line)
        if draft:
            files.append(draft.group(1))
            continue
        for following in lines[index + 1:index + 15]:
            match = _STEP_AT.search(following)
            if match:
                steps.append(match.group(1).strip())
                break
    return {"steps": list(dict.fromkeys(steps)), "fichiers": list(dict.fromkeys(files))}


def learn_from_console(client_name: str, console: str, worktree: str = "") -> dict:
    """
    Ajoute aux créations du client qui refusent un doublon celles que montre `console` (événement du brouillon lu
    dans le worktree, sinon step) ; renvoie {"steps", "evenements"} nouveaux.
    """
    found, known = duplicate_refusals(console), not_idempotent(client_name)
    events = []
    for name in found["fichiers"]:
        path = next((p for p in (repo_files(worktree) if worktree and os.path.isdir(worktree) else ())
                     if p.rsplit("/", 1)[-1] == name), "")
        event = draft_event(_file_text(worktree, path)) if path else ""
        if event:
            events.append(event)
    new = {"steps": [s for s in found["steps"] if s not in known["steps"] and not s.startswith(SCREEN_DRAFT_STEP)],
           "evenements": [e for e in dict.fromkeys(events) if e not in known["evenements"]]}
    if new["steps"] or new["evenements"]:
        os.makedirs(cache_dir(client_name), exist_ok=True)
        with open(learned_path(client_name), "w", encoding="utf-8") as f:
            json.dump({"steps": known["steps"] + new["steps"],
                       "evenements": [e for e in known["evenements"] if e not in KNOWN_EVENTS] + new["evenements"]},
                      f, ensure_ascii=False, indent=2)
    return new


def _refuses_duplicate(step: dict, files_text: list, known: dict) -> bool:
    name = step["step"]
    if name.startswith(SCREEN_DRAFT_STEP):
        return any(draft_event(text) in known["evenements"] for text in files_text)
    return any(name == s or name.startswith(s) for s in known["steps"])


# ---------------------------------------------------------------------------
# Contrôles
# ---------------------------------------------------------------------------

def _init_corpus(worktree: str, init: dict) -> str:
    texts = []
    for feature in (init or {}).get("features") or []:
        texts.append(_read_text_file(os.path.join(worktree, *feature["chemin"].split("/"))))
        texts += [_file_text(worktree, p) for p in feature.get("fichiers") or []]
    return "\n".join(texts)


def check_idempotent_creations(worktree: str, features: list, known: dict = None, init: dict = None) -> list:
    """
    Codes fixes créés par un step qui refuse un doublon, sans suppression préalable du même code dans le même
    déroulé (Background puis scénario) : une erreur par step (la 2e exécution échoue). Features d'init exclues :
    elles s'exécutent une fois par run, après la purge.
    """
    known, present, errors = known or not_idempotent(), repo_files(worktree), []
    excluded = set(init_paths(init or {}))
    for rel in (f for f in features if f not in excluded):
        parsed = parse_steps(_read_text_file(os.path.join(worktree, *rel.split("/"))))
        scenarios = len({s["bloc"] for s in parsed if not s["background"]})
        for index, step in enumerate(parsed):
            files = step_files(worktree, rel, step, present)
            texts = [_file_text(worktree, p) for p in files]
            if not _refuses_duplicate(step, texts, known):
                continue
            created = _codes(step_values(step)) | {c for t in texts for c in _file_codes(t)}
            # Déroulé exécuté avant ce step : Background, puis le début du même scénario
            before = [s for s in parsed[:index] if s["background"] or s["bloc"] == step["bloc"]]
            deleted = {c for s in before if _DELETION.search(s["step"]) for c in _codes(step_values(s))}
            fixed = sorted(created - deleted)
            if not fixed:
                continue
            where = (f" — dans le Background, rejoué avant chacun des {scenarios} scénarios : le 2e échoue déjà"
                     if step["background"] and scenarios > 1 else "")
            errors.append(f"{rel}:{step['ligne']} « {step['step'][:70]} » crée des codes fixes "
                          f"({', '.join(fixed[:4])}{'…' if len(fixed) > 4 else ''}) et refuse un doublon "
                          f"(« existe déjà ») : la 2e exécution échoue{where}. Rends la création idempotente : "
                          f"supprime ces objets juste avant de les créer (step de suppression), ou crée-les une "
                          f"seule fois dans l'init du client, ou un code unique par exécution (longueur maximale "
                          f"du modèle respectée)")
    return errors


def check_created_before_use(worktree: str, features: list, init: dict = None, keys: set = None) -> list:
    """
    Codes propres à un ticket utilisés par une feature sans être créés ni par elle (step de création, fichier de
    données qu'elle intègre) ni par l'init du client : une erreur par code (première ligne d'usage).
    """
    keys = keys if keys is not None else ticket_keys(worktree)
    present, errors = repo_files(worktree), []
    corpus = _init_corpus(worktree, init)
    excluded = set(init_paths(init or {}))
    for rel in (f for f in features if f not in excluded):
        parsed = parse_steps(_read_text_file(os.path.join(worktree, *rel.split("/"))))
        created, used = set(), {}
        for step in parsed:
            files = step_files(worktree, rel, step, present)
            created |= {c for p in files for c in _file_codes(_file_text(worktree, p))}
            values = _codes(step_values(step))
            if _CREATION.search(step["step"]):
                created |= values
            else:
                for code in values:
                    used.setdefault(code, step["ligne"])
        for code, line in sorted(used.items(), key=lambda item: item[1]):
            if code in created or not _ticket_code(code, keys) or _ABSENT_ON_PURPOSE.search(code) or re.search(
                    rf"(?<![\w-]){re.escape(code)}(?![\w-])", corpus):
                continue
            errors.append(f"{rel}:{line} « {code} » est utilisé mais jamais créé : ni par la feature (Background, "
                          f"fichier de données qu'elle intègre, step de création), ni par l'init du client — "
                          f"crée-le dans le Background avec un code propre à la feature (la feature ne doit pas "
                          f"dépendre d'une autre ni d'un run précédent)")
    return errors


def data_errors(worktree: str, features: list, client_name: str = "", init: dict = None) -> dict:
    """Contrôles des données des `features` : {"idempotence": [...], "creation": [...]}."""
    return {"idempotence": check_idempotent_creations(worktree, features, not_idempotent(client_name), init),
            "creation": check_created_before_use(worktree, features, init)}


def errors_by_feature(errors: list) -> dict:
    """{feature: [erreurs]} d'une liste d'erreurs « chemin:ligne … »."""
    out = {}
    for error in errors:
        rel = error.split(":", 1)[0].split(" ", 1)[0]
        if rel.startswith(FEATURES_ROOT):
            out.setdefault(rel, []).append(error)
    return out


def feature_checks(worktree: str, features: list, client_name: str = "", init: dict = None) -> dict:
    """
    Contrôles statiques des `features` qui expliquent un échec : {feature: {"fichiers": [erreurs], "idempotence":
    [erreurs], "non_crees": [codes]}} — fichiers lus absents du dépôt, créations non rejouables, codes jamais créés.
    """
    from tools.cucumber_checks import missing_data_in_tree
    from tools.cucumber_init_data import init_path
    from tools.cucumber_tools import _read_json
    if not worktree or not os.path.isdir(worktree):
        return {}
    if init is None:
        init = _read_json(init_path(client_name), {}) if client_name else {}
    texts = {rel: _read_text_file(os.path.join(worktree, *rel.split("/"))) for rel in features
             if os.path.exists(os.path.join(worktree, *rel.split("/")))}
    out = {}
    for rel, line, name, target in missing_data_in_tree(repo_files(worktree), texts):
        out.setdefault(rel, {}).setdefault("fichiers", []).append(
            f"{rel}:{line} fichier « {name} » absent du dépôt : attendu en {target}")
    for error in check_idempotent_creations(worktree, list(texts), not_idempotent(client_name), init):
        out.setdefault(error.split(":", 1)[0], {}).setdefault("idempotence", []).append(error)
    for error in check_created_before_use(worktree, list(texts), init):
        code = re.search(r"« ([^»]+) »", error)
        if code:
            out.setdefault(error.split(":", 1)[0], {}).setdefault("non_crees", []).append(code.group(1))
    return out
