"""
Leçons de test du client (0 token) : chaque cause PROUVÉE et corrigée pendant la correction d'un lot devient une
leçon du client, gardée d'une génération à l'autre (.tmp/cucumber/<client>/lecons.json), avec sa preuve et la règle
à appliquer.

- Création : une correction donne « cause_prouvee » et « lecon » (règle, contrôle facultatif) ; la leçon est
  confirmée quand le run suivant ne montre plus cette cause (scénario vert, ou échec qui a changé).
- Injection : bloc « LEÇONS DU CLIENT » dans les prompts de génération et de correction de tous les lots suivants.
- Contrôles déterministes après chaque lot (types CONTROL_TYPES) : une leçon violée est une erreur de fichier.
- Récidive : une cause prouvée qui correspond à une leçon connue est une erreur de l'agent (« leçon déjà connue,
  n° … »), au journal et au bilan.
Aucune valeur propre à un client dans ce module : les leçons sont des données du client.
"""
import json
import os
import re
from datetime import datetime

FILE_NAME = "lecons.json"
FORMAT = 1
MAX_PROMPT_LESSONS = 40
# Contrôle déterministe : type -> champs requis
CONTROL_TYPES = {
    "colonne_interdite": ("step", "colonne"),      # step dont la table ne doit pas porter cette colonne
    "colonne_requise": ("step", "colonne"),        # step dont la table doit porter cette colonne
    "valeur_prefixe": ("step", "colonne", "prefixe"),  # valeurs de la colonne qui commencent par ce préfixe
    "motif_java_interdit": ("motif",),             # expression régulière interdite dans les classes Java du lot
}
_STEP_LINE = re.compile(r"^\s*(?:Given|When|Then|And|But|\*|Soit|Quand|Alors|Et|Mais)\s+(.*?)\s*$")


def _path(client: str) -> str:
    from tools import cucumber_tools
    return os.path.join(cucumber_tools.cache_dir(client), FILE_NAME)


def load(client: str) -> list:
    try:
        with open(_path(client), encoding="utf-8") as f:
            data = json.load(f)
        return [lesson for lesson in data.get("lecons") or [] if isinstance(lesson, dict)]
    except (OSError, ValueError, AttributeError):
        return []


def save(client: str, lessons: list) -> None:
    os.makedirs(os.path.dirname(_path(client)), exist_ok=True)
    with open(_path(client), "w", encoding="utf-8") as f:
        json.dump({"format": FORMAT, "lecons": lessons}, f, ensure_ascii=False, indent=2)


def _flat(value, limit: int = 600) -> str:
    return " ".join(str(value or "").split())[:limit]


def valid_control(control) -> dict:
    """Contrôle déterministe proposé, gardé s'il est d'un type connu avec tous ses champs (et un motif valide)."""
    if not isinstance(control, dict) or control.get("type") not in CONTROL_TYPES:
        return {}
    fields = CONTROL_TYPES[control["type"]]
    out = {"type": control["type"], **{f: _flat(control.get(f), 300) for f in fields}}
    if not all(out[f] for f in fields):
        return {}
    if control["type"] == "motif_java_interdit":
        try:
            re.compile(out["motif"])
        except re.error:
            return {}
    return out


def lesson_from_fix(fix_correction: dict) -> dict:
    """
    Leçon candidate d'une correction ({tag, cause, correction, cause_prouvee, lecon: {regle, controle}}) : seulement
    avec une cause prouvée et une règle. {} sinon.
    """
    proven = fix_correction.get("cause_prouvee") if isinstance(fix_correction, dict) else None
    lesson = fix_correction.get("lecon") if isinstance(fix_correction, dict) else None
    if not isinstance(proven, dict) or not str(proven.get("erreur", "")).strip() or not isinstance(lesson, dict) \
            or not _flat(lesson.get("regle")):
        return {}
    from tools import cucumber_fix_budget
    return {"cause": _flat(fix_correction.get("cause")), "preuve": _flat(proven.get("erreur")),
            "step": _flat(proven.get("step"), 200), "regle": _flat(lesson.get("regle")),
            "controle": valid_control(lesson.get("controle")),
            "cle": cucumber_fix_budget.proven_causes({"corrections": [{**fix_correction, "tag": "x"}]}).get("x", ""),
            "erreur_cle": error_key(proven.get("erreur"), proven.get("step"))}


def error_key(error, step) -> str:
    """Clé d'une erreur serveur sur un step (valeurs variables masquées) : sert à reconnaître une récidive."""
    from tools import cucumber_fix_budget
    return (cucumber_fix_budget._volatile_free(str(error or "")) + " | " + " ".join(str(step or "").lower().split())
            if str(error or "").strip() else "")


