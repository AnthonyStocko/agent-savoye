"""
Publication des analyses dans Jira, uniquement à la validation de l'utilisateur.

Avant validation, l'analyse n'existe que localement ('<TICKET>-cadrage.md' et mémoire de l'agent). La validation
publie le fichier dans le ticket (déterministe) et enregistre l'empreinte du texte publié : une analyse révisée après
publication ne correspond plus à cette empreinte et redevient un brouillon.

Analyse trop longue pour la description (CONTENT_LIMIT_EXCEEDED), pour tous les projets :
1. Mesure : taille de la description APRÈS conversion au format Jira (document ADF sérialisé), sections existantes
   comprises, comparée à la limite (celle du message d'erreur de Jira, sinon JIRA_DESCRIPTION_LIMIT ou 32 767), avec
   une marge (JIRA_LIMIT_MARGIN).
2. Elle tient : publiée dans la description, comme avant.
3. Sinon : résumé BREF dans la description (besoin, solution, impacts, points ouverts) suivi de SUMMARY_SENTENCE ;
   analyse ORIGINALE, intégrale et non modifiée, en commentaire(s) marqué(s) « [Analyse de l'agent — <TICKET> —
   version du <date>] » (texte Markdown exact dans un bloc de code), découpée aux titres de section en parties
   numérotées si elle dépasse la limite d'un commentaire. À une nouvelle validation, les commentaires d'analyse
   précédents sont remplacés (édités, complétés, les surplus supprimés) : une seule version dans le ticket.
4. Lecture (full_analysis) : le fichier local validé d'abord, sinon les commentaires marqués réassemblés dans l'ordre
   des parties (hors de JIRA_CONTEXT_MAX_CHARS), sinon la description ; partie manquante : IncompleteAnalysis, aucun
   patch sur une analyse incomplète.
"""
import hashlib
import json
import os
import re
from datetime import datetime

from tools import integration_tools
from tools.jira_tools import update_jira_cadrage

PUBLICATION_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp", "publication")
DEFAULT_LIMIT = 32767
DEFAULT_MARGIN = 1500
SUMMARY_SENTENCE = "Analyse complète en commentaire (« Analyse de l'agent »)."
MARKER_PREFIX = "[Analyse de l'agent"
_MARKER = re.compile(r"^\[Analyse de l'agent — (?P<key>[A-Z][A-Z0-9_]*-\d+) — version du (?P<version>[^\]—]+?)"
                     r"(?: — partie (?P<part>\d+)/(?P<total>\d+))?\]")
_LIMIT_IN_ERROR = re.compile(r"(\d{4,6})")
# Sections du résumé : (libellé, mots des titres de l'analyse qui l'alimentent)
SUMMARY_SECTIONS = (("Besoin", ("objectif", "besoin", "perimetre", "contexte", "probleme")),
                    ("Solution retenue", ("solution", "modification", "diagnostic", "cause", "approche")),
                    ("Impacts", ("impact", "regle", "test", "risque")),
                    ("Points ouverts", ("point", "question", "hypothese", "ouvert", "reserve", "a confirmer")))


class IncompleteAnalysis(RuntimeError):
    """Analyse lue dans Jira incomplète (partie de commentaire absente) : aucun patch ne doit en partir."""


def cadrage_path(issue_key: str) -> str:
    return f"{issue_key}-cadrage.md"


def _fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").strip().replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _record_path(issue_key: str) -> str:
    return os.path.join(PUBLICATION_DIR, f"{issue_key}.json")


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def load_record(issue_key: str) -> dict:
    try:
        with open(_record_path(issue_key), "r", encoding="utf-8") as f:
            record = json.load(f)
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        return {}


