"""
Registre des incidents résolus (0 token), par client et global : l'agent retient chaque échec corrigé, sa correction
et la preuve qu'elle marche (build vert), et l'applique seul quand l'échec revient — y compris après un changement de
version du client. Aucune valeur propre à un client dans le code : tout vient des runs observés.

1. Registre : .tmp/cucumber/<client>/incidents.json (client) et <AGENT_KNOWLEDGE_DIR>/cucumber/incidents.json
   (global, partagé). Fichiers versionnés (« format », « revision » incrémentée à chaque écriture). Un incident :
   signature (step sans ses valeurs + motif du message), cause, correction (actions rejouables : valeur du profil,
   version KIS, paramètre du job, step du projet en patch, relance), preuves (builds verts), versions concernées.
   Alimenté automatiquement (observe) : chaque échec d'init ouvre un incident ; le run vert suivant le ferme avec les
   changements faits entre l'échec et le run vert (réglages du profil, commits de l'espace de l'agent).
2. Consultation (lookup) : à chaque échec, la signature est cherchée d'abord dans le registre du client, puis dans le
   registre global. Correction connue du client → appliquée sans appel à Claude (apply), puis run de vérification ;
   sinon les incidents proches sont donnés en contexte à l'analyse (context_block).
3. Changement de version (carry_over) : les réglages prouvés de l'ancienne version (version KIS, clear, adresse de
   test, modules exclus, paramètres du job, délai d'attente, steps de contournement) sont reportés sur la nouvelle et
   doivent être revalidés par un run d'init avant toute génération (revalidation) ; un échec est un écart de version,
   donné avec l'ancien réglage et sa preuve (version_gap).
4. Conflit de verrouillage (StaleState, OptimisticLock, « Batch update returned unexpected row count ») : une seule
   relance automatique (run_with_lock_retry) ; s'il persiste, builds Jenkins et copies de base sur le même schéma au
   même moment (concurrency).
5. Bilan : « incident connu (n° …), correction appliquée » ou « incident nouveau » (line).
"""
import difflib
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime

from tools import cucumber_tools

FORMAT = 1
FILE_NAME = "incidents.json"
GLOBAL_FOLDER = "cucumber"
NATURE_LOCK = "conflit_verrouillage"
# Actions rejouables sans appel à Claude
ACTION_TYPES = ("profil", "kis", "parametre", "step_projet", "relance")
# Réglages suivis (profil appliqué) : leur changement entre un échec et le run vert suivant est une correction
PROFILE_SETTINGS = ("clear_name", "adresse_test", "modules_exclus", "init_tags", "workarounds")
SETTING_LABELS = {"kis_version": "version KIS", "clear_name": "clear", "adresse_test": "adresse de livraison de test",
                  "modules_exclus": "modules exclus", "job_params": "paramètres du job", "init_tags": "tags d'init",
                  "workarounds": "contournements TODO-VERSION", "delai_attente": "délai d'attente",
                  "steps": "steps de contournement"}

LOCK_CONFLICT = re.compile(r"Batch update returned unexpected row count|StaleState(?:Exception)?|StaleObjectState|"
                           r"OptimisticLock(?:ing)?(?:Exception|FailureException)?|ObjectOptimisticLockingFailure|"
                           r"Row was updated or deleted by another transaction", re.I)
_LOCK_TABLE = re.compile(r"(?:table|entity|entité|sur)\s*[«\"'\[]?\s*([a-z][a-z0-9_]*_[a-z0-9_]+)", re.I)
_TABLE_NAME = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
# Motifs nommés : le motif d'une signature, à défaut le message sans ses valeurs variables
MOTIFS = (
    (re.compile(r"No route found", re.I), lambda m: "No route found"),
    (re.compile(r"UnsupportedClassVersionError", re.I), lambda m: "UnsupportedClassVersionError"),
    (re.compile(r"ConditionTimeoutException\b[\s\S]{0,400}?\b(\d+)\s*seconds", re.I),
     lambda m: f"ConditionTimeoutException {m.group(1)} seconds"),
    (re.compile(r"ConditionTimeoutException", re.I), lambda m: "ConditionTimeoutException"),
    (re.compile(r"adresse de livraison incoh[ée]rente", re.I), lambda m: "Adresse de livraison incohérente"),
    (re.compile(r"has no field\s+[\"'«]?(\w+)", re.I), lambda m: f"has no field {m.group(1)}"),
    (LOCK_CONFLICT, lambda m: "conflit de verrouillage"),
)
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'|«[^»]*»")


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------

def step_key(step: str) -> str:
    """Step sans ses valeurs (arguments entre guillemets), casse et espaces normalisés."""
    return " ".join(_QUOTED.sub("\"…\"", step or "").lower().split())


def motif(message: str) -> str:
    """Motif d'un message : motif nommé (MOTIFS), sinon le message sans dates, identifiants, valeurs ni nombres."""
    text = str(message or "")
    for pattern, name in MOTIFS:
        found = pattern.search(text)
        if found:
            return name(found)
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+Z?", "<date>", text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{20,}\b", "<id>", text, flags=re.I)
    text = re.sub(r"\"[^\"]*\"|'[^']*'|«[^»]*»|<[^<>]*>", "<v>", text)
    text = re.sub(r"\b(?=[\w-]*\d)[\w-]+\b", "<n>", text)
    return " ".join(text.split())[:160]


def signature(step: str, message: str) -> dict:
    return {"step": step_key(step), "motif": motif(message)}


