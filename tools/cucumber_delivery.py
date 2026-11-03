"""
Livraison autonome des corrections Cucumber (0 token) : l'agent commite et pousse LUI-MÊME, sur le dépôt
<client>-cucumber du profil client et sur aucun autre dépôt.

Garde d'écriture (check_push), appelée avant TOUT push de l'agent (cucumber_checks.push_for_verification,
promotion, annulation) — un refus arrête avant l'appel à Git, avec un message et une trace dans le journal
(.tmp/cucumber/ecritures.jsonl) :
- pause (bouton « Pause » de l'onglet, .tmp/cucumber/pause.json) : aucune écriture ;
- dépôt : l'adresse d'envoi du worktree doit être EXACTEMENT celle du dépôt Cucumber du profil client
  (« cucumber_repo_url ») ; une copie de lecture (pushurl désactivée, READ_ONLY_PUSH_URL) est refusée ;
- fichiers des commits envoyés : features, steps et classes Java du projet sous src/test, fichiers de données de
  test (src/test/resources), A_REVOIR.md, COUVERTURE.md. Sans décision humaine (`decision`) : pom.xml, fichiers de
  l'init globale (fichiers protégés du profil) et suppression d'un scénario sont refusés.

Déroulé (main.autonomous_delivery) : commit sur la branche de travail (message structuré, commit_message), push,
run complet sur cette branche, puis promotion vers la branche cible par fusion rapide SEULEMENT si promotion_verdict
l'accepte (moins d'échecs que le run de référence, aucun nouvel échec, init verte ; suite entièrement verte : deux
runs complets verts consécutifs). Retour arrière (regression_to_revert, revert_on_target) : commit d'annulation
(git revert) poussé sans forcer, jamais de réécriture d'historique.
"""
import json
import os
import re
from datetime import datetime

from tools import cucumber_tools
from tools.cucumber_tools import _git

READ_ONLY_PUSH_URL = "lecture-seule://envoi-desactive"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
JOURNAL = "ecritures.jsonl"
PAUSE = "pause.json"
ROOT_FILES = ("A_REVOIR.md", "COUVERTURE.md")
TEST_SOURCES = ("src/test/",)
CODE_EXTENSIONS = (".feature", ".java")
DATA_ROOT = "src/test/resources/"
SCENARIO = re.compile(r"^\s*(?:Scenario Outline|Scenario Template|Scenario|Scénario|Plan du [Ss]cénario)\s*:\s*(.+?)\s*$")
_CUCUMBER_REPO = re.compile(r"-cucumber(-\d+)?$", re.I)


# ---------------------------------------------------------------------------
# Pause et journal
# ---------------------------------------------------------------------------

def _pause_path() -> str:
    return os.path.join(cucumber_tools.CACHE_ROOT, PAUSE)


def paused() -> dict:
    """{"par", "date"} si l'écriture est en pause, {} sinon."""
    return cucumber_tools._read_json(_pause_path(), {}) or {}


def set_pause(on: bool, user: str = "") -> None:
    if on:
        cucumber_tools._write_json(_pause_path(), {"par": user or "inconnu",
                                                   "date": datetime.now().isoformat(timespec="seconds")})
    elif os.path.exists(_pause_path()):
        os.remove(_pause_path())
    log_event("", "pause" if on else "reprise", par=user or "inconnu")


def journal_path() -> str:
    return os.path.join(cucumber_tools.CACHE_ROOT, JOURNAL)


