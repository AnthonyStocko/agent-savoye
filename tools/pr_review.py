"""
Revue assistée des pull requests Bitbucket (Data Center, API REST 1.0) des collaborateurs.

Déterministe (0 token) :
- review_inbox : PR ouvertes où l'utilisateur du jeton est relecteur (ou participant) ;
- parse_pr_url / pr_details / pr_diff : une PR, son diff (format unifié) et la carte des lignes commentables ;
- existing_comments : commentaires déjà posés (l'agent ne les répète pas) ;
- prepare_code : copie locale du dépôt au dernier commit de la PR (refs/pull-requests/<id>/from), pour que l'agent
  lise les fichiers entiers et pas seulement le diff ;
- post_comment : publication d'UN commentaire, uniquement sur action de l'utilisateur (ancré sur la ligne quand
  elle est dans le diff, sinon commentaire général citant fichier:ligne).

Configuration : BITBUCKET_URL, BITBUCKET_HTTP_TOKEN (jeton HTTP personnel ; la publication demande le droit
d'écriture sur les dépôts, la lecture suffit pour l'analyse).
"""
import json
import os
import re
import subprocess
from urllib.parse import urlsplit

import requests

WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(WORKSPACE_DIR, ".tmp", "pr_review")
JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
_PR_URL = re.compile(r"/projects/([^/]+)/repos/([^/]+)/pull-requests/(\d+)")
SEVERITIES = ("bloquant", "majeur", "mineur", "suggestion")


class BitbucketError(RuntimeError):
    pass


def base_url() -> str:
    return os.getenv("BITBUCKET_URL", "").strip().rstrip("/")


def _headers() -> dict:
    token = os.getenv("BITBUCKET_HTTP_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"} if token else {"Accept": "application/json"}


def configured() -> bool:
    return bool(base_url() and os.getenv("BITBUCKET_HTTP_TOKEN", "").strip())


def _get(path: str, params: dict = None, raw: bool = False):
    if not base_url():
        raise BitbucketError("BITBUCKET_URL non renseigné (onglet Configuration)")
    try:
        response = requests.get(f"{base_url()}{path}", headers=_headers(), params=params or {}, timeout=30)
    except requests.RequestException as e:
        raise BitbucketError(f"Bitbucket injoignable : {type(e).__name__}") from e
    if response.status_code == 401:
        raise BitbucketError("Bitbucket refuse le jeton (HTTP 401) : vérifie BITBUCKET_HTTP_TOKEN")
    if response.status_code != 200:
        raise BitbucketError(f"Bitbucket a répondu HTTP {response.status_code} : {response.text[:200]}")
    return response.text if raw else response.json()


def _paged(path: str, params: dict = None, limit: int = 200) -> list:
    values, start = [], 0
    while len(values) < limit:
        page = _get(path, {**(params or {}), "start": start, "limit": min(100, limit - len(values))})
        values += page.get("values") or []
        if page.get("isLastPage", True) or "nextPageStart" not in page:
            break
        start = page["nextPageStart"]
    return values


def jira_keys(*texts: str) -> list:
    return list(dict.fromkeys(k for t in texts for k in JIRA_KEY.findall(t or "")))


def summarize(pr: dict) -> dict:
    """Fiche courte d'une PR (réponse de l'API)."""
    repo = (pr.get("toRef") or {}).get("repository") or {}
    author = ((pr.get("author") or {}).get("user") or {})
    links = (pr.get("links") or {}).get("self") or [{}]
    source, target = (pr.get("fromRef") or {}), (pr.get("toRef") or {})
    return {"id": pr.get("id"), "titre": pr.get("title", ""), "description": pr.get("description", "") or "",
            "projet": (repo.get("project") or {}).get("key", ""), "depot": repo.get("slug", ""),
            "auteur": author.get("displayName") or author.get("name", ""), "auteur_id": author.get("name", ""),
            "source": source.get("displayId", ""), "cible": target.get("displayId", ""),
            "commit": source.get("latestCommit", ""), "version": pr.get("version", 0),
            "maj": pr.get("updatedDate", 0), "url": links[0].get("href", ""),
            "relecteurs": [{"nom": (r.get("user") or {}).get("displayName", ""), "statut": r.get("status", "")}
                           for r in pr.get("reviewers") or []],
            "jira": jira_keys(pr.get("title", ""), source.get("displayId", ""), pr.get("description", ""))}


def review_inbox(role: str = "REVIEWER") -> list:
    """PR ouvertes où l'utilisateur du jeton a ce rôle (REVIEWER, PARTICIPANT, AUTHOR), plus récentes d'abord."""
    prs = _paged("/rest/api/1.0/dashboard/pull-requests", {"state": "OPEN", "role": role, "order": "NEWEST"})
    return [summarize(p) for p in prs]