def same_signature(a: dict, b: dict) -> bool:
    """Même motif, et même step (un incident sans step vaut pour tout step)."""
    return bool(a.get("motif")) and a.get("motif") == b.get("motif") and (
        not a.get("step") or not b.get("step") or a["step"] == b["step"])


def is_lock_conflict(text: str) -> bool:
    return bool(LOCK_CONFLICT.search(str(text or "")))


def lock_table(text: str) -> str:
    """Table en conflit de verrouillage citée par le message ('' si absente)."""
    text = str(text or "")
    found = _LOCK_TABLE.search(text)
    if found:
        return found.group(1)
    tables = [t for t in _TABLE_NAME.findall(text) if not t.startswith(("java_", "org_"))]
    return tables[0] if tables else ""


# ---------------------------------------------------------------------------
# Registres (client et global)
# ---------------------------------------------------------------------------

def client_path(client: str) -> str:
    return os.path.join(cucumber_tools.cache_dir(client), FILE_NAME)


def global_path() -> str:
    """Registre global dans les connaissances partagées ('' si AGENT_KNOWLEDGE_DIR est vide)."""
    from tools import knowledge_tools
    base = knowledge_tools.knowledge_dir()
    return os.path.join(base, GLOBAL_FOLDER, FILE_NAME) if base else ""


def _empty(client: str = "") -> dict:
    data = {"format": FORMAT, "revision": 0, "incidents": []}
    if client:
        data.update(client=client, reglages={}, ouverts=[], revalidation={})
    return data


def load(client: str) -> dict:
    data = cucumber_tools._read_json(client_path(client), None)
    if not isinstance(data, dict):
        return _empty(client)
    for key, value in _empty(client).items():
        data.setdefault(key, value)
    return data


def save(client: str, data: dict) -> None:
    data["format"], data["revision"] = FORMAT, int(data.get("revision") or 0) + 1
    data["date"] = datetime.now().isoformat(timespec="seconds")
    cucumber_tools._write_json(client_path(client), data)


def load_global() -> dict:
    path = global_path()
    if not path:
        return _empty()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _empty()
    return data if isinstance(data, dict) and isinstance(data.get("incidents"), list) else _empty()


def _save_global(data: dict) -> bool:
    """Registre global réécrit d'un bloc (secrets masqués) ; False si le partage est indisponible."""
    from tools import knowledge_tools, secret_tools
    path = global_path()
    if not path:
        return False
    data["format"], data["revision"] = FORMAT, int(data.get("revision") or 0) + 1
    data["date"] = datetime.now().isoformat(timespec="seconds")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return knowledge_tools.write_atomic(path, secret_tools.mask_secrets(
            json.dumps(data, ensure_ascii=False, indent=1))[0], overwrite=True)
    except OSError as e:
        print(f"[INCIDENTS] ⚠️ Registre global non écrit ({path}) : {e}")
        return False


def _action_key(actions: list) -> str:
    """Clé d'une correction (actions, patchs réduits à leur empreinte)."""
    reduced = [{k: (hashlib.sha1(v.encode("utf-8")).hexdigest() if k == "patch" else v) for k, v in a.items()
                if k != "libelle"} for a in actions or []]
    return json.dumps(reduced, sort_keys=True, ensure_ascii=False)


def _upsert(incidents: list, sig: dict, cause: str, actions: list, proof: dict, version: str, example: str,
            client: str = "") -> dict:
    key = _action_key(actions)
    entry = next((e for e in incidents if e.get("signature") == sig and _action_key(
        (e.get("correction") or {}).get("actions")) == key), None)
    if entry is None:
        entry = {"numero": max((int(e.get("numero") or 0) for e in incidents), default=0) + 1, "signature": sig,
                 "exemple": (example or "")[:400], "cause": cause, "versions": [], "preuves": [], "appliquee": 0,
                 "correction": {"actions": actions, "libelle": correction_label(actions)},
                 "date": datetime.now().isoformat(timespec="seconds")}
        if client:
            entry["clients"] = []
        incidents.append(entry)
    entry["cause"] = cause or entry.get("cause", "")
    if version and version not in entry["versions"]:
        entry["versions"].append(version)
    if client and client not in entry.setdefault("clients", []):
        entry["clients"].append(client)
    entry["preuves"] = (entry.get("preuves") or []) + [proof]
    entry["preuves"] = entry["preuves"][-10:]
    return entry


def record(client: str, sig: dict, cause: str, actions: list, proof: dict, version: str = "",
           example: str = "", share: bool = True) -> dict:
    """Incident résolu (correction suivie d'un run vert) ajouté ou complété, côté client puis global. Renvoie l'entrée."""
    data = load(client)
    entry = _upsert(data["incidents"], sig, cause, actions, proof, version, example)
    save(client, data)
    if share and global_path():
        shared = load_global()
        _upsert(shared["incidents"], sig, cause, actions, {**proof, "client": client}, version, example, client)
        _save_global(shared)
    return entry


def correction_label(actions: list) -> str:
    parts = []
    for action in actions or []:
        kind = action.get("type")
        if kind == "profil":
            parts += [f"{SETTING_LABELS.get(k, k)} = {_short(v)}" for k, v in (action.get("champs") or {}).items()]
        elif kind == "kis":
            parts.append(f"version KIS = {action.get('valeur')}")
        elif kind == "parametre":
            parts += [f"paramètre {k} = {_short(v)}" for k, v in (action.get("parametres") or {}).items()]
        elif kind == "step_projet":
            parts.append(f"step(s) du projet : {', '.join(action.get('fichiers') or []) or 'patch'}")
        elif kind == "relance":
            parts.append("relance du run")
    return " ; ".join(parts) or "aucune action"


