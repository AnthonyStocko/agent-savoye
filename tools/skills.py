"""
Auto-amélioration de l'agent : compétences (« skills ») tirées de ses propres réussites.

Quand l'agent résout une tâche complexe ET que la réussite est prouvée (scénario rendu vert sur Jenkins après
correction, initialisation corrigée et revérifiée), un appel court lui fait rédiger la procédure réutilisable :
déclencheurs (messages d'échec, situations), étapes, pièges, exemple. Elle est enregistrée comme compétence, puis
proposée (index + chemin, lecture à la demande) aux tâches suivantes du même domaine dont le contexte lui ressemble.

- Stockage : un fichier Markdown par compétence, en-tête « clé: valeur » puis la procédure, dans
  AGENT_KNOWLEDGE_DIR/competences (partagé avec l'équipe), à défaut .tmp/competences.
- Domaines : cucumber-correction, cucumber-init (extensible).
- Une compétence n'est jamais une règle : les règles du prompt priment ; l'utilisateur peut la désactiver ou la
  supprimer (onglet Connaissances partagées). Secrets masqués avant écriture. SKILLS_AUTO=0 coupe l'apprentissage
  et l'injection.
"""
import json
import os
import re
import unicodedata
from datetime import datetime

from tools.secret_tools import mask_secrets

WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOMAINS = {"cucumber-correction": "correction de tests Cucumber en échec",
           "cucumber-init": "correction des compléments d'initialisation Cucumber"}
_WORD = re.compile(r"[a-zà-ÿ0-9_]{4,}")
MAX_BODY = 6000
_KV_SECRET = re.compile(r"(?i)\b(token|jeton|password|passwd|pwd|mot[ _-]?de[ _-]?passe|secret|api[_-]?key|apikey)"
                        r"(\s*[:=]\s*)([^\s,;'\"]+)")


def _mask(text: str) -> str:
    return _KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***", mask_secrets(text)[0])


def enabled() -> bool:
    return os.getenv("SKILLS_AUTO", "1").strip() != "0"


def min_turns() -> int:
    """Complexité minimale (tours de l'agent cumulés) pour qu'une réussite donne une compétence."""
    try:
        return max(0, int(os.getenv("SKILLS_MIN_TURNS", "").strip() or 8))
    except ValueError:
        return 8


def skills_dir() -> str:
    shared = os.getenv("AGENT_KNOWLEDGE_DIR", "").strip()
    return os.path.join(shared, "competences") if shared else os.path.join(WORKSPACE_DIR, ".tmp", "competences")


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:60] or "competence"


def _parse(text: str) -> tuple:
    meta, body, lines = {}, [], (text or "").splitlines()
    index = 0
    if lines and lines[0].strip() == "---":
        index = 1
        while index < len(lines) and lines[index].strip() != "---":
            key, _, value = lines[index].partition(":")
            if key.strip():
                meta[key.strip()] = value.strip()
            index += 1
        index += 1
    body = lines[index:]
    return meta, "\n".join(body).strip()


def _render(meta: dict, body: str) -> str:
    head = "\n".join(f"{k}: {' '.join(str(v).split())}" for k, v in meta.items())
    return f"---\n{head}\n---\n\n{body.strip()}\n"


