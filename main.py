import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from string import Template
from urllib.parse import urlsplit
import requests
from dotenv import load_dotenv

from tools import cucumber_tools, db_tools, integration_tools
from tools.jira_cli import attach_files, create_or_get_issue
from tools.epic_tools import epic_manifest_path, load_epic_manifest, sync_epic_tasks
from tools.jira_tools import (
    fetch_application_tickets, find_jira_issue_by_summary, get_epic_children, get_full_jira_issue_context,
    get_issue_field_values, update_jira_cadrage, update_jira_release_note,
)
from tools.memory_tools import (
    get_capitalized_experience,
    save_cadrage_memory,
    save_refine_feedback,
    save_patch_memory
)

# Force l'encodage UTF-8 sous Windows
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

load_dotenv()


def _safe_memory(action, *args, **kwargs) -> bool:
    """La capitalisation ne doit jamais faire échouer une étape réussie."""
    try:
        action(*args, **kwargs)
        return True
    except Exception as e:
        print(f"[MÉMOIRE] ⚠️ Capitalisation impossible ({e})")
        return False


def _read_text(path: str) -> str:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    return ""


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    try:
        return max(int(value), 0) if value else default
    except ValueError:
        print(f"[CONFIG] ⚠️ {name}='{value}' n'est pas un nombre : valeur par défaut {default} utilisée.")
        return default


# ---------------------------------------------------------------------------
# Économie de tokens
# ---------------------------------------------------------------------------

TOKEN_USAGE_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp", "token_usage.csv")
TOKEN_USAGE_FIELDS = ["date", "ticket", "etape", "modele", "tokens_entree", "cache_ecrit", "cache_lu",
                      "tokens_sortie", "tours", "duree_s", "cout_usd", "taille_prompt", "code_retour"]

# Variable .env du modèle Claude par étape (vide = modèle par défaut de Claude Code)
CLAUDE_MODEL_ENV = {
    "cadrage": "CLAUDE_MODEL_CADRAGE",
    "epic-cadrage": "CLAUDE_MODEL_CADRAGE",
    "refine": "CLAUDE_MODEL_REFINE",
    "patch": "CLAUDE_MODEL_PATCH",
    "generate-cucumber-postmerge": "CLAUDE_MODEL_POSTMERGE",
    "besoin": "CLAUDE_MODEL_BESOIN",
    "cadrage-reponses": "CLAUDE_MODEL_REFINE",
    "generate-cucumber-client": "CLAUDE_MODEL_CUCUMBER",
}

# ---------------------------------------------------------------------------
# Modèles de prompts (prompts/*.md, personnalisables via PROMPTS_DIR)
# ---------------------------------------------------------------------------

DEFAULT_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


def render_prompt(name: str, **values) -> str:
    """
    Modèle '<name>.md' complété par ses variables ${nom} (string.Template, '$$' pour un '$' littéral).
    Cherché d'abord dans PROMPTS_DIR (prompts personnalisés), sinon dans le dossier prompts/ du projet.
    """
    custom_dir = os.getenv("PROMPTS_DIR", "").strip()
    path = ""
    for directory in ([custom_dir] if custom_dir else []) + [DEFAULT_PROMPTS_DIR]:
        candidate = os.path.join(directory, f"{name}.md")
        if os.path.exists(candidate):
            path = candidate
            break
    if not path:
        sys.exit(f"[PROMPTS] ❌ Modèle de prompt '{name}.md' introuvable (PROMPTS_DIR='{custom_dir}', {DEFAULT_PROMPTS_DIR}).")
    try:
        return Template(_read_text(path)).substitute(values)
    except (KeyError, ValueError) as e:
        sys.exit(f"[PROMPTS] ❌ Modèle '{path}' invalide : variable inconnue ou '$' isolé ({e}). "
                 f"Variables disponibles : {', '.join(sorted(values)) or 'aucune'}.")


def format_jira_context(jira_context: dict, max_chars: int) -> str:
    """
    Contexte Jira déjà récupéré par main.py (description, Épopée, COST), injecté dans le prompt
    de cadrage. La taille est plafonnée et chaque troncature est signalée à l'agent.
    """
    if not jira_context or max_chars <= 0:
        return ""

    def clip(label: str, text: str, budget: int) -> str:
        text = (text or "").strip()
        if len(text) <= budget:
            return text
        return text[:budget].rstrip() + f"\n[... {label} TRONQUÉ(E) : {len(text) - budget} caractères non affichés ...]"

    epic = jira_context.get("epic") or {}
    costs = jira_context.get("costs") or []
    desc_budget, epic_budget = max_chars // 2, max_chars // 5
    cost_budget = max(max_chars - desc_budget - epic_budget, 0) // max(len(costs), 1)

    parts = [
        f"=== CONTEXTE JIRA DU TICKET {jira_context.get('issue_key', '')} (déjà récupéré) ===",
        f"Titre : {jira_context.get('summary', '')}",
        f"Composants : {', '.join(jira_context.get('components', [])) or 'aucun'}",
        "## Description",
        clip("Description", jira_context.get("description", ""), desc_budget) or "(vide)",
    ]
    if epic:
        parts += [f"## Épopée {epic.get('key', '')} — {epic.get('summary', '')}",
                  clip("Épopée", epic.get("description", ""), epic_budget) or "(description vide)"]
    else:
        parts.append("## Épopée : aucune trouvée par l'API")
    if costs:
        for cost in costs:
            parts += [f"## COST {cost.get('key', '')} — {cost.get('summary', '')}",
                      clip(f"COST {cost.get('key', '')}", cost.get("description", ""), cost_budget) or "(description vide)"]
    else:
        parts.append("## COST : aucun ticket lié trouvé par l'API")
    parts.append("=== FIN CONTEXTE JIRA ===")
    return "\n".join(parts)


def parse_claude_output(raw: str) -> tuple:
    """
    Sortie de `claude --print --output-format json` -> (texte de la réponse, usage).
    Repli si la sortie n'est pas du JSON : (sortie brute, None).
    """
    raw = raw or ""
    candidates = [raw.strip()] + [l.strip() for l in reversed(raw.splitlines()) if l.strip().startswith(("{", "["))]
    data = None
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(obj, list):
            obj = next((m for m in reversed(obj) if isinstance(m, dict) and m.get("type") == "result"), None)
        if isinstance(obj, dict) and (obj.get("type") == "result" or "result" in obj):
            data = obj
            break
    if data is None:
        return raw, None

    text = data.get("result")
    if not isinstance(text, str):
        text = f"[Claude Code] Fin sans réponse texte ({data.get('subtype', 'inconnu')})."
    u = data.get("usage") or {}
    usage = {
        "tokens_entree": u.get("input_tokens", 0) or 0,
        "cache_ecrit": u.get("cache_creation_input_tokens", 0) or 0,
        "cache_lu": u.get("cache_read_input_tokens", 0) or 0,
        "tokens_sortie": u.get("output_tokens", 0) or 0,
        "tours": data.get("num_turns", ""),
        "duree_s": round((data.get("duration_ms") or 0) / 1000, 1),
        "cout_usd": data.get("total_cost_usd", ""),
        "modeles": ",".join((data.get("modelUsage") or {}).keys()),
    }
    return text, usage


def run_claude(prompt: str, model: str, add_dirs: list, allowed_tools: list, extra_args: tuple = (),
               cwd: str = None) -> tuple:
    """
    Exécute `claude --print` sur le prompt (envoyé sur stdin) et affiche sa réponse.
    Renvoie (code retour, texte de la réponse, usage des tokens, quota atteint).
    """
    claude_bin = shutil.which("claude") or "claude"
    cmd = [claude_bin, "--print", "--output-format", "json"]
    if model:
        cmd.extend(["--model", model])
    for directory in add_dirs:
        cmd.extend(["--add-dir", directory])
    cmd.extend(extra_args)
    cmd.extend(["--allowedTools", *allowed_tools])

    # stderr séparé : la sortie standard doit rester du JSON exploitable
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=(os.name == "nt"),
        cwd=cwd,
    )

    raw_stdout, raw_stderr = process.communicate(input=prompt)
    # Texte de la réponse (champ `result`), ou sortie brute en repli si ce n'est pas du JSON
    stdout_output, token_usage = parse_claude_output(raw_stdout)
    if raw_stderr and raw_stderr.strip():
        stdout_output = f"{stdout_output.rstrip()}\n{raw_stderr.strip()}" if stdout_output.strip() else raw_stderr.strip()
    session_limit_hit = "hit your session limit" in (stdout_output or "")

    if stdout_output:
        print(stdout_output)
        if session_limit_hit:
            print("\n⚠️ QUOTA ATTEINT : La session Claude Code a atteint sa limite d'utilisation.")
    return process.returncode, stdout_output, token_usage, session_limit_hit