def record_publication(issue_key: str, text: str, when: datetime = None, **details) -> dict:
    """Mémorise que `text` est la version publiée dans Jira (date, empreinte et forme de la publication)."""
    record = {"empreinte": _fingerprint(text),
              "publiee_le": (when or datetime.now()).isoformat(timespec="minutes"), **details}
    os.makedirs(PUBLICATION_DIR, exist_ok=True)
    with open(_record_path(issue_key), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return record


def status(issue_key: str, text: str = None) -> dict:
    """
    État de l'analyse : {"publiee": bool, "publiee_le": date de la dernière publication ou "", "libelle": texte,
    "message": forme de la publication (résumé + commentaires) ou ""}. `text` : analyse à évaluer (par défaut le
    fichier '<TICKET>-cadrage.md').
    """
    text = _read(cadrage_path(issue_key)) if text is None else text
    record = load_record(issue_key)
    published_at = record.get("publiee_le", "")
    published = bool(text.strip()) and record.get("empreinte") == _fingerprint(text)
    date_label = published_at.replace("T", " à ") if published_at else ""
    if published:
        label = f"Publiée dans Jira le {date_label}"
        if record.get("message"):
            label += f" — {record['message']}"
    elif published_at:
        label = f"Analyse non publiée (brouillon) : révisée depuis sa publication du {date_label}"
    else:
        label = "Analyse non publiée (brouillon)"
    return {"publiee": published, "publiee_le": published_at, "libelle": label,
            "message": record.get("message", "") if published else ""}


def save_draft(issue_key: str, text: str) -> None:
    """Enregistre l'analyse localement, sans rien publier."""
    with open(cadrage_path(issue_key), "w", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------------------
# Mesure, résumé et découpage
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def limit() -> int:
    return _env_int("JIRA_DESCRIPTION_LIMIT", DEFAULT_LIMIT)


def margin() -> int:
    return _env_int("JIRA_LIMIT_MARGIN", DEFAULT_MARGIN)


def adf_size(doc) -> int:
    """Taille d'un champ Jira après conversion : document ADF sérialisé en JSON."""
    return len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))


def limit_from_error(message: str) -> int:
    """Limite citée par une erreur CONTENT_LIMIT_EXCEEDED de Jira (0 si le message n'en donne pas)."""
    if "CONTENT_LIMIT_EXCEEDED" not in (message or "") and "limit" not in (message or "").lower():
        return 0
    numbers = [int(n) for n in _LIMIT_IN_ERROR.findall(message or "") if 1000 <= int(n) <= 1_000_000]
    return min(numbers) if numbers else 0


def is_content_limit(message: str) -> bool:
    return "CONTENT_LIMIT_EXCEEDED" in (message or "") or bool(re.search(r"exceed\w* .{0,40}limit|too (long|large)",
                                                                       message or "", re.I))


def _plain(title: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", title.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def sections(text: str) -> list:
    """[(niveau, titre, lignes)] : préambule (niveau 0) puis chaque section de titre Markdown."""
    out, current = [], [0, "", []]
    fence = False
    for line in (text or "").splitlines():
        if line.strip().startswith("```"):
            fence = not fence
        heading = None if fence else re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            out.append(tuple(current))
            current = [len(heading.group(1)), heading.group(2).strip(), [line]]
        else:
            current[2].append(line)
    out.append(tuple(current))
    return [s for s in out if s[0] or any(line.strip() for line in s[2])]


def _brief(lines: list, budget: int) -> str:
    """Début d'une section (sans son titre), coupé à une ligne entière, au plus `budget` caractères."""
    kept, size = [], 0
    for line in lines[1:]:
        if not line.strip() or line.strip().startswith("```"):
            continue
        line = re.sub(r"^(?:#+|[-*]|\d+\.)\s+", "", line.strip())
        if size + len(line) > budget:
            if not kept:
                kept.append(line[:max(budget - 1, 0)].rstrip() + "…")
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept)


def summary(text: str, issue_key: str, budget: int) -> str:
    """
    Résumé BREF de l'analyse (Markdown, titre « # … » de l'analyse) : besoin reformulé, solution retenue, impacts,
    points ouverts — début des sections correspondantes — puis SUMMARY_SENTENCE. Tient dans `budget` caractères.
    """
    parts = sections(text)
    title = next((t for level, t, _ in parts if level == 1), f"Cadrage {issue_key}")
    head = f"# {title}\n\n"
    tail = f"\n\n{SUMMARY_SENTENCE}"
    room = max(budget - len(head) - len(tail), 0)
    picked = []
    for label, words in SUMMARY_SECTIONS:
        found = [lines for level, t, lines in parts if level >= 2 and any(w in _plain(t) for w in words)]
        if found:
            picked.append((label, found[0]))
    blocks = []
    if picked and room > 200:
        each = room // len(picked) - 40
        for label, lines in picked:
            brief = _brief(lines, max(each, 0))
            if brief:
                blocks.append(f"**{label}** : {brief}")
    body = "\n\n".join(blocks)
    if len(body) > room:
        body = body[:room].rsplit("\n", 1)[0]
    return head + body + tail if body else head.rstrip() + tail


