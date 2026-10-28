import os
import re
import time
import requests


def trigger_cucumber_job(job_name: str, parameters: dict) -> str:
    """
    Déclenche un job Jenkins Cucumber avec des paramètres (ex: BRANCH, COMPONENT)
    et retourne l'URL du build généré.
    """
    # Lu à chaque appel : la configuration enregistrée dans l'IHM s'applique sans redémarrage
    jenkins_url = os.getenv("JENKINS_URL", "").strip().rstrip("/")
    jenkins_token = os.getenv("JENKINS_API_TOKEN", "")
    if not jenkins_url or not job_name.strip():
        print("[JENKINS] JENKINS_URL ou nom du job non renseigné (onglet Configuration).")
        return ""
    auth = (os.getenv("JENKINS_USER", ""), jenkins_token) if jenkins_token else None
    build_url = f"{jenkins_url}/job/{job_name}/buildWithParameters"

    try:
        response = requests.post(build_url, auth=auth, data=parameters, timeout=10)
        if response.status_code in [200, 201]:
            print(f"[JENKINS] Job {job_name} lancé avec succès.")
            return f"{jenkins_url}/job/{job_name}/lastBuild"
        else:
            print(f"[JENKINS] Erreur lancement job ({response.status_code}) : {response.text}")
            return ""
    except Exception as e:
        print(f"[JENKINS] Impossible de contacter Jenkins : {e}")
        return ""


def _jenkins() -> tuple:
    """(URL de Jenkins, authentification), lus à chaque appel ; URL vide si non configurée."""
    jenkins_token = os.getenv("JENKINS_API_TOKEN", "")
    auth = (os.getenv("JENKINS_USER", ""), jenkins_token) if jenkins_token else None
    return os.getenv("JENKINS_URL", "").strip().rstrip("/"), auth


def same_base(url: str) -> str:
    """
    URL renvoyée par Jenkins (en-tête Location, « url » d'un build) ramenée au schéma et à l'hôte de JENKINS_URL :
    derrière un proxy, Jenkins annonce souvent http://hôte (port 80), injoignable ou très lent depuis le poste,
    alors que JENKINS_URL est en https.
    """
    jenkins_url, _ = _jenkins()
    match = re.match(r"^https?://[^/]+", url or "")
    base = re.match(r"^https?://[^/]+", jenkins_url or "")
    return (base.group(0) + url[match.end():]).rstrip("/") if match and base else (url or "").rstrip("/")


def _job_path(job_name: str) -> str:
    """Chemin d'un job, dossiers compris (DOSSIER/JOB -> job/DOSSIER/job/JOB)."""
    return "/".join(f"job/{part}" for part in job_name.strip().strip("/").split("/") if part)


def job_definitions(job_name: str) -> dict:
    """
    Paramètres d'un job et leur valeur actuelle (configuration du job) : {nom: {"type", "defaut"}} ; defaut vaut None
    si Jenkins ne la donne pas (mot de passe, fichier). {} si le job est introuvable ou Jenkins injoignable. Les
    valeurs peuvent contenir des identifiants : ni affichées, ni journalisées, ni stockées par l'appelant.
    """
    jenkins_url, auth = _jenkins()
    if not jenkins_url or not job_name.strip():
        return {}
    try:
        response = requests.get(f"{jenkins_url}/{_job_path(job_name)}/api/json", auth=auth, timeout=20, params={
            "tree": "property[parameterDefinitions[name,type,defaultParameterValue[value]]]"})
        if response.status_code != 200:
            print(f"[JENKINS] Job {job_name} introuvable ({response.status_code}).")
            return {}
        return {p["name"]: {"type": p.get("type", ""), "defaut": (p.get("defaultParameterValue") or {}).get("value")}
                for prop in response.json().get("property", []) for p in prop.get("parameterDefinitions") or []}
    except (requests.RequestException, ValueError) as e:
        print(f"[JENKINS] Impossible de lire les paramètres du job {job_name} : {e}")
        return {}


def job_parameters(job_name: str) -> dict:
    """Paramètres acceptés par un job : {nom: type Jenkins} ; {} si le job est introuvable ou Jenkins injoignable."""
    return {name: d["type"] for name, d in job_definitions(job_name).items()}


