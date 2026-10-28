"""
Pilotage réel du job Jenkins qui exécute le dépôt <client>-cucumber (0 token) : rôles de ses paramètres, paramètres
d'un build, version testée.

RÔLES (ROLES) : le job ne reçoit pas de patch, il lit une branche du dépôt client. Les noms de ses paramètres sont
propres au job : la correspondance « paramètre → rôle » (branche, dépôt, arguments, attente, commit, environnement,
artefact monté) est détectée sur les paramètres du job (job_definitions), enregistrée dans le profil client
(« job_roles ») et corrigeable dans l'IHM ; aucun nom de paramètre n'est écrit en dur.
PARAMÈTRES D'UN BUILD (build_parameters) : ceux de la configuration actuelle du job, puis branche = branche de
travail de l'agent, dépôt = dépôt client, attente décochée, arguments = valeur actuelle + filtre de tags Cucumber
(« -Dcucumber.filter.tags='<expression>' » : un paramètre de tags du job n'atteint pas Cucumber).
SECRETS : les arguments contiennent des identifiants. Lus depuis la configuration du job, ils sont enregistrés
auprès du masquage commun (secret_tools.register_secrets) et ne sont jamais affichés, journalisés, stockés ni envoyés
à Claude : visible_parameters les masque dans tout ce qui est montré ou historisé.
VERSION TESTÉE (tested_artifact, publication) : l'artefact monté à chaque run (paramètres du rôle « composant_* »),
sa date de publication lue dans le dépôt Maven (maven-metadata.xml), comparée aux derniers commits des tâches
(deployment) : source de vérité du « code déployé ».
"""
import os
import re
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from tools import jenkins_tools, secret_tools
from tools.cucumber_tools import _git, _read_json, _write_json, cache_dir, git_repos

ROLES = {"branche": "branche du dépôt client lue par le job (branche de travail de l'agent)",
         "depot": "dépôt client lu par le job",
         "arguments": "arguments Maven du job (reçoivent le filtre de tags Cucumber ; contiennent des identifiants)",
         "attente": "attente de la fin du déploiement (décochée pour les runs de l'agent)",
         "commit": "commit attendu en tête de la branche",
         "environnement": "environnement d'exécution",
         "composant_proprietaire": "propriétaire (groupe Maven) de l'artefact monté",
         "composant_nom": "nom de l'artefact monté",
         "composant_version": "version de l'artefact monté"}
# Rôles sans lesquels l'agent ne peut pas piloter le job
REQUIRED_ROLES = ("branche", "arguments")
# Détection : (motif du nom, motif exclu) ; plusieurs candidats pour un rôle = ambiguïté, rôle laissé vide
_DETECTION = {
    "branche": (r"BRANCH", r"STANDARD|STD"),
    "depot": (r"REPO", r"STANDARD|STD"),
    "arguments": (r"ARGLINE|ARGS\b|ARGUMENTS?|_OPTS\b|MAVEN_OPTS", r""),
    "attente": (r"^WAIT$|^WAIT_|_WAIT$|ATTENTE", r""),
    "commit": (r"COMMIT", r"STANDARD|STD"),
    "environnement": (r"^ENV$|ENVIRONMENT|ENVIRONNEMENT", r""),
    "composant_proprietaire": (r"(COMPONENT|ARTIFACT).*(OWNER|GROUP)", r""),
    "composant_nom": (r"(COMPONENT|ARTIFACT).*NAME|ARTIFACT_?ID", r""),
    "composant_version": (r"(COMPONENT|ARTIFACT).*VERSION", r""),
}
TAG_FILTER = "cucumber.filter.tags"
_TAG_OPTION = re.compile(rf"\s*-D{re.escape(TAG_FILTER)}=(?:'[^']*'|\"[^\"]*\"|\S+)")
# Options des arguments dont la valeur est un identifiant (masquée partout, en plus des motifs communs)
_CREDENTIAL_KEY = re.compile(r"user|login|pass|pwd|token|secret|key|auth|cred|account|compte|identifiant", re.I)
_OPTION = re.compile(r"-D([\w.-]+)=('[^']*'|\"[^\"]*\"|\S+)")
_UNREADABLE_TYPES = ("Password", "File", "Credentials")


def empty_roles() -> dict:
    return {role: "" for role in ROLES}