def report_token_usage(issue: str, step: str, model: str, usage: dict, prompt_chars: int, returncode: int) -> None:
    """Affiche la ligne [TOKENS] et l'ajoute à .tmp/token_usage.csv (ne fait jamais échouer l'étape)."""
    if not usage:
        print(f"\n[TOKENS] Usage indisponible (sortie de Claude Code non JSON) — prompt : {prompt_chars} caractères.")
        return
    model_label = usage.get("modeles") or model or "défaut"
    cost = usage.get("cout_usd")
    cost_label = f"{cost:.4f} $" if isinstance(cost, (int, float)) else "?"
    print(f"\n[TOKENS] entrée={usage['tokens_entree']} | cache écrit={usage['cache_ecrit']} | "
          f"cache lu={usage['cache_lu']} | sortie={usage['tokens_sortie']} | tours={usage['tours']} | "
          f"coût={cost_label} | durée={usage['duree_s']} s | modèle={model_label} | prompt={prompt_chars} car.")
    row = {
        "date": datetime.now().isoformat(timespec="seconds"), "ticket": issue, "etape": step,
        "modele": model_label, "taille_prompt": prompt_chars, "code_retour": returncode,
        **{k: usage.get(k, "") for k in ("tokens_entree", "cache_ecrit", "cache_lu", "tokens_sortie",
                                          "tours", "duree_s", "cout_usd")},
    }
    try:
        os.makedirs(os.path.dirname(TOKEN_USAGE_CSV), exist_ok=True)
        is_new = not os.path.exists(TOKEN_USAGE_CSV)
        with open(TOKEN_USAGE_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TOKEN_USAGE_FIELDS, delimiter=";")
            if is_new:
                writer.writeheader()
            writer.writerow(row)
    except OSError as e:
        print(f"[TOKENS] ⚠️ Écriture de {TOKEN_USAGE_CSV} impossible ({e})")


# ---------------------------------------------------------------------------
# Release note & base de données
# ---------------------------------------------------------------------------

def clean_release_note(text: str) -> str:
    """Ramène le fichier rédigé par l'agent à un seul paragraphe (sans titre ni puce)."""
    lines = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s == "---" or re.fullmatch(r"\**release\s*notes?\s*:?\**", s, re.IGNORECASE):
            continue
        lines.append(re.sub(r"^[-*]\s+", "", s))
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def publish_release_note(issue_key: str, note_path: str, run_started_at: float) -> bool:
    """Ajoute la release note en fin de description Jira. Un échec n'interrompt jamais l'étape patch."""
    if not os.path.exists(note_path) or os.path.getmtime(note_path) < run_started_at:
        print(f"[RELEASE NOTE] ⚠️ '{note_path}' n'a pas été produit pendant cette exécution : description Jira inchangée.")
        return False
    paragraph = clean_release_note(_read_text(note_path))
    if not paragraph:
        print(f"[RELEASE NOTE] ⚠️ '{note_path}' est vide : description Jira inchangée.")
        return False
    try:
        update_jira_release_note(issue_key, paragraph)
        print(f"[RELEASE NOTE] ✅ Paragraphe « Release note » ajouté à la fin de la description de {issue_key}.")
        return True
    except Exception as e:
        print(f"[RELEASE NOTE] ⚠️ Mise à jour de la description Jira impossible ({e}). Fichier local conservé : {note_path}")
        return False


# Formulations du bloc base propres à l'étape (défaut : cadrage, révision et patch d'un ticket)
DB_BLOCK_WORDING = {
    "default": {
        "db_usage": "les données du cas décrit dans le ticket",
        "db_findings_target": "la Partie 2 (Analyse Technique) du cadrage",
        "db_private_targets": "dans le cadrage,\n  dans Jira ni dans le patch",
        "db_max_commands": "8",
    },
    "generate-cucumber-client": {
        "db_usage": "le paramétrage qui active ou conditionne les spécifiques du lot, ainsi que les valeurs de "
                    "référence utiles aux jeux de données des scénarios",
        "db_findings_target": "le champ « raison » du JSON final (paramétrage constaté), et appuie les jeux de "
                              "données des scénarios sur ces valeurs réelles",
        "db_private_targets": "dans les scénarios,\n  les Step Definitions ni le JSON final",
        "db_max_commands": "3",
    },
}


def build_db_block(db_schema: str, python_exe: str, workspace_dir: str, step: str = "default") -> str:
    """Bloc de prompt de l'accès base en lecture seule ('' si non demandé ou non configuré)."""
    if not db_schema:
        return ""
    if not db_tools.is_valid_identifier(db_schema):
        print(f"[BDD] ⚠️ Schéma '{db_schema}' invalide : l'agent travaillera sans la base.")
        return ""
    if not db_tools.is_db_configured():
        print(f"[BDD] ⚠️ Schéma '{db_schema}' demandé mais connexion non configurée (onglet Configuration) : "
              f"l'agent travaillera sans la base.")
        return ""
    db_type = db_tools.get_db_config()["type"]
    print(f"[BDD] Accès en lecture seule activé : {db_tools.describe_db_target()}, schéma {db_schema}.")
    db_cli = os.path.join(workspace_dir, "tools", "db_tools.py")
    base_cmd = f'"{python_exe}" "{db_cli}" --schema {db_schema}'
    if db_type == "sqlserver":
        schema_hint = f"Sous SQL Server, préfixe les tables par le schéma ({db_schema}.NOM_TABLE)."
    else:
        schema_hint = f"Les noms de tables non qualifiés sont résolus dans le schéma {db_schema}."
    return "\n" + render_prompt("db_access", db_type=db_type, db_schema=db_schema, base_cmd=base_cmd,
                                schema_hint=schema_hint, **DB_BLOCK_WORDING.get(step, DB_BLOCK_WORDING["default"]))


def bitbucket_base_url(repo_url: str = "") -> str:
    """BITBUCKET_URL du .env, à défaut l'hôte de l'URL de dépôt fournie."""
    configured = os.getenv("BITBUCKET_URL", "").strip().rstrip("/")
    if configured or not repo_url.startswith("http"):
        return configured
    parts = urlsplit(repo_url)
    return f"{parts.scheme}://{parts.netloc}"


def detect_odatio_version(std_repo_path: str) -> str:
    """
    Lit le fichier pom.xml racine du dépôt standard pour extraire la version d'Odatio,
    en nettoyant les suffixes de dev comme -SNAPSHOT.
    """
    pom_path = os.path.join(std_repo_path, "pom.xml")
    if os.path.exists(pom_path):
        try:
            tree = ET.parse(pom_path)
            root = tree.getroot()
            ns = {'mvn': 'http://maven.apache.org/POM/4.0.0'}
            version_node = root.find('mvn:version', ns)
            if version_node is None:
                parent_node = root.find('mvn:parent', ns)
                if parent_node is not None:
                    version_node = parent_node.find('mvn:version', ns)
            if version_node is not None and version_node.text:
                raw_version = version_node.text.strip()
                # On retire -SNAPSHOT pour correspondre aux tags/branches Git
                clean_version = raw_version.replace("-SNAPSHOT", "").strip()
                print(f"[VERSION ODATIO] Version brute : {raw_version} | Version nettoyée : {clean_version}")
                return clean_version
        except Exception as e:
            print(f"[VERSION ODATIO] Impossible de lire pom.xml ({e})")
    return "dev"