def parse_pr_url(url: str) -> tuple:
    """(projet, dépôt, numéro) d'une adresse de PR Bitbucket ; ValueError si elle n'en est pas une."""
    match = _PR_URL.search(urlsplit((url or "").strip()).path)
    if not match:
        raise ValueError("adresse de PR attendue : …/projects/<PROJET>/repos/<dépôt>/pull-requests/<n°>")
    return match.group(1).upper(), match.group(2), int(match.group(3))


def _pr_path(project: str, repo: str, number: int) -> str:
    return f"/rest/api/1.0/projects/{project}/repos/{repo}/pull-requests/{number}"


def pr_details(project: str, repo: str, number: int) -> dict:
    return summarize(_get(_pr_path(project, repo, number)))


def diff_to_text(data: dict) -> tuple:
    """
    Diff JSON de Bitbucket en diff unifié lisible, et carte des lignes commentables :
    {chemin: {n° de ligne (nouvelle version): "ADDED" | "CONTEXT"}} (les lignes supprimées : "REMOVED:<n° source>").
    """
    out, lines_map = [], {}
    for diff in data.get("diffs") or []:
        source = (diff.get("source") or {}).get("toString")
        destination = (diff.get("destination") or {}).get("toString")
        path = destination or source or "?"
        out.append(f"diff --git a/{source or path} b/{destination or path}")
        if not source:
            out.append("new file")
        if not destination:
            out.append("deleted file")
        if diff.get("binary"):
            out.append("(fichier binaire)")
            continue
        out += [f"--- {'a/' + source if source else '/dev/null'}", f"+++ {'b/' + destination if destination else '/dev/null'}"]
        commentable = lines_map.setdefault(path, {})
        for hunk in diff.get("hunks") or []:
            out.append(f"@@ -{hunk.get('sourceLine', 0)},{hunk.get('sourceSpan', 0)} "
                       f"+{hunk.get('destinationLine', 0)},{hunk.get('destinationSpan', 0)} @@")
            for segment in hunk.get("segments") or []:
                kind = segment.get("type", "CONTEXT")
                mark = {"ADDED": "+", "REMOVED": "-"}.get(kind, " ")
                for line in segment.get("lines") or []:
                    out.append(mark + line.get("line", ""))
                    if kind == "REMOVED":
                        continue
                    commentable[line.get("destination")] = kind
        if diff.get("truncated"):
            out.append("(diff tronqué par Bitbucket)")
    return "\n".join(out) + "\n", lines_map


def pr_diff(project: str, repo: str, number: int, context: int = 10) -> tuple:
    """(diff unifié, carte des lignes commentables, fichiers modifiés)."""
    data = _get(_pr_path(project, repo, number) + "/diff", {"contextLines": context, "withComments": "false"})
    text, lines_map = diff_to_text(data)
    return text, lines_map, sorted(lines_map)


def existing_comments(project: str, repo: str, number: int) -> list:
    """Commentaires déjà posés sur la PR : [{auteur, fichier, ligne, texte}]."""
    out = []
    for activity in _paged(_pr_path(project, repo, number) + "/activities", limit=300):
        comment = activity.get("comment") or {}
        if activity.get("action") != "COMMENTED" or not comment:
            continue
        anchor = activity.get("commentAnchor") or {}
        stack = [comment]
        while stack:
            current = stack.pop()
            out.append({"auteur": (current.get("author") or {}).get("displayName", ""),
                        "fichier": anchor.get("path", ""), "ligne": anchor.get("line"),
                        "texte": " ".join(str(current.get("text", "")).split())[:500]})
            stack += current.get("comments") or []
    return out


