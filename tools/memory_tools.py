import difflib
import glob
import json
import os
import re
from datetime import datetime

MEMORY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp", "memory")

CAPITALIZATION_HEADER = "=== CAPITALISATION ET RETOURS D'EXPÉRIENCE ==="
CAPITALIZATION_FOOTER = "=== FIN CAPITALISATION ==="

# Mots trop fréquents pour discriminer deux tickets
_STOPWORDS = {
    "pour", "dans", "avec", "sans", "sont", "être", "cette", "cela", "elle", "ils", "leur", "leurs",
    "plus", "moins", "afin", "lors", "dont", "entre", "comme", "mais", "donc", "aussi", "tout", "tous",
    "toutes", "faut", "doit", "peut", "ticket", "cadrage", "analyse", "partie", "fonctionnelle",
    "technique", "objectif", "impacts", "impact", "règles", "gestion", "scénarios", "fichiers",
    "composants", "modifications", "stratégie", "test", "tests", "this", "that", "with", "from",
    "true", "false", "null", "return", "public", "private", "class", "import", "string", "void",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_memory_dir() -> str:
    """S'assure que le dossier de mémoire existe et renvoie son chemin."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    return MEMORY_DIR


def _get_ticket_memory_path(issue_key: str) -> str:
    """Renvoie le chemin du fichier JSON de mémoire pour un ticket donné."""
    return os.path.join(get_memory_dir(), f"{issue_key.strip().upper()}.json")


def _write_memory(data: dict) -> None:
    """Écriture atomique : un crash en cours d'écriture ne corrompt pas le JSON existant."""
    path = _get_ticket_memory_path(data["issue_key"])
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _empty_record(issue_key: str, project_key: str = "") -> dict:
    key = issue_key.strip().upper()
    return {
        "issue_key": key,
        "project_key": (project_key or key.split("-")[0]).strip().upper(),
        "created_at": _now(),
        "updated_at": _now(),
        "status": "nouveau",  # nouveau -> cadrage -> révisé -> validé
        "summary": "",
        "components": [],
        "initial_context": {"description": "", "epic": "", "costs": ""},
        "initial_cadrage": "",
        "feedbacks": [],
        "final_cadrage": "",
        "patch_info": {},
    }


def load_ticket_memory(issue_key: str, project_key: str = "") -> dict:
    """Charge les données de mémoire d'un ticket (ou un enregistrement vierge)."""
    path = _get_ticket_memory_path(issue_key)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Migration des enregistrements V1 (initial_context était une chaîne)
            base = _empty_record(issue_key, project_key)
            base.update(data)
            if not isinstance(base["initial_context"], dict):
                base["initial_context"] = {"description": str(base["initial_context"] or ""), "epic": "", "costs": ""}
            return base
        except Exception:
            pass
    return _empty_record(issue_key, project_key)


def _apply_jira_context(data: dict, jira_context: dict) -> None:
    """Copie le besoin initial (description + Epic + COST) issu de get_full_jira_issue_context."""
    if not jira_context or jira_context.get("error"):
        return
    data["summary"] = jira_context.get("summary") or data["summary"]
    data["components"] = jira_context.get("components") or data["components"]
    epic = jira_context.get("epic") or {}
    costs = jira_context.get("costs") or []
    data["initial_context"] = {
        "description": jira_context.get("description", ""),
        "epic": f"[{epic.get('key', '')}] {epic.get('summary', '')}\n{epic.get('description', '')}".strip() if epic else "",
        "costs": "\n\n".join(f"[{c.get('key', '')}] {c.get('summary', '')}\n{c.get('description', '')}" for c in costs),
    }


def _short_diff(before: str, after: str, max_chars: int = 2000) -> str:
    """Diff unifié compact entre deux versions de cadrage (seules les lignes modifiées)."""
    lines = [
        l for l in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if (l.startswith("+") or l.startswith("-")) and not l.startswith(("+++", "---"))
    ]
    diff = "\n".join(lines)
    return diff if len(diff) <= max_chars else diff[:max_chars] + "\n[...]"


# ---------------------------------------------------------------------------
# Enregistrement (cadrage / refine / patch)
# ---------------------------------------------------------------------------

def save_cadrage_memory(issue_key: str, project_key: str, cadrage_text: str = "", jira_context: dict = None) -> None:
    """Enregistre le besoin initial et le cadrage généré par l'agent (réinitialise le cadrage final)."""
    data = load_ticket_memory(issue_key, project_key)
    data["project_key"] = project_key.strip().upper()
    _apply_jira_context(data, jira_context)
    if cadrage_text:
        data["initial_cadrage"] = cadrage_text
        data["final_cadrage"] = cadrage_text
        data["status"] = "cadrage"
    data["updated_at"] = _now()
    _write_memory(data)


def save_refine_feedback(issue_key: str, feedback_text: str, previous_cadrage: str = "",
                         updated_cadrage: str = "", source: str = "consigne") -> None:
    """
    Enregistre un retour utilisateur.
    source = "consigne" (texte saisi pour l'agent) ou "edition_manuelle" (modification directe du cadrage).
    """
    data = load_ticket_memory(issue_key)
    entry = {"timestamp": _now(), "source": source, "feedback": (feedback_text or "").strip()}
    if previous_cadrage and updated_cadrage and previous_cadrage != updated_cadrage:
        entry["diff"] = _short_diff(previous_cadrage, updated_cadrage)
    if entry["feedback"] or entry.get("diff"):
        data["feedbacks"].append(entry)
    if updated_cadrage:
        data["final_cadrage"] = updated_cadrage
        data["status"] = "révisé"
    data["updated_at"] = _now()
    _write_memory(data)


def record_manual_edit(issue_key: str, before: str, after: str) -> bool:
    """Capitalise les corrections faites à la main dans l'IHM avant refine/patch. Renvoie True si un écart a été mémorisé."""
    if not before or not after or before.strip() == after.strip():
        return False
    save_refine_feedback(issue_key, "Correction manuelle du cadrage par l'utilisateur",
                         previous_cadrage=before, updated_cadrage=after, source="edition_manuelle")
    return True


def _parse_patch(patch_path: str) -> dict:
    """Extrait les fichiers modifiés et le volume de lignes d'un fichier .patch Git."""
    files, added, removed = [], 0, 0
    try:
        with open(patch_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
                if m:
                    files.append(m.group(2))
                elif line.startswith("+") and not line.startswith("+++"):
                    added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    removed += 1
    except OSError:
        pass
    return {"files": files, "added": added, "removed": removed}


def _extract_recap(agent_output: str, max_chars: int = 1500) -> str:
    """Conserve la fin de la sortie de l'agent (récapitulatif des modifications)."""
    text = (agent_output or "").strip()
    return text if len(text) <= max_chars else "[...]\n" + text[-max_chars:]


def save_patch_memory(issue_key: str, patch_paths: list = None, agent_output: str = "",
                      final_cadrage: str = "", jira_context: dict = None) -> None:
    """Indexe le livrable final (cadrage validé + patch) comme référence pour les prochains tickets."""
    data = load_ticket_memory(issue_key)
    if not data["summary"]:
        # Patch généré directement, sans phase cadrage : on récupère quand même le besoin initial
        _apply_jira_context(data, jira_context)
    modified_files, added, removed = [], 0, 0
    for path in patch_paths or []:
        parsed = _parse_patch(path)
        modified_files.extend(f for f in parsed["files"] if f not in modified_files)
        added += parsed["added"]
        removed += parsed["removed"]
    data["patch_info"] = {
        "timestamp": _now(),
        "patch_files": [os.path.basename(p) for p in patch_paths or []],
        "modified_files": modified_files,
        "lines_added": added,
        "lines_removed": removed,
        "summary": _extract_recap(agent_output),
    }
    if final_cadrage:
        data["final_cadrage"] = final_cadrage
    data["status"] = "validé"
    data["updated_at"] = _now()
    _write_memory(data)


# ---------------------------------------------------------------------------
# Recherche de tickets similaires
# ---------------------------------------------------------------------------

def _tokens(text: str) -> set:
    words = re.findall(r"[A-Za-zÀ-ÿ_][A-Za-zÀ-ÿ0-9_]{3,}", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS}


# Les cadrages n'utilisent pas toujours exactement les titres du gabarit : synonymes par section
SECTION_KEYWORDS = {
    "diagnostic": ("Diagnostic", "Cause"),
    "fichiers": ("Composants & Fichiers", "Fichiers concernés", "Fichiers impactés", "Composants impactés"),
}


def _extract_section(markdown: str, section: str, max_chars: int = 800) -> str:
    """
    Renvoie le bloc sous le premier titre (#..###) ou puce '- **' correspondant à `section`,
    jusqu'au titre de niveau égal ou supérieur suivant.
    """
    keywords = [k.lower() for k in SECTION_KEYWORDS.get(section, (section,))]
    lines = (markdown or "").splitlines()
    start, level = None, 0
    # Les titres Markdown priment ; une puce '- **Titre**' (gabarit du prompt) sert de repli
    for pattern in (r"^(#{1,6}) ", r"^- \*\*[^*:]+\*\*\s*:?\s*$|^- \*\*[^*]{0,60}\*\*\s*:"):
        for i, line in enumerate(lines):
            m = re.match(pattern, line)
            title = line.split("**")[1] if line.startswith("- **") and line.count("**") >= 2 else line
            if m and any(k in title.lower() for k in keywords):
                start = i
                level = len(m.group(1)) if pattern.startswith("^(#") else 99
                break
        if start is not None:
            break
    if start is None:
        return ""
    out = [lines[start]]
    for line in lines[start + 1:]:
        heading = re.match(r"^(#{1,6}) ", line)
        if heading and len(heading.group(1)) <= level:
            break
        if level == 99 and (heading or line.startswith("- **")):
            break
        out.append(line)
    text = "\n".join(out).strip()
    return text if len(text) <= max_chars else text[:max_chars] + " [...]"


def _memory_text(mem: dict) -> str:
    ctx = mem.get("initial_context") or {}
    return " ".join([
        mem.get("summary", ""),
        " ".join(mem.get("components", [])),
        ctx.get("description", "") if isinstance(ctx, dict) else str(ctx),
        _extract_section(mem.get("final_cadrage", ""), "fichiers", 3000),
        _extract_section(mem.get("final_cadrage", ""), "diagnostic", 3000),
        " ".join(mem.get("patch_info", {}).get("modified_files", [])),
    ])


def _load_all() -> list:
    memories = []
    for filepath in glob.glob(os.path.join(get_memory_dir(), "*.json")):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                memories.append(load_ticket_memory(json.load(f)["issue_key"]))
        except Exception:
            continue
    return memories


def _as_list(component_name) -> list:
    if not component_name:
        return []
    if isinstance(component_name, str):
        component_name = component_name.split(",")
    return [c.strip().lower() for c in component_name if c and c.strip()]


def find_similar_tickets(project_key: str, component_name=None, query_text: str = "",
                         exclude_issue_key: str = "", max_results: int = 3) -> list:
    """
    Classe les tickets mémorisés du même projet par pertinence :
    composant commun, recouvrement lexical (titre, description, fichiers impactés), statut validé, récence.
    Renvoie une liste de dicts {"memory", "score", "reasons"}.
    """
    project = (project_key or "").strip().upper()
    exclude = (exclude_issue_key or "").strip().upper()
    components = set(_as_list(component_name))
    query_tokens = _tokens(query_text)

    scored = []
    for mem in _load_all():
        if mem["issue_key"] == exclude or (project and mem["project_key"] != project):
            continue
        if not (mem.get("final_cadrage") or mem.get("feedbacks") or mem.get("patch_info")):
            continue

        score, reasons = 0.0, []
        common = components & {c.lower() for c in mem.get("components", [])}
        if common:
            score += 3 * len(common)
            reasons.append(f"composant {', '.join(sorted(common))}")
        if query_tokens:
            mem_tokens = _tokens(_memory_text(mem))
            overlap = query_tokens & mem_tokens
            if overlap:
                score += 10 * len(overlap) / len(query_tokens | mem_tokens)
                reasons.append(f"termes communs : {', '.join(sorted(overlap)[:6])}")
        if mem.get("status") == "validé":
            score += 1
            reasons.append("patch validé")
        if mem.get("feedbacks"):
            score += 0.5
        scored.append({"memory": mem, "score": round(score, 2), "reasons": reasons})

    scored.sort(key=lambda s: (s["score"], s["memory"].get("updated_at", "")), reverse=True)
    if components or query_tokens:
        # Avec des critères de recherche, on écarte les tickets sans aucun lien
        scored = [s for s in scored if any(r.startswith(("composant", "termes")) for r in s["reasons"])]
    return scored[:max_results]


def get_project_lessons(project_key: str, exclude_issue_key: str = "", max_items: int = 8) -> list:
    """Consignes utilisateur récentes du projet (toutes tâches confondues), dédoublonnées."""
    project = (project_key or "").strip().upper()
    exclude = (exclude_issue_key or "").strip().upper()
    items = []
    for mem in _load_all():
        if mem["project_key"] != project or mem["issue_key"] == exclude:
            continue
        for fb in mem.get("feedbacks", []):
            if fb.get("source", "consigne") == "consigne" and fb.get("feedback"):
                items.append((fb.get("timestamp", ""), mem["issue_key"], fb["feedback"]))
    items.sort(reverse=True)
    seen, lessons = set(), []
    for _, key, text in items:
        if text.lower() not in seen:
            seen.add(text.lower())
            lessons.append(f"[{key}] {text}")
    return lessons[:max_items]


def get_capitalized_experience(project_key: str, component_name=None, query_text: str = "",
                               exclude_issue_key: str = "", max_results: int = 3, max_chars: int = 8000) -> str:
    """
    Construit le bloc à injecter dans le prompt de Claude Code.
    Renvoie une chaîne vide si aucune expérience exploitable n'existe.
    """
    lessons = get_project_lessons(project_key, exclude_issue_key)
    similar = find_similar_tickets(project_key, component_name, query_text, exclude_issue_key, max_results)
    if not lessons and not similar:
        return ""

    parts = [
        CAPITALIZATION_HEADER,
        "Ces éléments proviennent de tickets déjà traités sur ce projet. Ce sont des REPÈRES, pas des vérités :",
        "vérifie toujours le code actuel avant de réutiliser une approche, et applique en priorité les consignes utilisateur.",
        "",
    ]
    if lessons:
        parts.append("## Consignes récurrentes de l'utilisateur (à respecter)")
        parts.extend(f"- {l}" for l in lessons)
        parts.append("")

    for item in similar:
        mem = item["memory"]
        parts.append(f"## Ticket similaire {mem['issue_key']} — {mem.get('summary') or 'sans titre'} "
                     f"(statut : {mem.get('status')}, pertinence : {', '.join(item['reasons']) or 'récence'})")
        corrections = mem.get("feedbacks", [])
        if corrections:
            parts.append("- Corrections apportées par l'utilisateur sur l'analyse de l'agent :")
            for fb in corrections[-3:]:
                parts.append(f"  - ({fb.get('source', 'consigne')}) {fb.get('feedback', '')}")
                if fb.get("diff"):
                    parts.append("    ```diff\n" + fb["diff"][:600] + "\n    ```")
        for keyword in ("diagnostic", "fichiers"):
            section = _extract_section(mem.get("final_cadrage", ""), keyword, 600)
            if section:
                parts.append(section)
        patch = mem.get("patch_info") or {}
        if patch.get("modified_files"):
            files = patch["modified_files"]
            parts.append(f"- Fichiers modifiés par le patch validé : {', '.join(files[:15])}"
                         + (f" (+{len(files) - 15} autres)" if len(files) > 15 else ""))
        parts.append("")

    parts.append(CAPITALIZATION_FOOTER)
    block = "\n".join(parts)
    if len(block) > max_chars:
        block = block[:max_chars] + f"\n[... tronqué ...]\n{CAPITALIZATION_FOOTER}"
    return block


# ---------------------------------------------------------------------------
# Administration (IHM)
# ---------------------------------------------------------------------------

def get_all_memories() -> list:
    """Renvoie la liste résumée de toutes les données mémorisées."""
    results = [{
        "issue_key": m["issue_key"],
        "project_key": m["project_key"],
        "summary": m.get("summary", ""),
        "components": ", ".join(m.get("components", [])),
        "status": m.get("status", ""),
        "updated_at": m.get("updated_at", ""),
        "feedbacks_count": len(m.get("feedbacks", [])),
        "has_cadrage": bool(m.get("final_cadrage")),
        "has_patch": bool(m.get("patch_info", {}).get("patch_files")),
    } for m in _load_all()]
    results.sort(key=lambda x: x["updated_at"], reverse=True)
    return results


def delete_ticket_memory(issue_key: str) -> bool:
    path = _get_ticket_memory_path(issue_key)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def clear_memory(project_key: str = "") -> int:
    """Supprime la mémoire d'un projet (ou toute la mémoire si project_key est vide)."""
    count = 0
    for mem in _load_all():
        if not project_key or mem["project_key"] == project_key.strip().upper():
            count += delete_ticket_memory(mem["issue_key"])
    return count


def import_existing_deliverables(workspace_dir: str) -> int:
    """
    Amorce la mémoire avec les livrables déjà présents dans le workspace
    (<TICKET>-cadrage.md et <TICKET>-*.patch). Ne remplace pas un ticket déjà mémorisé.
    """
    imported = 0
    for cadrage_path in glob.glob(os.path.join(workspace_dir, "*-cadrage.md")):
        issue_key = os.path.basename(cadrage_path)[:-len("-cadrage.md")].upper()
        if os.path.exists(_get_ticket_memory_path(issue_key)):
            continue
        with open(cadrage_path, "r", encoding="utf-8", errors="replace") as f:
            cadrage = f.read()
        save_cadrage_memory(issue_key, issue_key.split("-")[0], cadrage_text=cadrage)
        patches = glob.glob(os.path.join(workspace_dir, f"{issue_key}-*.patch"))
        if patches:
            save_patch_memory(issue_key, patches, agent_output="Importé depuis l'historique local", final_cadrage=cadrage)
        imported += 1
    return imported