def ensure_cucumber_std_repo(repo_url: str, odatio_version: str) -> str:
    """
    Clone ou met à jour le dépôt Cucumber Standard et bascule sur la branche/tag
    correspondant à la version Odatio, avec gestion intelligente des branches distantes.
    """
    if not repo_url or not repo_url.startswith("http"):
        return ""

    clean_url = repo_url.split("/browse")[0].rstrip("/")
    # URL de navigation Bitbucket (.../projects/COMPONENT-QA/repos/cucumber) -> URL clonable (.../scm/component-qa/cucumber.git)
    web_repo = re.match(r"^(https?://[^/]+)/projects/([^/]+)/repos/([^/]+)$", clean_url)
    if web_repo:
        clean_url = f"{web_repo.group(1)}/scm/{web_repo.group(2).lower()}/{web_repo.group(3)}.git"

    workspace_dir = os.path.dirname(os.path.abspath(__file__))
    target_dir = os.path.join(workspace_dir, ".tmp", "cucumber_std")
    
    bitbucket_token = os.getenv("BITBUCKET_HTTP_TOKEN", "").strip()
    bitbucket_url = bitbucket_base_url(clean_url)
    git_env = os.environ.copy()
    git_cmd_extra = ["-c", f"http.extraHeader=Authorization: Bearer {bitbucket_token}"] if bitbucket_token else []

    if "/projects/" in clean_url and "/repos/" not in clean_url and not clean_url.endswith(".git"):
        project_key = clean_url.split("/")[-1].upper()
        print(f"\n==================================================")
        print(f"[CUCUMBER STD] Recherche du dépôt Cucumber dans le projet '{project_key}'...")
        api_url = f"{bitbucket_url}/rest/api/1.0/projects/{project_key}/repos?limit=100"
        headers = {"Authorization": f"Bearer {bitbucket_token}"} if bitbucket_token else {}
        
        try:
            res = requests.get(api_url, headers=headers, timeout=10)
            if res.status_code == 200:
                repos = res.json().get("values", [])
                cuc_repo = next((r["slug"] for r in repos if "cucumber" in r["slug"].lower()), None)
                if not cuc_repo:
                    cuc_repo = next((r["slug"] for r in repos if "qa" in r["slug"].lower()), None)
                if cuc_repo:
                    clean_url = f"{bitbucket_url}/scm/{project_key}/{cuc_repo}.git"
                    print(f"[CUCUMBER STD] Dépôt trouvé via API Bitbucket : {clean_url}")
                else:
                    print(f"[CUCUMBER STD] Aucun dépôt Cucumber trouvé dans le projet {project_key}.")
                    return ""
        except Exception as e:
            print(f"[CUCUMBER STD] Erreur API Bitbucket ({e})")
            return ""

    if not clean_url.endswith(".git") and "/scm/" in clean_url:
        clean_url += ".git"

    print(f"\n==================================================")
    print(f"[CUCUMBER STD] Préparation des tests Cucumber Standard (Version cible Odatio : {odatio_version})...")
    print(f"URL Git : {clean_url}")
    
    if os.path.exists(os.path.join(target_dir, ".git")):
        print(f"🔄 Mise à jour du dépôt Cucumber Standard dans {target_dir}...")
        subprocess.run(["git"] + git_cmd_extra + ["-C", target_dir, "fetch", "--all", "--tags", "--prune"], check=False, env=git_env)
    else:
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir, ignore_errors=True)
        print(f"📥 Clonage du dépôt Cucumber Standard depuis {clean_url}...")
        os.makedirs(target_dir, exist_ok=True)
        res = subprocess.run(["git"] + git_cmd_extra + ["clone", clean_url, target_dir], capture_output=True, text=True, env=git_env)
        if res.returncode != 0:
            print(f"❌ Échec du clonage Cucumber Standard : {res.stderr.strip()}")
            return ""

    # Liste des candidats (tags, branches locales, branches distantes, branches par défaut)
    clean_ver = odatio_version.replace("-SNAPSHOT", "").strip()
    candidate_refs = [
        f"v{clean_ver}",
        clean_ver,
        f"release/{clean_ver}",
        f"origin/v{clean_ver}",
        f"origin/{clean_ver}",
        f"origin/release/{clean_ver}",
        "dev",
        "origin/dev",
        "main",
        "origin/main",
        "master",
        "origin/master"
    ]

    checked_out = False
    for ref in candidate_refs:
        checkout_res = subprocess.run(["git", "-C", target_dir, "checkout", ref], capture_output=True, text=True)
        if checkout_res.returncode == 0:
            print(f"✅ Dépôt Cucumber Standard aligné avec succès sur la référence Git '{ref}'.")
            checked_out = True
            break

    if not checked_out:
        print("⚠️ Aucune des branches spécifiques trouvée. Récupération de la branche distante par défaut...")
        # Alignement sur la branche HEAD distante
        subprocess.run(["git", "-C", target_dir, "checkout", "-b", "default_branch", "--track", "origin/HEAD"], capture_output=True, text=True)

    print(f"==================================================\n")
    return target_dir
    
