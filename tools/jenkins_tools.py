import os
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


def _job_path(job_name: str) -> str:
    """Chemin d'un job, dossiers compris (DOSSIER/JOB -> job/DOSSIER/job/JOB)."""
    return "/".join(f"job/{part}" for part in job_name.strip().strip("/").split("/") if part)


def job_parameters(job_name: str) -> dict:
    """Paramètres acceptés par un job : {nom: type Jenkins} ; {} si le job est introuvable ou Jenkins injoignable."""
    jenkins_url, auth = _jenkins()
    if not jenkins_url or not job_name.strip():
        return {}
    try:
        response = requests.get(f"{jenkins_url}/{_job_path(job_name)}/api/json", auth=auth, timeout=20,
                                params={"tree": "property[parameterDefinitions[name,type]]"})
        if response.status_code != 200:
            print(f"[JENKINS] Job {job_name} introuvable ({response.status_code}).")
            return {}
        return {p["name"]: p.get("type", "") for prop in response.json().get("property", [])
                for p in prop.get("parameterDefinitions") or []}
    except (requests.RequestException, ValueError) as e:
        print(f"[JENKINS] Impossible de lire les paramètres du job {job_name} : {e}")
        return {}


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
            return response.headers["Location"].rstrip("/")
        print(f"[JENKINS] Erreur lancement job ({response.status_code}) : {response.text[:300]}")
    except (OSError, requests.RequestException) as e:
        print(f"[JENKINS] Impossible de lancer le job {job_name} : {e}")
    return ""


def wait_build_result(queue_url: str, timeout_s: int, poll_s: int = 30) -> dict:
    """
    Attend la fin d'un build lancé (polling borné à timeout_s) : {"url", "result", "artifacts"} ; result vaut
    SUCCESS, UNSTABLE, FAILURE, ABORTED, ou TIMEOUT / INDISPONIBLE si le build n'a pas abouti à temps.
    """
    _, auth = _jenkins()
    deadline, build_url = time.time() + timeout_s, ""
    while time.time() < deadline:
        try:
            if not build_url:
                item = requests.get(f"{queue_url}/api/json", auth=auth, timeout=20).json()
                if item.get("cancelled"):
                    return {"url": "", "result": "ABORTED", "artifacts": []}
                build_url = ((item.get("executable") or {}).get("url") or "").rstrip("/")
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