def _short(value) -> str:
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        text = ", ".join(value) or "aucun"
    else:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 80 else text[:77] + "…"


# ---------------------------------------------------------------------------
# Consultation
# ---------------------------------------------------------------------------

def _latest(entry: dict) -> str:
    return max((p.get("date", "") for p in entry.get("preuves") or []), default=entry.get("date", ""))


def lookup(client: str, step: str, message: str, version: str = "") -> dict:
    """
    Incident connu pour cet échec : registre du client d'abord (correction applicable sans Claude), puis registre
    global (seules les relances y sont génériques ; le reste est donné en contexte). {} si incident nouveau.
    Renvoie {"numero", "origine", "applicable", "incident"}.
    """
    sig = signature(step, message)
    own = [e for e in load(client)["incidents"] if same_signature(e.get("signature") or {}, sig)
           and (e.get("correction") or {}).get("actions")]
    if own:
        best = max(own, key=lambda e: (version in (e.get("versions") or []), e.get("signature", {}).get("step") == sig[
            "step"], _latest(e)))
        return {"numero": best["numero"], "origine": "client", "applicable": True, "incident": best}
    shared = [e for e in load_global()["incidents"] if same_signature(e.get("signature") or {}, sig)]
    if shared:
        best = max(shared, key=lambda e: (client in (e.get("clients") or []), _latest(e)))
        generic = all(a.get("type") == "relance" for a in (best.get("correction") or {}).get("actions") or [])
        return {"numero": f"G{best['numero']}", "origine": "global", "applicable": generic, "incident": best}
    return {}


def nearby(client: str, step: str, message: str, limit: int = 3) -> list:
    """Incidents proches (client puis global), pour l'analyse d'un incident nouveau."""
    sig = signature(step, message)
    target = f"{sig['step']} {sig['motif']}"
    scored = []
    for origin, data in (("client", load(client)), ("global", load_global())):
        for entry in data["incidents"]:
            other = entry.get("signature") or {}
            ratio = difflib.SequenceMatcher(None, target, f"{other.get('step', '')} {other.get('motif', '')}").ratio()
            if ratio >= 0.45 or (other.get("motif") and other.get("motif") == sig["motif"]):
                label = entry["numero"] if origin == "client" else f"G{entry['numero']}"
                scored.append((ratio, {**entry, "numero": label, "origine": origin}))
    return [e for _, e in sorted(scored, key=lambda x: -x[0])[:limit]]


def context_block(client: str, failures) -> str:
    """Bloc « INCIDENTS PROCHES » ajouté au contexte de l'analyse d'un échec ('' si aucun)."""
    lines, seen = [], set()
    for failure in failures or []:
        for entry in nearby(client, failure.get("step", ""), failure.get("message", "")):
            if entry["numero"] in seen:
                continue
            seen.add(entry["numero"])
            proof = (entry.get("preuves") or [{}])[-1]
            lines.append(f"- n° {entry['numero']} ({entry['origine']}) step « {entry['signature'].get('step')} » — "
                         f"motif « {entry['signature'].get('motif')} » — cause : {entry.get('cause', '')[:200]} — "
                         f"correction : {(entry.get('correction') or {}).get('libelle', '')[:300]} — preuve : build "
                         f"{proof.get('build') or '?'} (versions {', '.join(entry.get('versions') or []) or '?'})")
    if not lines:
        return ""
    return ("\n\nINCIDENTS PROCHES DÉJÀ RÉSOLUS (registre des incidents : réutilise leur correction si la cause est "
            "la même, sinon dis pourquoi elle ne s'applique pas) :\n" + "\n".join(lines[:6]))


def line(match: dict, applied: bool = False) -> str:
    """Bilan d'un échec : « incident connu (n° …), correction appliquée » ou « incident nouveau »."""
    if not match:
        return "incident nouveau"
    label = (match.get("incident", {}).get("correction") or {}).get("libelle", "")
    if applied:
        return f"incident connu (n° {match['numero']}), correction appliquée : {label}"
    if match.get("applicable"):
        return f"incident connu (n° {match['numero']}), correction connue : {label}"
    return f"incident connu (n° {match['numero']}, registre global), correction à adapter : {label}"


def describe(client: str, step: str, message: str, version: str = "") -> dict:
    """{"numero", "libelle"} du bilan d'un échec (incident connu ou nouveau)."""
    match = lookup(client, step, message, version)
    return {"numero": match.get("numero", ""), "connu": bool(match), "libelle": line(match)}


# ---------------------------------------------------------------------------
# Application d'une correction connue (0 token)
# ---------------------------------------------------------------------------

