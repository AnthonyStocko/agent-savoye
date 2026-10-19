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
