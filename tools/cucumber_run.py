"""
Lancement automatique d'un run Jenkins du dépôt <client>-cucumber (onglet Cucumber, étape Exécution ; main.py
--run-jenkins), 0 token. Le job ne reçoit pas de patch : il lit la branche de travail de l'agent.

1. Contrôles préalables : Jenkins configuré, job du profil (« run_job », repli CUCUMBER_RUN_JOB), branche de travail
   (profil « verify_branch », jamais la branche d'intégration) ; rôles des paramètres du job (cucumber_job :
   branche et arguments requis), détectés sur le job et enregistrés dans le profil.
2. Le worktree est commité et poussé sur la branche de travail (jamais de push forcé) ; avant le lancement, le
   commit poussé doit être en tête de la branche distante (cucumber_checks.push_for_verification), sinon rien n'est
   lancé.
3. Le job est lancé avec sa configuration actuelle, la branche, le dépôt client, l'attente décochée et le filtre de
   tags ajouté à ses arguments ; suivi jusqu'à sa fin (CUCUMBER_VERIFY_TIMEOUT). Le nombre de tests exécutés lu dans
   la console est comparé à celui attendu pour les tags (une feature taguée : ses scénarios). La console et les
   rapports Cucumber JSON sont ensuite importés (cucumber_import, bilan) par l'appelant.
4. Version testée : artefact monté (rôles « composant_* »), date de publication lue dans le dépôt Maven et comparée
   aux derniers commits des tâches (cucumber_job.deployment, deploiement.json).
Chaque lancement est historisé dans etat.json (« runs_jenkins ») avec ses paramètres montrables : les arguments du
job (identifiants) n'y sont jamais écrits.
"""
import os
from datetime import datetime

from tools import cucumber_checks, cucumber_job, cucumber_profile, jenkins_tools
from tools.cucumber_tools import load_state, rev_parse, save_state

DEFAULT_TIMEOUT = 3600


def run_job(profile: dict) -> str:
    """Job Jenkins qui exécute le dépôt client : profil, sinon CUCUMBER_RUN_JOB."""
    return (profile.get("run_job") or os.getenv("CUCUMBER_RUN_JOB", "")).strip()


def timeout() -> int:
    try:
        return int(os.getenv("CUCUMBER_VERIFY_TIMEOUT", "") or DEFAULT_TIMEOUT)
    except ValueError:
        return DEFAULT_TIMEOUT


def preflight(profile: dict, worktree: str, client_name: str = "", base_branch: str = "") -> dict:
    """
    Contrôles avant tout push : {"erreurs": [...], "avertissements", "job", "branche", "roles", "definitions"}. Les
    paramètres du job et leur valeur actuelle sont lus sur Jenkins ; les rôles détectés sont enregistrés dans le
    profil de `client_name`.
    """
    job, branch = run_job(profile), (profile.get("verify_branch") or "").strip()
    out = {"erreurs": [], "avertissements": [], "job": job, "branche": branch, "roles": cucumber_job.empty_roles(),
           "definitions": {}}
    if not jenkins_tools.jenkins_configured():
        out["erreurs"].append("Jenkins non configuré : renseignez JENKINS_URL (onglet Configuration)")
    if not job:
        out["erreurs"].append("job Jenkins du client non renseigné : champ « job Jenkins » du profil client (ou "
                              "CUCUMBER_RUN_JOB)")
    out["erreurs"] += branch_errors(branch, base_branch)
    if not worktree or not os.path.exists(os.path.join(worktree, ".git")):
        out["erreurs"].append("aucun worktree de génération : générez d'abord des tests")
    if out["erreurs"]:
        return out
    job_info = cucumber_job.prepare(job, profile)
    if client_name and job_info["definitions"]:
        cucumber_profile.record_job_roles(client_name, cucumber_job.detect_roles(job_info["definitions"])[0])
    out.update(roles=job_info["roles"], definitions=job_info["definitions"],
               avertissements=job_info["avertissements"])
    out["erreurs"] += job_info["erreurs"]
    return out


def branch_errors(branch: str, base_branch: str = "") -> list:
    """Branche de travail absente, ou égale à la branche d'intégration (l'agent n'y pousse jamais)."""
    if not branch:
        return ["branche de travail non renseignée (profil client) : l'agent y pousse le worktree avant de lancer "
                "le job"]
    if base_branch and branch.strip() == base_branch.strip():
        return [f"la branche de travail {branch} est la branche d'intégration : l'agent n'y pousse jamais, "
                f"choisissez une branche à lui dans le profil client"]
    return []


def selection_check(console: str, worktree: str, tags: str) -> str:
    """
    Contrôle du filtre de tags par la console : '' si le nombre de tests exécutés (dernier « Tests run ») est celui
    attendu pour `tags` dans le worktree (un tag de feature : ses scénarios), sinon le constat.
    """
    from tools.cucumber_import import console_facts
    from tools.cucumber_verify import count_gap, feature_texts
    run = (console_facts(console).get("tests") or {}).get("executes")
    if run is None:
        return "nombre de tests exécutés introuvable dans la console : filtre de tags non contrôlé"
    gap = count_gap(run, feature_texts(worktree), tags)
    return f"{gap} (rôle « arguments » du job à vérifier)" if gap else ""