def marker(issue_key: str, version: str, part: int = 0, total: int = 0) -> str:
    return f"[Analyse de l'agent — {issue_key} — version du {version}" + (
        f" — partie {part}/{total}]" if total > 1 else "]")


def comment_adf(marker_line: str, markdown: str) -> dict:
    """Commentaire d'analyse : marqueur, puis le Markdown ORIGINAL exact dans un bloc de code (relu sans perte)."""
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": marker_line, "marks": [{"type": "strong"}]}]},
        {"type": "codeBlock", "attrs": {"language": "markdown"}, "content": [{"type": "text", "text": markdown}]}]}


def _chunks(lines: list, level: int, fits) -> list:
    """Lignes d'une section découpées aux titres de niveau `level` (puis plus fins) pour que chaque bloc tienne."""
    text = "\n".join(lines)
    if fits(text):
        return [text]
    if level > 6:
        # Section sans sous-titre plus grande qu'un commentaire : coupe aux paragraphes (cas extrême)
        blocks, current = [], []
        for line in lines:
            if current and not line.strip() and not fits("\n".join(current + [line])):
                blocks.append("\n".join(current))
                current = []
            current.append(line)
        return blocks + (["\n".join(current)] if current else [])
    groups, current = [], []
    for line in lines:
        if re.match(rf"^#{{{level}}}\s", line) and current:
            groups.append(current)
            current = []
        current.append(line)
    groups.append(current)
    if len(groups) == 1:
        return _chunks(lines, level + 1, fits)
    out = []
    for group in groups:
        out += _chunks(group, level + 1, fits)
    return out


def split_parts(text: str, issue_key: str, version: str, max_size: int) -> list:
    """
    Analyse découpée en parties qui tiennent chacune dans un commentaire (`max_size`, ADF sérialisé), coupées aux
    titres de section (niveau 2, puis 3… pour une section trop longue), jamais au milieu d'une section quand elle
    tient seule. La concaténation des parties (séparateur « \\n ») redonne exactement le texte.
    """
    probe = marker(issue_key, version, 99, 99)

    def fits(chunk: str) -> bool:
        return adf_size(comment_adf(probe, chunk)) <= max_size
    if fits(text):
        return [text]
    blocks = _chunks(text.split("\n"), 2, fits)
    parts, current = [], ""
    for block in blocks:
        candidate = block if not current else current + "\n" + block
        if current and not fits(candidate):
            parts.append(current)
            current = block
        else:
            current = candidate
    parts.append(current)
    return parts


# ---------------------------------------------------------------------------
# Jira (remplaçable dans les tests)
# ---------------------------------------------------------------------------

class JiraBackend:
    """Accès Jira réel de la publication (tools.jira_tools)."""

    def get_description(self, key):
        from tools.jira_tools import get_jira_description
        return get_jira_description(key)

    def put_description(self, key, doc):
        from tools.jira_tools import put_jira_description
        put_jira_description(key, doc)

    def list_comments(self, key):
        from tools.jira_tools import list_jira_comments
        return list_jira_comments(key)

    def add_comment(self, key, body):
        from tools.jira_tools import add_jira_comment
        return add_jira_comment(key, body)

    def update_comment(self, key, comment_id, body):
        from tools.jira_tools import update_jira_comment
        update_jira_comment(key, comment_id, body)

    def delete_comment(self, key, comment_id):
        from tools.jira_tools import delete_jira_comment
        delete_jira_comment(key, comment_id)


def _comment_text(body) -> tuple:
    """(marqueur, Markdown) d'un commentaire d'analyse ; ('', '') pour tout autre commentaire."""
    from tools.jira_tools import _adf_plain_text
    nodes = (body or {}).get("content") or [] if isinstance(body, dict) else []
    if not nodes:
        return "", ""
    head = _adf_plain_text(nodes[0]).strip()
    if not head.startswith(MARKER_PREFIX):
        return "", ""
    code = next((n for n in nodes[1:] if n.get("type") == "codeBlock"), None)
    text = "".join(c.get("text", "") for c in (code or {}).get("content") or [])
    return head, text


