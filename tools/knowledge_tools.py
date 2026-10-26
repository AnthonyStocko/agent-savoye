"""
Connaissances partagées : analyses d'agent-savoye déposées dans un répertoire commun (partage réseau), consultées
avant chaque nouvelle analyse. Complète la mémoire locale (tools/memory_tools.py) sans la remplacer.

Configuration (.env, onglet ⚙️ Configuration, section « Connaissances partagées ») :
- AGENT_KNOWLEDGE_DIR : répertoire partagé (chemin UNC \\\\serveur\\partage\\..., lecteur mappé ou chemin POSIX) ;
  vide = fonction désactivée ;
- AGENT_KNOWLEDGE_AUTHOR : auteur des documents déposés (défaut : utilisateur système) ;
- AGENT_KNOWLEDGE_MAX_CONTEXT : taille maximale (caractères) du bloc injecté dans un prompt.

Aucun nom de client, de projet ni de chemin d'entreprise dans le code : tout vient de la configuration et du
contexte de l'analyse.
"""
import concurrent.futures
import datetime
import functools
import getpass
import json
import os
import re
import subprocess
import time
import uuid

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_MAX_CONTEXT = 6000
# Au-delà, un partage est signalé lent (les dépôts et la consultation en pâtiront)
SLOW_ACCESS_MS = 2000
# Délai maximal d'une opération du test : un partage réseau injoignable peut bloquer longtemps
TEST_TIMEOUT_S = 15


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def normalize_dir(value: str) -> str:
    """
    Chemin du répertoire saisi : guillemets et espaces retirés, variables d'environnement et ~ développés,
    séparateur final retiré. Un chemin UNC (\\\\serveur\\partage), un lecteur mappé (X:\\...) ou un chemin POSIX
    est gardé tel quel.
    """
    path = os.path.expanduser(os.path.expandvars((value or "").strip().strip("\"'").strip()))
    if not path:
        return ""
    stripped = path.rstrip("\\/")
    # Racines à garder entières : « / », « X:\ », « \\serveur\partage » (sans séparateur final)
    if not stripped:
        return path[:1]
    if len(stripped) == 2 and stripped[1] == ":":
        return stripped + path[2:3]
    return stripped


def knowledge_dir() -> str:
    """Répertoire partagé configuré (AGENT_KNOWLEDGE_DIR, lu à chaque appel), '' si la fonction est désactivée."""
    return normalize_dir(os.getenv("AGENT_KNOWLEDGE_DIR", ""))


def enabled() -> bool:
    return bool(knowledge_dir())


def author() -> str:
    """Auteur des documents : AGENT_KNOWLEDGE_AUTHOR, à défaut l'utilisateur système."""
    configured = os.getenv("AGENT_KNOWLEDGE_AUTHOR", "").strip()
    if configured:
        return configured
    try:
        return getpass.getuser()
    except (OSError, KeyError, ImportError):
        return os.getenv("USERNAME") or os.getenv("USER") or "inconnu"


def max_context() -> int:
    """Taille maximale du bloc injecté dans un prompt (AGENT_KNOWLEDGE_MAX_CONTEXT, défaut 6000 ; 0 = aucun)."""
    try:
        return max(0, int(os.getenv("AGENT_KNOWLEDGE_MAX_CONTEXT", "").strip() or DEFAULT_MAX_CONTEXT))
    except ValueError:
        return DEFAULT_MAX_CONTEXT


# ---------------------------------------------------------------------------
# Test du répertoire (bouton « Tester »)
# ---------------------------------------------------------------------------