def record(client_name: str, run: dict) -> None:
    """Historique des lancements dans etat.json (« runs_jenkins »)."""
    state = load_state(client_name)
    state.setdefault("runs_jenkins", []).append(run)
    save_state(client_name, state)


def tested_version(client_name: str, params: dict, roles: dict, inventory: dict = None, client_root: str = "") -> str:
    """
    Version testée : artefact monté par le build, date de publication (dépôt Maven) comparée aux derniers commits
    des tâches, enregistrée (deploiement.json). Renvoie la ligne affichée ('' si le job ne désigne pas d'artefact).
    """
    artifact = cucumber_job.tested_artifact(params, roles)
    if not artifact:
        return ""
    published = cucumber_job.publication(artifact)
    tasks = cucumber_job.deployment(inventory, client_root, artifact, published) if inventory else {}
    return cucumber_job.deployment_line(cucumber_job.record_deployment(client_name, artifact, published, tasks))


def launch(client_name: str, worktree: str, profile: dict, tags: str = "", log=print, repository: str = "",
           base_branch: str = "", inventory: dict = None, client_root: str = "") -> dict:
    """
    Push, contrôle du commit en tête de la branche distante, lancement et suivi (voir docstring du module).
    `repository` : dépôt client (rôle « depot ») ; `base_branch` : branche d'intégration, interdite ; `inventory` et
    `client_root` : tâches et dépôts dont les commits sont comparés à la publication de l'artefact testé.
    Renvoie {"ok", "message", "build", "resultat", "commit", "branche", "tags", "parametres", "selection",
    "version_testee"} ; ok faux si rien n'a été lancé ou si le build n'a pas abouti (le build terminé, même en échec
    de tests, est importable).
    """
    tags = (tags or "").strip()
    check = preflight(profile, worktree, client_name, base_branch)
    run = {"date": datetime.now().isoformat(timespec="seconds"), "job": check["job"], "branche": check["branche"],
           "tags": tags, "commit": "", "build": "", "resultat": "", "ok": False, "message": "", "parametres": {},
           "selection": "", "version_testee": ""}
    for warning in check["avertissements"]:
        log(f"[RUN JENKINS] ⚠️ {warning}")
    if check["erreurs"]:
        run["message"] = " ; ".join(check["erreurs"])
        return run
    error, committed = cucumber_checks.push_for_verification(
        worktree, check["branche"], f"Cucumber coverage of {client_name} specifics (run Jenkins)")
    run["commit"] = rev_parse(worktree, "HEAD")
    if error:
        run["message"] = f"rien n'est lancé : {error}"
        record(client_name, run)
        return run
    log(f"[RUN JENKINS] Worktree {'commité et ' if committed else ''}poussé sur {check['branche']} "
        f"({run['commit'][:10]}), en tête de la branche distante.")
    roles = check["roles"]
    params = cucumber_job.build_parameters(check["definitions"], roles, check["branche"], repository, tags,
                                           run["commit"])
    run["parametres"] = cucumber_job.visible_parameters(params, roles)
    run["version_testee"] = tested_version(client_name, params, roles, inventory, client_root)
    if run["version_testee"]:
        log(f"[RUN JENKINS] {run['version_testee']}")
    queue = jenkins_tools.trigger_job(check["job"], params)
    if not queue:
        run["message"] = f"lancement du job {check['job']} refusé par Jenkins"
        record(client_name, run)
        return run
    log(f"[RUN JENKINS] Job {check['job']} lancé (branche {check['branche']}"
        + (f", filtre de tags {tags}" if tags else "") + f") ; suivi jusqu'à sa fin (au plus {timeout()} s)...")
    build = jenkins_tools.wait_build_result(queue, timeout())
    run.update(build=build["url"], resultat=build["result"])
    run["ok"] = bool(build["url"]) and build["result"] not in ("TIMEOUT", "INDISPONIBLE", "ABORTED")
    run["message"] = (f"build terminé : {build['result']}" if run["ok"] else
                      f"build non abouti : {build['result']}" + (f" ({build['url']})" if build["url"] else ""))
    if run["ok"] and tags:
        console = jenkins_tools.build_bytes(build["url"], "consoleText").decode("utf-8", "replace")
        run["selection"] = selection_check(console, worktree, tags)
        if run["selection"]:
            log(f"[RUN JENKINS] ⚠️ {run['selection']}")
    record(client_name, run)
    return run


def last_run(state: dict) -> dict:
    """Dernier lancement Jenkins ({} sans lancement)."""
    return (state.get("runs_jenkins") or [{}])[-1]