def trigger_job(job_name: str, parameters: dict) -> str:
    """
    Déclenche un job avec des paramètres texte. Renvoie l'URL de l'élément de file d'attente Jenkins ('' en cas
    d'échec), à passer à wait_build_result pour suivre CE build (et non le dernier du job).
    """
    jenkins_url, auth = _jenkins()
    if not jenkins_url or not job_name.strip():
        print("[JENKINS] JENKINS_URL ou nom du job non renseigné (onglet Configuration).")
        return ""
    try:
        response = requests.post(f"{jenkins_url}/{_job_path(job_name)}/buildWithParameters", auth=auth,
                                 data=parameters, timeout=60)
        if response.status_code in (200, 201) and response.headers.get("Location"):
            print(f"[JENKINS] Job {job_name} lancé.")
            return same_base(response.headers["Location"])
        print(f"[JENKINS] Erreur lancement job ({response.status_code}) : {response.text[:300]}")
    except requests.RequestException as e:
        print(f"[JENKINS] Impossible de lancer le job {job_name} : {e}")
    return ""


def jenkins_configured() -> bool:
    return bool(_jenkins()[0])


def trigger_cucumber_job_with_file(job_name: str, parameters: dict, file_parameter: str, file_path: str) -> str:
    """
    Déclenche un job avec un paramètre fichier (ex. PATCH = le .patch du lot) en plus des paramètres texte.
    Renvoie l'URL de l'élément de file d'attente Jenkins ('' en cas d'échec), à passer à wait_build_result.
    """
    jenkins_url, auth = _jenkins()
    if not jenkins_url or not job_name.strip():
        print("[JENKINS] JENKINS_URL ou nom du job non renseigné (onglet Configuration).")
        return ""
    try:
        with open(file_path, "rb") as f:
            response = requests.post(f"{jenkins_url}/{_job_path(job_name)}/buildWithParameters", auth=auth,
                                     data=parameters, files={file_parameter: (os.path.basename(file_path), f)},
                                     timeout=60)
        if response.status_code in (200, 201) and response.headers.get("Location"):
            print(f"[JENKINS] Job {job_name} lancé.")
            return same_base(response.headers["Location"])
        print(f"[JENKINS] Erreur lancement job ({response.status_code}) : {response.text[:300]}")
    except (OSError, requests.RequestException) as e:
        print(f"[JENKINS] Impossible de lancer le job {job_name} : {e}")
    return ""


def build_from_queue(job_name: str, queue_url: str) -> str:
    """
    URL du build issu d'un élément de file d'attente, retrouvée parmi les derniers builds du job par son queueId :
    Jenkins oublie l'élément de file quelques minutes après le démarrage du build (coupure réseau pendant le suivi).
    """
    jenkins_url, auth = _jenkins()
    queue_id = re.search(r"/queue/item/(\d+)", queue_url or "")
    if not (jenkins_url and job_name and queue_id):
        return ""
    try:
        response = requests.get(f"{jenkins_url}/{_job_path(job_name)}/api/json", auth=auth, timeout=20,
                                params={"tree": "builds[url,queueId]{0,20}"})
        for build in response.json().get("builds") or []:
            if str(build.get("queueId")) == queue_id.group(1):
                return same_base(build.get("url", ""))
    except (requests.RequestException, ValueError):
        return ""
    return ""