def log_event(client: str, kind: str, **data) -> dict:
    """Trace d'écriture (refus, commit poussé, promotion, annulation, pause) ajoutée au journal, affichée."""
    event = {"date": datetime.now().isoformat(timespec="seconds"), "client": client, "type": kind,
             "par": data.pop("par", "") or launched_by(), **data}
    os.makedirs(cucumber_tools.CACHE_ROOT, exist_ok=True)
    with open(journal_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    if kind == "refus":
        print(f"[LIVRAISON] ⛔ Écriture refusée : {data.get('motif', '')}")
    return event


def events(client: str = "", kinds=(), limit: int = 50) -> list:
    """Dernières traces du journal (client, types), les plus récentes en dernier."""
    try:
        with open(journal_path(), encoding="utf-8") as f:
            items = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []
    items = [e for e in items if (not client or e.get("client") == client) and (not kinds or e.get("type") in kinds)]
    return items[-limit:]


# ---------------------------------------------------------------------------
# Identité : compte technique, utilisateur connecté
# ---------------------------------------------------------------------------

def launched_by() -> str:
    """Utilisateur connecté qui a lancé l'action (AGENT_AUTH_USER, transmis par l'application)."""
    return os.getenv("AGENT_AUTH_USER", "").strip()


def identity_env() -> dict:
    """
    Variables Git du compte technique (AGENT_GIT_NAME, AGENT_GIT_EMAIL ; signature : AGENT_GIT_SIGNING_KEY) : auteur
    et committeur de chaque commit de l'agent. {} si le compte technique n'est pas configuré.
    """
    name, email = os.getenv("AGENT_GIT_NAME", "").strip(), os.getenv("AGENT_GIT_EMAIL", "").strip()
    if not (name and email):
        return {}
    out = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
           "GIT_COMMITTER_EMAIL": email}
    key = os.getenv("AGENT_GIT_SIGNING_KEY", "").strip()
    if key:
        out.update(GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="commit.gpgsign", GIT_CONFIG_VALUE_0="true",
                   GIT_CONFIG_KEY_1="user.signingkey", GIT_CONFIG_VALUE_1=key)
    return out


def apply_identity() -> dict:
    """Compte technique appliqué au processus (hérité par toutes les commandes Git). Renvoie les variables posées."""
    values = identity_env()
    os.environ.update(values)
    return values


def commit_message(lot: dict, analysis: dict = None, specs: dict = None, client: str = "") -> str:
    """
    Message d'un commit de correction : cause, correction, run d'origine, tâches, utilisateur qui a lancé l'action.
    `analysis` : analyse de l'import corrigé (causes et actions par échec), `specs` : {id: libellé}.
    """
    echecs = {e["id"]: e for e in (analysis or {}).get("echecs") or []}
    corrections = lot.get("corrections") or []
    tasks = [t for t in [lot.get("spec_correction"), *(lot.get("ids") or [])] if t]
    lines = [f"Cucumber {client} : correction de {len(corrections)} scénario(s) (lot {lot.get('numero')})", ""]
    for item in corrections[:20]:
        found = echecs.get(item.get("id")) or {}
        lines.append(f"- {item.get('titre') or item.get('id')} ({item.get('fichier', '')})")
        lines.append(f"  Cause : {(found.get('cause') or item.get('message') or '?')[:200]}")
        lines.append(f"  Correction : {(found.get('action') or 'scénario corrigé d après l écart constaté')[:200]}")
    run = (analysis or {}).get("build") or (analysis or {}).get("run") or ""
    lines += ["", f"Run d'origine : import n° {lot.get('import', '?')}" + (f" — {run}" if run else "")]
    if tasks:
        lines.append("Tâches : " + ", ".join(f"{t} ({(specs or {}).get(t, '')})".replace(" ()", "") for t in tasks))
    if launched_by():
        lines += ["", f"Lancé-par: {launched_by()}"]
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# Garde d'écriture
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Adresse comparable : sans identifiants, sans « / » ni « .git » final, en minuscules."""
    url = re.sub(r"^(https?://)[^@/]+@", r"\1", (url or "").strip())
    return re.sub(r"(\.git)?/*$", "", url).lower()


def push_url(worktree: str) -> str:
    return _git(worktree, "remote", "get-url", "--push", "origin").stdout.strip()


def client_of(worktree: str) -> str:
    """Client d'un espace de l'agent (.tmp/cucumber/<client>/…) ; '' hors de ces espaces."""
    root = os.path.normcase(os.path.abspath(cucumber_tools.CACHE_ROOT))
    path = os.path.normcase(os.path.abspath(worktree or ""))
    if not path.startswith(root + os.sep):
        return ""
    return os.path.relpath(path, root).split(os.sep, 1)[0]


def authorized_url(client: str) -> str:
    """Dépôt Cucumber du profil client, seul autorisé en écriture ('' si inconnu)."""
    from tools import cucumber_profile
    return (cucumber_profile.effective_profile(cucumber_profile.load_profile(client)).get("cucumber_repo_url")
            or "").strip()


def register_repo(client: str, cucumber_repo_dir: str) -> str:
    """Adresse du dépôt Cucumber du client relevée sur son clone (origin), enregistrée comme détection du profil."""
    from tools import cucumber_profile
    url = _git(cucumber_repo_dir, "remote", "get-url", "origin").stdout.strip() if cucumber_repo_dir else ""
    if not url or not _CUCUMBER_REPO.search(normalize_url(url).rsplit("/", 1)[-1]):
        return ""
    document = cucumber_profile.load_profile(client)
    if (document.get("detecte") or {}).get("cucumber_repo_url") != url:
        document.setdefault("detecte", {})["cucumber_repo_url"] = url
        cucumber_tools._write_json(cucumber_profile.profile_path(client), document)
    return url


def disable_push(repo_dir: str) -> None:
    """Copie de lecture : adresse d'envoi désactivée (un push y échoue, la garde le refuse avant Git)."""
    if repo_dir and os.path.isdir(os.path.join(repo_dir, ".git")):
        _git(repo_dir, "config", "remote.origin.pushurl", READ_ONLY_PUSH_URL)