def _git(worktree: str, *args, stdin: str = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", input=stdin)


def apply_patch(worktree: str, patch: str) -> str:
    """Patch d'un step du projet appliqué au worktree : '' si appliqué, 'deja' si déjà présent, sinon l'erreur."""
    if not patch.strip():
        return "patch vide"
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8", newline="") as f:
        f.write(patch if patch.endswith("\n") else patch + "\n")
        path = f.name
    try:
        if _git(worktree, "apply", "--reverse", "--check", path).returncode == 0:
            return "deja"
        done = _git(worktree, "apply", "--whitespace=nowarn", path)
        if done.returncode != 0:
            done = _git(worktree, "apply", "--3way", "--whitespace=nowarn", path)
        return "" if done.returncode == 0 else (done.stderr.strip().splitlines() or ["patch non applicable"])[-1]
    finally:
        os.remove(path)


def _save_profile_fields(client: str, fields: dict) -> None:
    from tools import cucumber_profile
    document = cucumber_profile.load_profile(client)
    document.setdefault("saisi", {}).update(fields)
    cucumber_tools._write_json(cucumber_profile.profile_path(client), document)


def _save_job_params(client: str, params: dict) -> None:
    from tools import cucumber_profile
    current = cucumber_profile.effective_profile(cucumber_profile.load_profile(client)).get("job_params") or []
    kept = [p for p in current if p.split("=", 1)[0].strip() not in params]
    _save_profile_fields(client, {"job_params": kept + [f"{k}={v}" for k, v in params.items()]})


def _save_kis(client: str, kis: str, origin: dict) -> None:
    from tools import client_version, cucumber_profile
    document = cucumber_profile.load_profile(client)
    current = document.get(client_version.FIELD) or {}
    deduction = dict(current.get("deduction") or {})
    if not deduction:
        return
    deduction["jenkins"] = {**(deduction.get("jenkins") or {}), "kis_version": kis}
    deduction["kis"] = {"kis": kis, **origin}
    client_version.save(client, current.get("choix") or {}, deduction)


def apply(client: str, incident: dict, worktree: str = "", commit: bool = True) -> dict:
    """
    Correction d'un incident connu appliquée sans Claude : profil, version KIS, paramètres du job, steps du projet
    (patch appliqué au worktree, commité). Renvoie {"ok", "fichiers", "relance", "action", "erreurs"}.
    """
    out = {"ok": True, "fichiers": [], "relance": False, "erreurs": [], "action": ""}
    done = []
    for action in (incident.get("correction") or {}).get("actions") or []:
        kind = action.get("type")
        if kind == "profil":
            _save_profile_fields(client, action.get("champs") or {})
            done.append(correction_label([action]))
        elif kind == "kis":
            _save_kis(client, action.get("valeur", ""), {"source": "incident", "incident": incident.get("numero")})
            done.append(correction_label([action]))
        elif kind == "parametre":
            _save_job_params(client, action.get("parametres") or {})
            done.append(correction_label([action]))
        elif kind == "relance":
            out["relance"] = True
            done.append("relance du run")
        elif kind == "step_projet":
            if not worktree or not os.path.isdir(worktree):
                out["erreurs"].append("step du projet : aucun espace de travail")
                continue
            error = apply_patch(worktree, action.get("patch", ""))
            if error == "deja":
                done.append(f"step(s) du projet déjà présent(s) : {', '.join(action.get('fichiers') or [])}")
            elif error:
                out["erreurs"].append(f"step(s) du projet {', '.join(action.get('fichiers') or [])} : {error}")
            else:
                out["fichiers"] += list(action.get("fichiers") or [])
                done.append(correction_label([action]))
    if out["fichiers"] and commit:
        _git(worktree, "add", "--", *out["fichiers"])
        _git(worktree, "commit", "-q", "-m", f"Cucumber : correction connue de l'incident n° {incident.get('numero')} "
                                             f"({(incident.get('signature') or {}).get('motif', '')[:80]})")
    out["ok"] = not out["erreurs"]
    out["action"] = f"incident connu (n° {incident.get('numero')}), correction appliquée : " + (" ; ".join(done) or
                                                                                              "aucune action")
    data = load(client)
    for entry in data["incidents"]:
        if entry.get("numero") == incident.get("numero"):
            entry["appliquee"] = int(entry.get("appliquee") or 0) + 1
    save(client, data)
    return out


# ---------------------------------------------------------------------------
# Alimentation automatique : échec ouvert, fermé par le run vert suivant
# ---------------------------------------------------------------------------

def settings(profile: dict) -> dict:
    """Réglages suivis du profil appliqué (et version KIS de la version du client)."""
    from tools import cucumber_wait_steps
    deduction = ((profile or {}).get("version_client") or {}).get("deduction") or {}
    values = {k: (profile or {}).get(k) for k in PROFILE_SETTINGS}
    values["modules_exclus"] = sorted(values.get("modules_exclus") or [])
    values["kis_version"] = (deduction.get("jenkins") or {}).get("kis_version", "")
    values["job_params"] = sorted((profile or {}).get("job_params") or [])
    values["delai_attente"] = os.getenv(cucumber_wait_steps.DELAY_ENV, "").strip()
    return values


def version_of(profile: dict) -> str:
    return ((((profile or {}).get("version_client") or {}).get("deduction") or {}).get("version_client") or "")


def _params(values: list) -> dict:
    return {p.split("=", 1)[0].strip(): p.split("=", 1)[1] if "=" in p else "" for p in values or []}


def setting_actions(before: dict, after: dict) -> list:
    """Actions rejouables d'après les réglages changés entre un échec et le run vert suivant."""
    actions = []
    fields = {k: after.get(k) for k in PROFILE_SETTINGS if after.get(k) != before.get(k) and after.get(k)}
    if fields:
        actions.append({"type": "profil", "champs": fields})
    if after.get("kis_version") and after.get("kis_version") != before.get("kis_version"):
        actions.append({"type": "kis", "valeur": after["kis_version"]})
    old, new = _params(before.get("job_params")), _params(after.get("job_params"))
    changed = {k: v for k, v in new.items() if old.get(k) != v}
    if changed:
        actions.append({"type": "parametre", "parametres": changed})
    return actions


def _agent_files(paths: list) -> list:
    return [p for p in paths if p and not p.startswith("target/") and "/target/" not in p]


def step_actions(worktree: str, start: str, end: str) -> list:
    """Steps du projet changés entre deux commits de l'espace de l'agent : une action « step_projet » (patch)."""
    if not (worktree and start and end and start != end and os.path.isdir(worktree)):
        return []
    if _git(worktree, "cat-file", "-e", f"{start}^{{commit}}").returncode != 0:
        return []
    files = _agent_files(_git(worktree, "diff", "--name-only", start, end).stdout.split())
    if not files:
        return []
    patch = _git(worktree, "diff", "--binary", start, end, "--", *files).stdout
    return [{"type": "step_projet", "fichiers": files, "patch": patch}] if patch.strip() else []


def _head(worktree: str) -> str:
    return _git(worktree, "rev-parse", "HEAD").stdout.strip() if worktree and os.path.isdir(worktree) else ""


def observe(client: str, profile: dict, run: dict, worktree: str = "", applied: dict = None) -> dict:
    """
    Résultat d'un run d'init de l'agent (run_init_and_import : {ok, build, numero, init_en_echec}) : un échec ouvre
    (ou prolonge) un incident ; un run vert ferme les incidents ouverts avec les changements faits depuis (incident
    résolu, avec ce build pour preuve), enregistre les réglages prouvés de la version et clôt sa revalidation.
    `applied` : incident connu dont la correction vient d'être appliquée. Renvoie {"incident", "enregistres"}.
    """
    data = load(client)
    version = version_of(profile)
    now = datetime.now().isoformat(timespec="seconds")
    current = settings(profile)
    out = {"incident": {}, "enregistres": []}
    if not run.get("ok"):
        failure = run.get("init_en_echec") or next(iter(run.get("echecs") or []), {}) or {}
        step, message = failure.get("step", ""), failure.get("message") or run.get("message", "")
        if not (step or message):
            return out
        sig = signature(step, message)
        opened = data["ouverts"]
        if not opened or opened[-1].get("signature") != sig:
            opened.append({"signature": sig, "exemple": message[:400], "step": step, "build": run.get("build", ""),
                           "date": now, "reglages": current, "commit": _head(worktree), "espace": worktree,
                           "version": version})
        else:
            opened[-1]["build"] = run.get("build", "")
        if applied:
            opened[-1]["appliquee"] = applied.get("numero")
        out["incident"] = lookup(client, step, message, version)
        save(client, data)
        return out
    proof = {"build": run.get("build", ""), "import": run.get("numero") or "", "date": now, "version": version}
    head = _head(worktree)
    opened = data["ouverts"]
    for index, item in enumerate(opened):
        later = opened[index + 1] if index + 1 < len(opened) else {"reglages": current, "commit": head}
        actions = setting_actions(item.get("reglages") or {}, later.get("reglages") or {})
        same_space = (item.get("espace") or "") == (worktree or "")
        actions += step_actions(worktree, item.get("commit", ""), later.get("commit", "") or head) if same_space else []
        if not actions and is_lock_conflict(item.get("exemple", "")):
            actions = [{"type": "relance"}]
        if not actions:
            continue
        cause = (f"« {item.get('step') or '?'} » : {item.get('exemple', '')[:200]} — résolu par : "
                 f"{correction_label(actions)}")
        entry = _upsert(data["incidents"], item["signature"], cause, actions, proof, version, item.get("exemple", ""))
        out["enregistres"].append(entry["numero"])
        if global_path():
            shared = load_global()
            _upsert(shared["incidents"], item["signature"], cause, actions, {**proof, "client": client}, version,
                    item.get("exemple", ""), client)
            _save_global(shared)
    data["ouverts"] = []
    if version:
        steps = sorted({e["numero"] for e in data["incidents"] if version in (e.get("versions") or []) and any(
            a.get("type") == "step_projet" for a in (e.get("correction") or {}).get("actions") or [])})
        data["reglages"][version] = {"reglages": current, "steps": steps, "build": run.get("build", ""), "date": now}
    revalidation = data.get("revalidation") or {}
    if revalidation and revalidation.get("vers") == version and revalidation.get("statut") != "ok":
        revalidation.update(statut="ok", build=run.get("build", ""), date_fin=now)
        if current.get("kis_version"):
            _prove_kis(profile, current["kis_version"])
    save(client, data)
    return out


def _prove_kis(profile: dict, kis: str) -> None:
    """Version KIS reportée prouvée par un run vert : enregistrée pour la version du Standard."""
    from tools import client_version
    deduction = ((profile or {}).get("version_client") or {}).get("deduction") or {}
    standard = (deduction.get("standard") or {}).get("version", "")
    if standard and (deduction.get("kis") or {}).get("source") == "report":
        client_version.record_kis(standard, kis)


# ---------------------------------------------------------------------------
# Changement de version du client
# ---------------------------------------------------------------------------

def bootstrap(client: str, fetch_params=None, imports: list = None) -> list:
    """
    Réglages prouvés amorcés depuis l'historique : runs importés dont l'init est verte (au moins un test, aucune init
    en échec), version et KIS lues dans les paramètres de leur build (rôles composant_version et kis_version du job),
    autres réglages repris du profil actuel. Seules les versions sans réglages enregistrés sont amorcées, depuis leur
    run vert le plus récent. `imports` (tests) : [{"numero", "build", "analyse"}]. Renvoie les versions amorcées.
    """
    from tools import cucumber_import, cucumber_profile
    if fetch_params is None:
        from tools import jenkins_tools
        fetch_params = jenkins_tools.build_parameters
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))
    roles = profile.get("job_roles") or {}
    if not (roles.get("composant_version")):
        return []
    if imports is None:
        imports = [{"numero": e["numero"], "build": e.get("url") or e.get("build") or "",
                    "analyse": cucumber_import.load_analysis(client, e["numero"]),
                    "console": lambda n=e["numero"]: _import_console(cucumber_import.imports_dir(client), n)}
                   for e in cucumber_import.load_index(client).get("imports") or []]
    data = load(client)
    added = []
    for item in sorted(imports, key=lambda i: -int(i.get("numero") or 0)):
        analysis = item.get("analyse") or {}
        url = item.get("build", "")
        if not url.startswith("http") or analysis.get("init_en_echec") or not analysis.get("tests"):
            continue
        params = fetch_params(url) or {}
        version = str(params.get(roles["composant_version"]) or "").strip()
        if not version:
            # Build purgé de Jenkins : ligne de version lue dans la branche du dépôt Cucumber clonée par le job
            console = item.get("console")
            version = _console_version_line(console() if callable(console) else console or "")
        if not version or _proven_key(data, version) or version in added:
            continue
        values = settings(profile)
        # KIS : celle du build seulement (jamais celle de la version actuelle à la place d'une autre)
        values["kis_version"] = str(params.get(roles.get("kis_version") or "") or "")
        data["reglages"][version] = {"reglages": values, "steps": [], "build": url, "import": item.get("numero"),
                                     "ligne": not params,
                                     "date": datetime.now().isoformat(timespec="seconds"),
                                     "source": "amorce : run importé à init verte (KIS du build s'il est encore sur "
                                               "Jenkins, autres réglages du profil actuel)"}
        added.append(version)
    if added:
        save(client, data)
    return added