def load_skills(domain: str = "", include_disabled: bool = False) -> list:
    """Compétences enregistrées : [{nom, description, domaine, declencheurs, chemin, utilisations, actif, ...}]."""
    folder, out = skills_dir(), []
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not name.endswith(".md"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8") as f:
                meta, body = _parse(f.read())
        except OSError:
            continue
        skill = {**meta, "corps": body, "chemin": path, "fichier": name,
                 "actif": meta.get("actif", "oui") != "non",
                 "utilisations": int(meta.get("utilisations", "0") or 0) if str(meta.get("utilisations", "0")).isdigit()
                 else 0}
        if (not domain or skill.get("domaine") == domain) and (include_disabled or skill["actif"]):
            out.append(skill)
    return out


def _words(text: str) -> set:
    return set(_WORD.findall((text or "").lower()))


def relevant(domain: str, context: str, limit: int = 6) -> list:
    """Compétences actives du domaine les plus proches du contexte (déclencheurs et description)."""
    words = _words(context)
    scored = []
    for skill in load_skills(domain):
        overlap = len(words & _words(skill.get("declencheurs", "") + " " + skill.get("description", "")))
        scored.append((overlap, skill.get("utilisations", 0), skill))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [skill for score, _, skill in scored[:limit] if score > 0 or len(scored) <= limit]


def prompt_block(domain: str, context: str) -> str:
    """Bloc « compétences acquises » d'un prompt ('' sans compétence ou si désactivé)."""
    if not enabled():
        return ""
    skills = relevant(domain, context)
    if not skills:
        return ""
    lines = ["", "=== COMPÉTENCES ACQUISES (procédures tirées de tes réussites passées, vérifiées sur Jenkins) ===",
             "Avant d'agir, lis (Read) celles dont les déclencheurs correspondent à ta situation et applique-les si "
             "elles conviennent ; les règles de ce prompt priment toujours sur une compétence. Cite celles utilisées "
             "dans « competences_utilisees » du JSON final."]
    for skill in skills:
        lines.append(f"- {skill.get('nom', skill['fichier'])} — {skill.get('description', '')} (déclencheurs : "
                     f"{skill.get('declencheurs', '')[:200]}) : '{skill['chemin']}'")
    return "\n".join(lines) + "\n"


def record_use(names: list) -> list:
    """Compétences citées par l'agent comme utilisées : compteur et date mis à jour. Renvoie celles trouvées."""
    wanted = {slug(n) for n in names or [] if str(n).strip()}
    done = []
    for skill in load_skills(include_disabled=True):
        if slug(skill.get("nom", "")) in wanted or skill["fichier"][:-3] in wanted:
            meta = {k: v for k, v in skill.items() if k not in ("corps", "chemin", "fichier", "actif", "utilisations")}
            meta.update(utilisations=skill["utilisations"] + 1, derniere_utilisation=datetime.now().isoformat(
                timespec="seconds"), actif="oui" if skill["actif"] else "non")
            with open(skill["chemin"], "w", encoding="utf-8") as f:
                f.write(_render(meta, skill["corps"]))
            done.append(skill.get("nom", skill["fichier"]))
    return done


def parse_proposal(text: str) -> dict:
    """JSON de l'agent : {action: creer|mettre_a_jour|aucune, nom, description, declencheurs, etapes, pieges, ...}."""
    for candidate in re.findall(r"```(?:json)?\s*(.*?)```", text or "", flags=re.S)[::-1] + [text or ""]:
        start, end = candidate.find("{"), candidate.rfind("}")
        if 0 <= start < end:
            try:
                data = json.loads(candidate[start:end + 1])
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return {}


def save_proposal(domain: str, proposal: dict, origin: str, client: str = "") -> str:
    """
    Enregistre (ou met à jour) la compétence proposée. Rien si action « aucune », nom ou étapes absents. Renvoie le
    chemin écrit ('' sinon).
    """
    action = str(proposal.get("action", "")).lower()
    steps = [str(s).strip() for s in proposal.get("etapes") or [] if str(s).strip()]
    name = str(proposal.get("nom", "")).strip()
    if action not in ("creer", "mettre_a_jour") or not name or not steps:
        return ""
    folder = skills_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{slug(name)}.md")
    previous = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            previous, _ = _parse(f.read())
    now = datetime.now().isoformat(timespec="seconds")
    meta = {"nom": name, "domaine": domain,
            "description": str(proposal.get("description", "")).strip(),
            "declencheurs": ", ".join(str(t).strip() for t in proposal.get("declencheurs") or [] if str(t).strip()),
            "client": client or previous.get("client", ""), "cree_le": previous.get("cree_le", now), "maj_le": now,
            "origine": origin, "utilisations": previous.get("utilisations", "0"),
            "actif": previous.get("actif", "oui"), "auteur": "agent"}
    body = [f"# {name}", "", meta["description"], "", "## Quand l'utiliser", "",
            *[f"- {t}" for t in proposal.get("declencheurs") or []], "", "## Procédure", "",
            *[f"{i}. {s}" for i, s in enumerate(steps, 1)]]
    pitfalls = [str(p).strip() for p in proposal.get("pieges") or [] if str(p).strip()]
    if pitfalls:
        body += ["", "## Pièges", "", *[f"- {p}" for p in pitfalls]]
    if str(proposal.get("exemple", "")).strip():
        body += ["", "## Exemple (réussite d'origine)", "", str(proposal["exemple"]).strip()]
    text = _mask("\n".join(body))[:MAX_BODY]
    with open(path, "w", encoding="utf-8") as f:
        f.write(_render(meta, text))
    return path


def set_active(path: str, active: bool) -> None:
    with open(path, encoding="utf-8") as f:
        meta, body = _parse(f.read())
    meta["actif"] = "oui" if active else "non"
    with open(path, "w", encoding="utf-8") as f:
        f.write(_render(meta, body))


def index_text(domain: str) -> str:
    """Compétences existantes du domaine (pour mettre à jour plutôt que dupliquer)."""
    skills = load_skills(domain, include_disabled=True)
    return "\n".join(f"- {s.get('nom', s['fichier'])} : {s.get('description', '')} (déclencheurs : "
                     f"{s.get('declencheurs', '')[:150]})" for s in skills) or "(aucune)"