def _git(args: list, cwd: str = None) -> subprocess.CompletedProcess:
    token = os.getenv("BITBUCKET_HTTP_TOKEN", "").strip()
    extra = ["-c", f"http.extraHeader=Authorization: Bearer {token}"] if token else []
    return subprocess.run(["git"] + extra + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def prepare_code(project: str, repo: str, number: int, commit: str) -> tuple:
    """
    Copie locale du dépôt au dernier commit de la PR (lecture seule pour l'agent) : clone gardé entre deux revues,
    fetch de refs/pull-requests/<n°>/from. Renvoie (dossier, '') ou ('', raison) — l'analyse se fait alors sur le
    diff seul.
    """
    clone = os.path.join(ROOT, "depots", f"{project}_{repo}")
    work = os.path.join(ROOT, "code", f"{project}_{repo}_{number}")
    url = f"{base_url()}/scm/{project.lower()}/{repo}.git"
    if not os.path.isdir(os.path.join(clone, ".git")):
        os.makedirs(os.path.dirname(clone), exist_ok=True)
        result = _git(["clone", "--no-checkout", "--quiet", url, clone])
        if result.returncode != 0:
            return "", f"clonage impossible : {result.stderr.strip()[:200]}"
    fetched = _git(["fetch", "--quiet", "origin", f"+refs/pull-requests/{number}/from:refs/pr/{number}"], clone)
    if fetched.returncode != 0:
        return "", f"récupération de la PR impossible : {fetched.stderr.strip()[:200]}"
    target = commit or f"refs/pr/{number}"
    if os.path.isdir(work):
        _git(["worktree", "remove", "--force", work], clone)
    _git(["worktree", "prune"], clone)
    added = _git(["worktree", "add", "--detach", "--force", work, target], clone)
    if added.returncode != 0:
        return "", f"copie du code de la PR impossible : {added.stderr.strip()[:200]}"
    return work, ""


def anchor_type(lines_map: dict, path: str, line) -> str:
    """Type de ligne Bitbucket pour ancrer un commentaire ('' : ligne hors du diff, commentaire général)."""
    try:
        return (lines_map.get(path) or {}).get(int(line), "")
    except (TypeError, ValueError):
        return ""


def post_comment(project: str, repo: str, number: int, text: str, path: str = "", line=None,
                 lines_map: dict = None) -> dict:
    """
    Publie UN commentaire sur la PR (au nom du propriétaire du jeton). Ligne présente dans le diff : ancré dessus ;
    sinon commentaire général préfixé de « fichier:ligne ». Renvoie {"id", "ancre"}.
    """
    kind = anchor_type(lines_map or {}, path, line) if path else ""
    body = {"text": text}
    if kind:
        body["anchor"] = {"path": path, "line": int(line), "lineType": kind, "fileType": "TO", "diffType": "EFFECTIVE"}
    elif path:
        body["text"] = f"`{path}" + (f":{line}" if line else "") + f"` — {text}"
    try:
        response = requests.post(f"{base_url()}{_pr_path(project, repo, number)}/comments",
                                 headers={**_headers(), "Content-Type": "application/json"}, json=body, timeout=30)
    except requests.RequestException as e:
        raise BitbucketError(f"Bitbucket injoignable : {type(e).__name__}") from e
    if response.status_code not in (200, 201):
        raise BitbucketError(f"commentaire refusé (HTTP {response.status_code}) : {response.text[:200]}"
                             + (" — le jeton a-t-il le droit d'écriture ?" if response.status_code in (401, 403)
                                else ""))
    return {"id": response.json().get("id"), "ancre": bool(kind)}


# ---------------------------------------------------------------------------
# Résultats d'analyse (un fichier par PR)
# ---------------------------------------------------------------------------

def result_path(project: str, repo: str, number: int) -> str:
    return os.path.join(ROOT, "revues", f"{project}_{repo}_{number}.json")


def load_result(project: str, repo: str, number: int) -> dict:
    try:
        with open(result_path(project, repo, number), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_result(project: str, repo: str, number: int, result: dict) -> None:
    path = result_path(project, repo, number)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def parse_review(text: str) -> dict:
    """Bloc JSON final de l'agent, normalisé (gravités connues, lignes entières)."""
    data = {}
    for candidate in re.findall(r"```(?:json)?\s*(.*?)```", text or "", flags=re.S)[::-1] + [text or ""]:
        start, end = candidate.find("{"), candidate.rfind("}")
        if 0 <= start < end:
            try:
                data = json.loads(candidate[start:end + 1])
                break
            except ValueError:
                continue
    comments = []
    for c in data.get("commentaires") or []:
        if not isinstance(c, dict) or not str(c.get("message", "")).strip():
            continue
        try:
            line = int(c.get("ligne")) if c.get("ligne") not in (None, "") else None
        except (TypeError, ValueError):
            line = None
        severity = str(c.get("gravite", "mineur")).lower()
        comments.append({"fichier": str(c.get("fichier", "")).strip(), "ligne": line,
                         "gravite": severity if severity in SEVERITIES else "mineur",
                         "message": str(c["message"]).strip(), "suggestion": str(c.get("suggestion", "")).strip()})
    order = {s: i for i, s in enumerate(SEVERITIES)}
    comments.sort(key=lambda c: (order[c["gravite"]], c["fichier"], c["ligne"] or 0))
    verdict = str(data.get("verdict", "")).lower()
    return {"resume": str(data.get("resume", "")).strip(), "verdict": verdict if verdict in (
                "approuver", "changements_demandes", "a_discuter") else "a_discuter",
            "risque": str(data.get("risque", "")).lower() or "?", "points_positifs": [
                str(p) for p in data.get("points_positifs") or []][:10],
            "questions": [str(q) for q in data.get("questions") or []][:10],
            "tests": str(data.get("tests", "")).strip(), "conformite_ticket": str(data.get("conformite_ticket", "")).strip(),
            "commentaires": comments[:60]}