def wait_build_result(queue_url: str, timeout_s: int, poll_s: int = 30, job_name: str = "") -> dict:
    """
    Attend la fin d'un build lancé (polling borné à timeout_s) : {"url", "result", "artifacts"} ; result vaut
    SUCCESS, UNSTABLE, FAILURE, ABORTED, ou TIMEOUT / INDISPONIBLE si le build n'a pas abouti à temps. Les URL
    annoncées par Jenkins sont ramenées à JENKINS_URL (same_base) ; avec `job_name`, un build dont l'élément de file
    a disparu est retrouvé par son queueId.
    """
    _, auth = _jenkins()
    deadline, build_url = time.time() + timeout_s, ""
    queue_url = same_base(queue_url)
    while time.time() < deadline:
        try:
            if not build_url:
                response = requests.get(f"{queue_url}/api/json", auth=auth, timeout=20)
                if response.status_code == 404:
                    # Élément de file oublié par Jenkins : build retrouvé par son queueId
                    build_url = build_from_queue(job_name, queue_url)
                    if not build_url:
                        time.sleep(poll_s)
                        continue
                    item = {}
                else:
                    item = response.json()
                if item.get("cancelled"):
                    return {"url": "", "result": "ABORTED", "artifacts": []}
                build_url = build_url or same_base((item.get("executable") or {}).get("url") or "")
            if build_url:
                build = requests.get(f"{build_url}/api/json", auth=auth, timeout=20,
                                     params={"tree": "result,building,artifacts[relativePath]"}).json()
                if not build.get("building") and build.get("result"):
                    return {"url": build_url, "result": build["result"],
                            "artifacts": [a["relativePath"] for a in build.get("artifacts") or []]}
        except (requests.RequestException, ValueError) as e:
            print(f"[JENKINS] Suivi du build momentanément impossible : {e}")
        time.sleep(poll_s)
    return {"url": build_url, "result": "TIMEOUT" if build_url else "INDISPONIBLE", "artifacts": []}


def fetch_artifact(build_url: str, relative_path: str, target: str) -> bool:
    """Télécharge un artefact d'un build dans `target`. Renvoie True s'il a été récupéré."""
    _, auth = _jenkins()
    try:
        response = requests.get(f"{build_url}/artifact/{relative_path}", auth=auth, timeout=120)
        if response.status_code != 200:
            return False
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(response.content)
        return True
    except (OSError, requests.RequestException):
        return False


def failed_stage(build_url: str) -> str:
    """Nom de la première étape en échec d'un build de pipeline (API wfapi) ; '' si inconnue ou aucune."""
    _, auth = _jenkins()
    try:
        response = requests.get(f"{build_url}/wfapi/describe", auth=auth, timeout=20)
        if response.status_code != 200:
            return ""
        return next((s.get("name", "") for s in response.json().get("stages") or []
                     if s.get("status") in ("FAILED", "FAILURE")), "")
    except (requests.RequestException, ValueError):
        return ""


def build_info(build_url: str) -> dict:
    """Build terminé : {"url", "numero", "resultat", "debut" (ms), "artifacts"} ; {} si introuvable."""
    _, auth = _jenkins()
    try:
        response = requests.get(f"{build_url.rstrip('/')}/api/json", auth=auth, timeout=20,
                                params={"tree": "number,result,timestamp,artifacts[relativePath]"})
        if response.status_code != 200:
            return {}
        build = response.json()
        return {"url": build_url.rstrip("/"), "numero": build.get("number"), "resultat": build.get("result") or "",
                "debut": build.get("timestamp"), "artifacts": [a["relativePath"] for a in build.get("artifacts") or []]}
    except (requests.RequestException, ValueError):
        return {}


def build_bytes(build_url: str, relative: str) -> bytes:
    """Contenu d'un fichier d'un build (consoleText, artifact/<chemin>), en mémoire seulement ; b'' si absent."""
    _, auth = _jenkins()
    try:
        response = requests.get(f"{build_url.rstrip('/')}/{relative}", auth=auth, timeout=120)
        return response.content if response.status_code == 200 else b""
    except requests.RequestException:
        return b""


# Un seul build Cucumber à la fois (contrainte de l'environnement de test)
# ---------------------------------------------------------------------------

LOCK_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp", "jenkins_cucumber.lock")


def exclusive_jobs(job_name: str = "") -> list:
    """
    Jobs dont un seul build peut tourner à la fois : CUCUMBER_EXCLUSIVE_JOBS (séparés par des virgules), à défaut
    `job_name`, CUCUMBER_VERIFY_JOB, CUCUMBER_RUN_JOB et JENKINS_CUCUMBER_JOB.
    """
    configured = [j.strip() for j in os.getenv("CUCUMBER_EXCLUSIVE_JOBS", "").split(",") if j.strip()]
    jobs = configured or [job_name, os.getenv("CUCUMBER_VERIFY_JOB", ""), os.getenv("CUCUMBER_RUN_JOB", ""),
                          os.getenv("JENKINS_CUCUMBER_JOB", "")]
    return list(dict.fromkeys(j.strip() for j in jobs if j and j.strip()))