def outgoing_range(worktree: str) -> tuple:
    """(base, HEAD) des commits qui seraient envoyés (absents de toute référence distante) ; (None, HEAD) si aucun."""
    head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    new = _git(worktree, "rev-list", "--reverse", "HEAD", "--not", "--remotes").stdout.split()
    if not new:
        return None, head
    parent = _git(worktree, "rev-parse", "--verify", "-q", f"{new[0]}^").stdout.strip()
    return parent or "", head


def _changes(worktree: str, base: str, head: str) -> list:
    """[(statut, chemin)] entre base et head (base '' : premier commit, tout est ajouté)."""
    out = _git(worktree, "diff", "--name-status", "--no-renames", "-z", base or EMPTY_TREE, head).stdout.split("\0")
    out = [x for x in out if x]
    return [(out[i], out[i + 1]) for i in range(0, len(out) - 1, 2)]


def scenario_titles(text: str) -> list:
    return [m.group(1) for line in (text or "").splitlines() for m in [SCENARIO.match(line)] if m
            and not line.lstrip().startswith("#")]


def _show(worktree: str, ref: str, path: str) -> str:
    return _git(worktree, "show", f"{ref}:{path}").stdout if ref else ""


def file_errors(worktree: str, base: str, head: str, protected=(), decision: bool = False) -> list:
    """Refus par fichier des changements base..head (voir docstring du module)."""
    errors, protected = [], set(protected or ())
    for status, path in _changes(worktree, base, head):
        name = path.rsplit("/", 1)[-1]
        allowed = (path in ROOT_FILES or (path.startswith(TEST_SOURCES) and path.endswith(CODE_EXTENSIONS))
                   or path.startswith(DATA_ROOT))
        if name == "pom.xml":
            # Exception explicite : seule la version de la bibliothèque Cucumber (montée alignée sur le client)
            from tools.cucumber_library_upgrade import only_version_changed
            if not decision and not (path == "pom.xml" and only_version_changed(
                    _show(worktree, base, path), _show(worktree, head, path))):
                errors.append(f"{path} : pom.xml modifié sans décision humaine")
            continue
        if not allowed:
            errors.append(f"{path} : hors des fichiers autorisés (features, steps et classes Java sous src/test, "
                          f"données de test, A_REVOIR.md, COUVERTURE.md)")
            continue
        if path in protected and not decision:
            errors.append(f"{path} : fichier de l'init globale (protégé) modifié sans décision humaine")
            continue
        if path.endswith(".feature") and not decision:
            if status.startswith("D"):
                errors.append(f"{path} : feature supprimée sans décision humaine")
                continue
            lost = [t for t in scenario_titles(_show(worktree, base, path))
                    if t not in set(scenario_titles(_show(worktree, head, path)))]
            if lost:
                errors.append(f"{path} : scénario(s) supprimé(s) sans décision humaine : " + ", ".join(lost[:5]))
    return errors