def _timed(function, *args):
    """(résultat, durée en ms) d'une opération bornée à TEST_TIMEOUT_S ; TimeoutError au-delà."""
    started = time.perf_counter()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        result = executor.submit(function, *args).result(timeout=TEST_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        raise TimeoutError(f"aucune réponse en {TEST_TIMEOUT_S} s") from None
    finally:
        executor.shutdown(wait=False)
    return result, round((time.perf_counter() - started) * 1000)


def _write_witness(path: str) -> None:
    """Crée un fichier témoin, le relit puis le supprime."""
    witness = os.path.join(path, f".agent-savoye-test-{uuid.uuid4().hex}.tmp")
    content = f"test d'écriture agent-savoye {time.time()}"
    with open(witness, "w", encoding="utf-8") as f:
        f.write(content)
    try:
        with open(witness, "r", encoding="utf-8") as f:
            if f.read() != content:
                raise OSError("contenu relu différent du contenu écrit")
    finally:
        os.remove(witness)


def test_dir(path: str = None) -> dict:
    """
    Teste le répertoire partagé (`path`, à défaut AGENT_KNOWLEDGE_DIR) : existence, lecture du contenu, droit
    d'écriture (fichier témoin créé, relu puis supprimé) et temps d'accès. Renvoie {"ok", "message", "etapes":
    [(étape, ok, détail)], "duree_ms"} ; le message dit quoi vérifier en cas d'échec.
    """
    path = knowledge_dir() if path is None else normalize_dir(path)
    steps, total = [], 0
    if not path:
        return {"ok": False, "message": "AGENT_KNOWLEDGE_DIR est vide : les connaissances partagées sont désactivées.",
                "etapes": [], "duree_ms": 0}

    def fail(step: str, detail: str, advice: str) -> dict:
        steps.append((step, False, detail))
        return {"ok": False, "message": f"{path} : {detail} — {advice}", "etapes": steps, "duree_ms": total}
    try:
        exists, elapsed = _timed(os.path.isdir, path)
    except TimeoutError as e:
        return fail("existence", f"partage injoignable ({e})",
                    "vérifiez la connexion réseau ou le VPN et le nom du serveur")
    total += elapsed
    if not exists:
        if os.path.exists(path):
            return fail("existence", "le chemin existe mais n'est pas un dossier", "indiquez un dossier")
        return fail("existence", "dossier introuvable ou partage injoignable",
                    "vérifiez le chemin (UNC \\\\serveur\\partage\\dossier, lecteur mappé ou chemin POSIX), la "
                    "connexion réseau ou le VPN, et que le dossier existe")
    steps.append(("existence", True, f"{elapsed} ms"))
    try:
        entries, elapsed = _timed(os.listdir, path)
    except TimeoutError as e:
        return fail("lecture", f"lecture du dossier sans réponse ({e})", "partage saturé ou injoignable")
    except PermissionError:
        return fail("lecture", "lecture du dossier refusée", "demandez le droit de lecture sur le partage")
    except OSError as e:
        return fail("lecture", f"lecture du dossier impossible ({e})", "vérifiez le partage")
    total += elapsed
    steps.append(("lecture", True, f"{len(entries)} élément(s), {elapsed} ms"))
    try:
        _, elapsed = _timed(_write_witness, path)
    except TimeoutError as e:
        return fail("écriture", f"écriture sans réponse ({e})", "partage saturé ou injoignable")
    except PermissionError:
        return fail("écriture", "pas de droit d'écriture (fichier témoin refusé)",
                    "demandez le droit d'écriture sur le dossier : sans lui, les analyses restent en file locale")
    except OSError as e:
        return fail("écriture", f"écriture impossible ({e})", "vérifiez l'espace disque et les droits du partage")
    total += elapsed
    steps.append(("écriture", True, f"fichier témoin créé, relu et supprimé, {elapsed} ms"))
    slow = total > SLOW_ACCESS_MS
    return {"ok": True, "etapes": steps, "duree_ms": total,
            "message": f"{path} : joignable, lecture et écriture OK, temps d'accès {total} ms"
                       + (" — partage lent : les dépôts et la consultation seront ralentis" if slow else "")}


# ---------------------------------------------------------------------------
# Format d'un document : nommage, en-tête YAML, écriture atomique sans écrasement
# ---------------------------------------------------------------------------

# Liste fermée des types de documents (un dossier par type sous le dossier du projet)
KNOWLEDGE_TYPES = (
    "cadrage", "besoin", "decoupage-epopee", "recap-patch", "release-note", "postmerge-cucumber",
    "cucumber-inventaire", "cucumber-sante", "cucumber-couverture", "cucumber-a-revoir",
    "cucumber-diagnostic-run", "audit", "autre",
)
STATUS_ACTIVE, STATUS_OBSOLETE, STATUS_REPLACED = "actif", "obsolète", "remplacé"
STATUSES = (STATUS_ACTIVE, STATUS_OBSOLETE, STATUS_REPLACED)
# Champs de l'en-tête, dans l'ordre d'écriture
HEADER_FIELDS = (
    "projet", "client", "type", "sujet", "tickets", "composants", "tags", "date", "auteur", "version_agent",
    "depots", "statut", "remplace_par", "sources_partagees", "resume",
)
LIST_FIELDS = ("tickets", "composants", "tags", "depots", "sources_partagees")
SUMMARY_MAX_LINES = 5
SUMMARY_LINE_MAX = 200
# Longueurs bornées : un chemin UNC complet doit rester sous la limite historique de 260 caractères de Windows
MAX_FOLDER_LEN, MAX_SUBJECT_LEN, MAX_AUTHOR_LEN, MAX_PATH_LEN = 60, 60, 30, 240
MAX_NAME_ATTEMPTS = 99

_FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(text: str, max_len: int = MAX_SUBJECT_LEN, fallback: str = "sans-titre") -> str:
    """
    Nom de fichier ou de dossier valide sous Windows comme sous POSIX : caractères interdits et blancs remplacés
    par « - », longueur bornée, points et tirets de bord retirés, noms réservés (CON, NUL, COM1...) préfixés.
    """
    name = re.sub(r"\s+", "-", _FORBIDDEN_CHARS.sub("-", str(text or "")).strip())
    name = re.sub(r"-{2,}", "-", name)[:max_len].strip(" .-")
    if not name:
        name = fallback
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        name = "_" + name
    return name


def _name_part(text: str, max_len: int, fallback: str) -> str:
    """Partie d'un nom de document : « _ » sert de séparateur, il est remplacé par « - »."""
    return safe_name(str(text or "").replace("_", "-"), max_len, fallback)


def project_folder(meta: dict) -> str:
    """Dossier du projet : le client s'il est connu, sinon le projet Jira (toujours présent dans l'en-tête)."""
    return safe_name(meta.get("client") or meta.get("projet"), MAX_FOLDER_LEN, "sans-projet")


def document_path(base: str, meta: dict) -> str:
    """
    Chemin du document : <base>/<projet>/<type>/<AAAA-MM-JJ_HHMMSS>_<sujet>_<auteur>.md, noms nettoyés et
    longueur totale bornée (le sujet est raccourci en premier).
    """
    when = _parse_date(meta.get("date"))
    directory = os.path.join(base, project_folder(meta), safe_name(meta.get("type"), MAX_FOLDER_LEN, "autre"))
    stamp = when.strftime("%Y-%m-%d_%H%M%S")
    author_part = _name_part(meta.get("auteur"), MAX_AUTHOR_LEN, "inconnu")
    # Place laissée au sujet : séparateurs, extension et suffixe éventuel « -NN » déduits
    room = MAX_PATH_LEN - len(directory) - len(stamp) - len(author_part) - len(os.sep + "__.md") - 3
    subject_part = _name_part(meta.get("sujet"), max(8, min(MAX_SUBJECT_LEN, room)), "analyse")
    return os.path.join(directory, f"{stamp}_{subject_part}_{author_part}.md")


def _with_suffix(path: str, attempt: int) -> str:
    """Variante du nom quand il est déjà pris : « nom.md », « nom-2.md », « nom-3.md »..."""
    return path if attempt <= 1 else f"{path[:-3]}-{attempt}.md"


def _parse_date(value) -> datetime.datetime:
    """Date de l'en-tête (ISO 8601) ; maintenant si elle est absente ou illisible."""
    try:
        return datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.datetime.now()


@functools.lru_cache(maxsize=1)
def agent_version() -> str:
    """Version d'agent-savoye : `git describe` du dépôt de l'agent, « inconnue » hors dépôt git."""
    try:
        result = subprocess.run(["git", "-C", AGENT_DIR, "describe", "--tags", "--always", "--dirty"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "inconnue"
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "inconnue"


def _unique(values) -> list:
    """Valeurs non vides, sans doublon, ordre conservé."""
    seen = []
    for value in values or ():
        value = str(value).strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def bound_summary(text: str) -> str:
    """Résumé borné à SUMMARY_MAX_LINES lignes non vides de SUMMARY_LINE_MAX caractères au plus."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return "\n".join(line if len(line) <= SUMMARY_LINE_MAX else line[:SUMMARY_LINE_MAX - 1] + "…"
                     for line in lines[:SUMMARY_MAX_LINES])


def make_meta(projet: str, doc_type: str, sujet: str, client: str = "", tickets=(), composants=(), tags=(),
              resume: str = "", depots=(), sources=(), auteur: str = None, date: str = None) -> dict:
    """
    En-tête d'un nouveau document (statut « actif »). `doc_type` appartient à KNOWLEDGE_TYPES ; `depots` est une
    liste de {"nom", "commit"} ; `sources` les documents partagés consultés pour produire l'analyse.
    """
    if doc_type not in KNOWLEDGE_TYPES:
        raise ValueError(f"type de document inconnu : {doc_type!r} (attendu : {', '.join(KNOWLEDGE_TYPES)})")
    return {
        "projet": str(projet or "").strip(), "client": str(client or "").strip(), "type": doc_type,
        "sujet": str(sujet or "").strip(), "tickets": _unique(tickets), "composants": _unique(composants),
        "tags": _unique(tags), "date": date or datetime.datetime.now().isoformat(timespec="seconds"),
        "auteur": auteur or author(), "version_agent": agent_version(),
        "depots": [{"nom": str(d.get("nom", "")), "commit": str(d.get("commit", ""))} for d in depots or ()],
        "statut": STATUS_ACTIVE, "remplace_par": "", "sources_partagees": _unique(sources),
        "resume": bound_summary(resume),
    }


def render_header(meta: dict) -> str:
    """
    En-tête YAML entre deux lignes « --- » : une clé par ligne, valeur en JSON (sous-ensemble valide du YAML,
    relu sans dépendance). Les champs connus d'abord, dans l'ordre de HEADER_FIELDS, puis les éventuels autres.
    """
    keys = [k for k in HEADER_FIELDS if k in meta] + [k for k in meta if k not in HEADER_FIELDS]
    lines = [f"{key}: {json.dumps(meta[key], ensure_ascii=False)}" for key in keys]
    return "---\n" + "\n".join(lines) + "\n---\n"


def render_document(meta: dict, body: str) -> str:
    """Document complet : en-tête puis corps tel quel."""
    return render_header(meta) + (body or "")


def parse_document(text: str) -> tuple:
    """
    (en-tête, corps) d'un document. Une valeur non JSON (en-tête retouché à la main) est gardée en texte ; un
    document sans en-tête renvoie ({}, texte).
    """
    text = (text or "").lstrip("﻿")
    match = re.match(r"---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not match:
        return {}, text
    meta = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if not sep or not key.strip() or key.startswith((" ", "\t", "#")):
            continue
        value = value.strip()
        try:
            meta[key.strip()] = json.loads(value)
        except ValueError:
            meta[key.strip()] = value.strip("\"'")
    for key in LIST_FIELDS:
        if key in meta and not isinstance(meta[key], list):
            meta[key] = [meta[key]] if meta[key] else []
    return meta, text[match.end():]


def read_document(path: str) -> tuple:
    """(en-tête, corps) du document `path`."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return parse_document(f.read())


def _claim(tmp: str, final: str) -> bool:
    """Donne au fichier temporaire son nom définitif sans jamais écraser ; False si le nom est déjà pris."""
    if os.name == "nt":
        # Sous Windows, le renommage refuse d'écraser une cible existante : il est atomique et sûr
        try:
            os.rename(tmp, final)
        except FileExistsError:
            return False
        return True
    try:
        # Sous POSIX, rename écraserait : un lien physique, lui, échoue si la cible existe
        os.link(tmp, final)
    except FileExistsError:
        return False
    except OSError:
        # Partage sans liens physiques : contrôle puis renommage (fenêtre de concurrence minime)
        if os.path.exists(final):
            return False
        os.rename(tmp, final)
        return True
    os.remove(tmp)
    return True


def write_atomic(path: str, content: str, overwrite: bool = False) -> bool:
    """
    Écrit `content` dans `path` de façon atomique : fichier temporaire dans le même dossier, puis renommage. Sans
    `overwrite`, un fichier existant n'est jamais écrasé (renvoie False) ; avec, il est remplacé d'un bloc.
    """
    directory, name = os.path.split(path)
    tmp = os.path.join(directory, f".~{name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if overwrite:
            os.replace(tmp, path)
            return True
        return _claim(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_document(base: str, meta: dict, body: str) -> str:
    """
    Dépose un nouveau document sous `base` et renvoie son chemin. Jamais d'écrasement : si le nom est pris (même
    seconde, même sujet, même auteur), un suffixe « -2 », « -3 »... est ajouté.
    """
    path = document_path(base, meta)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = render_document(meta, body)
    for attempt in range(1, MAX_NAME_ATTEMPTS + 1):
        candidate = _with_suffix(path, attempt)
        if write_atomic(candidate, content):
            return candidate
    raise FileExistsError(f"{path} : {MAX_NAME_ATTEMPTS} noms déjà pris")


def update_header(path: str, **changes) -> dict:
    """
    Modifie l'en-tête d'un document existant (statut, remplace_par...) sans toucher au corps, par écriture
    atomique ; renvoie le nouvel en-tête.
    """
    if "statut" in changes and changes["statut"] not in STATUSES:
        raise ValueError(f"statut inconnu : {changes['statut']!r} (attendu : {', '.join(STATUSES)})")
    meta, body = read_document(path)
    meta.update(changes)
    write_atomic(path, render_document(meta, body), overwrite=True)
    return meta