def detect_roles(definitions: dict) -> tuple:
    """
    Rôles détectés sur les paramètres du job ({nom: {"type", "defaut"}} ou {nom: type}) : (rôles, avertissements).
    Un rôle sans candidat ou avec plusieurs candidats reste vide (avertissement si le rôle est requis ou ambigu).
    """
    roles, warnings = empty_roles(), []
    names = list(definitions or {})
    for role, (pattern, excluded) in _DETECTION.items():
        found = [n for n in names if re.search(pattern, n, re.I) and not (excluded and re.search(excluded, n, re.I))]
        if role == "attente" and len(found) > 1:
            found = [n for n in found if "Boolean" in _type(definitions[n])] or found
        if len(found) == 1:
            roles[role] = found[0]
        elif found:
            warnings.append(f"rôle « {role} » ambigu ({', '.join(found)}) : à choisir dans le profil client")
        elif role in REQUIRED_ROLES:
            warnings.append(f"rôle « {role} » introuvable parmi les paramètres du job : à renseigner dans le profil")
    return roles, warnings


def _type(definition) -> str:
    return definition.get("type", "") if isinstance(definition, dict) else str(definition or "")


def effective_roles(profile: dict, definitions: dict) -> tuple:
    """
    Rôles appliqués : profil (« job_roles », puis « verify_branch_param » pour la branche) > détection. Un rôle du
    profil absent des paramètres du job est signalé et ignoré. Renvoie (rôles, avertissements).
    """
    detected, warnings = detect_roles(definitions)
    chosen = {**{"branche": profile.get("verify_branch_param") or ""}, **(profile.get("job_roles") or {})}
    roles, absent = dict(detected), []
    for role, name in chosen.items():
        if role not in ROLES or not name:
            continue
        if definitions and name not in definitions:
            absent.append(f"paramètre {name} (rôle « {role} » du profil) absent du job : détection retenue")
            continue
        roles[role] = name
        # Rôle choisi dans le profil : l'avertissement de détection ne s'applique plus
        warnings = [w for w in warnings if f"« {role} »" not in w]
    return roles, warnings + absent


def register_job_secrets(definitions: dict, roles: dict) -> None:
    """Identifiants des arguments du job (valeurs des options -D… d'identifiant, et la valeur entière) masqués."""
    argline = str((definitions.get(roles.get("arguments") or "") or {}).get("defaut") or "")
    values = [argline] + [v.strip("'\"") for k, v in _OPTION.findall(argline) if _CREDENTIAL_KEY.search(k)]
    secret_tools.register_secrets(values)


def with_tag_filter(argline: str, tags: str) -> str:
    """Arguments du job avec le filtre de tags Cucumber (un filtre déjà présent est remplacé ; vide : retiré)."""
    base = _TAG_OPTION.sub("", argline or "").strip()
    tags = (tags or "").strip().replace("'", "")
    return f"{base} -D{TAG_FILTER}='{tags}'".strip() if tags else base


def current_values(definitions: dict) -> dict:
    """Valeurs de la configuration actuelle du job (paramètres lisibles : ni mot de passe, ni fichier)."""
    out = {}
    for name, definition in (definitions or {}).items():
        value = definition.get("defaut") if isinstance(definition, dict) else None
        if value is None or any(t in _type(definition) for t in _UNREADABLE_TYPES):
            continue
        out[name] = ("true" if value else "false") if isinstance(value, bool) else str(value)
    return out


def fixed_parameters(profile: dict, definitions: dict = None) -> tuple:
    """
    Paramètres imposés par le profil client (« job_params » : lignes NOM=valeur) : ({nom: valeur}, avertissements).
    La configuration actuelle du job est celle du dernier client lancé (artefact monté, suites Standard cochées) :
    ces valeurs la remplacent pour chaque run de l'agent. Un nom absent du job est signalé et ignoré.
    """
    fixed, warnings = {}, []
    for line in (profile or {}).get("job_params") or []:
        name, sep, value = str(line).partition("=")
        name = name.strip()
        if not sep or not name:
            warnings.append(f"paramètre imposé illisible (attendu NOM=valeur) : {line}")
        elif definitions and name not in definitions:
            warnings.append(f"paramètre imposé {name} absent du job : ignoré")
        else:
            fixed[name] = value.strip()
    return fixed, warnings


def build_parameters(definitions: dict, roles: dict, branch: str, repository: str = "", tags: str = "",
                     commit: str = "", env: str = "", fixed: dict = None) -> dict:
    """
    Paramètres d'un build : configuration actuelle du job, remplacée par les paramètres imposés du profil (`fixed`),
    puis branche de travail, dépôt client, attente décochée, commit et environnement (si le job a ces rôles) et
    filtre de tags ajouté aux arguments.
    """
    params = {**current_values(definitions), **(fixed or {})}
    for role, value in (("branche", branch), ("depot", repository), ("commit", commit), ("environnement", env)):
        if roles.get(role) and value:
            params[roles[role]] = value
    if roles.get("attente"):
        params[roles["attente"]] = "false"
    if roles.get("arguments"):
        params[roles["arguments"]] = with_tag_filter(params.get(roles["arguments"], ""), tags)
    return params


