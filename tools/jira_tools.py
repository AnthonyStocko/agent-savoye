import os
import re
import requests
from dotenv import load_dotenv

load_dotenv()


def _extract_adf_text(adf_data) -> str:
    """Extrait le texte brut d'une structure Atlassian Document Format (ADF) ou renvoie la chaîne si c'est déjà du texte."""
    if not adf_data:
        return ""
    if isinstance(adf_data, str):
        return adf_data
    
    text_parts = []
    if isinstance(adf_data, dict):
        if adf_data.get("type") == "text":
            text_parts.append(adf_data.get("text", ""))
        for child in adf_data.get("content", []):
            text_parts.append(_extract_adf_text(child))
    elif isinstance(adf_data, list):
        for item in adf_data:
            text_parts.append(_extract_adf_text(item))
            
    return "\n".join([t for t in text_parts if t.strip()])


class JiraSearchError(Exception):
    """Erreur de recherche Jira à remonter à l'utilisateur (auth, JQL invalide, réseau)."""


def is_epic_issue_type(issue_type: dict) -> bool:
    """Vrai pour une épopée : niveau hiérarchique 1, ou à défaut le nom du type de ticket."""
    issue_type = issue_type or {}
    if issue_type.get("hierarchyLevel") is not None:
        return issue_type["hierarchyLevel"] == 1
    return issue_type.get("name", "").strip().lower() in ("epic", "épopée", "epopée", "epopee")


def _status_jql_values(api: str, auth: tuple, statuses: list) -> list:
    """
    Valeurs JQL des statuts demandés. Le JQL compare le nom d'origine du statut, pas son nom affiché :
    « Ouvert » peut être la traduction de OPEN. Chaque nom est donc remplacé par les identifiants de tous
    les statuts qui s'affichent ou se nomment ainsi (nom gardé tel quel s'il est inconnu ou en cas d'erreur).
    """
    names = [s.strip() for s in statuses if s.strip()]
    try:
        response = requests.get(f"{api}/status", auth=auth, timeout=15)
        known = response.json() if response.status_code == 200 else []
    except (requests.RequestException, ValueError):
        known = []
    values = []
    for name in names:
        ids = [s["id"] for s in known
               if name.lower() in ((s.get("name") or "").lower(), (s.get("untranslatedName") or "").lower())]
        values += ids or [f'"{name}"']
    return list(dict.fromkeys(values))