def check_push(worktree: str, branch: str, client: str = "", decision: bool = True, protected=None,
               head: str = "HEAD") -> str:
    """
    Garde de tout push de l'agent : '' si autorisé, sinon le motif (tracé dans le journal). `client` : déduit de
    l'emplacement du worktree à défaut ; inconnu (espace hors de l'agent) : seuls la pause, la copie de lecture et
    les fichiers sont contrôlés. `decision` : action lancée par une personne (pom, init globale, suppression permis).
    """
    client = client or client_of(worktree)
    target = push_url(worktree)

    def refuse(motif: str) -> str:
        log_event(client, "refus", motif=motif, branche=branch, depot=normalize_url(target))
        return motif
    pause = paused()
    if pause:
        return refuse(f"écriture en pause (par {pause.get('par')} le {pause.get('date', '')[:16]}) : rien n'est poussé")
    if not target or target == READ_ONLY_PUSH_URL:
        return refuse(f"{worktree} est une copie de lecture (aucune adresse d'envoi) : push refusé")
    if client:
        allowed = authorized_url(client)
        if not allowed:
            return refuse(f"dépôt Cucumber du profil de {client} inconnu (cucumber_repo_url) : push refusé")
        if normalize_url(target) != normalize_url(allowed):
            return refuse(f"{normalize_url(target)} n'est pas le dépôt Cucumber du profil de {client} "
                          f"({normalize_url(allowed)}) : push refusé")
    if client:
        from tools import cucumber_modules, cucumber_profile
        profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))
        if protected is None:
            protected = profile.get("protected_files") or []
        # Aucune feature active ne référence un clear, un tag d'init ou un step d'un module décoché
        found = cucumber_modules.traces(cucumber_modules.worktree_features(worktree), profile) \
            if head == "HEAD" else []
        if found:
            return refuse(f"{len(found)} trace(s) d'un module décoché dans des features actives ("
                          + " ; ".join(found[:3]) + ") : appliquez d'abord le nettoyage du module (« Données du "
                          "projet », Modules testés)")
    base, tip = outgoing_range(worktree) if head == "HEAD" else (None, head)
    if base is not None:
        errors = file_errors(worktree, base, tip, protected or (), decision)
        if errors:
            return refuse("fichiers refusés : " + " ; ".join(errors[:5]))
    return ""


# ---------------------------------------------------------------------------
# Promotion et retour arrière
# ---------------------------------------------------------------------------

def failing(analysis: dict) -> set:
    return {i for i, r in ((analysis or {}).get("resultats") or {}).items() if r.get("statut") != "passe"}


def promotion_verdict(reference: dict, runs: list, allow_equal: bool = False) -> dict:
    """
    Promotion de la branche de travail : {ok, motif}. Chaque run complet sur la branche de travail : init verte,
    aucun nouvel échec face au run de référence, moins d'échecs que lui ; suite de référence entièrement verte (ou
    absente) : deux runs complets verts consécutifs.
    """
    before = failing(reference)
    green_suite = bool(reference) and not before and not (reference or {}).get("init_en_echec")
    if not runs:
        return {"ok": False, "motif": "aucun run complet sur la branche de travail"}
    for number, run in enumerate(runs, 1):
        if run.get("erreur") or run.get("non_analyse"):
            return {"ok": False, "motif": f"run {number} non obtenu : {run.get('erreur') or run['non_analyse']}"}
        if not run.get("run_complet"):
            return {"ok": False, "motif": f"run {number} : pas un run complet de la suite active"}
        if run.get("init_en_echec"):
            return {"ok": False, "motif": f"run {number} : init en échec"}
        new = sorted(failing(run) - before)
        if new:
            return {"ok": False, "motif": f"run {number} : {len(new)} nouvel(s) échec(s) : "
                                          + ", ".join(i.split("::", 1)[-1] for i in new[:5])}
    last = runs[-1]
    if allow_equal:
        # Montée de version : pas plus d'échecs qu'avant (init verte, aucun nouvel échec, vérifiés ci-dessus)
        if len(failing(last)) > len(before):
            return {"ok": False, "motif": f"plus d'échecs qu'avant : {len(failing(last))} pour {len(before)}"}
        return {"ok": True, "motif": f"{len(before)} → {len(failing(last))} échec(s), aucun nouvel échec, init verte"}
    if green_suite or not reference:
        greens = [r for r in runs if not failing(r)]
        if len(runs) < 2 or len(greens) < 2 or failing(runs[-1]) or failing(runs[-2]):
            return {"ok": False, "motif": "suite entièrement verte : deux runs complets verts consécutifs exigés "
                                          f"({len(greens)} vert(s) sur {len(runs)})"}
        return {"ok": True, "motif": "deux runs complets verts consécutifs"}
    if len(failing(last)) >= len(before):
        return {"ok": False, "motif": f"pas moins d'échecs : {len(failing(last))} pour {len(before)} au run de "
                                      f"référence"}
    return {"ok": True, "motif": f"{len(before)} → {len(failing(last))} échec(s), aucun nouvel échec, init verte"}