_CLONE_BRANCH = re.compile(r"git clone\b[^\n]*?--branch\s+(\S+)\s+\S*-cucumber(?:\.git)?\b")


def _import_console(folder: str, numero) -> str:
    path = os.path.join(folder, str(numero))
    names = sorted(n for n in os.listdir(path) if "console" in n.lower()) if os.path.isdir(path) else []
    return "\n".join(cucumber_tools._read_text_file(os.path.join(path, n)) for n in names)


def _console_version_line(console: str) -> str:
    """Ligne de version (majeure.mineure) de la branche du dépôt <client>-cucumber clonée par le build ('' sinon)."""
    from tools import cucumber_repo_setup
    found = _CLONE_BRANCH.search(console or "")
    branch = found.group(1).strip("'\"") if found else ""
    return cucumber_repo_setup.version_line(branch) if re.search(r"\d+\.\d+", branch) else ""


def _proven_key(data: dict, version: str) -> str:
    """Clé des réglages prouvés d'une version : la version exacte, sinon une ligne majeure.mineure amorcée."""
    from tools import cucumber_repo_setup
    proven = data.get("reglages") or {}
    if version in proven:
        return version
    line = cucumber_repo_setup.version_line(version)
    return next((k for k in proven if line and k == line and proven[k].get("ligne")), "")