def fetch_tickets_data(
    project_key: str = None,
    statuses: list = None,
    labels: list = None,
    team: str = None,
    epics_only: bool = False,
    atlassian_team: str = None
) -> list:
    """
    Récupère les tickets Jira sous forme de liste de dictionnaires (clé, titre/summary, statut, labels, épique).
    `epics_only` restreint la recherche aux épopées ; `atlassian_team` filtre strictement sur un composant
    ou une équipe Atlassian (champ Team), contrairement à `team` qui cherche aussi dans le texte.
    """
    domain = os.getenv("JIRA_DOMAIN", "").replace("https://", "").replace("http://", "").strip("/")
    email = os.getenv("JIRA_EMAIL", "").strip()
    token = os.getenv("JIRA_API_TOKEN", "").strip()
    project = project_key or os.getenv("JIRA_PROJECT_KEY", "").strip()

    if not all([domain, email, token]):
        raise JiraSearchError("Identifiants Jira manquants dans la configuration (.env).")

    url = f"https://{domain}/rest/api/3/search/jql"
    auth = (email, token)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    jql_clauses = [f'project = "{project}"']

    if epics_only:
        # hierarchyLevel = 1 : épopées, quel que soit le nom (traduit ou non) du type de ticket
        jql_clauses.append('hierarchyLevel = 1')

    if atlassian_team and atlassian_team.strip():
        jql_clauses.append(_team_jql_clause(project, atlassian_team.strip()))

    if statuses:
        status_list_str = ", ".join(_status_jql_values(f"https://{domain}/rest/api/3", auth, statuses))
        if status_list_str:
            jql_clauses.append(f'status IN ({status_list_str})')

    if labels:
        for label in labels:
            if label.strip():
                jql_clauses.append(f'labels = "{label.strip()}"')

    if team and team.strip():
        jql_clauses.append(f'(component = "{team.strip()}" OR text ~ "{team.strip()}")')

    jql = " AND ".join(jql_clauses)
    payload = {
        "jql": jql,
        "fields": ["key", "summary", "description", "status", "parent", "labels", "components", "issuetype"]
    }

    try:
        response = requests.post(url, headers=headers, auth=auth, json=payload, timeout=30)
    except requests.RequestException as e:
        raise JiraSearchError(f"Jira injoignable : {e}") from e

    if response.status_code != 200:
        try:
            messages = response.json().get("errorMessages") or [response.text[:300]]
        except ValueError:
            messages = [response.text[:300]]
        raise JiraSearchError(f"Recherche Jira refusée ({response.status_code}) : {' '.join(messages)} — JQL : {jql}")

    issues = response.json().get("issues", [])

    # Jira Cloud répond 200 avec une liste vide aux requêtes anonymes : un token expiré
    # ou révoqué ressemble donc à « aucun ticket ». On vérifie l'authentification.
    if not issues:
        try:
            me = requests.get(f"https://{domain}/rest/api/3/myself", headers=headers, auth=auth, timeout=10)
        except requests.RequestException as e:
            raise JiraSearchError(f"Jira injoignable : {e}") from e
        if me.status_code != 200:
            raise JiraSearchError(
                f"Authentification Jira refusée ({me.status_code}) pour {email} : "
                "le token API (JIRA_API_TOKEN) est probablement expiré ou révoqué."
            )

    try:

        results = []
        for issue in issues:
            key = issue["key"]
            summary = issue["fields"].get("summary", "")
            status_name = issue["fields"].get("status", {}).get("name", "Inconnu")
            issue_labels = issue["fields"].get("labels", [])
            parent = issue["fields"].get("parent")

            epic_info = "Aucune Épopée associée."
            if parent:
                epic_key = parent["key"]
                epic_url = f"https://{domain}/rest/api/3/issue/{epic_key}?fields=summary,description"
                epic_resp = requests.get(epic_url, headers=headers, auth=auth)
                if epic_resp.status_code == 200:
                    epic_data = epic_resp.json()
                    epic_summary = epic_data["fields"].get("summary", "")
                    epic_info = f"Épopée [{epic_key}] - {epic_summary}"

            results.append({
                "key": key,
                "summary": summary,
                "status": status_name,
                "labels": issue_labels,
                "epic": epic_info,
                "is_epic": is_epic_issue_type(issue["fields"].get("issuetype")),
                "components": [c.get("name", "") for c in issue["fields"].get("components") or []],
                "description": _extract_adf_text(issue["fields"].get("description"))[:3000]
            })

        return results

    except Exception as e:
        raise JiraSearchError(f"Erreur lors du traitement des tickets Jira : {e}") from e


def get_jira_issue_summary(issue_key: str) -> str:
    """Récupère uniquement le titre (summary) d'un ticket Jira par sa clé."""
    domain = os.getenv("JIRA_DOMAIN", "").replace("https://", "").replace("http://", "").strip("/")
    email = os.getenv("JIRA_EMAIL", "").strip()
    token = os.getenv("JIRA_API_TOKEN", "").strip()

    if not all([domain, email, token, issue_key]):
        return ""

    url = f"https://{domain}/rest/api/3/issue/{issue_key}?fields=summary"
    auth = (email, token)
    headers = {"Accept": "application/json"}

    try:
        response = requests.get(url, headers=headers, auth=auth, timeout=5)
        if response.status_code == 200:
            return response.json().get("fields", {}).get("summary", "")
    except Exception:
        pass
    return ""


def fetch_tickets_with_epic_context(
    project_key: str = None,
    statuses: list = None,
    labels: list = None,
    team: str = None
) -> str:
    """Récupère les tickets Jira du projet selon des filtres paramétrables (statuts, labels, équipe)."""
    try:
        tickets = fetch_tickets_data(project_key=project_key, statuses=statuses, labels=labels, team=team)
    except JiraSearchError as e:
        return f"Erreur : {e}"

    if not tickets:
        return "Aucun ticket trouvé avec ces critères."

    results = []
    for issue in tickets:
        results.append(
            f"--- TICKET TROUVÉ ---\n"
            f"Clé: {issue['key']}\n"
            f"Statut Jira: {issue['status']}\n"
            f"Labels: {issue['labels']}\n"
            f"Titre du ticket: {issue['summary']}\n"
            f"Contexte Fonctionnel (Épopée): {issue['epic']}\n"
        )

    return "\n".join(results)


