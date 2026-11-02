"""
Tâche de fond de la détection de la version du Standard d'un client (onglet Cucumber, « Données du projet »).

Indépendante de la détection complète du projet (qui met d'abord à jour tous les clones) : tout est lu par l'API
Bitbucket (branche integration), sans cloner, et par l'API Jenkins. Ordre :
  1. sources rapides : paramètres des derniers builds Jenkins du client, pom de son dépôt Cucumber → premier
     résultat affiché (« provisoire ») ;
  2. code de l'application : pom de chaque dépôt du projet client (X/N), groupIds du Standard (pom des dépôts du
     projet Standard, mis en cache) → résultat confirmé ou conflit.
État dans .tmp/cucumber/<client>/version_standard_tache.json : {statut: en_cours|ok|conflit|non_detectee, etape,
progression, debut, fin, cause, provisoire, resultat}. Délai maximal DEADLINE_S : au-delà, ou sur erreur,
« non_detectee » avec la cause précise ; jamais d'état « en cours » sans fin (l'IHM tient aussi ce délai). Chaque
source consultée, et ce qu'on y a trouvé ou non, est écrite dans le journal technique.

Lancement : python -m tools.standard_version_task --client <nom> --client-repo <URL ou clone> [--std-repo <URL>]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

import requests

from tools import standard_version as sv
from tools.cucumber_tools import (CACHE_ROOT, STD_CACHE_DIR, _git, _read_json, _write_json, cache_dir,
                                  git_repos, java_package_segment)

DEADLINE_S = 300
JOURNAL = os.path.join(os.path.dirname(CACHE_ROOT), "journal_technique.log")
_PROCESSES = {}


class SourceError(RuntimeError):
    """Cause précise d'un échec de lecture (dépôt inaccessible, jeton…)."""


# ---------------------------------------------------------------------------
# État et journal
# ---------------------------------------------------------------------------

def state_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "version_standard_tache.json")


def load_state(client_name: str) -> dict:
    return _read_json(state_path(client_name), {}) or {}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def journal(client_name: str, message: str) -> None:
    """Ligne du journal technique (.tmp/journal_technique.log), préfixée du client."""
    try:
        os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [VERSION STANDARD {client_name}] {message}\n")
    except OSError:
        pass
    print(f"[VERSION STANDARD] {message}", flush=True)


class Task:
    """Avancement écrit à chaque étape ; délai vérifié entre deux lectures."""

    def __init__(self, client_name: str, deadline_s: int = DEADLINE_S):
        self.client, self.started, self.deadline = client_name, time.time(), deadline_s
        self.state = {"statut": "en_cours", "etape": "démarrage", "progression": "", "debut": _now(), "fin": "",
                      "cause": "", "provisoire": load_state(client_name).get("provisoire") or {}, "resultat": {},
                      "pid": os.getpid()}
        self.save()

    def save(self) -> None:
        _write_json(state_path(self.client), self.state)

    def step(self, label: str, progress: str = "") -> None:
        self.check()
        self.state.update(etape=label, progression=progress)
        self.save()

    def check(self) -> None:
        if time.time() - self.started > self.deadline:
            raise SourceError(f"délai de {self.deadline // 60} min dépassé pendant « {self.state['etape']}"
                              + (f" {self.state['progression']}" if self.state["progression"] else "") + " »")

    def end(self, statut: str, result: dict = None, cause: str = "") -> None:
        self.state.update(statut=statut, resultat=result or {}, cause=cause, fin=_now(), etape="", progression="")
        self.save()


# ---------------------------------------------------------------------------
# Bitbucket (API REST, branche integration) ou clones locaux
# ---------------------------------------------------------------------------