def carry_over(client: str, old: dict, new: dict) -> dict:
    """
    Version du client changée (`old` → `new` : déductions) : réglages prouvés de l'ancienne version reportés sur la
    nouvelle (version KIS absente de la nouvelle, champs du profil), steps de contournement listés pour la
    revalidation, qui doit passer (run d'init vert) avant toute génération. Renvoie la revalidation ({} si rien de
    prouvé pour l'ancienne version). `new` peut être complété (KIS) : la déduction enregistrée est mise à jour.
    """
    before, after = (old or {}).get("version_client", ""), (new or {}).get("version_client", "")
    if not before or not after or before == after:
        return {}
    data = load(client)
    if not _proven_key(data, before):
        try:
            bootstrap(client)
        except Exception as e:  # noqa: BLE001 - historique illisible : rien à reporter, jamais bloquant
            print(f"[INCIDENTS] ⚠️ Amorçage des réglages prouvés impossible : {e}")
        data = load(client)
    proven = (data.get("reglages") or {}).get(_proven_key(data, before)) or {}
    if not proven:
        return {}
    values, build = proven.get("reglages") or {}, proven.get("build", "")
    reports = []
    from tools import cucumber_profile
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))
    if values.get("kis_version"):
        current = ((new.get("jenkins") or {}).get("kis_version") or "")
        if not current:
            _save_kis(client, values["kis_version"], {"source": "report", "version": before, "build": build})
        reports.append({"reglage": "kis_version", "valeur": values["kis_version"], "preuve": build,
                        "applique": not current, "actuel": current})
    fields = {}
    for key in PROFILE_SETTINGS:
        old_value, now_value = values.get(key), profile.get(key)
        if key == "modules_exclus":
            now_value = sorted(now_value or [])
        if not old_value:
            continue
        if old_value != now_value:
            fields[key] = old_value
        reports.append({"reglage": key, "valeur": old_value, "preuve": build, "applique": old_value != now_value})
    if fields:
        _save_profile_fields(client, fields)
    if values.get("job_params"):
        missing = {k: v for k, v in _params(values["job_params"]).items()
                   if _params(profile.get("job_params") or []).get(k) != v}
        if missing:
            _save_job_params(client, missing)
        reports.append({"reglage": "job_params", "valeur": values["job_params"], "preuve": build,
                        "applique": bool(missing)})
    if values.get("delai_attente"):
        reports.append({"reglage": "delai_attente", "valeur": values["delai_attente"], "preuve": build,
                        "applique": False})
    steps = list(proven.get("steps") or [])
    if steps:
        reports.append({"reglage": "steps", "valeur": [f"n° {n}" for n in steps], "preuve": build, "applique": False})
    data["revalidation"] = {"de": before, "vers": after, "statut": "a_faire", "reports": reports, "steps": steps,
                            "date": datetime.now().isoformat(timespec="seconds")}
    save(client, data)
    return data["revalidation"]


