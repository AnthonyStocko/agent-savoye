import os
import requests
from dotenv import load_dotenv

load_dotenv()

BITBUCKET_URL = os.getenv("BITBUCKET_URL", "").rstrip("/")
BITBUCKET_TOKEN = os.getenv("BITBUCKET_HTTP_TOKEN")

def fetch_file_from_bitbucket(project_key: str, repo_slug: str, file_path: str, commit_or_branch: str = "main") -> str:
    """Récupère le contenu d'un fichier du dépôt Bitbucket en lecture seule."""
    url = f"{BITBUCKET_URL}/rest/api/1.0/projects/{project_key}/repos/{repo_slug}/raw/{file_path}?at={commit_or_branch}"
    headers = {
        "Authorization": f"Bearer {BITBUCKET_TOKEN}",
        "Accept": "text/plain"
    }
    
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.text
    else:
        return f"Erreur de lecture ({response.status_code}) : {response.text}"