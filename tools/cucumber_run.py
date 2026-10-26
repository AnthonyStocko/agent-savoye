"""
Lancement automatique d'un run Jenkins du dépôt <client>-cucumber (onglet Cucumber, étape Exécution ; main.py
--run-jenkins), 0 token.

1. Contrôles préalables : Jenkins configuré, job du profil (« run_job », repli CUCUMBER_RUN_JOB), branche lue par le
   job (profil « verify_branch ») ; le job doit accepter le paramètre qui porte la branche (profil
   « verify_branch_param », défaut CUCUMBER_SPEC_BRANCH) et, si des tags sont demandés, le paramètre TAGS.
2. Le worktree est commité et poussé sur la branche (jamais de push forcé) ; avant le lancement, la branche lue
   par le job doit contenir le commit attendu (cucumber_checks.push_for_verification), sinon rien n'est lancé.
3. Le job est lancé avec la branche, les tags et le commit (CUCUMBER_SPEC_COMMIT si le job l'accepte), suivi
   jusqu'à sa fin (CUCUMBER_VERIFY_TIMEOUT), puis sa console et ses rapports Cucumber JSON sont importés
   (cucumber_import, bilan) par l'appelant.
Chaque lancement est historisé dans etat.json (« runs_jenkins »).
"""
import os
from datetime import datetime

from tools import cucumber_checks, jenkins_tools
from tools.cucumber_profile import DEFAULT_VERIFY_BRANCH_PARAM
from tools.cucumber_tools import load_state, rev_parse, save_state

TAGS_PARAM = "TAGS"
COMMIT_PARAM = "CUCUMBER_SPEC_COMMIT"
DEFAULT_TIMEOUT = 3600


def run_job(profile: dict) -> str:
    """Job Jenkins qui exécute le dépôt client : profil, sinon CUCUMBER_RUN_JOB."""
    return (profile.get("run_job") or os.getenv("CUCUMBER_RUN_JOB", "")).strip()


def timeout() -> int:
    try:
        return int(os.getenv("CUCUMBER_VERIFY_TIMEOUT", "") or DEFAULT_TIMEOUT)
    except ValueError:
        return DEFAULT_TIMEOUT


def preflight(profile: dict, worktree: str, tags: str = "") -> dict:
    """
    Contrôles avant tout push : {"erreurs": [...], "job", "branche", "parametre", "acceptes"}. Les paramètres
    acceptés par le job sont lus sur Jenkins.
    """
    job, branch = run_job(profile), (profile.get("verify_branch") or "").strip()
    param = profile.get("verify_branch_param") or DEFAULT_VERIFY_BRANCH_PARAM
    out = {"erreurs": [], "job": job, "branche": branch, "parametre": param, "acceptes": {}}
    if not jenkins_tools.jenkins_configured():
        out["erreurs"].append("Jenkins non configuré : renseignez JENKINS_URL (onglet Configuration)")
    if not job:
        out["erreurs"].append("job Jenkins du client non renseigné : champ « job Jenkins » du profil client (ou "
                              "CUCUMBER_RUN_JOB)")
    if not branch:
        out["erreurs"].append("branche lue par le job non renseignée (profil client) : l'agent y pousse le worktree "
                              "avant de lancer le job")
    if not worktree or not os.path.exists(os.path.join(worktree, ".git")):
        out["erreurs"].append("aucun worktree de génération : générez d'abord des tests")
    if out["erreurs"]:
        return out
    out["acceptes"] = jenkins_tools.job_parameters(job)
    if not out["acceptes"]:
        out["erreurs"].append(f"job {job} introuvable ou sans paramètres (Jenkins injoignable ?)")
    elif param not in out["acceptes"]:
        out["erreurs"].append(f"le job {job} n'a pas de paramètre {param} : il ne lirait pas la branche {branch} "
                              f"(champ « paramètre qui porte la branche » du profil)")
    elif tags and TAGS_PARAM not in out["acceptes"]:
        out["erreurs"].append(f"le job {job} n'a pas de paramètre {TAGS_PARAM} : les tags « {tags} » ne seraient "
                              f"pas appliqués")
    return out


def parameters(check: dict, head: str, tags: str) -> dict:
    """Paramètres du build : branche, tags demandés et commit attendu (si le job les accepte)."""
    params = {check["parametre"]: check["branche"]}
    if tags:
        params[TAGS_PARAM] = tags
    if COMMIT_PARAM in check["acceptes"]:
        params[COMMIT_PARAM] = head
    return params


def record(client_name: str, run: dict) -> None:
    """Historique des lancements dans etat.json (« runs_jenkins »)."""
    state = load_state(client_name)
    state.setdefault("runs_jenkins", []).append(run)
    save_state(client_name, state)


def launch(client_name: str, worktree: str, profile: dict, tags: str = "", log=print) -> dict:
    """
    Push, contrôle du commit sur la branche lue par le job, lancement et suivi (voir docstring du module).
    Renvoie {"ok", "message", "build", "resultat", "commit", "branche", "tags"} ; ok faux si rien n'a été lancé ou
    si le build n'a pas abouti (le build terminé, même en échec de tests, est importable).
    """
    tags = (tags or "").strip()
    check = preflight(profile, worktree, tags)
    run = {"date": datetime.now().isoformat(timespec="seconds"), "job": check["job"], "branche": check["branche"],
           "tags": tags, "commit": "", "build": "", "resultat": "", "ok": False, "message": ""}
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
        f"({run['commit'][:10]}), présent sur origin.")
    queue = jenkins_tools.trigger_job(check["job"], parameters(check, run["commit"], tags))
    if not queue:
        run["message"] = f"lancement du job {check['job']} refusé par Jenkins"
        record(client_name, run)
        return run
    log(f"[RUN JENKINS] Job {check['job']} lancé (branche {check['branche']}"
        + (f", tags {tags}" if tags else "") + f") ; suivi jusqu'à sa fin (au plus {timeout()} s)...")
    build = jenkins_tools.wait_build_result(queue, timeout())
    run.update(build=build["url"], resultat=build["result"])
    run["ok"] = bool(build["url"]) and build["result"] not in ("TIMEOUT", "INDISPONIBLE", "ABORTED")
    run["message"] = (f"build terminé : {build['result']}" if run["ok"] else
                      f"build non abouti : {build['result']}" + (f" ({build['url']})" if build["url"] else ""))
    record(client_name, run)
    return run


def last_run(state: dict) -> dict:
    """Dernier lancement Jenkins ({} sans lancement)."""
    return (state.get("runs_jenkins") or [{}])[-1]