def _headers() -> dict:
    token = os.getenv("BITBUCKET_HTTP_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def project_of(repo_input: str) -> tuple:
    """(URL de base Bitbucket, clé du projet) d'une URL de projet ou de dépôt ; ('', '') pour un clone local."""
    match = re.match(r"^(https?://[^/]+)/(?:projects/([^/]+)|scm/([^/]+)/)", (repo_input or "").strip())
    if not match:
        return "", ""
    return match.group(1), (match.group(2) or match.group(3)).upper()


def _get(url: str, params: dict = None, what: str = "") -> requests.Response:
    try:
        response = requests.get(url, headers=_headers(), params=params or {}, timeout=20)
    except requests.RequestException as e:
        raise SourceError(f"Bitbucket injoignable ({what or url}) : {type(e).__name__}") from e
    if response.status_code == 401:
        raise SourceError("jeton Bitbucket refusé (HTTP 401) : vérifiez BITBUCKET_HTTP_TOKEN (onglet Configuration)")
    if response.status_code == 403:
        raise SourceError(f"accès refusé par Bitbucket (HTTP 403) à {what or url} : droits du jeton")
    return response


def list_repos(base: str, key: str) -> list:
    """Dépôts (slugs) d'un projet Bitbucket ; SourceError si le projet est inaccessible."""
    slugs, start = [], 0
    while True:
        response = _get(f"{base}/rest/api/1.0/projects/{key}/repos", {"start": start, "limit": 100},
                        f"projet {key}")
        if response.status_code == 404:
            raise SourceError(f"projet Bitbucket {key} inaccessible (HTTP 404)")
        if response.status_code != 200:
            raise SourceError(f"liste des dépôts du projet {key} illisible (HTTP {response.status_code})")
        page = response.json()
        slugs += [r["slug"] for r in page.get("values") or []]
        if page.get("isLastPage", True):
            return slugs
        start = page.get("nextPageStart", start + 100)


def raw_file(base: str, key: str, slug: str, path: str, branch: str = "integration") -> str:
    """Contenu d'un fichier à la branche `branch` ; '' si absent (404)."""
    response = _get(f"{base}/rest/api/1.0/projects/{key}/repos/{slug}/raw/{path}", {"at": f"refs/heads/{branch}"},
                    f"{slug}/{path}")
    return response.text if response.status_code == 200 else ""


class Source:
    """Accès aux pom d'un projet : API Bitbucket (URL) ou clones locaux (chemin)."""

    def __init__(self, repo_input: str, branches=("integration",)):
        self.input, self.branches = (repo_input or "").strip(), branches
        self.base, self.key = project_of(self.input)
        self.local = {} if self.base else git_repos(self.input)
        if not self.base and not self.local:
            raise SourceError(f"dépôt « {self.input or '(vide)'} » inaccessible : ni projet Bitbucket ni clone local")

    def repos(self) -> list:
        return list_repos(self.base, self.key) if self.base else sorted(self.local)

    def pom(self, slug: str) -> str:
        for branch in self.branches:
            if self.base:
                text = raw_file(self.base, self.key, slug, "pom.xml", branch)
            else:
                shown = _git(self.local[slug], "show", f"origin/{branch}:pom.xml")
                text = shown.stdout if shown.returncode == 0 else ""
            if text:
                return text
        return ""


# ---------------------------------------------------------------------------
# Détection
# ---------------------------------------------------------------------------

def std_group_ids_cached(std_input: str, task: Task, client: str) -> set:
    """groupIds du Standard : clone local s'il existe, sinon pom des dépôts du projet Standard ; mis en cache."""
    cache = os.path.join(STD_CACHE_DIR, "groupids_standard.json")
    cached = _read_json(cache, {}) or {}
    if cached.get("depot") == std_input and cached.get("groupids"):
        journal(client, f"groupIds du Standard (cache) : {', '.join(cached['groupids'])}")
        return set(cached["groupids"])
    base, key = project_of(std_input)
    local = os.path.join(os.path.dirname(CACHE_ROOT), f"std_{key}") if key else std_input
    groups = sv.std_group_ids(local) if local and os.path.isdir(local) else set()
    if not groups and base:
        source = Source(std_input, ("development", "dev", "master", "main"))
        slugs = source.repos()
        for n, slug in enumerate(slugs, 1):
            task.step("lecture des pom du Standard", f"{n}/{len(slugs)}")
            group = sv.pom_group_id(source.pom(slug))
            if group:
                groups.add(group)
    journal(client, f"groupIds du Standard ({'clone local' if local and os.path.isdir(local) else 'Bitbucket'}) : "
                    f"{', '.join(sorted(groups)) or 'aucun'}")
    if groups:
        _write_json(cache, {"depot": std_input, "groupids": sorted(groups), "date": _now()})
    return groups


def fast_sources(client: str, source: Source, task: Task, builds_reader) -> tuple:
    """(Jenkins, bibliothèque) : sources rapides, chacune journalisée."""
    cucumber_repo = f"{java_package_segment(client)}-cucumber"
    task.step("lecture des builds Jenkins")
    job = os.getenv("CUCUMBER_VERIFY_JOB", "").strip()
    try:
        from tools import cucumber_profile
        job = (cucumber_profile.effective_profile(cucumber_profile.load_profile(client)).get("run_job") or job).strip()
    except (OSError, ValueError):
        pass
    builds = builds_reader(job) if job else []
    mine = [b for b in builds if any(cucumber_repo in str(v).lower() for v in (b.get("parametres") or {}).values())]
    jenkins = sv.jenkins_source(builds, cucumber_repo)
    journal(client, f"Jenkins, job « {job or 'non configuré'} » : {len(builds)} build(s) lu(s), {len(mine)} pour "
                    f"{cucumber_repo} — " + (jenkins["detail"] if jenkins else
                                             "aucun paramètre CUCUMBER_STANDARD_BRANCH / KIS_VERSION / "
                                             "COMPONENT_VERSION au format version"))
    task.step("lecture du pom du dépôt Cucumber")
    text = source.pom(cucumber_repo) if cucumber_repo in source.repos_cache else ""
    version = sv.library_from_pom(text)
    library = sv.library_source(cucumber_repo, version)
    journal(client, f"{cucumber_repo}/pom.xml : " + (f"bibliothèque Cucumber {version}" if version else
                                                       ("dépôt absent du projet" if cucumber_repo not in
                                                        source.repos_cache else "pas de bibliothèque test-jar")))
    return jenkins, library, len(builds), len(mine)


def run(client: str, client_repo: str, std_repo: str, deadline_s: int = DEADLINE_S, builds_reader=None) -> dict:
    """Détection complète, état et journal tenus à jour ; renvoie l'état final."""
    if builds_reader is None:
        from tools import jenkins_tools
        builds_reader = (lambda job: jenkins_tools.recent_builds(job, 20)) if jenkins_tools.jenkins_configured() \
            else (lambda job: [])
    task = Task(client, deadline_s)
    journal(client, f"détection lancée (projet {client_repo}, Standard {std_repo or 'non configuré'})")
    try:
        task.step("récupération des dépôts du projet client")
        source = Source(client_repo)
        source.repos_cache = source.repos()
        journal(client, f"projet client : {len(source.repos_cache)} dépôt(s) — {', '.join(source.repos_cache)}")
        jenkins, library, n_builds, n_mine = fast_sources(client, source, task, builds_reader)
        task.state["provisoire"] = sv.resolve({}, jenkins, library)
        task.save()
        groups = std_group_ids_cached(std_repo, task, client) if std_repo else set()
        if not groups:
            journal(client, "groupIds du Standard inconnus : code de l'application non analysable")
        apps = [s for s in source.repos_cache if "cucumber" not in s.lower()]
        poms = {}
        for n, slug in enumerate(apps, 1):
            task.step("lecture des pom du projet client", f"{n}/{len(apps)}")
            text = source.pom(slug)
            found = sv.pom_versions(text, groups, f"{slug}/pom.xml") if text and groups else []
            journal(client, f"{slug}/pom.xml : " + ("absent sur integration" if not text else
                                                     ", ".join(f"{f['artefact'].split(':', 1)[-1]} {f['version']} "
                                                               f"({f['emplacement']})" for f in found)
                                                     or "aucune version du Standard"))
            if text:
                poms[slug] = text
        code = sv.code_from_poms(poms, groups) if groups else {}
        result = sv.resolve(code, jenkins, library)
        sv.save(client, result)
        journal(client, f"résultat : {sv.line(result)}")
        if result["statut"] == "introuvable":
            causes = []
            if not code:
                causes.append(f"aucun pom avec une version du Standard ({len(poms)} pom lu(s) sur {len(apps)} "
                              f"dépôt(s))" if groups else "groupIds du Standard inconnus (dépôt du Standard non "
                                                          "configuré ou illisible)")
            if not jenkins:
                causes.append(f"aucun build Jenkins pour ce client ({n_builds} build(s) lu(s), {n_mine} du client)"
                              if n_builds else "aucun build Jenkins lu (job non configuré ou injoignable)")
            task.end("non_detectee", result, " ; ".join(causes))
        else:
            task.end(result["statut"], result)
    except SourceError as e:
        journal(client, f"❌ version non détectée : {e}")
        task.end("non_detectee", task.state.get("provisoire") or {}, str(e))
    except Exception as e:  # noqa: BLE001 - toute erreur doit terminer la tâche, jamais « en cours » sans fin
        journal(client, f"❌ version non détectée : erreur inattendue {type(e).__name__} : {e}")
        task.end("non_detectee", task.state.get("provisoire") or {}, f"erreur inattendue : {type(e).__name__} : {e}")
    return task.state


# ---------------------------------------------------------------------------
# Pilotage depuis l'IHM
# ---------------------------------------------------------------------------

def view(client_name: str, now: float = None) -> dict:
    """
    État à afficher : l'état « en cours » d'une tâche disparue ou plus vieille que le délai devient
    « non_detectee » (cause : délai dépassé), écrit dans l'état ; jamais d'« en cours » sans fin.
    """
    state = load_state(client_name)
    if state.get("statut") != "en_cours":
        return state
    try:
        age = (now or time.time()) - datetime.fromisoformat(state["debut"]).timestamp()
    except (KeyError, ValueError):
        age = DEADLINE_S + 1
    process = _PROCESSES.get(client_name)
    gone = process is not None and process.poll() is not None
    late = age > DEADLINE_S + 30
    if late or gone:
        if process is not None and process.poll() is None:
            process.kill()
        state.update(statut="non_detectee", fin=_now(),
                     cause=(f"délai de {DEADLINE_S // 60} min dépassé pendant « {state.get('etape', '?')} "
                            f"{state.get('progression', '')} »".replace("  ", " ") if late else
                            f"tâche interrompue (code retour {process.returncode}) pendant « {state.get('etape')} »"))
        _write_json(state_path(client_name), state)
    return state


def running(client_name: str) -> bool:
    process = _PROCESSES.get(client_name)
    return bool(process) and process.poll() is None


def due(state: dict, interval_s: int) -> bool:
    """Détection à (re)faire : jamais faite, ou terminée depuis plus de `interval_s` (commits ou builds nouveaux)."""
    if not state:
        return True
    if state.get("statut") == "en_cours":
        return False
    try:
        return time.time() - datetime.fromisoformat(state.get("fin") or "").timestamp() >= interval_s
    except ValueError:
        return True


def start(client_name: str, client_repo: str, std_repo: str, force: bool = False, interval_s: int = 300) -> bool:
    """Lance la tâche si elle ne tourne pas et qu'elle est due (`force` : tout de suite). True si lancée."""
    if running(client_name) or not (force or due(view(client_name), interval_s)):
        return False
    previous = load_state(client_name)
    # État écrit avant le lancement : l'IHM affiche l'étape dès le premier rafraîchissement ; le dernier résultat
    # reste affiché comme provisoire pendant la nouvelle détection
    _write_json(state_path(client_name), {"statut": "en_cours", "etape": "démarrage", "progression": "",
                                          "debut": _now(), "fin": "", "cause": "",
                                          "provisoire": previous.get("resultat") or {}, "resultat": {}})
    workspace = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _PROCESSES[client_name] = subprocess.Popen(
        [sys.executable, "-m", "tools.standard_version_task", "--client", client_name, "--client-repo", client_repo,
         "--std-repo", std_repo], cwd=workspace, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Détection de la version du Standard d'un client")
    parser.add_argument("--client", required=True)
    parser.add_argument("--client-repo", required=True)
    parser.add_argument("--std-repo", default="")
    parser.add_argument("--deadline", type=int, default=DEADLINE_S)
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv()
    state = run(args.client.lower(), args.client_repo, args.std_repo or os.getenv("BITBUCKET_STD_REPO_PATH", ""),
                args.deadline)
    print(json.dumps({k: state[k] for k in ("statut", "cause")}, ensure_ascii=False))
    return 0 if state["statut"] in ("ok", "conflit") else 1


if __name__ == "__main__":
    sys.exit(main())
