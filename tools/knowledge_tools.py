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
import getpass
import os
import time
import uuid

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