def analysis_comments(key: str, comments: list) -> list:
    """Commentaires d'analyse de l'agent du ticket : [{"id", "version", "partie", "total", "texte"}]."""
    out = []
    for comment in comments or []:
        head, text = _comment_text(comment.get("body"))
        found = _MARKER.match(head) if head else None
        if found and found.group("key") == key:
            out.append({"id": comment.get("id"), "version": found.group("version").strip(),
                        "partie": int(found.group("part") or 1), "total": int(found.group("total") or 1),
                        "texte": text, "created": comment.get("created", "")})
    return out


def _publish_comments(key: str, parts: list, version: str, jira) -> int:
    """Commentaires d'analyse remplacés par `parts` : édités à leur place, complétés, surplus supprimés."""
    old = sorted(analysis_comments(key, jira.list_comments(key)), key=lambda c: (c["created"], c["partie"]))
    total = len(parts)
    for index, part in enumerate(parts, 1):
        body = comment_adf(marker(key, version, index, total), part)
        if index <= len(old):
            jira.update_comment(key, old[index - 1]["id"], body)
        else:
            jira.add_comment(key, body)
    for comment in old[total:]:
        jira.delete_comment(key, comment["id"])
    return total


def _remove_comments(key: str, jira) -> int:
    old = analysis_comments(key, jira.list_comments(key))
    for comment in old:
        jira.delete_comment(key, comment["id"])
    return len(old)