def add_jira_attachment(issue_key: str, file_path: str) -> str:
    """Attache un fichier local (ex: .patch) au ticket Jira spécifié."""
    domain = os.getenv("JIRA_DOMAIN", "").replace("https://", "").replace("http://", "").strip("/")
    email = os.getenv("JIRA_EMAIL", "").strip()
    token = os.getenv("JIRA_API_TOKEN", "").strip()

    if not os.path.exists(file_path):
        return f"Erreur : le fichier {file_path} n'existe pas."

    url = f"https://{domain}/rest/api/3/issue/{issue_key}/attachments"
    auth = (email, token)
    headers = {"X-Atlassian-Token": "no-check"}

    try:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f)}
            response = requests.post(url, headers=headers, auth=auth, files=files)

        if response.status_code in [200, 201]:
            return f"Fichier {file_path} attaché avec succès au ticket {issue_key}."
        else:
            return f"Erreur d'ajout ({response.status_code}) : {response.text}"
    except Exception as e:
        return f"Erreur : {str(e)}"


def get_jira_issue_details(issue_key: str) -> str:
    """Récupère la description complète et l'épopée parente d'un ticket Jira."""
    domain = os.getenv("JIRA_DOMAIN", "").replace("https://", "").replace("http://", "").strip("/")
    email = os.getenv("JIRA_EMAIL", "").strip()
    token = os.getenv("JIRA_API_TOKEN", "").strip()

    if not all([domain, email, token]):
        return "Erreur : Identifiants Jira manquants."

    url = f"https://{domain}/rest/api/3/issue/{issue_key}"
    auth = (email, token)
    headers = {"Accept": "application/json"}

    try:
        response = requests.get(url, headers=headers, auth=auth)
        if response.status_code != 200:
            return f"Erreur API Jira ({response.status_code}) : {response.text}"

        data = response.json()
        fields = data.get("fields", {})
        summary = fields.get("summary", "")
        desc = _extract_adf_text(fields.get("description")) or "Aucune description"
        parent = fields.get("parent")

        epic_info = "Aucune Épopée parente."
        if parent:
            epic_key = parent["key"]
            epic_url = f"https://{domain}/rest/api/3/issue/{epic_key}"
            epic_resp = requests.get(epic_url, headers=headers, auth=auth)
            if epic_resp.status_code == 200:
                epic_fields = epic_resp.json().get("fields", {})
                epic_desc = _extract_adf_text(epic_fields.get('description'))
                epic_info = f"Épopée [{epic_key}] : {epic_fields.get('summary')}\nDescription Épopée: {epic_desc}"

        return f"TICKET {issue_key}\nTitre: {summary}\nDescription: {desc}\n\n{epic_info}"
    except Exception as e:
        return f"Erreur : {str(e)}"