def visible_parameters(params: dict, roles: dict) -> dict:
    """Paramètres montrables (journal, historique, IHM) : arguments réduits au filtre de tags, secrets masqués."""
    shown = {}
    for name, value in params.items():
        if name == roles.get("arguments"):
            tag = re.search(rf"-D{re.escape(TAG_FILTER)}=('[^']*'|\S+)", value or "")
            value = f"{secret_tools.MASK}" + (f" -D{TAG_FILTER}={tag.group(1)}" if tag else "")
        shown[name] = secret_tools.mask_secrets(str(value))[0]
    return shown


def tested_artifact(params: dict, roles: dict) -> dict:
    """Artefact monté par le build : {"proprietaire", "nom", "version"} ({} si le job n'a pas ces rôles)."""
    values = {key: params.get(roles.get(f"composant_{key}") or "", "")
              for key in ("proprietaire", "nom", "version")}
    return values if values["nom"] else {}


# ---------------------------------------------------------------------------
# Date de publication de l'artefact testé (dépôt Maven)
# ---------------------------------------------------------------------------

def maven_repository() -> tuple:
    """(URL du dépôt Maven, authentification), lus à chaque appel ; URL vide si non configurée."""
    password = os.getenv("MAVEN_REPOSITORY_PASSWORD", "")
    auth = (os.getenv("MAVEN_REPOSITORY_USER", ""), password) if password else None
    return os.getenv("MAVEN_REPOSITORY_URL", "").strip().rstrip("/"), auth


def _metadata(url: str, auth) -> ElementTree.Element:
    try:
        response = requests.get(url, auth=auth, timeout=20)
        return ElementTree.fromstring(response.content) if response.status_code == 200 else None
    except (requests.RequestException, ElementTree.ParseError):
        return None


def _stamp(value: str) -> str:
    """lastUpdated Maven (AAAAMMJJHHMMSS, UTC) -> ISO ; '' si illisible."""
    try:
        return datetime.strptime((value or "").strip()[:14], "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return ""


def publication(artifact: dict) -> dict:
    """
    Date de publication de l'artefact dans le dépôt Maven : maven-metadata.xml de la version (instantané : dernier
    déploiement), à défaut date du POM de la version, à défaut lastUpdated de l'artefact si la version demandée est
    sa dernière. {"date" (ISO UTC), "version", "source"} ou {"erreur"}.
    """
    repository, auth = maven_repository()
    if not artifact:
        return {"erreur": "artefact monté inconnu : rôles « composant_* » du job non renseignés"}
    if not repository:
        return {"erreur": "MAVEN_REPOSITORY_URL non configuré (onglet Configuration)"}
    group = "/".join(p for p in artifact.get("proprietaire", "").split(".") if p)
    base = f"{repository}/{group + '/' if group else ''}{artifact['nom']}"
    version = artifact.get("version", "").strip()
    root = _metadata(f"{base}/maven-metadata.xml", auth)
    if not version or version.upper() in ("LATEST", "RELEASE"):
        version = (root.findtext("versioning/release") if root is not None and version.upper() == "RELEASE" else
                   root.findtext("versioning/latest") if root is not None else "") or ""
        if not version:
            return {"erreur": f"maven-metadata.xml illisible : {base}/maven-metadata.xml"}
    versioned = _metadata(f"{base}/{version}/maven-metadata.xml", auth)
    date = _stamp(versioned.findtext("versioning/lastUpdated") or "") if versioned is not None else ""
    if date:
        return {"date": date, "version": version, "source": f"{base}/{version}/maven-metadata.xml"}
    pom = f"{base}/{version}/{artifact['nom']}-{version}.pom"
    try:
        response = requests.head(pom, auth=auth, timeout=20, allow_redirects=True)
        modified = response.headers.get("Last-Modified", "") if response.status_code == 200 else ""
        if modified:
            return {"date": parsedate_to_datetime(modified).astimezone(timezone.utc).isoformat(timespec="seconds"),
                    "version": version, "source": pom}
    except (requests.RequestException, TypeError, ValueError):
        pass
    if root is not None and root.findtext("versioning/latest") == version:
        date = _stamp(root.findtext("versioning/lastUpdated") or "")
        if date:
            return {"date": date, "version": version, "source": f"{base}/maven-metadata.xml"}
    return {"erreur": f"date de publication de {artifact['nom']} {version} introuvable dans le dépôt Maven"}


# ---------------------------------------------------------------------------
# Code déployé : publication de l'artefact comparée aux commits des tâches
# ---------------------------------------------------------------------------

def deployment_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "deploiement.json")


def load_deployment(client_name: str) -> dict:
    """Dernière version testée : {"artefact", "publication", "date", "taches": {id: {...}}} ; {} sans run."""
    return _read_json(deployment_path(client_name), {}) or {}


