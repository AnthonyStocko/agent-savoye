"""
Code du Standard lu dans Bitbucket (API REST), sans clone ni fetch.

Deux sources de même interface, pour les contrôles déterministes (preuves des jobs de l'init, modules déployés,
groupIds du Standard) :
  - RemoteStandard : projet Bitbucket du Standard (ex. COMPONENT), lu par l'API REST à la référence de la version
    du client (branche v<version>, release/<version>, tag) ;
  - LocalStandard : dossier de clones git (tests, ou Standard local configuré).

Références : mêmes candidats que pour les clones (« origin/v26.24 » → branche v26.24, « refs/tags/v26.24 » → tag,
« HEAD » → branche par défaut). Chaque référence est résolue en commit ; listes de fichiers et contenus sont gardés en
cache disque par commit (un contenu à un commit ne change jamais : cache toujours valide, jamais de fetch).

Recherche de code : Bitbucket n'indexe que la branche par défaut. Un nom trouvé y est confirmé en lisant le fichier
à la référence de la version ; un fichier absent ou différent à cette version donne un résultat « non confirmé »
(indéterminé), jamais une absence. Une erreur d'API lève BitbucketUnavailable : ni preuve, ni absence.
"""
import hashlib
import json
import os
import re
from urllib.parse import quote

from tools.cucumber_tools import _git, git_repos, pick_ref

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp", "bitbucket_cache")


class BitbucketUnavailable(RuntimeError):
    """API Bitbucket en erreur (réseau, droits, adresse) : ni preuve, ni absence."""


def project_key(repo_input: str) -> str:
    """Clé du projet d'une adresse « …/projects/<KEY> » (projet entier), '' sinon."""
    text = (repo_input or "").rstrip("/")
    if "/projects/" in text and "/repos/" not in text and not text.endswith(".git"):
        return text.split("/")[-1].upper()
    return ""


class LocalStandard:
    """Standard en clones git locaux (un dossier par dépôt)."""

    def __init__(self, root: str):
        self.root = root
        self._repos = git_repos(root)
        self.label = root

    def repos(self) -> list:
        return sorted(self._repos)

    def ref(self, repo: str, candidates) -> str:
        return pick_ref(self._repos[repo], candidates) if repo in self._repos else ""

    def files(self, repo: str, ref: str) -> list:
        return _git(self._repos[repo], "ls-tree", "-r", "--name-only", ref).stdout.splitlines()

    def show(self, repo: str, ref: str, path: str) -> str:
        shown = _git(self._repos[repo], "show", f"{ref}:{path}")
        return shown.stdout if shown.returncode == 0 else ""

    def find_quoted(self, name: str, candidates, suffix: str = ".java") -> tuple:
        """Fichiers citant « "name" » à la référence de version de chaque dépôt : ([(dépôt, réf, chemin)], [])."""
        hits = []
        for repo in self.repos():
            ref = self.ref(repo, candidates)
            if not ref:
                continue
            grep = _git(self._repos[repo], "grep", "-I", "-l", "-F", f'"{name}"', ref, "--", f"*{suffix}")
            hits += [(repo, ref, line.split(":", 1)[-1]) for line in grep.stdout.splitlines()]
        return hits, []

    def find_file(self, suffix: str, candidates) -> tuple:
        """Fichier dont le chemin finit par `suffix`, à la référence de version : ((dépôt, réf, chemin) | None, [])."""
        for repo in self.repos():
            ref = self.ref(repo, candidates)
            if ref:
                hit = next((f for f in self.files(repo, ref) if f.endswith(suffix)), "")
                if hit:
                    return (repo, ref, hit), []
        return None, []