def revalidation(client: str) -> dict:
    return load(client).get("revalidation") or {}


def pending(client: str, version: str = "") -> dict:
    """Revalidation à faire (ou en écart) pour la version courante ({} sinon)."""
    current = revalidation(client)
    if current.get("statut") in ("a_faire", "ecart") and (not version or current.get("vers") == version):
        return current
    return {}


def apply_carried_steps(client: str, worktree: str) -> dict:
    """Steps de contournement de l'ancienne version appliqués au worktree (un commit). {"fichiers", "erreurs"}."""
    current = revalidation(client)
    incidents = {e["numero"]: e for e in load(client)["incidents"]}
    files, errors = [], []
    for numero in current.get("steps") or []:
        entry = incidents.get(numero)
        if not entry:
            continue
        for action in (entry.get("correction") or {}).get("actions") or []:
            if action.get("type") != "step_projet":
                continue
            error = apply_patch(worktree, action.get("patch", ""))
            if error and error != "deja":
                errors.append(f"n° {numero} ({', '.join(action.get('fichiers') or [])}) : {error}")
            elif not error:
                files += action.get("fichiers") or []
    if files:
        _git(worktree, "add", "--", *files)
        _git(worktree, "commit", "-q", "-m", f"Cucumber : steps de contournement de la version {current.get('de')} "
                                             f"reportés sur {current.get('vers')} (incidents "
                                             f"{', '.join(str(n) for n in current.get('steps') or [])})")
    return {"fichiers": files, "erreurs": errors}


def version_gap(client: str, failure: dict, build: str = "") -> list:
    """
    Revalidation en échec : écart de version, avec l'ancien réglage et sa preuve (le réglage visé par l'incident
    connu de cet échec s'il y en a un, sinon tous les réglages reportés). Enregistré ; renvoie les lignes du bilan.
    """
    data = load(client)
    current = data.get("revalidation") or {}
    if not current:
        return []
    step, message = failure.get("step", ""), failure.get("message", "")
    match = lookup(client, step, message, current.get("de", ""))
    targeted = set()
    for action in ((match.get("incident") or {}).get("correction") or {}).get("actions") or []:
        targeted |= set((action.get("champs") or {}).keys()) if action.get("type") == "profil" else {
            {"kis": "kis_version", "parametre": "job_params", "step_projet": "steps"}.get(action.get("type"), "")}
    reports = [r for r in current.get("reports") or [] if r["reglage"] in targeted] or current.get("reports") or []
    lines = [f"Écart de version {current.get('de')} → {current.get('vers')} : l'init échoue sur « {step or '?'} » — "
             f"{message[:200]}" + (f" (build {build})" if build else "")]
    lines += [f"    {SETTING_LABELS.get(r['reglage'], r['reglage'])} de la version {current.get('de')} : "
              f"{_short(r['valeur'])} — preuve : init verte, build {r.get('preuve') or '?'}" for r in reports]
    if match:
        lines.append(f"    {line(match)}")
    current.update(statut="ecart", ecart=lines, build=build, date_fin=datetime.now().isoformat(timespec="seconds"))
    data["revalidation"] = current
    save(client, data)
    return lines


def revalidation_line(current: dict) -> str:
    if not current:
        return ""
    if current.get("statut") == "ok":
        return (f"Réglages de la version {current.get('de')} revalidés sur {current.get('vers')} (init verte, build "
                f"{current.get('build') or '?'}).")
    labels = ", ".join(SETTING_LABELS.get(r["reglage"], r["reglage"]) for r in current.get("reports") or [])
    if current.get("statut") == "ecart":
        return (current.get("ecart") or [f"Écart de version {current.get('de')} → {current.get('vers')}"])[0]
    return (f"Version {current.get('vers')} : réglages prouvés de la version {current.get('de')} reportés ({labels}) — "
            f"run d'init de revalidation à faire avant toute génération.")


# ---------------------------------------------------------------------------
# Conflits de verrouillage : relance unique, puis concurrence
# ---------------------------------------------------------------------------

def failure_text(run: dict) -> str:
    failures = run.get("echecs") or []
    init = run.get("init_en_echec") or {}
    return " ".join([run.get("message", ""), init.get("message", ""), run.get("console_erreur", "")]
                    + [f"{f.get('message', '')} {f.get('journal', '')}" for f in failures])