def last_commit(repo_dir: str, paths: list) -> dict:
    """Dernier commit (HEAD) qui modifie `paths` : {"sha", "date" ISO, "sujet"} ; {} sans commit."""
    out = _git(repo_dir, "log", "-1", "--format=%h%x1f%cI%x1f%s", "HEAD", "--", *paths).stdout.strip()
    sha, _, rest = out.partition("\x1f")
    date, _, subject = rest.partition("\x1f")
    return {"sha": sha, "date": date, "sujet": subject[:120]} if sha else {}


def _utc(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return stamp.astimezone(timezone.utc) if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def deployment(inventory: dict, client_root: str, artifact: dict, published: dict) -> dict:
    """
    Code de chaque tâche dans l'artefact testé : {id: {"etat": deploye | non_deploye | inconnu, "commit", "preuve"}}.
    Dernier commit des fichiers de la tâche antérieur à la publication : déployé ; postérieur : non déployé (le
    run a testé une version qui ne le contient pas) ; sans date ou sans commit : inconnu.
    """
    repos, out = git_repos(client_root), {}
    label = f"{artifact.get('nom', '?')} {published.get('version') or artifact.get('version', '')}".strip()
    for spec in inventory.get("specifiques") or []:
        by_repo = {}
        for item in spec.get("fichiers") or []:
            if item.get("repo") in repos and item.get("path"):
                by_repo.setdefault(item["repo"], []).append(item["path"])
        commits = [dict(c, repo=r) for r, paths in by_repo.items() for c in [last_commit(repos[r], paths)] if c]
        latest = max(commits, key=lambda c: _utc(c["date"]), default={})
        if not latest or not published.get("date"):
            out[spec["id"]] = {"etat": "inconnu", "commit": latest, "preuve": (
                published.get("erreur") or "date de publication inconnue") if latest else
                "aucun commit trouvé pour les fichiers de la tâche"}
            continue
        after = _utc(latest["date"]) > _utc(published["date"])
        out[spec["id"]] = {"etat": "non_deploye" if after else "deploye", "commit": latest, "preuve": (
            f"commit {latest['sha']} ({latest['repo']}, {latest['date'][:16].replace('T', ' ')}) "
            f"{'postérieur' if after else 'antérieur'} à la publication de {label} "
            f"({published['date'][:16].replace('T', ' ')} UTC)")}
    return out


def record_deployment(client_name: str, artifact: dict, published: dict, tasks: dict) -> dict:
    """Version testée du dernier run, affichée dans l'IHM (deploiement.json)."""
    document = {"artefact": artifact, "publication": published, "taches": tasks,
                "date": datetime.now().isoformat(timespec="seconds")}
    _write_json(deployment_path(client_name), document)
    return document


def deployment_line(document: dict) -> str:
    """« Version testée : <nom> <version> publiée le JJ/MM/AAAA HH:MM UTC (n tâche(s) non déployée(s)) »."""
    artifact, published = document.get("artefact") or {}, document.get("publication") or {}
    if not artifact:
        return ""
    name = f"{artifact.get('proprietaire', '')}:{artifact['nom']}".lstrip(":")
    version = published.get("version") or artifact.get("version") or "?"
    when = published.get("date", "")
    late = sum(1 for t in (document.get("taches") or {}).values() if t["etat"] == "non_deploye")
    return (f"Version testée : {name} {version} — " + (
        f"publiée le {when[8:10]}/{when[5:7]}/{when[:4]} {when[11:16]} UTC" if when else
        f"date de publication inconnue ({published.get('erreur', '?')})")
            + (f" ; {late} tâche(s) dont le dernier commit est postérieur (code non déployé)" if late else ""))


# ---------------------------------------------------------------------------
# Lancement d'un build sur la branche de travail
# ---------------------------------------------------------------------------

def prepare(job: str, profile: dict) -> dict:
    """
    Définitions et rôles du job : {"job", "definitions", "roles", "erreurs", "avertissements"}. Rôles requis
    manquants : erreur. Les identifiants des arguments sont enregistrés auprès du masquage commun.
    """
    out = {"job": job, "definitions": {}, "roles": empty_roles(), "erreurs": [], "avertissements": []}
    out["definitions"] = jenkins_tools.job_definitions(job) if job else {}
    if not out["definitions"]:
        out["erreurs"].append(f"job {job or '?'} introuvable ou sans paramètres (Jenkins injoignable ?)")
        return out
    out["roles"], out["avertissements"] = effective_roles(profile, out["definitions"])
    register_job_secrets(out["definitions"], out["roles"])
    for role in REQUIRED_ROLES:
        if not out["roles"][role]:
            out["erreurs"].append(f"paramètre du job {job} pour le rôle « {role} » ({ROLES[role]}) non trouvé : "
                                  f"à choisir dans le profil client (correspondance paramètre → rôle)")
    return out