def busy_builds(jobs: list) -> list:
    """Builds en cours ou en file d'attente des `jobs` : ["<job> : <url ou 'en file d'attente'>"]."""
    jenkins_url, auth = _jenkins()
    busy = []
    if not jenkins_url:
        return busy
    for job in jobs:
        try:
            response = requests.get(f"{jenkins_url}/{_job_path(job)}/api/json", auth=auth, timeout=20,
                                    params={"tree": "inQueue,builds[url,building]{0,5}"})
            if response.status_code != 200:
                continue
            data = response.json()
            if data.get("inQueue"):
                busy.append(f"{job} : en file d'attente")
            busy += [f"{job} : {b['url']}" for b in data.get("builds") or [] if b.get("building")]
        except (requests.RequestException, ValueError) as e:
            print(f"[JENKINS] État du job {job} illisible : {e}")
    return busy


def wait_until_idle(jobs: list, timeout_s: int, poll_s: int = 30) -> bool:
    """Attend qu'aucun build des `jobs` ne tourne ni n'attende (au plus timeout_s). Renvoie True si libre."""
    deadline, announced = time.time() + timeout_s, False
    while True:
        busy = busy_builds(jobs)
        if not busy:
            return True
        if not announced:
            print(f"[JENKINS] Un build Cucumber est déjà en cours, attente de sa fin (un seul à la fois) : "
                  f"{' ; '.join(busy)}")
            announced = True
        if time.time() >= deadline:
            return False
        time.sleep(poll_s)


def stop_build(build_url: str) -> None:
    """Arrête un build (dépassement de délai) pour qu'il ne chevauche pas le suivant."""
    _, auth = _jenkins()
    try:
        requests.post(f"{build_url}/stop", auth=auth, timeout=20)
        print(f"[JENKINS] Build arrêté (délai dépassé) : {build_url}")
    except requests.RequestException as e:
        print(f"[JENKINS] Arrêt du build impossible ({build_url}) : {e}")


def _acquire_local_lock(timeout_s: int, poll_s: int = 15) -> bool:
    """Verrou entre processus de l'agent sur ce poste (deux générations lancées en même temps)."""
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    deadline, announced = time.time() + timeout_s, False
    while True:
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                # Verrou abandonné (processus tué) : périmé au-delà du délai maximal d'un build
                if time.time() - os.path.getmtime(LOCK_FILE) > timeout_s + 600:
                    os.remove(LOCK_FILE)
                    continue
            except OSError:
                continue
        if not announced:
            print("[JENKINS] Un autre traitement de l'agent utilise Jenkins : attente de la fin de son build.")
            announced = True
        if time.time() >= deadline:
            return False
        time.sleep(poll_s)


def run_exclusive(job_name: str, trigger, timeout_s: int) -> dict:
    """
    Lance un build par `trigger()` (renvoie l'URL de file d'attente, '' en cas d'échec) et attend son résultat en
    garantissant qu'un seul build Cucumber tourne à la fois : verrou local entre traitements de l'agent, attente
    qu'aucun build des exclusive_jobs ne tourne ni n'attende, puis lancement ; un build qui dépasse le délai est arrêté
    pour ne pas chevaucher le suivant. Renvoie le résultat de wait_build_result ({"url", "result", "artifacts"}),
    result INDISPONIBLE et "message" si rien n'a été lancé.
    """
    if not _acquire_local_lock(timeout_s):
        return {"url": "", "result": "INDISPONIBLE", "artifacts": [], "message": "verrou Jenkins local non obtenu"}
    try:
        if not wait_until_idle(exclusive_jobs(job_name), timeout_s):
            return {"url": "", "result": "INDISPONIBLE", "artifacts": [],
                    "message": "un autre build Cucumber occupe Jenkins au-delà du délai"}
        queue = trigger()
        if not queue:
            return {"url": "", "result": "INDISPONIBLE", "artifacts": [], "message": "lancement refusé par Jenkins"}
        build = wait_build_result(queue, timeout_s, job_name=job_name)
        if build["result"] == "TIMEOUT" and build["url"]:
            stop_build(build["url"])
            wait_until_idle([job_name], 600)
        return build
    finally:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