def run_with_lock_retry(run_fn, concurrency_fn=None, log=print) -> dict:
    """
    Run lancé par `run_fn()` ; en échec sur un conflit de verrouillage : UNE relance automatique. Si l'échec
    persiste, `concurrency_fn(run)` dit quels autres builds ou copies de base utilisaient le même schéma au même
    moment (clé « concurrence » du résultat). Renvoie le résultat du dernier run (« relance » : nombre de relances).
    """
    run = run_fn()
    if run.get("ok") or not is_lock_conflict(failure_text(run)):
        return {**run, "relance": 0}
    log(f"[INCIDENTS] Conflit de verrouillage ({lock_table(failure_text(run)) or 'table non citée'}) au build "
        f"{run.get('build') or '?'} : relance automatique du run (une fois).")
    again = run_fn()
    again = {**again, "relance": 1, "build_initial": run.get("build", "")}
    if again.get("ok") or not is_lock_conflict(failure_text(again)):
        return again
    found = concurrency_fn(again) if concurrency_fn else {"message": "concurrence non vérifiée"}
    again["concurrence"] = found
    log(f"[INCIDENTS] ⛔ Conflit de verrouillage persistant après relance : {found.get('message', '')}")
    return again


_SCHEMA_PARAM = re.compile(r"schema|schéma", re.I)
_SCHEMA_OPTION = re.compile(r"-D[\w.-]*schema[\w.-]*=(['\"]?)([\w$+-]+)\1", re.I)
_CURRENT_SCHEMA = re.compile(r"currentSchema=([\w$-]+)", re.I)


def schema_of(params: dict) -> str:
    """Schéma de base d'un build d'après ses paramètres (paramètre *SCHEMA*, option -D…schema…=, currentSchema=)."""
    for name, value in (params or {}).items():
        text = str(value or "")
        if _SCHEMA_PARAM.search(str(name)) and re.fullmatch(r"[\w$-]+", text.strip()):
            return text.strip().lower()
        found = _SCHEMA_OPTION.search(text) or _CURRENT_SCHEMA.search(text)
        if found:
            return found.group(found.lastindex).lower()
    return ""


def _overlap(a: tuple, b: tuple) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def concurrency(client: str, build: dict, builds: list, copies: list, schema: str = "") -> dict:
    """
    Autres builds (Jenkins) et copies de base (journal db_restore) sur le même schéma pendant `build`
    ({url, debut (ms), duree (ms), parametres}). `builds` : builds récents des jobs exclusifs ; `copies` : historique
    des copies. Renvoie {"builds", "copies", "schema", "message"}.
    """
    schema = (schema or schema_of(build.get("parametres") or {})).lower()
    start = (build.get("debut") or 0) / 1000
    window = (start, start + max((build.get("duree") or 0) / 1000, 60))
    found_builds = []
    for other in builds or []:
        if not other.get("debut") or other.get("url") == build.get("url"):
            continue
        begin = other["debut"] / 1000
        span = (begin, begin + max((other.get("duree") or 0) / 1000, 60) if other.get("duree") else begin + 6 * 3600)
        other_schema = schema_of(other.get("parametres") or {})
        if _overlap(window, span) and (not schema or not other_schema or other_schema == schema):
            found_builds.append(f"{other.get('url')} (schéma {other_schema or 'non lu'})")
    found_copies = []
    for copy in copies or []:
        try:
            begin = datetime.fromisoformat(copy.get("date", "")).timestamp()
        except ValueError:
            continue
        span = (begin, begin + max(int(copy.get("duree_s") or 0), 60))
        target = (copy.get("cible") or "").rsplit("/", 1)[-1].strip().lower()
        if _overlap(window, span) and (not schema or target == schema):
            found_copies.append(f"copie de base du {copy.get('date')} vers {copy.get('cible')}")
    when = datetime.fromtimestamp(start).isoformat(timespec="minutes") if start else "?"
    if found_builds or found_copies:
        message = (f"schéma {schema or 'non lu'} utilisé en même temps ({when}) par : "
                   + " ; ".join(found_builds + found_copies) + " — attendez leur fin avant de relancer")
    else:
        message = (f"aucun autre build ni aucune copie de base sur le schéma {schema or 'non lu'} pendant le build "
                   f"({when}) : conflit interne au run (écritures concurrentes de l'application) — à analyser dans "
                   f"les logs du serveur")
    return {"builds": found_builds, "copies": found_copies, "schema": schema, "message": message}


def check_concurrency(client: str, build_url: str, jobs: list) -> dict:
    """concurrency() sur les données réelles : build Jenkins, builds récents des jobs exclusifs, journal des copies."""
    from tools import db_restore, jenkins_tools
    info = jenkins_tools.build_info(build_url) if build_url else {}
    builds = [b for job in jobs or [] for b in jenkins_tools.recent_builds(job, 30)]
    me = next((b for b in builds if b.get("url") == jenkins_tools.same_base(build_url)), {})
    build = {"url": jenkins_tools.same_base(build_url) if build_url else "", "debut": info.get("debut") or me.get(
        "debut"), "duree": me.get("duree") or 0, "parametres": me.get("parametres") or
        jenkins_tools.build_parameters(build_url)}
    if not build["debut"]:
        return {"builds": [], "copies": [], "schema": "", "message": "horaires du build illisibles : concurrence non "
                                                                    "vérifiée"}
    # Schéma posé par le pipeline (absent des paramètres) : lu dans la console du build (-D…schema…=)
    schema = schema_of(build["parametres"])
    if not schema and build_url:
        console = jenkins_tools.build_bytes(build_url, "consoleText").decode("utf-8", "replace")
        schema = schema_of({"console": console})
    return concurrency(client, build, builds, db_restore.load_history(client), schema)