def promote(worktree: str, target: str, client: str = "", decision: bool = False) -> dict:
    """
    Fusion rapide de HEAD (branche de travail, déjà poussée) vers `target` : push sans forcer, seulement si la cible
    distante est un ancêtre de HEAD. Renvoie {ok, motif, commit, avant}.
    """
    _git(worktree, "fetch", "-q", "origin", target)
    head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    remote = f"origin/{target}"
    before = _git(worktree, "rev-parse", "--verify", "-q", remote).stdout.strip()
    if not before:
        return {"ok": False, "motif": f"branche cible {target} introuvable sur origin", "commit": head, "avant": ""}
    if _git(worktree, "merge-base", "--is-ancestor", before, head).returncode != 0:
        return {"ok": False, "motif": f"fusion rapide impossible : {target} a avancé depuis la branche de travail "
                                      f"(à intégrer d'abord, jamais de push forcé)", "commit": head, "avant": before}
    # Commits promus : déjà envoyés sur la branche de travail, seule la cible change (fichiers contrôlés à nouveau)
    error = check_push(worktree, target, client, decision=decision, head=head) or _promotion_files(
        worktree, before, head, client, decision)
    if error:
        return {"ok": False, "motif": error, "commit": head, "avant": before}
    pushed = _git(worktree, "push", "origin", f"{head}:refs/heads/{target}")
    if pushed.returncode != 0:
        return {"ok": False, "motif": f"push vers {target} refusé : {pushed.stderr.strip()[-200:]}", "commit": head,
                "avant": before}
    return {"ok": True, "motif": f"{target} : {before[:8]} → {head[:8]} (fusion rapide)", "commit": head,
            "avant": before}


def _promotion_files(worktree: str, before: str, head: str, client: str, decision: bool = False) -> str:
    from tools import cucumber_profile
    protected = cucumber_profile.effective_profile(cucumber_profile.load_profile(client)).get(
        "protected_files") if client else []
    errors = file_errors(worktree, before, head, protected or [], decision=decision)
    if errors:
        motif = "fichiers refusés : " + " ; ".join(errors[:5])
        log_event(client, "refus", motif=motif, branche="promotion")
        return motif
    return ""


def regression_to_revert(previous: dict, current: dict, promotion: dict, touched: list) -> dict:
    """
    Run sur la branche cible après une promotion : {revert, motif, nouveaux}. Annulation si des échecs nouveaux
    (absents ou verts au run précédent) portent sur une feature touchée par les commits promus (`touched`).
    """
    new = sorted(failing(current) - failing(previous))
    linked = [i for i in new if i.split("::", 1)[0] in set(touched)]
    if not promotion or not linked:
        return {"revert": False, "motif": "aucun échec nouveau lié au dernier commit de l'agent", "nouveaux": new}
    return {"revert": True, "nouveaux": linked,
            "motif": f"{len(linked)} échec(s) nouveau(x) dans des features modifiées par la promotion "
                     f"{promotion.get('commit', '')[:8]} : " + ", ".join(i.split("::", 1)[-1] for i in linked[:5])}


def touched_files(repo: str, before: str, head: str) -> list:
    return [p for _, p in _changes(repo, before, head)] if before and head else []