def get_full_jira_issue_context(issue_key: str, exclude_sections: tuple = ()) -> dict:
    """
    Récupère la description du ticket, les détails de l'Épopée (Epic) parente,
    ainsi que les fiches COST ou tickets d'exigences liés.
    `exclude_sections` : titres des sections de la description à ignorer (ex. section produite par l'agent).
    """
    domain = os.getenv("JIRA_DOMAIN", "").replace("https://", "").replace("http://", "").strip("/")
    email = os.getenv("JIRA_EMAIL", "").strip()
    token = os.getenv("JIRA_API_TOKEN", "").strip()

    if not all([domain, email, token]):
        return {"error": "Identifiants Jira manquants dans le fichier .env"}

    url = f"https://{domain}/rest/api/3/issue/{issue_key}"
    auth = (email, token)
    headers = {"Accept": "application/json"}

    try:
        response = requests.get(url, headers=headers, auth=auth, timeout=10)
        if response.status_code != 200:
            return {"error": f"Impossible de récupérer le ticket {issue_key} (Code {response.status_code})"}

        data = response.json()
        fields = data.get("fields", {})

        # 1. Description du ticket
        description = _extract_adf_text(remove_adf_sections(fields.get("description"), exclude_sections))

        # 2. Récupération de l'Épopée (Epic / Parent)
        epic_data = {}
        parent = fields.get("parent")
        epic_key = parent.get("key") if parent else (fields.get("customfield_10008") or fields.get("epic"))
        
        if epic_key:
            epic_res = requests.get(f"https://{domain}/rest/api/3/issue/{epic_key}", headers=headers, auth=auth, timeout=10)
            if epic_res.status_code == 200:
                epic_json = epic_res.json()
                epic_fields = epic_json.get("fields", {})
                epic_data = {
                    "key": epic_key,
                    "summary": epic_fields.get("summary", ""),
                    "description": _extract_adf_text(epic_fields.get("description"))
                }

        # 3. Récupération des fiches COST ou tickets liés
        cost_data = []
        issue_links = fields.get("issuelinks", [])
        for link in issue_links:
            linked_issue = link.get("inwardIssue") or link.get("outwardIssue")
            if linked_issue:
                linked_key = linked_issue.get("key", "")
                issue_type = linked_issue.get("fields", {}).get("issuetype", {}).get("name", "")
                
                if "COST" in linked_key or issue_type in ["Cost", "Specification", "Requirement"]:
                    cost_res = requests.get(f"https://{domain}/rest/api/3/issue/{linked_key}", headers=headers, auth=auth, timeout=10)
                    if cost_res.status_code == 200:
                        cost_json = cost_res.json()
                        cost_fields = cost_json.get("fields", {})
                        cost_data.append({
                            "key": linked_key,
                            "summary": cost_fields.get("summary", ""),
                            "description": _extract_adf_text(cost_fields.get("description"))
                        })

        return {
            "issue_key": issue_key,
            "summary": fields.get("summary", ""),
            "components": [c.get("name", "") for c in fields.get("components") or []],
            "description": description,
            "epic": epic_data,
            "costs": cost_data
        }
    except Exception as e:
        return {"error": f"Erreur lors de la communication avec Jira : {e}"}


# ---------------------------------------------------------------------------
# Création de tickets (post-merge Cucumber)
# ---------------------------------------------------------------------------

_INLINE_MARKS = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)")


def _adf_inline(text: str) -> list:
    """Texte Markdown d'une ligne -> noeuds ADF avec gras, italique et code."""
    nodes = []
    for part in _INLINE_MARKS.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            nodes.append({"type": "text", "text": part[2:-2], "marks": [{"type": "strong"}]})
        elif part.startswith("`") and part.endswith("`"):
            nodes.append({"type": "text", "text": part[1:-1], "marks": [{"type": "code"}]})
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            nodes.append({"type": "text", "text": part[1:-1], "marks": [{"type": "em"}]})
        else:
            nodes.append({"type": "text", "text": part})
    return nodes or [{"type": "text", "text": " "}]


def _adf_paragraph(text: str) -> dict:
    return {"type": "paragraph", "content": _adf_inline(text)}