def known(lessons: list, key: str) -> dict:
    """Leçon déjà connue pour cette clé de cause prouvée, {} sinon."""
    return next((lesson for lesson in lessons if key and lesson.get("cle") == key), {})


def match(lessons: list, fix_correction: dict) -> dict:
    """
    Leçon connue qui couvre la cause prouvée d'une correction : même clé de cause, ou même erreur serveur (valeurs
    masquées) sur le même step. {} sinon.
    """
    proven = fix_correction.get("cause_prouvee") if isinstance(fix_correction, dict) else None
    if not isinstance(proven, dict) or not str(proven.get("erreur", "")).strip():
        return {}
    from tools import cucumber_fix_budget
    key = cucumber_fix_budget.proven_causes({"corrections": [{**fix_correction, "tag": "x"}]}).get("x", "")
    errors = error_key(proven.get("erreur"), proven.get("step"))
    return next((lesson for lesson in lessons
                 if (key and lesson.get("cle") == key) or (errors and lesson.get("erreur_cle") == errors)), {})


def add(client: str, candidate: dict, origin: dict) -> dict:
    """Leçon confirmée ajoutée (numérotée) ; une leçon de même clé existante est renvoyée telle quelle."""
    lessons = load(client)
    existing = known(lessons, candidate.get("cle", "")) or next(
        (item for item in lessons if candidate.get("erreur_cle") and item.get("erreur_cle") == candidate["erreur_cle"]),
        {})
    if existing:
        return existing
    lesson = {"numero": max((int(item.get("numero") or 0) for item in lessons), default=0) + 1, **candidate,
              "date": datetime.now().isoformat(timespec="seconds"), "origine": origin, "recidives": []}
    save(client, lessons + [lesson])
    return lesson


def record_recurrence(client: str, numero: int, origin: dict) -> None:
    lessons = load(client)
    for lesson in lessons:
        if lesson.get("numero") == numero:
            lesson.setdefault("recidives", []).append({**origin, "date": datetime.now().isoformat(timespec="seconds")})
    save(client, lessons)


def prompt_block(client: str) -> str:
    """Bloc des prompts de génération et de correction : règles apprises sur ce client, à appliquer d'office."""
    lessons = load(client)[-MAX_PROMPT_LESSONS:]
    if not lessons:
        return ""
    lines = ["", "=== LEÇONS DU CLIENT (causes déjà prouvées et corrigées : applique ces règles d'office) ===",
             "Une erreur couverte par une leçon est une erreur de l'agent (« leçon déjà connue »)."]
    for lesson in lessons:
        lines.append(f"- Leçon n° {lesson['numero']}" + (f" ({lesson['step']})" if lesson.get("step") else "")
                     + f" : {lesson['regle']} — preuve : « {lesson.get('preuve', '')[:200]} »")
    return "\n".join(lines)


def _feature_tables(text: str) -> list:
    """[(step, en-têtes, lignes, n° de ligne)] des steps avec table d'une feature (Background compris)."""
    out, lines = [], text.splitlines()
    for index, line in enumerate(lines):
        found = _STEP_LINE.match(line)
        if not found:
            continue
        rows = []
        for follow in lines[index + 1:]:
            stripped = follow.strip()
            if stripped.startswith("#"):
                continue
            if not stripped.startswith("|"):
                break
            rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
        if rows:
            out.append((found.group(1), rows[0], rows[1:], index + 1))
    return out


def check_files(worktree: str, files: list, lessons: list) -> list:
    """Contrôles déterministes des leçons sur les fichiers du lot : une erreur par leçon violée (0 token)."""
    errors = []
    for rel in files:
        path = os.path.join(worktree, *rel.split("/"))
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        for lesson in lessons:
            control = valid_control(lesson.get("controle"))
            if not control:
                continue
            label = f"{rel} : leçon n° {lesson['numero']} non appliquée — {lesson['regle']}"
            if control["type"] == "motif_java_interdit":
                if rel.endswith(".java") and re.search(control["motif"], text):
                    errors.append(label)
                continue
            if not rel.endswith(".feature"):
                continue
            for step, header, rows, line in _feature_tables(text):
                if step.strip() != control["step"]:
                    continue
                if control["type"] == "colonne_interdite" and control["colonne"] in header:
                    errors.append(f"{label} (l. {line})")
                elif control["type"] == "colonne_requise" and control["colonne"] not in header:
                    errors.append(f"{label} (l. {line})")
                elif control["type"] == "valeur_prefixe" and control["colonne"] in header:
                    column = header.index(control["colonne"])
                    bad = [r[column] for r in rows if column < len(r) and r[column]
                           and not r[column].startswith(control["prefixe"]) and not r[column].startswith("$")]
                    if bad:
                        errors.append(f"{label} (l. {line} : {', '.join(bad[:3])})")
    return errors