class RemoteStandard:
    """Standard lu dans un projet Bitbucket par l'API REST (aucun clone)."""

    def __init__(self, project: str, base: str = "", token: str = "", http=None, cache_dir: str = CACHE_DIR):
        self.project = project.upper()
        self.base = (base or os.getenv("BITBUCKET_URL", "")).strip().rstrip("/")
        self.token = token if token else os.getenv("BITBUCKET_HTTP_TOKEN", "").strip()
        self.cache_dir = os.path.join(cache_dir, self.project)
        self.label = f"Bitbucket {self.project}"
        self._http = http
        self._repos = None
        self._refs = {}
        self.calls = 0

    # -- HTTP ---------------------------------------------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs):
        if self._http is not None:
            return self._http(method, url, **kwargs)
        import requests
        if not self.base:
            raise BitbucketUnavailable("BITBUCKET_URL non renseignée")
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            return requests.request(method, url, headers=headers, timeout=60, **kwargs)
        except requests.RequestException as exc:
            raise BitbucketUnavailable(f"API Bitbucket injoignable : {' '.join(str(exc).split())[:200]}") from exc

    def _get(self, path: str, params: dict = None, raw: bool = False):
        self.calls += 1
        response = self._request("GET", f"{self.base}{path}", params=params or {})
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise BitbucketUnavailable(f"API Bitbucket {path} : HTTP {response.status_code}")
        return response.text if raw else response.json()

    def _paged(self, path: str, params: dict = None) -> list:
        values, start = [], 0
        while True:
            page = self._get(path, {**(params or {}), "start": start, "limit": 1000})
            if page is None:
                return values
            values += page.get("values") or []
            if page.get("isLastPage", True):
                return values
            start = page.get("nextPageStart", start + len(page.get("values") or []))

    def _repo_path(self, repo: str) -> str:
        return f"/rest/api/1.0/projects/{self.project}/repos/{quote(repo, safe='')}"

    # -- Cache disque par commit ------------------------------------------------------------------------------

    def _cache(self, kind: str, repo: str, commit: str, path: str = ""):
        key = hashlib.sha1(f"{kind}|{repo}|{commit}|{path}".encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, repo, f"{kind}-{key}.json")

    def _cached(self, file: str):
        try:
            with open(file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _store(self, file: str, value) -> None:
        os.makedirs(os.path.dirname(file), exist_ok=True)
        with open(file, "w", encoding="utf-8") as f:
            json.dump(value, f)

    # -- Interface commune ------------------------------------------------------------------------------------

    def repos(self) -> list:
        if self._repos is None:
            self._repos = sorted(v["slug"] for v in self._paged(f"/rest/api/1.0/projects/{self.project}/repos"))
        return self._repos

    def _commit(self, repo: str, candidate: str) -> str:
        """Commit d'une référence candidate (« origin/<branche> », « refs/tags/<tag> », « <tag> », « HEAD »)."""
        key = (repo, candidate)
        if key in self._refs:
            return self._refs[key]
        commit = ""
        if candidate in ("HEAD", "origin/HEAD"):
            found = self._get(f"{self._repo_path(repo)}/branches/default")
            commit = (found or {}).get("latestCommit", "")
        else:
            name = candidate.split("origin/", 1)[1] if candidate.startswith("origin/") else candidate
            is_tag = candidate.startswith("refs/tags/") or not candidate.startswith("origin/")
            name = name.split("refs/tags/", 1)[-1]
            kind = "tags" if is_tag else "branches"
            for value in self._paged(f"{self._repo_path(repo)}/{kind}", {"filterText": name}):
                if value.get("displayId") == name:
                    commit = value.get("latestCommit") or value.get("latestChangeset") or ""
                    break
        self._refs[key] = commit
        return commit

    def ref(self, repo: str, candidates) -> str:
        """Première référence candidate qui existe, renvoyée comme son commit (immuable)."""
        for candidate in candidates:
            commit = self._commit(repo, candidate)
            if commit:
                self._refs[(repo, commit)] = commit
                self._refs.setdefault(("nom", repo, commit), candidate)
                return commit
        return ""

    def ref_name(self, repo: str, commit: str) -> str:
        """Nom lisible (« v26.24 ») de la référence résolue en `commit`."""
        return self._refs.get(("nom", repo, commit), commit[:10]).split("/")[-1]

    def files(self, repo: str, ref: str) -> list:
        file = self._cache("files", repo, ref)
        cached = self._cached(file)
        if cached is not None:
            return cached
        values = self._paged(f"{self._repo_path(repo)}/files", {"at": ref})
        self._store(file, values)
        return values

    def show(self, repo: str, ref: str, path: str) -> str:
        file = self._cache("raw", repo, ref, path)
        cached = self._cached(file)
        if cached is not None:
            return cached
        text = self._get(f"{self._repo_path(repo)}/raw/{quote(path)}", {"at": ref}, raw=True)
        text = text or ""
        self._store(file, text)
        return text

    def search(self, query: str) -> list:
        """Recherche de code (branche par défaut indexée) : [(dépôt, chemin)] du projet."""
        self.calls += 1
        response = self._request("POST", f"{self.base}/rest/search/latest/search",
                                 json={"query": f"{query} project:{self.project}",
                                       "entities": {"code": {"start": 0, "limit": 25}}})
        if response.status_code != 200:
            raise BitbucketUnavailable(f"recherche de code Bitbucket : HTTP {response.status_code}")
        return [(v["repository"]["slug"], v["file"])
                for v in (response.json().get("code") or {}).get("values") or []]

    def find_quoted(self, name: str, candidates, suffix: str = ".java") -> tuple:
        """
        Fichiers citant « "name" » : candidats de la recherche (branche par défaut), confirmés à la référence de
        version du dépôt. ([(dépôt, réf, chemin)] confirmés, [« dépôt/chemin : raison »] non confirmés).
        """
        confirmed, unconfirmed = [], []
        for repo, path in self.search(f'"{name}"'):
            if not path.endswith(suffix):
                continue
            ref = self.ref(repo, candidates)
            if not ref:
                unconfirmed.append(f"{repo}/{path} : aucune référence de la version dans {repo}")
                continue
            if f'"{name}"' in self.show(repo, ref, path):
                confirmed.append((repo, ref, path))
            else:
                unconfirmed.append(f"{repo}/{path} : absent ou différent à {self.ref_name(repo, ref)}")
        return confirmed, unconfirmed

    def find_file(self, suffix: str, candidates) -> tuple:
        """Fichier dont le chemin finit par `suffix` (classe Java) : candidat de la recherche, présent à la version."""
        simple = suffix.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        unconfirmed = []
        for repo, path in self.search(f"{simple} ext:java"):
            if not path.endswith(suffix):
                continue
            ref = self.ref(repo, candidates)
            if ref and self.show(repo, ref, path):
                return (repo, ref, path), []
            unconfirmed.append(f"{repo}/{path} : absent à la version")
        return None, unconfirmed


def standard_source(std):
    """Source du Standard : objet déjà construit, ou dossier de clones (LocalStandard)."""
    return std if hasattr(std, "find_quoted") else LocalStandard(std or "")


def ref_label(std, repo: str, ref: str) -> str:
    """Nom lisible d'une référence (« v26.24 »)."""
    if hasattr(std, "ref_name"):
        return std.ref_name(repo, ref)
    return re.split(r"/", ref)[-1]


PRODUCTS = ("wms", "tms", "oms", "wes", "exc", "ref", "slo", "tra", "inv", "lab", "tls")


def repos_for_client(repos: list, deployed: set) -> list:
    """
    Dépôts du Standard utiles à un client : ceux d'un domaine dont un module est déployé chez lui (préfixe
    « <produit>-<domaine>- » d'un artefact déployé, ex. wms-preparation-*), plus les dépôts transverses (sans
    préfixe de produit : socle, utilitaires, contribs). `deployed` vide : tous les dépôts (rien n'est deviné).
    """
    if not deployed:
        return list(repos)
    kept = []
    for repo in repos:
        parts = repo.split("-")
        if parts[0] not in PRODUCTS or len(parts) < 2:
            kept.append(repo)
        elif any(d.startswith(f"{parts[0]}-{parts[1]}-") or d == f"{parts[0]}-{parts[1]}" for d in deployed):
            kept.append(repo)
    return kept