def _markdown_to_adf_rich(markdown: str) -> dict:
    """
    Markdown -> ADF avec tableaux, listes (y compris imbriquées et numérotées),
    blocs de code et marques inline.
    """
    lines, out, i = markdown.split("\n"), [], 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
        elif s.startswith("```"):
            lang = s[3:].strip()
            buf = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            node = {"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(buf) or " "}]}
            if lang:
                node["attrs"] = {"language": lang}
            out.append(node)
            i += 1
        elif s.startswith("#"):
            level = len(s) - len(s.lstrip("#"))
            out.append({"type": "heading", "attrs": {"level": min(level, 6)}, "content": _adf_inline(s.lstrip("#").strip())})
            i += 1
        elif s == "---":
            out.append({"type": "rule"})
            i += 1
        elif s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            body = [r for r in rows[1:] if not all(re.fullmatch(r":?-{2,}:?", c) for c in r)]
            table_rows = [{"type": "tableRow", "content": [
                {"type": "tableHeader", "content": [_adf_paragraph(c)]} for c in rows[0]]}]
            for r in body:
                table_rows.append({"type": "tableRow", "content": [
                    {"type": "tableCell", "content": [_adf_paragraph(c)]} for c in r]})
            out.append({"type": "table", "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
                        "content": table_rows})
        elif re.match(r"^(- |\* |\d+\. )", s):
            ordered = bool(re.match(r"^\d+\. ", s))
            items = []
            while i < len(lines):
                raw = lines[i]
                m = re.match(r"^(- |\* |\d+\. )(.*)", raw.strip())
                # Une puce indentée sans puce parente (après un paragraphe ou une ligne vide) ouvre la liste :
                # sinon aucune ligne n'est consommée et la conversion boucle sans fin
                if m and (not raw.startswith("  ") or not items):
                    items.append([_adf_paragraph(m.group(2))])
                elif m and items:
                    last = items[-1]
                    if last[-1]["type"] != "bulletList":
                        last.append({"type": "bulletList", "content": []})
                    last[-1]["content"].append({"type": "listItem", "content": [_adf_paragraph(m.group(2))]})
                else:
                    break
                i += 1
            out.append({"type": "orderedList" if ordered else "bulletList",
                        "content": [{"type": "listItem", "content": it} for it in items]})
        else:
            out.append(_adf_paragraph(s))
            i += 1
    return {"type": "doc", "version": 1, "content": out or [_adf_paragraph(markdown or " ")]}


def _jira_connection():
    domain = os.getenv("JIRA_DOMAIN", "").replace("https://", "").replace("http://", "").strip("/")
    email = os.getenv("JIRA_EMAIL", "").strip()
    token = os.getenv("JIRA_API_TOKEN", "").strip()
    if not all([domain, email, token]):
        raise RuntimeError("Identifiants Jira manquants dans le fichier .env")
    return f"https://{domain}/rest/api/3", (email, token)


def _resolve_team_id(project_key: str, team_name: str) -> str:
    """
    Les équipes Atlassian (champ Team, ex. 'oda_Prenom_N') ne sont pas des composants Jira :
    leur identifiant est retrouvé sur un ticket récent du projet qui porte déjà cette équipe.
    """
    api, auth = _jira_connection()
    team_field = os.getenv("JIRA_TEAM_FIELD", "customfield_10001")
    payload = {
        "jql": f'project = "{project_key}" AND "Team[Team]" IS NOT EMPTY ORDER BY updated DESC',
        "maxResults": 100,
        "fields": [team_field],
    }
    response = requests.post(f"{api}/search/jql", auth=auth, json=payload, timeout=15)
    if response.status_code == 200:
        for issue in response.json().get("issues", []):
            team = issue["fields"].get(team_field) or {}
            if team.get("name", "").lower() == team_name.lower():
                return team["id"]
    return ""


def _team_jql_clause(project_key: str, team_name: str) -> str:
    """Clause JQL filtrant sur `team_name`, composant Jira s'il existe dans le projet, sinon équipe Atlassian."""
    try:
        api, auth = _jira_connection()
        response = requests.get(f"{api}/project/{project_key}/components", auth=auth, timeout=15)
        project_components = [c["name"] for c in response.json()] if response.status_code == 200 else []
        if team_name in project_components:
            return f'component = "{team_name}"'
        team_id = _resolve_team_id(project_key, team_name)
        # Sans authentification valide, Jira Cloud répond « projet introuvable » (404) ou une liste vide
        me = None if team_id else requests.get(f"{api}/myself", auth=auth, timeout=10)
    except requests.RequestException as e:
        raise JiraSearchError(f"Jira injoignable : {e}") from e
    if not team_id:
        if me.status_code != 200:
            raise JiraSearchError(
                f"Authentification Jira refusée ({me.status_code}) pour {auth[0]} : "
                "le token API (JIRA_API_TOKEN) est probablement expiré ou révoqué."
            )
        raise JiraSearchError(f"'{team_name}' n'est ni un composant ni une équipe connue du projet {project_key}.")
    return f'"Team[Team]" = "{team_id}"'


def build_jira_issue_fields(project_key: str, summary: str, description: str, component: str,
                            labels: list, issue_type: str = "", parent_key: str = "") -> dict:
    """
    Construit le bloc 'fields' de création. `component` est envoyé comme composant Jira s'il existe
    dans le projet, sinon comme équipe Atlassian (champ Team). `parent_key` rattache le ticket à une épopée.
    """
    api, auth = _jira_connection()
    fields = {
        "project": {"key": project_key},
        "summary": summary[:255],
        "issuetype": {"name": issue_type or os.getenv("JIRA_POSTMERGE_ISSUE_TYPE", "Development")},
        "description": _markdown_to_adf_rich(description),
        "labels": [l.strip().replace(" ", "_") for l in labels or [] if l and l.strip()],
    }
    if parent_key:
        fields["parent"] = {"key": parent_key}
    if component:
        response = requests.get(f"{api}/project/{project_key}/components", auth=auth, timeout=15)
        project_components = [c["name"] for c in response.json()] if response.status_code == 200 else []
        if component in project_components:
            fields["components"] = [{"name": component}]
        else:
            team_id = _resolve_team_id(project_key, component)
            if not team_id:
                raise ValueError(f"'{component}' n'est ni un composant ni une équipe connue du projet {project_key}.")
            fields[os.getenv("JIRA_TEAM_FIELD", "customfield_10001")] = team_id
    return fields


def find_jira_issue_by_summary(project_key: str, summary: str) -> str:
    """Renvoie la clé d'un ticket du projet ayant exactement ce résumé (évite les doublons), sinon ''."""
    api, auth = _jira_connection()
    escaped = summary.replace("\\", "\\\\").replace('"', '\\"')
    payload = {
        "jql": f'project = "{project_key}" AND summary ~ "\\"{escaped}\\"" ORDER BY created DESC',
        "maxResults": 20,
        "fields": ["summary"],
    }
    response = requests.post(f"{api}/search/jql", auth=auth, json=payload, timeout=15)
    if response.status_code == 200:
        for issue in response.json().get("issues", []):
            if issue["fields"].get("summary", "").strip() == summary.strip():
                return issue["key"]
    return ""


def create_jira_issue(project_key: str, summary: str, description: str, component: str, labels: list,
                      issue_type: str = "", parent_key: str = "") -> str:
    """
    Crée un ticket via l'API REST v3 et renvoie sa clé (ex: SPEC-2045).
    La description est en Markdown (convertie en ADF). Lève RuntimeError en cas d'échec.
    """
    api, auth = _jira_connection()
    fields = build_jira_issue_fields(project_key, summary, description, component, labels, issue_type, parent_key)
    response = requests.post(f"{api}/issue", auth=auth, json={"fields": fields},
                             headers={"Accept": "application/json"}, timeout=30)
    if response.status_code != 201:
        raise RuntimeError(f"Création du ticket refusée par Jira ({response.status_code}) : {response.text[:1000]}")
    return response.json()["key"]


def set_jira_description_markdown(issue_key: str, markdown: str) -> None:
    """Remplace la description d'un ticket par ce Markdown (converti en ADF). Lève RuntimeError en cas d'échec."""
    api, auth = _jira_connection()
    response = requests.put(f"{api}/issue/{issue_key}", auth=auth, timeout=30,
                            json={"fields": {"description": _markdown_to_adf_rich(markdown)}})
    if response.status_code not in (200, 204):
        raise RuntimeError(f"Mise à jour de {issue_key} refusée par Jira ({response.status_code}) : {response.text[:500]}")


EPIC_TASK_TYPES = ("Tâche", "Task", "Development", "Story")


def resolve_epic_task_type(project_key: str, wanted: str = "") -> str:
    """
    Type des tâches créées sous une épopée : `wanted` s'il existe dans le projet, sinon le premier type
    disponible parmi EPIC_TASK_TYPES (OXY : « Tâche », SPEC : « Development »). '' si l'API ne répond pas.
    """
    try:
        api, auth = _jira_connection()
        response = requests.get(f"{api}/issue/createmeta/{project_key}/issuetypes", auth=auth, timeout=15)
        data = response.json() if response.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        return wanted
    available = {t["name"].lower(): t["name"] for t in data.get("issueTypes") or data.get("values") or []
                 if t.get("hierarchyLevel", 0) == 0}
    for name in ([wanted] if wanted else []) + list(EPIC_TASK_TYPES):
        if name.lower() in available:
            return available[name.lower()]
    return wanted


def get_epic_children(epic_key: str) -> list:
    """Tickets déjà rattachés à l'épopée : [{key, summary, status}] (liste vide en cas d'erreur)."""
    try:
        api, auth = _jira_connection()
        payload = {"jql": f'parent = "{epic_key}" ORDER BY key ASC', "maxResults": 100, "fields": ["summary", "status"]}
        response = requests.post(f"{api}/search/jql", auth=auth, json=payload, timeout=15)
        if response.status_code != 200:
            return []
        return [{"key": i["key"], "summary": i["fields"].get("summary", ""),
                 "status": (i["fields"].get("status") or {}).get("name", "")}
                for i in response.json().get("issues", [])]
    except Exception:
        return []


def get_jira_attachment_names(issue_key: str) -> set:
    """Noms des pièces jointes déjà présentes sur un ticket (pour ne pas les attacher deux fois)."""
    api, auth = _jira_connection()
    response = requests.get(f"{api}/issue/{issue_key}?fields=attachment", auth=auth, timeout=15)
    if response.status_code != 200:
        return set()
    return {a["filename"] for a in response.json()["fields"].get("attachment", [])}


# ---------------------------------------------------------------------------
# Release note (ajoutée en fin de description après le patch)
# ---------------------------------------------------------------------------

RELEASE_NOTE_TITLE = "Release note"


def _adf_plain_text(node) -> str:
    """Texte d'un noeud ADF sur une seule ligne (pour comparer des titres)."""
    if isinstance(node, list):
        return "".join(_adf_plain_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return _adf_plain_text(node.get("content", []))


def _is_release_note_heading(node: dict) -> bool:
    if not isinstance(node, dict) or node.get("type") != "heading":
        return False
    title = re.sub(r"[^a-z ]", "", _adf_plain_text(node).lower()).strip()
    return bool(re.fullmatch(r"release\s*notes?", title))


def upsert_release_note_adf(adf_doc, paragraph: str, title: str = RELEASE_NOTE_TITLE, level: int = 2) -> dict:
    """
    Renvoie une copie du document ADF où la section « Release note » est placée en FIN de description.
    Le reste du document est conservé tel quel ; une section existante (son titre jusqu'au titre
    suivant de niveau égal ou supérieur) est remplacée, jamais dupliquée.
    """
    import copy
    if isinstance(adf_doc, dict) and adf_doc.get("type") == "doc":
        doc = copy.deepcopy(adf_doc)
    else:
        doc = {"type": "doc", "version": 1, "content": []}
    kept, skip_level = [], None
    for node in doc.get("content") or []:
        if isinstance(node, dict) and node.get("type") == "heading":
            node_level = (node.get("attrs") or {}).get("level", 1)
            if skip_level is not None and node_level <= skip_level:
                skip_level = None
            if skip_level is None and _is_release_note_heading(node):
                skip_level = node_level
                continue
        if skip_level is None:
            kept.append(node)
    kept.append({"type": "heading", "attrs": {"level": level}, "content": [{"type": "text", "text": title}]})
    kept.append(_adf_paragraph(paragraph.strip()))
    doc["content"] = kept
    return doc


def update_jira_release_note(issue_key: str, paragraph: str) -> None:
    """
    Ajoute (ou remplace) la section « Release note » à la fin de la description du ticket.
    Le document ADF renvoyé par Jira est modifié directement (pas de reconversion Markdown) :
    la description d'origine et le cadrage restent intacts. Lève RuntimeError en cas d'échec.
    """
    if not paragraph or not paragraph.strip():
        raise ValueError("Release note vide.")
    api, auth = _jira_connection()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    response = requests.get(f"{api}/issue/{issue_key}?fields=description", auth=auth, headers=headers, timeout=15)
    if response.status_code != 200:
        raise RuntimeError(f"Lecture de {issue_key} refusée par Jira ({response.status_code}) : {response.text[:300]}")
    description = response.json().get("fields", {}).get("description")
    payload = {"fields": {"description": upsert_release_note_adf(description, paragraph)}}
    response = requests.put(f"{api}/issue/{issue_key}", auth=auth, headers=headers, json=payload, timeout=30)
    if response.status_code not in (200, 204):
        raise RuntimeError(f"Mise à jour de {issue_key} refusée par Jira ({response.status_code}) : {response.text[:300]}")


# ---------------------------------------------------------------------------
# Sections de description gérées par l'agent (ex. « Intégration du besoin »)
# ---------------------------------------------------------------------------

def _normalize_title(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w ]", "", text.lower())).strip()


def _heading_level(node) -> int:
    if isinstance(node, dict) and node.get("type") == "heading":
        return (node.get("attrs") or {}).get("level", 1)
    return 0


def _split_adf_sections(adf_doc, titles) -> tuple:
    """
    Renvoie (copie du document, noeuds conservés, index du premier retrait ou None) : chaque section dont
    le titre figure dans `titles` (son titre jusqu'au titre suivant de niveau égal ou supérieur) est retirée.
    """
    import copy
    if isinstance(adf_doc, dict) and adf_doc.get("type") == "doc":
        doc = copy.deepcopy(adf_doc)
    else:
        doc = {"type": "doc", "version": 1, "content": []}
    wanted = {_normalize_title(t) for t in titles}
    kept, removed_at, skip_level = [], None, None
    for node in doc.get("content") or []:
        level = _heading_level(node)
        if level:
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if skip_level is None and _normalize_title(_adf_plain_text(node)) in wanted:
                skip_level = level
                removed_at = len(kept) if removed_at is None else removed_at
                continue
        if skip_level is None:
            kept.append(node)
    return doc, kept, removed_at


def remove_adf_sections(adf_doc, titles):
    """Copie du document ADF sans les sections `titles`. Renvoie l'entrée telle quelle si rien à retirer."""
    if not titles or not (isinstance(adf_doc, dict) and adf_doc.get("type") == "doc"):
        return adf_doc
    doc, kept, _ = _split_adf_sections(adf_doc, titles)
    doc["content"] = kept
    return doc


def upsert_adf_section(adf_doc, title: str, markdown: str, level: int = 2) -> dict:
    """
    Copie du document ADF où la section `title` contient `markdown`. Une section existante est remplacée
    à sa place ; sinon elle est insérée avant la release note, ou en fin de description.
    Le reste du document est conservé tel quel.
    """
    doc, kept, insert_at = _split_adf_sections(adf_doc, [title])
    if insert_at is None:
        insert_at = next((i for i, n in enumerate(kept) if _is_release_note_heading(n)), len(kept))
    section = [{"type": "heading", "attrs": {"level": level}, "content": [{"type": "text", "text": title}]}]
    section += _markdown_to_adf_rich(markdown)["content"] if markdown.strip() else []
    doc["content"] = kept[:insert_at] + section + kept[insert_at:]
    return doc


def update_jira_section(issue_key: str, title: str, markdown: str) -> None:
    """
    Ajoute ou remplace la section `title` de la description du ticket (document ADF modifié directement :
    le reste de la description reste intact). Lève RuntimeError en cas d'échec.
    """
    api, auth = _jira_connection()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    response = requests.get(f"{api}/issue/{issue_key}?fields=description", auth=auth, headers=headers, timeout=15)
    if response.status_code != 200:
        raise RuntimeError(f"Lecture de {issue_key} refusée par Jira ({response.status_code}) : {response.text[:300]}")
    description = response.json().get("fields", {}).get("description")
    payload = {"fields": {"description": upsert_adf_section(description, title, markdown)}}
    response = requests.put(f"{api}/issue/{issue_key}", auth=auth, headers=headers, json=payload, timeout=30)
    if response.status_code not in (200, 204):
        raise RuntimeError(f"Mise à jour de {issue_key} refusée par Jira ({response.status_code}) : {response.text[:300]}")