def revert_on_target(worktree: str, target: str, before: str, head: str, reason: str, client: str = "") -> dict:
    """
    Commit d'annulation (git revert before..head) sur la cible, dans `worktree` extrait sur origin/<target>, poussé
    sans forcer. Jamais de réécriture d'historique. Renvoie {ok, motif, commit}.
    """
    _git(worktree, "fetch", "-q", "origin", target)
    _git(worktree, "checkout", "-q", "--detach", f"origin/{target}")
    reverted = _git(worktree, "revert", "--no-edit", f"{before}..{head}")
    if reverted.returncode != 0:
        _git(worktree, "revert", "--abort")
        return {"ok": False, "motif": f"annulation impossible : {reverted.stderr.strip()[-200:]}", "commit": ""}
    if launched_by() or reason:
        message = _git(worktree, "log", "-1", "--format=%B").stdout.strip()
        _git(worktree, "commit", "-q", "--amend", "-m", message + f"\n\nCause : {reason[:300]}"
             + (f"\nLancé-par: {launched_by()}" if launched_by() else ""))
    # Retour exact à l'état d'avant la promotion sur les fichiers touchés : scénarios ajoutés par l'agent retirés
    # légitimement (sinon : contrôle complet, sans décision humaine)
    touched = touched_files(worktree, before, head)
    exact = bool(touched) and _git(worktree, "diff", "--quiet", before, "HEAD", "--", *touched).returncode == 0
    error = check_push(worktree, target, client, decision=exact)
    if error:
        return {"ok": False, "motif": error, "commit": ""}
    commit = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    pushed = _git(worktree, "push", "origin", f"HEAD:refs/heads/{target}")
    if pushed.returncode != 0:
        return {"ok": False, "motif": f"push de l'annulation refusé : {pushed.stderr.strip()[-200:]}", "commit": ""}
    return {"ok": True, "motif": f"annulation {commit[:8]} poussée sur {target} (sans forcer)", "commit": commit}


# ---------------------------------------------------------------------------
# Droits du jeton Bitbucket
# ---------------------------------------------------------------------------

def token_scope(fetch=None) -> dict:
    """
    Dépôts que le jeton BITBUCKET_HTTP_TOKEN peut écrire (API REST, permission REPO_WRITE) : {"ok", "trop_larges",
    "erreur"}. `fetch(start)` : page JSON (tests) ; par défaut l'API Bitbucket de BITBUCKET_URL / BITBUCKET_BASE_URL.
    """
    if fetch is None:
        import requests
        token = os.getenv("BITBUCKET_HTTP_TOKEN", "").strip()
        base = (os.getenv("BITBUCKET_URL") or os.getenv("BITBUCKET_BASE_URL") or "").strip().rstrip("/")
        if not base:
            # Hôte d'un dépôt configuré (URL de projet client ou du Cucumber Standard)
            for name in ("BITBUCKET_CLIENT_REPO_PATH", "BITBUCKET_CUCUMBER_STD_REPO"):
                found = re.match(r"^(https?://[^/]+)", os.getenv(name, "").strip())
                if found:
                    base = found.group(1)
                    break
        if not (token and base):
            return {"ok": None, "trop_larges": [], "erreur": "jeton ou adresse Bitbucket non configurés"}

        def fetch(start):
            response = requests.get(f"{base}/rest/api/1.0/repos", params={"permission": "REPO_WRITE", "start": start,
                                                                          "limit": 100},
                                    headers={"Authorization": f"Bearer {token}"}, timeout=10)
            response.raise_for_status()
            return response.json()
    writable, start = [], 0
    try:
        for _ in range(50):
            page = fetch(start)
            writable += [f"{(r.get('project') or {}).get('key', '?')}/{r.get('slug', '')}" for r in page.get("values")
                         or []]
            if page.get("isLastPage", True):
                break
            start = page.get("nextPageStart", start + 100)
    except Exception as e:  # noqa: BLE001 - réseau, droits : droits non vérifiés, affiché
        return {"ok": None, "trop_larges": [], "erreur": f"droits du jeton non vérifiés : {e}"}
    wide = sorted(r for r in writable if not _CUCUMBER_REPO.search(r.rsplit("/", 1)[-1]))
    return {"ok": not wide, "trop_larges": wide, "erreur": "", "ecriture": len(writable)}


def token_scope_line(scope: dict) -> str:
    if scope.get("ok") is None:
        return f"⚠️ {scope.get('erreur')}"
    if scope["ok"]:
        return "Jeton Bitbucket : écriture limitée aux dépôts *-cucumber."
    return (f"⛔ Jeton Bitbucket trop large : écriture possible sur {len(scope['trop_larges'])} dépôt(s) hors "
            f"*-cucumber ({', '.join(scope['trop_larges'][:5])}…) — réduisez ses droits à la lecture seule.")