def ensure_local_repo(repo_input: str, repo_label: str) -> str:
    """
    Si repo_input est une URL de projet Bitbucket, interroge l'API REST 
    pour récupérer et cloner / mettre à jour TOUS les dépôts du projet.
    """
    if not repo_input or repo_input == "STD_ONLY":
        return ""

    if not (repo_input.startswith("http://") or repo_input.startswith("https://") or repo_input.startswith("git@")):
        print(f"[LOCAL REPO] Utilisation du chemin local fourni pour '{repo_label}' : {repo_input}")
        return repo_input

    workspace_dir = os.path.dirname(os.path.abspath(__file__))
    tmp_dir = os.path.join(workspace_dir, ".tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    bitbucket_token = os.getenv("BITBUCKET_HTTP_TOKEN", "").strip()
    bitbucket_url = bitbucket_base_url(repo_input)

    git_env = os.environ.copy()
    git_cmd_extra = ["-c", f"http.extraHeader=Authorization: Bearer {bitbucket_token}"] if bitbucket_token else []

    if "/projects/" in repo_input and "/repos/" not in repo_input and not repo_input.endswith(".git"):
        project_key = repo_input.rstrip("/").split("/")[-1].upper()
        target_dir = os.path.join(tmp_dir, f"{repo_label}_{project_key}")
        os.makedirs(target_dir, exist_ok=True)

        print(f"\n==================================================")
        print(f"[API BITBUCKET] Interrogation du projet '{project_key}' sur {bitbucket_url}...")

        api_url = f"{bitbucket_url}/rest/api/1.0/projects/{project_key}/repos?limit=100"
        headers = {"Authorization": f"Bearer {bitbucket_token}"} if bitbucket_token else {}

        repos_to_clone = []
        try:
            res = requests.get(api_url, headers=headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                repos_to_clone = [repo["slug"] for repo in data.get("values", [])]
                print(f"[API BITBUCKET] {len(repos_to_clone)} dépôt(s) trouvé(s) pour {project_key} : {', '.join(repos_to_clone)}")
            else:
                print(f"[API BITBUCKET] Code retour {res.status_code}, passage sur la liste de secours.")
        except Exception as e:
            print(f"[API BITBUCKET] Erreur API ({e}), passage sur la liste de secours.")

        if not repos_to_clone:
            repos_to_clone = [
                f"odatio-{project_key.lower()}",
                f"odatio-ui-{project_key.lower()}",
                "studio",
                "wms-goodsin-backend",
                "wms-preparation-backend",
                "traduction"
            ]
            print(f"[API BITBUCKET] Liste de secours chargée : {', '.join(repos_to_clone)}")

        print(f"[GIT REPO] Dossier cible local : {target_dir}")
        print(f"==================================================\n")

        for idx, repo_name in enumerate(repos_to_clone, 1):
            repo_dir = os.path.join(target_dir, repo_name)
            clone_url = f"{bitbucket_url}/scm/{project_key}/{repo_name}.git"

            if os.path.exists(os.path.join(repo_dir, ".git")):
                print(f"[{idx}/{len(repos_to_clone)}] 🔄 Mettre à jour {repo_name} dans {repo_dir}...")
                cmd = ["git"] + git_cmd_extra + ["-C", repo_dir, "pull"]
                res_pull = subprocess.run(cmd, capture_output=True, text=True, env=git_env)
                if res_pull.returncode == 0:
                    print(f"     ✅ {repo_name} mis à jour avec succès.")
                else:
                    print(f"     ⚠️ Erreur pull sur {repo_name} : {res_pull.stderr.strip()}")
            else:
                print(f"[{idx}/{len(repos_to_clone)}] 📥 Clonage de {repo_name} depuis {clone_url}...")
                cmd = ["git"] + git_cmd_extra + ["clone", clone_url, repo_dir]
                res_clone = subprocess.run(cmd, capture_output=True, text=True, env=git_env)
                if res_clone.returncode == 0:
                    print(f"     ✅ {repo_name} cloné avec succès.")
                else:
                    print(f"     ❌ Échec clonage {repo_name} : {res_clone.stderr.strip()}")

        print(f"\n[GIT COMPLETE] Traitement terminé pour le projet {project_key}.\n")
        return target_dir

    else:
        clean_name = repo_input.rstrip("/").split("/")[-1].replace(".git", "")
        target_dir = os.path.join(tmp_dir, f"{repo_label}_{clean_name}")

        print(f"\n[GIT SINGLE REPO] Traitement du dépôt unique : {repo_input}")
        if os.path.exists(target_dir) and os.path.exists(os.path.join(target_dir, ".git")):
            print(f"🔄 Mettre à jour le dépôt local {repo_label} : {target_dir}")
            cmd = ["git"] + git_cmd_extra + ["-C", target_dir, "pull"]
            res_pull = subprocess.run(cmd, capture_output=True, text=True, env=git_env)
            if res_pull.returncode == 0:
                print(f"✅ {clean_name} mis à jour avec succès.")
            else:
                print(f"⚠️ Erreur pull sur {clean_name} : {res_pull.stderr.strip()}")
        else:
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir, ignore_errors=True)
            print(f"📥 Clonage automatique du dépôt {repo_label} depuis {repo_input}...")
            cmd = ["git"] + git_cmd_extra + ["clone", repo_input, target_dir]
            res_clone = subprocess.run(cmd, capture_output=True, text=True, env=git_env)
            if res_clone.returncode == 0:
                print(f"✅ {clean_name} cloné avec succès.")
            else:
                print(f"❌ Erreur lors du clonage de {repo_input} :\n{res_clone.stderr.strip()}")
                return repo_input

        return target_dir


POSTMERGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp", "postmerge")
POSTMERGE_SUMMARY = "[CUCUMBER] Adaptation des tests post-merge pour {source}"


def _git_auth_args() -> list:
    token = os.getenv("BITBUCKET_HTTP_TOKEN", "").strip()
    return ["-c", f"http.extraHeader=Authorization: Bearer {token}"] if token else []


def find_local_repo_dir(repo_slug: str, roots: list) -> str:
    """Retrouve le clone local d'un dépôt Bitbucket parmi les dossiers préparés par ensure_local_repo."""
    slug = repo_slug.strip().lower()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        candidate = os.path.join(root, repo_slug)
        if os.path.isdir(os.path.join(candidate, ".git")):
            return candidate
        if os.path.isdir(os.path.join(root, ".git")):
            remote = subprocess.run(["git", "-C", root, "config", "--get", "remote.origin.url"],
                                    capture_output=True, text=True).stdout.strip().lower()
            if remote.rstrip("/").endswith(f"/{slug}.git") or remote.rstrip("/").endswith(f"/{slug}"):
                return root
    return ""


def resolve_cucumber_repo(local_client_repo: str) -> str:
    """
    Dépôt Cucumber du client : correspondance POSTMERGE_CUCUMBER_REPOS ("NOVALIA=novalia-cucumber-2,OSCARO=...")
    sinon le dépôt '*cucumber*' du projet (le plus récent par son nom, ex. novalia-cucumber-2 > novalia-cucumber).
    """
    if not local_client_repo or not os.path.isdir(local_client_repo):
        return ""
    client = os.path.basename(local_client_repo.rstrip("\\/")).split("_", 1)[-1].upper()
    for entry in os.getenv("POSTMERGE_CUCUMBER_REPOS", "").split(","):
        if "=" in entry and entry.split("=", 1)[0].strip().upper() == client:
            return entry.split("=", 1)[1].strip()
    candidates = sorted(d for d in os.listdir(local_client_repo)
                        if "cucumber" in d.lower() and os.path.isdir(os.path.join(local_client_repo, d, ".git")))
    if candidates:
        print(f"[POST-MERGE] Dépôt Cucumber client détecté pour {client} : {candidates[-1]} (parmi {', '.join(candidates)})")
    return candidates[-1] if candidates else ""


def build_postmerge_diff(repo_dir: str, merge_commit: str, base_commit: str, label: str) -> dict:
    """git fetch + git diff du code fusionné ; le diff complet est écrit dans .tmp/postmerge/."""
    os.makedirs(POSTMERGE_DIR, exist_ok=True)
    subprocess.run(["git"] + _git_auth_args() + ["-C", repo_dir, "fetch", "--all", "--prune"],
                   capture_output=True, text=True)
    if subprocess.run(["git", "-C", repo_dir, "cat-file", "-e", f"{merge_commit}^{{commit}}"],
                      capture_output=True).returncode != 0:
        raise RuntimeError(f"Commit {merge_commit} introuvable dans {repo_dir} après git fetch.")

    # Bitbucket envoie 000… comme fromHash pour une branche créée : on compare alors au 1er parent
    if not base_commit or set(base_commit) == {"0"}:
        base_commit = f"{merge_commit}^1"

    def git_diff(*extra):
        return subprocess.run(["git", "-C", repo_dir, "diff", *extra, base_commit, merge_commit],
                              capture_output=True, text=True, encoding="utf-8", errors="replace").stdout

    diff_path = os.path.join(POSTMERGE_DIR, f"{label}-{merge_commit[:8]}.diff")
    with open(diff_path, "w", encoding="utf-8") as f:
        f.write(git_diff())
    return {"diff_path": diff_path, "stat": git_diff("--stat").strip(), "base": base_commit, "merge": merge_commit}


def main():
    parser = argparse.ArgumentParser(description="Agent Scrum Master - Cadrage, Patching & Cucumber")
    parser.add_argument("--issue", default="", help="Clé du ticket Jira (ex: SPEC-1975, OXY-123) ; inutile pour generate-cucumber-client")
    parser.add_argument("--project", default="", help="Clé du projet Jira (ex: SPEC, OXY) ; inutile pour generate-cucumber-client")
    parser.add_argument("--client-repo", default="", help="Chemin local ou URL du dépôt client")
    parser.add_argument("--std-repo", default="", help="Chemin local ou URL du dépôt socle standard")
    parser.add_argument("--step", choices=["cadrage", "epic-cadrage", "epic-sync", "refine", "patch", "generate-cucumber-postmerge", "besoin", "cadrage-reponses", "generate-cucumber-client"], default="cadrage", help="Étape du traitement (epic-cadrage : découpe une épopée en tâches enfants analysées ; besoin : reformulation du besoin et questions ouvertes ; cadrage-reponses : complète le cadrage avec les réponses de l'onglet Intégration ; generate-cucumber-client : patch de tests Cucumber couvrant tous les spécifiques d'un client)")
    parser.add_argument("--specifics", default="", help="generate-cucumber-client : ids des spécifiques à traiter, séparés par des virgules (défaut : non couverts et partiels de l'inventaire)")
    parser.add_argument("--resume", action="store_true", help="generate-cucumber-client : reprend les lots non terminés de la dernière génération")
    parser.add_argument("--inventory-only", action="store_true", help="generate-cucumber-client : pré-calculs seuls (inventaire, catalogues, couverture), sans appel à Claude")
    parser.add_argument("--client-name", default="", help="generate-cucumber-client : nom du client, qui donne le dépôt cible <client>-cucumber (défaut : déduit de --client-repo)")
    parser.add_argument("--feedback", default="", help="Remarques de l'utilisateur pour la révision")
    # Les tests Cucumber sont désormais traités après merge, par le ticket SPEC [CUCUMBER] (generate-cucumber-postmerge)
    parser.add_argument("--generate-cucumber", action="store_true", default=False, help="Réactiver ponctuellement la génération des tests Cucumber avec le patch")
    parser.add_argument("--skip-cucumber", dest="generate_cucumber", action="store_false", help="Désactiver la génération des tests Cucumber (comportement par défaut)")
    parser.add_argument("--db-schema", default="", help="Schéma de la base client consultable en lecture seule par l'agent (vide = pas d'accès base)")
    parser.add_argument("--application", default="", help="Étapes besoin et generate-cucumber-client : Projet / Application dont les tickets servent de référence, séparés par des virgules (étape besoin, défaut : valeur du champ JIRA_APPLICATION_FIELD du ticket)")
    parser.add_argument("--epic-task-type", default=os.getenv("JIRA_EPIC_TASK_ISSUE_TYPE", ""), help="Type des tâches créées sous une épopée (défaut : premier type existant du projet parmi Tâche, Task, Development, Story)")

    postmerge = parser.add_argument_group("Post-merge (--step generate-cucumber-postmerge, déclenché par webhook_server.py)")
    postmerge.add_argument("--repo-slug", default="", help="Dépôt Bitbucket fusionné (ex: odatio-novalia)")
    postmerge.add_argument("--merge-commit", default="", help="Commit de merge (ou toHash du push)")
    postmerge.add_argument("--base-commit", default="", help="Commit de référence du diff (défaut : 1er parent du merge)")
    postmerge.add_argument("--source-branch", default="")
    postmerge.add_argument("--target-branch", default="")
    postmerge.add_argument("--cucumber-repo", default="", help="Dépôt Cucumber client (défaut : POSTMERGE_CUCUMBER_REPOS puis détection automatique)")
    postmerge.add_argument("--cucumber-base-branch", default=os.getenv("POSTMERGE_CUCUMBER_BASE_BRANCH", "integration"))
    postmerge.add_argument("--jira-target-project",
                           default=os.getenv("POSTMERGE_JIRA_PROJECT", "").strip() or os.getenv("JIRA_PROJECT_KEY", "").strip())
    # Composant ou équipe Atlassian des tickets créés (post-merge et tâches d'épopée)
    postmerge.add_argument("--jira-component", default=os.getenv("POSTMERGE_JIRA_COMPONENT", "").strip())
    postmerge.add_argument("--jira-label", default=os.getenv("POSTMERGE_JIRA_LABEL", "").strip())

    args = parser.parse_args()
    is_postmerge = args.step == "generate-cucumber-postmerge"
    if is_postmerge:
        if not (args.repo_slug and args.merge_commit):
            parser.error("--repo-slug et --merge-commit sont obligatoires pour generate-cucumber-postmerge")
        if not args.jira_target_project:
            parser.error("Projet Jira des tickets post-merge inconnu : renseignez POSTMERGE_JIRA_PROJECT ou JIRA_PROJECT_KEY "
                         "(onglet Configuration)")
        args.generate_cucumber = True
    # Couverture Cucumber d'un client : pas de ticket Jira, un identifiant de travail CUCUMBER-<CLIENT>
    is_cucumber_client = args.step == "generate-cucumber-client"
    if is_cucumber_client:
        args.client_name = (args.client_name.strip().lower() or cucumber_tools.client_name_from_repo(
            args.client_repo or os.getenv("BITBUCKET_CLIENT_REPO_PATH", "")))
        if not args.client_name:
            parser.error("Nom du client inconnu : passez --client-name ou --client-repo")
        args.issue = args.issue or cucumber_tools.work_id(args.client_name)
        args.project = args.project or args.client_name.upper()
        args.generate_cucumber = True
    elif not (args.issue.strip() and args.project.strip()):
        parser.error("--issue et --project sont obligatoires pour cette étape")

    target_issue = args.issue.strip().upper()
    target_project = args.project.strip().upper()

    # Création des tâches d'un découpage déjà rédigé (ex. création Jira échouée) : sans relancer l'agent
    if args.step == "epic-sync":
        print(f"=== Création des tâches de l'épopée {target_issue} à partir du découpage existant ===")
        sys.exit(0 if sync_epic_tasks(target_issue, target_project, load_epic_manifest(target_issue),
                                      component=args.jira_component, issue_type=args.epic_task_type) else 1)

    workspace_dir = os.path.dirname(os.path.abspath(__file__))
    python_exe = sys.executable

    # Rédaction du besoin et complément du cadrage par les réponses : Jira et fichiers locaux seulement, aucun dépôt
    is_besoin = args.step == "besoin"
    is_cadrage_answers = args.step == "cadrage-reponses"
    jira_only = is_besoin or is_cadrage_answers
    local_std_repo = local_client_repo = local_cucumber_std_repo = ""
    odatio_version, branch_rule, repo_scope = "non requise", "", ""
    conventions_path = ""
    if not jira_only:
        std_repo_arg = args.std_repo or os.getenv("BITBUCKET_STD_REPO_PATH", "").strip()
        if not std_repo_arg:
            print("❌ Dépôt du socle Standard non configuré : renseignez BITBUCKET_STD_REPO_PATH (onglet Configuration) "
                  "ou passez --std-repo.")
            sys.exit(2)
        conventions_path = (os.getenv("DEV_CONVENTIONS_PATH", "").strip()
                            or os.path.join(workspace_dir, "odatio-specific-code-conventions-dev.md"))

        # 1. Dépôt Socle Standard
        local_std_repo = ensure_local_repo(std_repo_arg, "std")

        # 2. Détection de la version Odatio & Clonage Cucumber Standard (si activé)
        local_cucumber_std_repo = ""
        odatio_version = detect_odatio_version(local_std_repo)
        if args.generate_cucumber:
            cucumber_std_url = os.getenv("BITBUCKET_CUCUMBER_STD_REPO", "")
            local_cucumber_std_repo = ensure_cucumber_std_repo(cucumber_std_url, odatio_version)
        else:
            print("\n[CUCUMBER STD] Génération des tests Cucumber désactivée par l'utilisateur.")

        # 3. Dépôt Client & Règles de branches
        if target_project == "OXY" or args.client_repo == "STD_ONLY":
            print("ℹ️ Projet OXY détecté : Socle Standard uniquement (branche dev).")
            local_client_repo = ""
            branch_rule = f"Projet OXY : utilise la branche de base 'dev' sur le socle Standard. Branche de travail : '{target_issue}_dev_...'."
            repo_scope = f"Explore le code source UNIQUEMENT dans le socle standard '{local_std_repo}'."
        else:
            client_repo_arg = args.client_repo or os.getenv("BITBUCKET_CLIENT_REPO_PATH", "")
            local_client_repo = ensure_local_repo(client_repo_arg, "client")
            branch_rule = f"Projet '{target_project}' : la branche de base sur le dépôt client est 'integration'. Branche de travail : '{target_issue}_dev_...'."
            repo_scope = f"Explore le code source dans le socle '{local_std_repo}' et dans le dépôt client '{local_client_repo}'."

    cucumber_instruction = ""
    if args.generate_cucumber:
        if local_cucumber_std_repo:
            cucumber_instruction = "\n" + render_prompt("cucumber_enabled", odatio_version=odatio_version,
                                                        local_cucumber_std_repo=local_cucumber_std_repo,
                                                        local_client_repo=local_client_repo)
    else:
        cucumber_instruction = "\n" + render_prompt("cucumber_disabled")
    token_saving_rules = "\n" + render_prompt("token_saving_rules")

    # Besoin initial Jira : sert à retrouver les tickets similaires et à capitaliser
    jira_context = {} if is_cucumber_client else get_full_jira_issue_context(
        target_issue, exclude_sections=(integration_tools.SECTION_TITLE,) if is_besoin else ())
    if jira_context.get("error"):
        print(f"[MÉMOIRE] Contexte Jira indisponible : {jira_context['error']}")
        jira_context = {}

    # Récupération des retours d'expérience et leçons mémorisées (taille plafonnée : MEMORY_MAX_CHARS, 0 = désactivé)
    memory_max_chars = _env_int("MEMORY_MAX_CHARS", 6000)
    try:
        capitalized_exp = get_capitalized_experience(
            target_project,
            component_name=jira_context.get("components", []),
            query_text=f"{jira_context.get('summary', '')}\n{jira_context.get('description', '')[:3000]}",
            exclude_issue_key=target_issue,
            max_chars=memory_max_chars,
        ) if memory_max_chars and not (jira_only or is_cucumber_client) else ""
    except Exception as e:
        print(f"[MÉMOIRE] ⚠️ Lecture de la mémoire impossible ({e})")
        capitalized_exp = ""
    capitalization_block = f"\n{capitalized_exp}\n" if capitalized_exp else ""
    if capitalized_exp:
        print(f"[MÉMOIRE] Retours d'expérience injectés dans le prompt ({len(capitalized_exp)} caractères).")
    else:
        print("[MÉMOIRE] Aucun retour d'expérience pertinent trouvé.")

    cadrage_file = f"{target_issue}-cadrage.md"
    release_note_file = f"{target_issue}-release-note.md"
    cadrage_before_run = _read_text(cadrage_file)
    epic_previous_manifest = load_epic_manifest(target_issue) if args.step == "epic-cadrage" else {}
    run_started_at = time.time()

    # Accès base en lecture seule (cadrage, refine, patch) ; le mot de passe reste dans le .env
    db_block = "" if is_postmerge or jira_only else build_db_block(args.db_schema.strip(), python_exe, workspace_dir,
                                                                    step=args.step)

    # Contexte Jira injecté dans le cadrage : l'agent ne réinterroge Jira que si une information manque
    jira_context_block = format_jira_context(jira_context, _env_int("JIRA_CONTEXT_MAX_CHARS", 12000))
    if jira_context_block:
        print(f"[JIRA] Contexte du ticket injecté dans le prompt ({len(jira_context_block)} caractères).")
        jira_fetch_step = (
            f"Le contexte Jira du ticket {target_issue} (description, Épopée, COST) est fourni ci-dessous : n'interroge Jira "
            f"QUE si une information manque (Épopée ou COST absents alors qu'attendus, texte signalé TRONQUÉ). Dans ce cas, "
            f"récupère uniquement l'élément manquant, par exemple :\n"
            f"   \"{python_exe}\" -c \"from tools.jira_tools import get_jira_issue_details; print(get_jira_issue_details('<CLÉ>'))\"\n"
            f"{jira_context_block}"
        )
    else:
        jira_fetch_step = f"Récupère les détails du ticket Jira cible {target_issue}."

    release_note_step = render_prompt("release_note", release_note_file=release_note_file).strip()

    # Variables communes des modèles de prompts (prompts/*.md)
    prompt_values = {
        "token_saving_rules": token_saving_rules, "capitalization_block": capitalization_block, "db_block": db_block,
        "target_issue": target_issue, "target_project": target_project, "conventions_path": conventions_path,
        "branch_rule": branch_rule, "repo_scope": repo_scope, "jira_fetch_step": jira_fetch_step,
        "cucumber_instruction": cucumber_instruction, "release_note_step": release_note_step,
        "local_client_repo": local_client_repo, "local_cucumber_std_repo": local_cucumber_std_repo,
        "odatio_version": odatio_version,
    }

    # Construction du prompt selon l'étape demandée
    if args.step == "cadrage":
        prompt = render_prompt("cadrage", **prompt_values)
    elif args.step == "epic-cadrage":
        manifest_file = epic_manifest_path(target_issue)
        known_keys = {t.get("key") for t in epic_previous_manifest.get("tasks", [])}
        other_children = [c for c in get_epic_children(target_issue) if c["key"] not in known_keys]
        children_block = ""
        if other_children:
            children_block = (
                "\nTickets déjà rattachés à l'épopée (ne propose pas de tâche pour un besoin qu'ils couvrent déjà) :\n"
                + "\n".join(f"- {c['key']} [{c['status']}] {c['summary']}" for c in other_children) + "\n"
            )
        revision_block = ""
        if epic_previous_manifest.get("tasks"):
            revision_block = "\n" + render_prompt(
                "epic_revision", manifest_file=manifest_file,
                previous_manifest_json=json.dumps(epic_previous_manifest, ensure_ascii=False, indent=2),
                revision_instructions=args.feedback or "aucune : vérifie et complète le découpage existant",
            )
        prompt = render_prompt("epic_cadrage", **prompt_values, manifest_file=manifest_file,
                               children_block=children_block, revision_block=revision_block)
    elif args.step == "refine":
        prompt = render_prompt("refine", **prompt_values, feedback=args.feedback)
    elif is_besoin:
        besoin_output = integration_tools.agent_output_path(target_issue)
        previous_besoin = integration_tools.load_state(target_issue)
        qa_block = integration_tools.previous_qa_block(previous_besoin)
        app_block = build_application_block(target_issue, args.application)
        prompt = render_prompt("besoin", **prompt_values, output_file=besoin_output,
                               previous_qa_block=f"{qa_block}\n" if qa_block else "",
                               application_block=f"{app_block}\n" if app_block else "")
    elif is_cucumber_client:
        client_name = args.client_name
        if not local_client_repo:
            print("❌ Dépôt client non renseigné : passez --client-repo ou renseignez BITBUCKET_CLIENT_REPO_PATH.")
            sys.exit(2)
        if not local_cucumber_std_repo:
            print("❌ Dépôt Cucumber Standard indisponible : vérifiez BITBUCKET_CUCUMBER_STD_REPO (URL clonable, ex. "
                  "https://git.kiscloud.io/scm/component-qa/cucumber.git) et BITBUCKET_HTTP_TOKEN.")
            sys.exit(2)
        args.cucumber_repo, cucumber_repo_dir = resolve_client_cucumber_repo(client_name, local_client_repo,
                                                                             args.cucumber_repo.strip())
        if not cucumber_repo_dir:
            print(f"❌ Dépôt Cucumber client '{args.cucumber_repo}' introuvable dans {local_client_repo}. Il n'est pas "
                  f"créé automatiquement : créez-le dans Bitbucket ou passez --cucumber-repo.")
            sys.exit(2)
        print(f"[CUCUMBER] Dépôt Cucumber client : {args.cucumber_repo} ({cucumber_repo_dir})")
        # Pré-calculs en Python puis génération par lots : un appel Claude neuf par lot
        sys.exit(run_cucumber_client(args, target_issue, local_client_repo, local_std_repo, local_cucumber_std_repo,
                                     odatio_version, cucumber_repo_dir, token_saving_rules, python_exe,
                                     workspace_dir))
    elif is_cadrage_answers:
        integration_state = integration_tools.load_state(target_issue)
        if not os.path.exists(cadrage_file):
            print(f"❌ Aucun cadrage '{cadrage_file}' à compléter : lancez d'abord l'analyse du ticket.")
            sys.exit(2)
        if not integration_tools.answered(integration_state):
            print(f"❌ Aucune réponse enregistrée pour {target_issue} (onglet Intégration).")
            sys.exit(2)
        prompt = render_prompt("cadrage_reponses", **prompt_values, cadrage_file=cadrage_file,
                               integration_block=integration_tools.cadrage_answers_block(integration_state))
    elif args.step == "patch":
        prompt = render_prompt("patch_cucumber" if args.generate_cucumber else "patch", **prompt_values)
    elif is_postmerge:
        merged_repo_dir = find_local_repo_dir(args.repo_slug, [local_client_repo, local_std_repo])
        args.cucumber_repo = args.cucumber_repo or resolve_cucumber_repo(local_client_repo)
        cucumber_repo_dir = find_local_repo_dir(args.cucumber_repo, [local_client_repo]) if args.cucumber_repo else ""
        if not merged_repo_dir:
            print(f"❌ Dépôt fusionné '{args.repo_slug}' introuvable localement ({local_client_repo} / {local_std_repo}).")
            sys.exit(2)
        if not cucumber_repo_dir:
            print(f"❌ Dépôt Cucumber client '{args.cucumber_repo or '*cucumber*'}' introuvable dans {local_client_repo}. "
                  f"Renseignez POSTMERGE_CUCUMBER_REPOS (ex: NOVALIA=novalia-cucumber-2).")
            sys.exit(2)
        try:
            merge_diff = build_postmerge_diff(merged_repo_dir, args.merge_commit, args.base_commit, target_issue)
        except RuntimeError as e:
            print(f"❌ {e}")
            sys.exit(2)
        print(f"[POST-MERGE] Diff {merge_diff['base']}..{merge_diff['merge']} écrit dans {merge_diff['diff_path']}")

        client_name = os.path.basename(local_client_repo.rstrip("\\/")).split("_", 1)[-1].upper() if local_client_repo else "STD"
        postmerge_summary = POSTMERGE_SUMMARY.format(source=target_issue)
        description_file = os.path.join(workspace_dir, f"{target_issue}-postmerge-cucumber.md")
        jira_cli = os.path.join(workspace_dir, "tools", "jira_cli.py")
        worktree_root = os.path.join(POSTMERGE_DIR, "worktrees")
        source_title = jira_context.get("summary", "") or "(ticket source introuvable dans Jira)"

        prompt = render_prompt(
            "postmerge", **prompt_values,
            source_title=source_title, repo_slug=args.repo_slug, merged_repo_dir=merged_repo_dir,
            source_branch=args.source_branch or "?", target_branch=args.target_branch or "?",
            merge_base=merge_diff["base"], merge_commit=merge_diff["merge"], diff_path=merge_diff["diff_path"],
            diff_stat=merge_diff["stat"] or "(diff vide)", cucumber_repo_dir=cucumber_repo_dir,
            cucumber_repo=args.cucumber_repo, cucumber_base_branch=args.cucumber_base_branch,
            cucumber_std_repo_label=local_cucumber_std_repo or "non disponible", client_name=client_name,
            description_file=description_file, python_exe=python_exe, jira_cli=jira_cli,
            jira_target_project=args.jira_target_project, postmerge_summary=postmerge_summary,
            jira_component=args.jira_component, jira_label=args.jira_label,
            worktree_root=worktree_root, workspace_dir=workspace_dir,
        )

    print(f"=== Exécution Étape: {args.step.upper()} ===")
    print(f"Projet Jira          : {target_project}")
    print(f"Ticket Cible         : {target_issue}")
    print(f"Version Odatio       : {odatio_version}")
    print(f"Génération Cucumber  : {'Oui' if args.generate_cucumber else 'Non'}")
    print(f"Dépôt Cucumber Std   : {local_cucumber_std_repo if local_cucumber_std_repo else ('Désactivé' if not args.generate_cucumber else 'Non configuré')}")

    claude_model = os.getenv(CLAUDE_MODEL_ENV[args.step], "").strip()
    print(f"Modèle Claude        : {claude_model or 'défaut de Claude Code'}")
    print(f"Taille du prompt     : {len(prompt)} caractères")

    add_dirs = [workspace_dir] + [d for d in (local_std_repo, local_client_repo, local_cucumber_std_repo)
                                  if d and os.path.exists(d)]
    returncode, stdout_output, token_usage, session_limit_hit = run_claude(
        prompt, claude_model, add_dirs,
        ["Read", "Write", "Edit", "Grep", "Glob", "Bash", f"Bash({python_exe}:*)", "Bash(git:*)"])
    report_token_usage(target_issue, args.step, claude_model, token_usage, len(prompt), returncode)

    if returncode == 0 and not session_limit_hit:
        print(f"\n=== Étape {args.step.upper()} terminée avec succès pour {target_issue} ===")
        
        # Enregistrement en mémoire / capitalisation d'expérience
        cadrage_content = _read_text(cadrage_file)
        if args.step == "cadrage":
            if _safe_memory(save_cadrage_memory, target_issue, target_project,
                            cadrage_text=cadrage_content, jira_context=jira_context):
                print(f"[MÉMOIRE] Besoin initial et cadrage enregistrés pour {target_issue}.")
            restore_integration_section(target_issue)
        elif args.step == "epic-cadrage":
            manifest_file = epic_manifest_path(target_issue)
            # En révision, l'agent peut ne retoucher que les analyses : le manifeste existant reste valable
            is_revision = bool(epic_previous_manifest.get("tasks"))
            if not os.path.exists(manifest_file) or (not is_revision and os.path.getmtime(manifest_file) < run_started_at):
                print(f"[ÉPOPÉE] ❌ L'agent n'a pas écrit le manifeste '{manifest_file}' pendant cette exécution : aucune tâche créée.")
                sys.exit(1)
            if args.feedback.strip():
                _safe_memory(save_cadrage_memory, target_issue, target_project, jira_context=jira_context)
                _safe_memory(save_refine_feedback, target_issue, args.feedback)
            if not sync_epic_tasks(target_issue, target_project, epic_previous_manifest,
                                   component=args.jira_component, issue_type=args.epic_task_type,
                                   epic_components=jira_context.get("components", [])):
                sys.exit(1)
        elif args.step == "refine":
            if _safe_memory(save_refine_feedback, target_issue, args.feedback,
                            previous_cadrage=cadrage_before_run, updated_cadrage=cadrage_content):
                print(f"[MÉMOIRE] Consigne de révision et écarts enregistrés pour {target_issue}.")
            restore_integration_section(target_issue)
        elif is_cadrage_answers:
            if not finalize_cadrage_answers(target_issue, cadrage_content, cadrage_before_run):
                sys.exit(1)
        elif args.step == "patch":
            # Seuls les patchs produits pendant CETTE exécution
            patch_paths = [f for f in os.listdir(".")
                           if f.startswith(f"{target_issue}-") and f.endswith(".patch")
                           and os.path.getmtime(f) >= run_started_at]
            if not patch_paths:
                print("[MÉMOIRE] ⚠️ Aucun patch généré pendant cette exécution : mémoire non mise à jour.")
            else:
                if _safe_memory(save_patch_memory, target_issue, patch_paths, agent_output=stdout_output,
                                final_cadrage=cadrage_content, jira_context=jira_context):
                    print(f"[MÉMOIRE] Livrable validé indexé : {', '.join(patch_paths)}.")
                publish_release_note(target_issue, release_note_file, run_started_at)
        elif is_besoin:
            if not finalize_besoin(target_issue, target_project, besoin_output, previous_besoin, run_started_at):
                sys.exit(1)
        elif is_postmerge:
            if not finalize_postmerge(args, target_issue, postmerge_summary, description_file,
                                      workspace_dir, run_started_at, stdout_output):
                sys.exit(1)
    else:
        print(f"\n=== Étape {args.step.upper()} interrompue pour {target_issue} (Code: {returncode}) ===")
        # Quota atteint : Claude peut sortir en code 0, on signale quand même l'échec
        sys.exit(returncode or 1)


def build_application_block(issue_key: str, application_arg: str) -> str:
    """
    Tickets du même Projet / Application (champ JIRA_APPLICATION_FIELD) : écrits dans un fichier consulté par
    l'agent, dont l'index est injecté dans le prompt. '' si aucun.
    """
    field_id = os.getenv("JIRA_APPLICATION_FIELD", "").strip()
    if not field_id:
        print("[BESOIN] JIRA_APPLICATION_FIELD non renseigné : pas de tickets de référence.")
        return ""
    applications = ([a.strip() for a in application_arg.split(",") if a.strip()]
                    or get_issue_field_values(issue_key, field_id))
    if not applications:
        print(f"[BESOIN] Aucun Projet / Application ({field_id}) sur {issue_key} : pas de tickets de référence.")
        return ""
    tickets, corpus_path = prepare_application_corpus(issue_key, applications, field_id)
    return integration_tools.application_block(applications, tickets, corpus_path) if tickets else ""


def prepare_application_corpus(issue_key: str, applications: list, field_id: str) -> tuple:
    """
    Récupère les tickets Jira (tous projets) des `applications` et les écrit dans un fichier consulté par l'agent.
    Renvoie (tickets, chemin du fichier) ; ([], '') si aucun ticket.
    """
    try:
        tickets = fetch_application_tickets(field_id, applications, issue_key,
                                            _env_int("JIRA_APPLICATION_MAX_TICKETS", 300))
    except (RuntimeError, requests.RequestException) as e:
        print(f"[JIRA] ⚠️ Tickets de {', '.join(applications)} indisponibles : {e}")
        return [], ""
    if not tickets:
        print(f"[JIRA] Aucun autre ticket pour {', '.join(applications)}.")
        return [], ""
    corpus_path = integration_tools.write_application_corpus(issue_key, applications, tickets)
    print(f"[JIRA] {len(tickets)} ticket(s) {', '.join(applications)} fournis comme référence ({corpus_path}).")
    return tickets, corpus_path


def resolve_client_cucumber_repo(client_name: str, local_client_repo: str, wanted: str = "") -> tuple:
    """
    Dépôt Cucumber du client pour generate-cucumber-client : `wanted` s'il est fourni, sinon '<client>-cucumber',
    sinon la détection de resolve_cucumber_repo. Renvoie (nom du dépôt, clone local) ; clone '' si introuvable.
    """
    for slug in [wanted] if wanted else [f"{client_name}-cucumber", resolve_cucumber_repo(local_client_repo)]:
        repo_dir = find_local_repo_dir(slug, [local_client_repo]) if slug else ""
        if repo_dir:
            return slug, repo_dir
    return wanted or f"{client_name}-cucumber", ""


def cucumber_jira_tickets(client_name: str, application_arg: str) -> list:
    """Tickets Jira du Projet / Application (mis en cache 24 h pour les reprises) ; [] si non renseigné."""
    applications = [a.strip() for a in application_arg.split(",") if a.strip()]
    field_id = os.getenv("JIRA_APPLICATION_FIELD", "").strip()
    if not applications:
        return []
    if not field_id:
        print("[JIRA] JIRA_APPLICATION_FIELD non renseigné : pas de tickets Jira de référence.")
        return []
    tickets = cucumber_tools.cached_tickets(client_name)
    if tickets is not None:
        print(f"[JIRA] {len(tickets)} ticket(s) {', '.join(applications)} repris du cache.")
        return tickets
    try:
        tickets = fetch_application_tickets(field_id, applications, "", _env_int("JIRA_APPLICATION_MAX_TICKETS", 300))
    except (RuntimeError, requests.RequestException) as e:
        print(f"[JIRA] ⚠️ Tickets de {', '.join(applications)} indisponibles : {e}")
        return []
    cucumber_tools.save_tickets(client_name, tickets)
    print(f"[JIRA] {len(tickets)} ticket(s) {', '.join(applications)} récupérés.")
    return tickets


def run_cucumber_client(args, work_key: str, local_client_repo: str, local_std_repo: str,
                        local_cucumber_std_repo: str, odatio_version: str, cucumber_repo_dir: str,
                        token_saving_rules: str, python_exe: str, workspace_dir: str) -> int:
    """
    Couverture Cucumber des spécifiques d'un client. Python fait tout le déterministe (inventaire, catalogues,
    extraits Jira, git, récapitulatif) ; Claude n'écrit que les tests, par lots, avec un appel neuf par lot.
    Reprise : les lots terminés (etat.json) ne sont pas refaits. Renvoie le code de sortie.
    """
    client = args.client_name
    base = cucumber_tools.base_ref(cucumber_repo_dir, args.cucumber_base_branch)
    if not base:
        print(f"❌ Branche '{args.cucumber_base_branch}' introuvable dans {cucumber_repo_dir}.")
        return 2
    resume_state = cucumber_tools.load_state(client) if args.resume else {}
    if args.resume and not cucumber_tools.pending_lots(resume_state):
        print("[CUCUMBER] Aucun lot à reprendre.")
        return 0

    # 1. Pré-calculs (0 token). Reprise ou sélection faite sur un inventaire : celui-ci est réutilisé tel quel
    subprocess.run(["git", "-C", cucumber_repo_dir, "fetch", "origin", "--prune"], capture_output=True)
    inventory = cucumber_tools.load_inventory(client) if (resume_state or args.specifics) else {}
    started = time.time()
    if inventory.get("specifiques"):
        print(f"[CUCUMBER] Inventaire du {inventory['genere_le'].replace('T', ' ')} réutilisé.")
    else:
        print("[CUCUMBER] Inventaire des spécifiques, catalogues et extraits Jira...")
        inventory = cucumber_tools.prepare(client, local_client_repo, local_std_repo, local_cucumber_std_repo,
                                           odatio_version, cucumber_repo_dir, base,
                                           cucumber_jira_tickets(client, args.application))
    specifics = {s["id"]: s for s in inventory["specifiques"]}
    counts = {}
    for spec in specifics.values():
        counts[spec["couverture"]] = counts.get(spec["couverture"], 0) + 1
    print(f"[CUCUMBER] {len(specifics)} spécifique(s) en {time.time() - started:.0f} s : "
          + ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
          + f" — {cucumber_tools.inventory_path(client)}")
    if args.inventory_only:
        return 0

    # 2. Lots : reprise, sélection explicite ou, par défaut, tous les spécifiques non couverts ou partiels
    if resume_state:
        state = resume_state
        print(f"[CUCUMBER] Reprise : {len(cucumber_tools.pending_lots(state))} lot(s) restant(s).")
    else:
        wanted = [i.strip() for i in args.specifics.split(",") if i.strip()] if args.specifics else [
            i for i, s in specifics.items() if s["couverture"] in ("non_couvert", "partiel")]
        unknown = [i for i in wanted if i not in specifics]
        if unknown:
            print(f"⚠️ Spécifiques inconnus ignorés : {', '.join(unknown)}")
        wanted = [i for i in wanted if i in specifics]
        if not wanted:
            print("[CUCUMBER] Aucun spécifique à traiter.")
            return 0
        state = {"client": client, "debut": datetime.now().isoformat(timespec="seconds"), "base": base,
                 "lots": cucumber_tools.make_lots(wanted, _env_int("CUCUMBER_BATCH_SIZE", 3))}
    try:
        worktree = cucumber_tools.prepare_worktree(cucumber_repo_dir, client, state["base"], reset=not resume_state)
    except RuntimeError as e:
        print(f"❌ {e}")
        return 2
    cucumber_tools.save_state(client, state)

    catalogue = inventory["catalogue_standard"]
    fixed_values = {
        "client_name": client, "token_saving_rules": token_saving_rules,
        "catalogue_std": catalogue["catalogue_std"], "steps_std": catalogue["steps_std"],
        "features_std": catalogue["features_std"],
        "examples": ", ".join(f"'{e}'" for e in catalogue["examples"]) or "aucun",
        "couverture_existante": inventory["couverture_existante"], "cucumber_repo": args.cucumber_repo,
        "worktree": worktree,
    }
    model = os.getenv("CLAUDE_MODEL_CUCUMBER", "").strip() or "sonnet"
    max_turns = str(_env_int("CUCUMBER_MAX_TURNS", 25))
    run_dir = cucumber_tools.cache_dir(client)
    total = {"tokens_entree": 0, "cache_ecrit": 0, "cache_lu": 0, "tokens_sortie": 0, "cout_usd": 0.0}
    lots = state["lots"]
    exit_code = 0

    # 3. Un appel Claude neuf par lot ; commit après chaque lot
    for lot in lots:
        if lot.get("statut") == "termine":
            continue
        lot_specs = [specifics[i] for i in lot["ids"] if i in specifics]
        print(f"\n[LOT {lot['numero']}/{len(lots)}] " + " ; ".join(s["libelle"] for s in lot_specs))
        has_parameters = any(s["type"] == "parametrage" for s in lot_specs)
        db_block = build_db_block(args.db_schema.strip(), python_exe, workspace_dir,
                                  step="generate-cucumber-client") if has_parameters else ""
        prompt = render_prompt("cucumber_client_lot", **fixed_values, lot_block=cucumber_tools.lot_block(lot_specs),
                               lot_label=f"{lot['numero']}/{len(lots)}", db_block=db_block)
        tools_allowed = ["Read", "Write", "Edit", "Grep", "Glob"] + ([f"Bash({python_exe}:*)"] if db_block else [])
        # Accès limité au dossier de travail du client (worktree, sources, extraits Jira) et aux catalogues
        returncode, output, usage, limit_hit = run_claude(
            prompt, model, [cucumber_tools.STD_CACHE_DIR], tools_allowed,
            extra_args=("--max-turns", max_turns), cwd=run_dir)
        report_token_usage(work_key, "cucumber-lot", model, usage, len(prompt), returncode)
        for key in total:
            value = (usage or {}).get(key)
            total[key] += value if isinstance(value, (int, float)) else 0
        results = cucumber_tools.parse_lot_result(output)
        committed = cucumber_tools.commit_lot(worktree, client, lot["numero"])
        lot.update(tokens={k: (usage or {}).get(k) for k in ("tokens_entree", "cache_ecrit", "cache_lu",
                                                                "tokens_sortie", "cout_usd")},
                   commit=committed, fin=datetime.now().isoformat(timespec="seconds"))
        if returncode != 0 or limit_hit or results is None:
            lot["statut"] = "echec"
            lot["erreur"] = ("quota atteint" if limit_hit else
                             f"code {returncode}" if returncode != 0 else "JSON final absent ou invalide")
            cucumber_tools.save_state(client, state)
            print(f"[LOT {lot['numero']}/{len(lots)}] ❌ Échec ({lot['erreur']}) : relancez avec « Reprendre ».")
            exit_code = 1
            break
        lot.update(statut="termine", resultats=results)
        cucumber_tools.save_state(client, state)
        done = sum(1 for r in results if r.get("statut") == "couvert")
        print(f"[LOT {lot['numero']}/{len(lots)}] ✅ {done}/{len(lot_specs)} spécifique(s) couvert(s)"
              f"{' — commit créé' if committed else ' — aucun fichier écrit'}.")

    # 4. Patch et récapitulatif, produits en Python
    patch_file = cucumber_tools.patch_path(client)
    if cucumber_tools.write_patch(worktree, state["base"], patch_file):
        print(f"\n[CUCUMBER] Patch généré : {patch_file}")
    else:
        print("\n[CUCUMBER] Aucun test écrit : pas de patch.")
    print(f"[CUCUMBER] Récapitulatif : {cucumber_tools.write_coverage_summary(client, inventory, state)}")
    cost = f"{total['cout_usd']:.4f} $" if total["cout_usd"] else "?"
    print(f"[TOKENS TOTAL] entrée={total['tokens_entree']} | cache écrit={total['cache_ecrit']} | "
          f"cache lu={total['cache_lu']} | sortie={total['tokens_sortie']} | coût={cost}")
    return exit_code


def restore_integration_section(issue_key: str) -> None:
    """Republie la section « Intégration du besoin » (questions et réponses) si l'agent l'a réécrite ou supprimée."""
    state = integration_tools.load_state(issue_key)
    if not state:
        return
    try:
        integration_tools.publish_state(issue_key, state)
        print(f"[INTÉGRATION] Section « {integration_tools.SECTION_TITLE} » rétablie dans la description de {issue_key}.")
    except (RuntimeError, requests.RequestException) as e:
        print(f"[INTÉGRATION] ⚠️ Section « {integration_tools.SECTION_TITLE} » non rétablie : {e}")


def finalize_cadrage_answers(issue_key: str, cadrage: str, cadrage_before: str) -> bool:
    """Publie dans Jira le cadrage complété par l'agent (à la place de l'ancien, le reste de la description intact)."""
    if not cadrage.strip():
        print(f"[CADRAGE] ❌ Cadrage vide après l'exécution de l'agent.")
        return False
    if cadrage.strip() == cadrage_before.strip():
        print("[CADRAGE] Les réponses ne changent rien au cadrage : Jira non modifié.")
        return True
    try:
        update_jira_cadrage(issue_key, cadrage, stop_titles=(integration_tools.SECTION_TITLE,))
    except (RuntimeError, requests.RequestException) as e:
        print(f"[CADRAGE] ❌ Cadrage complété localement mais non publié dans Jira : {e}")
        return False
    print(f"[CADRAGE] Cadrage complété avec les réponses et mis à jour dans la description de {issue_key}.")
    return True


def finalize_besoin(issue_key: str, project_key: str, output_file: str, previous: dict, run_started_at: float) -> bool:
    """Fusionne le besoin rédigé avec les questions déjà répondues, l'enregistre et le publie dans Jira."""
    if not os.path.exists(output_file) or os.path.getmtime(output_file) < run_started_at:
        print(f"[BESOIN] ❌ L'agent n'a pas écrit '{output_file}' pendant cette exécution.")
        return False
    try:
        agent = integration_tools.load_agent_output(output_file)
    except ValueError as e:
        print(f"[BESOIN] ❌ Résultat de l'agent inexploitable : {e}")
        return False
    state = integration_tools.merge_agent_output(previous or {"issue": issue_key}, agent)
    state["project"] = project_key
    integration_tools.save_state(issue_key, state)
    pending = len(state["questions"]) - len(integration_tools.answered(state))
    print(f"[BESOIN] Besoin reformulé enregistré : {len(state['questions'])} question(s) dont {pending} en attente.")
    try:
        integration_tools.publish_state(issue_key, state)
    except (RuntimeError, requests.RequestException) as e:
        print(f"[BESOIN] ❌ Publication dans Jira impossible : {e}")
        return False
    print(f"[BESOIN] Section « {integration_tools.SECTION_TITLE} » mise à jour dans la description de {issue_key}.")
    return True


def finalize_postmerge(args, source_issue: str, summary: str, description_file: str,
                       workspace_dir: str, run_started_at: float, agent_output: str) -> bool:
    """
    Contrôle le résultat de l'agent : ticket SPEC créé, patch attaché. Rattrape ce que l'agent
    n'a pas fait (création depuis la description, pièce jointe manquante) puis capitalise.
    """
    keys = re.findall(rf"JIRA_KEY=({re.escape(args.jira_target_project)}-\d+)", agent_output or "")
    new_key = keys[-1] if keys else find_jira_issue_by_summary(args.jira_target_project, summary)
    description = _read_text(description_file)

    if not new_key:
        if not description:
            print("[POST-MERGE] ❌ Ni ticket Jira créé, ni description rédigée par l'agent.")
            return False
        print("[POST-MERGE] Ticket non créé par l'agent : création depuis la description rédigée.")
        try:
            new_key, _ = create_or_get_issue(args.jira_target_project, summary, description,
                                             args.jira_component, [args.jira_label])
        except Exception as e:
            print(f"[POST-MERGE] ❌ Création du ticket impossible : {e}")
            return False

    patch_paths = [os.path.join(workspace_dir, f) for f in os.listdir(workspace_dir)
                   if f.startswith(f"{new_key}-") and f.endswith(".patch")
                   and os.path.getmtime(os.path.join(workspace_dir, f)) >= run_started_at]
    if patch_paths:
        try:
            for message in attach_files(new_key, patch_paths):
                print(f"[POST-MERGE] {message}")
        except Exception as e:
            print(f"[POST-MERGE] ⚠️ Vérification des pièces jointes impossible : {e}")
    else:
        print(f"[POST-MERGE] ℹ️ Aucun patch Cucumber généré pour {new_key} (aucune adaptation BDD nécessaire ?).")

    ticket_context = {"summary": summary, "components": [args.jira_component], "description": description}
    _safe_memory(save_cadrage_memory, new_key, args.jira_target_project, cadrage_text=description, jira_context=ticket_context)
    if patch_paths:
        _safe_memory(save_patch_memory, new_key, patch_paths, agent_output=agent_output, final_cadrage=description)

    print(f"\n[POST-MERGE] ✅ Ticket {new_key} prêt pour {source_issue} : {summary}")
    print(f"POSTMERGE_RESULT source={source_issue} jira={new_key} patches={len(patch_paths)}")
    return True


if __name__ == "__main__":
    main()