def publish_to_jira(key: str, text: str, jira=None, when: datetime = None) -> dict:
    """
    Analyse publiée dans la description si elle tient (mesure après conversion, sections existantes comprises),
    sinon résumé dans la description et analyse intégrale en commentaire(s). Renvoie {"mode": "description" |
    "commentaires", "taille", "limite", "commentaires", "message"}.
    """
    from tools.jira_tools import upsert_cadrage_adf
    jira = jira or JiraBackend()
    stops = (integration_tools.SECTION_TITLE,)
    version = (when or datetime.now()).strftime("%d/%m/%Y %H:%M")
    current = jira.get_description(key)
    full = upsert_cadrage_adf(current, text, stops)
    size, maximum = adf_size(full), limit()
    if size <= maximum - margin():
        try:
            jira.put_description(key, full)
            removed = _remove_comments(key, jira)
            return {"mode": "description", "taille": size, "limite": maximum, "commentaires": 0,
                    "message": "", "retires": removed}
        except RuntimeError as e:
            if not is_content_limit(str(e)):
                raise
            maximum = limit_from_error(str(e)) or maximum
    # Trop longue : résumé dans la description, au plus la place laissée par les autres sections
    others = adf_size(upsert_cadrage_adf(current, f"# {SUMMARY_SENTENCE}", stops))
    budget = max(maximum - margin() - others, 0)
    brief = summary(text, key, budget // 2)
    while adf_size(upsert_cadrage_adf(current, brief, stops)) > maximum - margin() and len(brief) > 200:
        budget //= 2
        brief = summary(text, key, budget // 2)
    parts = split_parts(text, key, version, maximum - margin())
    count = _publish_comments(key, parts, version, jira)
    jira.put_description(key, upsert_cadrage_adf(current, brief, stops))
    message = (f"Analyse trop longue pour la description ({size} caractères, limite {maximum}) : résumé dans la "
               f"description, analyse complète en {count} commentaire(s)")
    return {"mode": "commentaires", "taille": size, "limite": maximum, "commentaires": count, "message": message}


def publish(issue_key: str, text: str = None, publisher=None, jira=None) -> dict:
    """
    Publie l'analyse validée dans Jira (à la place de l'ancienne ; la section « Intégration du besoin » et la release
    note sont conservées) : description, ou résumé + commentaires si elle est trop longue. Sans effet si cette
    version est déjà publiée. `publisher` (tests, appelants historiques) : fonction de publication dans la
    description. Renvoie le statut ; lève RuntimeError (ou requests.RequestException) si Jira refuse.
    """
    text = _read(cadrage_path(issue_key)) if text is None else text
    if not text.strip():
        raise RuntimeError(f"Aucune analyse à publier pour {issue_key}.")
    if status(issue_key, text)["publiee"]:
        return status(issue_key, text)
    if publisher is not None:
        publisher(issue_key, text, stop_titles=(integration_tools.SECTION_TITLE,))
        record_publication(issue_key, text)
        return status(issue_key, text)
    result = publish_to_jira(issue_key, text, jira)
    record_publication(issue_key, text, mode=result["mode"], commentaires=result["commentaires"],
                       message=result["message"])
    if result["message"]:
        print(f"[PUBLICATION] {issue_key} : {result['message']}.")
    return status(issue_key, text)


def validate(issue_key: str, text: str, publisher=None, jira=None) -> dict:
    """Validation de l'utilisateur : enregistre l'analyse relue puis la publie dans Jira."""
    save_draft(issue_key, text)
    return publish(issue_key, text, publisher, jira)


# ---------------------------------------------------------------------------
# Lecture de l'analyse complète (patch, révision)
# ---------------------------------------------------------------------------

def from_comments(issue_key: str, comments: list) -> tuple:
    """
    Analyse réassemblée depuis les commentaires marqués (dernière version, parties dans l'ordre) : (texte, nombre de
    commentaires) ; ('', 0) sans commentaire d'analyse. Partie absente : IncompleteAnalysis.
    """
    found = analysis_comments(issue_key, comments)
    if not found:
        return "", 0

    def stamp(version: str):
        try:
            return datetime.strptime(version, "%d/%m/%Y %H:%M")
        except ValueError:
            return datetime.min
    latest = max({c["version"] for c in found}, key=stamp)
    parts = [c for c in found if c["version"] == latest]
    total = max(c["total"] for c in parts)
    present = {c["partie"]: c for c in parts}
    missing = [n for n in range(1, total + 1) if n not in present]
    if missing:
        raise IncompleteAnalysis(
            f"Analyse de {issue_key} incomplète dans Jira (version du {latest}) : partie(s) "
            f"{', '.join(f'{n}/{total}' for n in missing)} absente(s) des commentaires « Analyse de l'agent ». Aucun "
            f"patch n'est généré sur une analyse incomplète : republiez l'analyse validée (onglet Lancement).")
    return "\n".join(present[n]["texte"] for n in range(1, total + 1)), total


def full_analysis(issue_key: str, jira=None) -> dict:
    """
    Analyse complète du ticket : {"texte", "source": "fichier" | "commentaires" | "description" | "", "message"}.
    Priorité au fichier local validé, puis aux commentaires d'analyse marqués (réassemblés, sans limite de taille),
    puis à la description. Lève IncompleteAnalysis si une partie manque ou si la description ne porte qu'un résumé.
    """
    local = _read(cadrage_path(issue_key))
    if local.strip():
        return {"texte": local, "source": "fichier", "message": f"analyse lue dans {cadrage_path(issue_key)}"}
    jira = jira or JiraBackend()
    text, count = from_comments(issue_key, jira.list_comments(issue_key))
    if text:
        return {"texte": text, "source": "commentaires",
                "message": f"analyse lue dans {count} commentaire(s) Jira"}
    from tools.jira_tools import _extract_adf_text
    description = _extract_adf_text(jira.get_description(issue_key)) or ""
    if SUMMARY_SENTENCE in description:
        raise IncompleteAnalysis(f"La description de {issue_key} ne porte qu'un résumé et aucun commentaire « Analyse "
                                 f"de l'agent » n'a été trouvé : analyse complète indisponible, aucun patch généré.")
    return {"texte": description, "source": "description" if description.strip() else "",
            "message": "analyse lue dans la description Jira" if description.strip() else ""}


def ensure_local(issue_key: str, jira=None) -> dict:
    """
    Fichier '<TICKET>-cadrage.md' garanti complet avant un patch ou une révision : absent, il est reconstitué depuis
    Jira (commentaires réassemblés ou description) et enregistré comme version publiée. Renvoie full_analysis().
    """
    result = full_analysis(issue_key, jira)
    if result["source"] in ("commentaires", "description"):
        save_draft(issue_key, result["texte"])
        if not status(issue_key, result["texte"])["publiee"]:
            record_publication(issue_key, result["texte"], mode=result["source"], reconstituee=True)
    return result
