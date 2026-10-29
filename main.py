import argparse
import csv
import io
import json
import logging
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

from tools import env_tools
from tools import cucumber_a_revoir, cucumber_compile, cucumber_db_proof, cucumber_fix_budget, cucumber_stray, cucumber_init_fix, cucumber_bilan, cucumber_checks, cucumber_coverage, cucumber_decisions, cucumber_detection, cucumber_diagnostic, cucumber_failures, cucumber_environment, cucumber_rules, cucumber_run, cucumber_testability, cucumber_preflight, cucumber_results, cucumber_links, cucumber_server_logs, groundcover_tools, cucumber_import, cucumber_init, cucumber_recreation, cucumber_init_data, cucumber_initializers, cucumber_profile, cucumber_tools, cucumber_verify, cucumber_versions, db_tools, integration_tools, knowledge_tools, pr_review, assistant, db_restore, jenkins_tools
from tools.jira_cli import attach_files, create_or_get_issue
from tools.epic_tools import epic_manifest_path, load_epic_manifest, sync_epic_tasks
from tools.jira_tools import (
    fetch_application_tickets, find_jira_issue_by_summary, get_epic_children, get_full_jira_issue_context,
    get_jira_issue_details,
    get_issue_field_values, std_only_projects, update_jira_cadrage, update_jira_release_note,
)
from tools.memory_tools import (
    _extract_recap,
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

# Lignes du .env en défaut : une phrase avec leurs numéros au lieu des avertissements bruts de python-dotenv
logging.getLogger("dotenv.main").setLevel(logging.ERROR)
load_dotenv()
if env_tools.check_env(".env"):
    print(f"[CONFIGURATION] ⚠️ {env_tools.check_line(env_tools.check_env('.env'))}")


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
TOKEN_USAGE_FIELDS = ["date", "ticket", "etape", "modele", "effort", "tokens_entree", "cache_ecrit", "cache_lu",
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

# Variable .env de l'effort Claude par étape (vide = réglage de Claude Code sur le poste)
CLAUDE_EFFORT_ENV = {step: name.replace("CLAUDE_MODEL_", "CLAUDE_EFFORT_") for step, name in CLAUDE_MODEL_ENV.items()}
CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def claude_effort(*names: str) -> str:
    """Première variable .env renseignée parmi `names` ; valeur hors CLAUDE_EFFORT_LEVELS : avertissement et vide."""
    value = next((v for v in (os.getenv(n, "").strip().lower() for n in names) if v), "")
    if value and value not in CLAUDE_EFFORT_LEVELS:
        print(f"[CONFIG] ⚠️ Effort Claude '{value}' ({' / '.join(names)}) invalide, attendu : "
              f"{', '.join(CLAUDE_EFFORT_LEVELS)}. Option --effort non envoyée.")
        return ""
    return value

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
    keys = {"tokens_entree": ("input_tokens", "inputTokens"),
            "cache_ecrit": ("cache_creation_input_tokens", "cacheCreationInputTokens"),
            "cache_lu": ("cache_read_input_tokens", "cacheReadInputTokens"),
            "tokens_sortie": ("output_tokens", "outputTokens")}
    usage = {key: u.get(name, 0) or 0 for key, (name, _) in keys.items()}
    if not any(usage.values()):
        # Arrêt sur budget (--max-budget-usd) : usage global à zéro, le détail reste dans modelUsage
        models = (data.get("modelUsage") or {}).values()
        usage = {key: sum((m.get(name) or 0) for m in models) for key, (_, name) in keys.items()}
    usage.update({
        "tours": data.get("num_turns", ""),
        "duree_s": round((data.get("duration_ms") or 0) / 1000, 1),
        "cout_usd": data.get("total_cost_usd", ""),
        "modeles": ",".join((data.get("modelUsage") or {}).keys()),
        "fin": data.get("subtype", ""),
        "session_id": data.get("session_id", ""),
    })
    return text, usage


def lean_session() -> bool:
    """CLAUDE_LEAN_SESSION (défaut 1) : sessions Cucumber allégées (outils demandés seuls, sans MCP ni commandes)."""
    return os.getenv("CLAUDE_LEAN_SESSION", "1").strip() != "0"


def cucumber_budget() -> str:
    """CUCUMBER_MAX_BUDGET_USD (par appel) si c'est un montant positif, sinon '' (pas de --max-budget-usd)."""
    value = os.getenv("CUCUMBER_MAX_BUDGET_USD", "").strip().replace(",", ".")
    try:
        return value if value and float(value) > 0 else ""
    except ValueError:
        print(f"[CONFIG] ⚠️ CUCUMBER_MAX_BUDGET_USD='{value}' n'est pas un montant : pas de budget par appel.")
        return ""


def budget_hit(usage: dict) -> bool:
    """L'appel s'est arrêté sur --max-budget-usd."""
    return "budget" in str((usage or {}).get("fin", ""))


def claude_command(model: str, add_dirs: list, allowed_tools: list, extra_args: tuple = (), effort: str = "",
                   lean: bool = False, budget_usd: str = "") -> list:
    """
    Ligne de commande `claude --print`. `lean` : seules les définitions des outils autorisés sont envoyées
    (--tools, noms sans motif : Bash(python:*) -> Bash), sans serveur MCP, commandes ni skills, et les sections
    propres au poste sorties du prompt système. `budget_usd` : --max-budget-usd.
    """
    cmd = [shutil.which("claude") or "claude", "--print", "--output-format", "json"]
    if model:
        cmd.extend(["--model", model])
    if effort in CLAUDE_EFFORT_LEVELS:
        cmd.extend(["--effort", effort])
    elif effort:
        print(f"[CONFIG] ⚠️ Effort Claude '{effort}' invalide, attendu : {', '.join(CLAUDE_EFFORT_LEVELS)}. "
              f"Option --effort non envoyée.")
    if lean:
        cmd.extend(["--tools", *dict.fromkeys(t.split("(", 1)[0] for t in allowed_tools)])
        cmd.extend(["--strict-mcp-config", "--disable-slash-commands", "--exclude-dynamic-system-prompt-sections"])
    if budget_usd:
        cmd.extend(["--max-budget-usd", budget_usd])
    for directory in add_dirs:
        cmd.extend(["--add-dir", directory])
    cmd.extend(extra_args)
    cmd.extend(["--allowedTools", *allowed_tools])
    return cmd


def claude_env() -> dict:
    """
    Environnement des appels `claude` : CLAUDE_CONFIG_DIR (onglet Configuration) choisit le compte Claude,
    c.-à-d. le dossier de configuration et d'identifiants de la CLI. Vide = compte par défaut (~/.claude).
    """
    env = os.environ.copy()
    config_dir = env.pop("CLAUDE_CONFIG_DIR", "").strip()
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config_dir)
    return env


def run_claude(prompt: str, model: str, add_dirs: list, allowed_tools: list, extra_args: tuple = (),
               cwd: str = None, effort: str = "", lean: bool = False, budget_usd: str = "") -> tuple:
    """
    Exécute `claude --print` sur le prompt (envoyé sur stdin) et affiche sa réponse.
    Renvoie (code retour, texte de la réponse, usage des tokens, quota atteint) ; usage["fin"] : subtype du résultat
    (error_max_budget_usd si le budget est atteint, cf. budget_hit).
    """
    cmd = claude_command(model, add_dirs, allowed_tools, extra_args, effort, lean, budget_usd)

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
        env=claude_env(),
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
    if budget_hit(token_usage):
        print(f"\n⚠️ BUDGET ATTEINT : arrêt de l'appel à --max-budget-usd {budget_usd}.")
    return process.returncode, stdout_output, token_usage, session_limit_hit


def report_token_usage(issue: str, step: str, model: str, usage: dict, prompt_chars: int, returncode: int,
                       effort: str = "") -> None:
    """Affiche la ligne [TOKENS] et l'ajoute à .tmp/token_usage.csv (ne fait jamais échouer l'étape)."""
    if not usage:
        print(f"\n[TOKENS] Usage indisponible (sortie de Claude Code non JSON) — prompt : {prompt_chars} caractères.")
        return
    model_label = usage.get("modeles") or model or "défaut"
    cost = usage.get("cout_usd")
    cost_label = f"{cost:.4f} $" if isinstance(cost, (int, float)) else "?"
    print(f"\n[TOKENS] entrée={usage['tokens_entree']} | cache écrit={usage['cache_ecrit']} | "
          f"cache lu={usage['cache_lu']} | sortie={usage['tokens_sortie']} | tours={usage['tours']} | "
          f"coût={cost_label} | durée={usage['duree_s']} s | modèle={model_label} | effort={effort or 'défaut'} | "
          f"prompt={prompt_chars} car.")
    row = {
        "date": datetime.now().isoformat(timespec="seconds"), "ticket": issue, "etape": step,
        "modele": model_label, "effort": effort, "taille_prompt": prompt_chars, "code_retour": returncode,
        **{k: usage.get(k, "") for k in ("tokens_entree", "cache_ecrit", "cache_lu", "tokens_sortie",
                                          "tours", "duree_s", "cout_usd")},
    }
    try:
        os.makedirs(os.path.dirname(TOKEN_USAGE_CSV), exist_ok=True)
        is_new = not os.path.exists(TOKEN_USAGE_CSV)
        if not is_new:
            with open(TOKEN_USAGE_CSV, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f, delimiter=";")
                old_rows = list(reader) if reader.fieldnames != TOKEN_USAGE_FIELDS else None
            if old_rows is not None:
                # Fichier d'une version précédente (sans colonne effort) : réécrit avec l'en-tête actuel
                with open(TOKEN_USAGE_CSV, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=TOKEN_USAGE_FIELDS, delimiter=";", extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(old_rows)
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


# ---------------------------------------------------------------------------
# Connaissances partagées : dépôt des analyses produites (knowledge_tools), jamais bloquant
# ---------------------------------------------------------------------------

def share_knowledge(body: str, projet: str, doc_type: str, sujet: str, **fields) -> dict:
    """
    Dépose une analyse dans le répertoire partagé et affiche le résultat ({} si la fonction est désactivée ou le
    texte vide). Ni un partage injoignable ni un refus (secret détecté) n'interrompent l'étape.
    """
    if not knowledge_tools.enabled() or not (body or "").strip():
        return {}
    result = knowledge_tools.publish_knowledge(body, projet, doc_type, sujet, **fields)
    print(knowledge_tools.publish_line(result))
    return result


def share_file(path: str, projet: str, doc_type: str, sujet: str, **fields) -> dict:
    """share_knowledge du contenu d'un fichier produit ({} s'il est absent)."""
    return share_knowledge(_read_text(path), projet, doc_type, sujet, **fields) if path else {}


def consult_knowledge(projet: str, **criteria) -> dict:
    """
    Consultation des connaissances partagées avant une analyse (knowledge_tools.consult) : message et documents
    retenus affichés ; {"bloc": "", "documents": []} si la fonction est désactivée. Jamais bloquant.
    """
    if not knowledge_tools.enabled():
        return {"bloc": "", "documents": []}
    shared = knowledge_tools.consult(projet, **criteria)
    print(f"[CONNAISSANCES] {shared['message']}" + "".join(f"\n    {p}" for p in shared["documents"]))
    return shared


def produced(path: str, since: float) -> bool:
    """Le fichier a-t-il été écrit pendant cette exécution ?"""
    return bool(path) and os.path.exists(path) and os.path.getmtime(path) >= since


def flush_knowledge_outbox() -> None:
    """Au démarrage : dépose les documents restés en file locale (partage indisponible lors d'un dépôt)."""
    if not (knowledge_tools.enabled() and knowledge_tools.outbox_count()):
        return
    result = knowledge_tools.flush_outbox()
    for message in result["messages"]:
        print(f"[CONNAISSANCES] {message}")
    print(f"[CONNAISSANCES] File locale : {result['deposes']} document(s) déposé(s), {result['restants']} en attente.")


def ticket_client(args, project: str) -> str:
    """Client d'une analyse de ticket, déduit du dépôt client ('' pour un projet du socle seul)."""
    if project.upper() in std_only_projects() or args.client_repo == "STD_ONLY":
        return ""
    return cucumber_tools.client_name_from_repo(args.client_repo or os.getenv("BITBUCKET_CLIENT_REPO_PATH", ""))


def epic_markdown(epic_key: str) -> str:
    """Découpage d'une épopée en un document : une section par tâche (clé, titre) suivie de son analyse."""
    tasks = load_epic_manifest(epic_key).get("tasks", [])
    parts = [f"# Découpage de l'épopée {epic_key}", ""]
    for number, task in enumerate(tasks, 1):
        parts += [f"## {number}. {task.get('key') or 'tâche non créée'} — {task.get('summary', '')}", "",
                  _read_text(task.get("file") or "").strip(), ""]
    return "\n".join(parts) if tasks else ""


def patch_recap(issue_key: str, patch_paths: list, agent_output: str) -> str:
    """Récapitulatif d'un patch : fichiers produits et fin de la sortie de l'agent (son récapitulatif)."""
    names = "\n".join(f"- {os.path.basename(p)}" for p in patch_paths)
    return f"# Récapitulatif du patch {issue_key}\n\nPatchs produits :\n{names}\n\n{_extract_recap(agent_output, 6000)}\n"


def inventory_markdown(client: str, specifics: dict) -> str:
    """Inventaire des spécifiques d'un client : un tableau id, libellé, type, couverture."""
    lines = [f"# Inventaire des spécifiques {client}", "", "| Spécifique | Libellé | Type | Couverture |",
             "|---|---|---|---|"]
    lines += [f"| {s['id']} | {str(s.get('libelle', '')).replace('|', '/')} | {s.get('type', '')} | {s.get('couverture', '')} |"
              for s in specifics.values()]
    return "\n".join(lines) + "\n"


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
                    "référence utiles aux jeux de données des scénarios (articles, emplacements, statuts, codes "
                    "existants). Une donnée de référence introuvable ou un échec « donnée de référence absente » se "
                    "vérifie en base avant d'être créé dans le Background ou signalé",
        "db_findings_target": "ta réponse finale (champ « raison » d'un lot, « restantes » d'une correction), et "
                              "appuie les jeux de données des scénarios sur ces valeurs réelles",
        "db_private_targets": "dans les scénarios,\n  les Step Definitions ni le JSON final",
        "db_max_commands": "3",
    },
}


def build_logs_block(python_exe: str, workspace_dir: str) -> str:
    """Bloc de prompt de la recherche en lecture seule dans les logs groundcover ('' si non configuré)."""
    if not groundcover_tools.configured():
        return ""
    cli = f'"{python_exe}" "{os.path.join(workspace_dir, "tools", "groundcover_tools.py")}"'
    print(f"[LOGS] Recherche dans les logs groundcover activée (filtre {groundcover_tools.config()['filter']}).")
    return "\n" + render_prompt("logs_access", cli=cli, max_lines=groundcover_tools.MAX_LINES,
                                 max_minutes=int(groundcover_tools.MAX_WINDOW.total_seconds() // 60))


def build_db_block(db_schema: str, python_exe: str, workspace_dir: str, step: str = "default",
                   profile: str = "") -> str:
    """
    Bloc de prompt de l'accès base en lecture seule ('' si non demandé ou non configuré). `profile` « cucumber » :
    base de l'environnement où sont joués les tests (CUCUMBER_DB_*, à défaut DB_*), passée à l'agent par --profile.
    """
    previous = db_tools.set_profile(profile)
    try:
        return _build_db_block(db_schema, python_exe, workspace_dir, step, profile)
    finally:
        db_tools.set_profile(previous)


def _build_db_block(db_schema: str, python_exe: str, workspace_dir: str, step: str, profile: str) -> str:
    if not db_schema and db_tools.config_prefix() != "DB_":
        # Base Cucumber renseignée : son schéma par défaut (CUCUMBER_DB_SCHEMA)
        db_schema = db_tools.get_db_config()["schema"]
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
    base_cmd = f'"{python_exe}" "{db_cli}" --schema {db_schema}' + (f" --profile {profile}" if profile else "")
    if db_type == "sqlserver":
        schema_hint = f"Sous SQL Server, préfixe les tables par le schéma ({db_schema}.NOM_TABLE)."
    else:
        schema_hint = f"Les noms de tables non qualifiés sont résolus dans le schéma {db_schema}."
    return "\n" + render_prompt("db_access", db_type=db_type, db_schema=db_schema, base_cmd=base_cmd,
                                schema_hint=schema_hint, **DB_BLOCK_WORDING.get(step, DB_BLOCK_WORDING["default"]))


def run_assistant(question: str, answer_file: str) -> int:
    """
    Onglet « Assistant » : réponse à une question libre sur les fonctionnalités de l'application ou ses résultats,
    par l'agent en lecture seule (code de l'application, résultats de .tmp/ et livrables). La réponse est écrite
    dans `answer_file` (Markdown, secrets masqués). Renvoie le code de sortie.
    """
    question = (question or "").strip()
    if not question:
        print("[ASSISTANT] ❌ Question vide.")
        return 2
    code_dir = os.path.dirname(os.path.abspath(__file__))
    prompt = render_prompt(
        "assistant", utilisateur=os.getenv("JIRA_EMAIL", "").split("@")[0] or "l'utilisateur", code=code_dir,
        token_saving_rules="\n" + render_prompt("token_saving_rules"), resultats=assistant.results_index(),
        historique=assistant.recent_exchanges(assistant.load_history()[:-1]), question=question)
    model = os.getenv("CLAUDE_MODEL_ASSISTANT", "").strip() or "sonnet"
    effort = claude_effort("CLAUDE_EFFORT_ASSISTANT")
    returncode, output, usage, limit_hit = run_claude(prompt, model, [code_dir], REVIEW_TOOLS,
                                                      extra_args=("--max-turns", str(_env_int("ASSISTANT_MAX_TURNS", 25))),
                                                      cwd=code_dir, effort=effort, lean=lean_session())
    report_token_usage("ASSISTANT", "ask", model, usage, len(prompt), returncode, effort)
    answer = assistant.clean_answer(output)
    if limit_hit or not answer:
        print("[ASSISTANT] ❌ Pas de réponse" + (" (quota atteint)." if limit_hit else "."))
        return 1
    if answer_file:
        os.makedirs(os.path.dirname(os.path.abspath(answer_file)), exist_ok=True)
        with open(answer_file, "w", encoding="utf-8") as f:
            f.write(answer)
    # Analyse déposée dans le répertoire de connaissances partagé (projet du client cité, sinon projet par défaut)
    project, client = assistant.subject_project(question, answer)
    share_knowledge(assistant.analysis_markdown(question, answer), project, "analyse-assistant",
                    " ".join(question.split())[:90], client=client, tags=["assistant"] + ([client] if client else []))
    return 0


def run_restore_config(client: str, source_schema: str, target_schema: str, backup: str = "") -> int:
    """
    Recopie de la config client (base client en direct, schéma `source_schema`) dans le schéma des tests Cucumber
    `target_schema`, méthode du script de restauration de l'équipe (db_restore) ; `backup` : retour arrière vers une
    sauvegarde. Jamais pendant un build Jenkins des jobs Cucumber. Compte rendu dans le répertoire partagé.
    """
    if not (client and target_schema):
        print("[CONFIG CLIENT] ❌ Client et schéma cible obligatoires.")
        return 2
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))

    def busy() -> list:
        return jenkins_tools.busy_builds(jenkins_tools.exclusive_jobs(profile.get("run_job", "")))

    log = lambda message: print(f"[CONFIG CLIENT] {message}")  # noqa: E731
    entry = (db_restore.restore_backup(client, backup, target_schema, log, busy) if backup else
             db_restore.restore_client_config(client, source_schema, target_schema, log, busy))
    print(f"[CONFIG CLIENT] {'✅' if entry['ok'] else '❌'} {entry['message']}")
    if entry.get("sauvegarde"):
        print(f"[CONFIG CLIENT] Sauvegarde du schéma d'avant : {entry['sauvegarde']}")
    share_knowledge(f"# {'Retour arrière' if backup else 'Recopie'} de la config client {client}\n\n"
                    f"- Date : {entry['date']}\n- Source : {entry['source']}\n- Cible : {entry['cible']}\n"
                    f"- Résultat : {'réussi' if entry['ok'] else 'échec'} — {entry['message']}\n"
                    + (f"- Sauvegarde du schéma d'avant : {entry['sauvegarde']}\n" if entry.get("sauvegarde") else ""),
                    client.upper(), "config-client-tests", f"Config client {client} dans le schéma {target_schema}",
                    client=client, tags=["cucumber", "base"])
    return 0 if entry["ok"] else 1


REVIEW_TOOLS = ["Read", "Grep", "Glob"]


def run_pr_review(pr_urls: list, feature: str = "") -> int:
    """
    Revue assistée d'une FONCTIONNALITÉ (lecture seule) : toutes ses pull requests Bitbucket relues ensemble — diff et
    code de chaque PR, ticket(s) Jira, conventions de l'équipe, commentaires déjà posés. Résultat (résumé, cohérence
    entre les PR, verdict, commentaires ligne par ligne rattachés à leur PR) enregistré pour l'onglet « Revue de PR ».
    Rien n'est publié sur Bitbucket ici. Renvoie le code de sortie.
    """
    urls = [u for u in dict.fromkeys(u.strip() for u in pr_urls or [] if u and u.strip())]
    if not urls:
        print("[REVUE PR] ❌ Aucune pull request à relire (--pr-url).")
        return 2
    run_dir = os.path.join(pr_review.ROOT, "runs", re.sub(r"[^\w.-]", "_", feature or "pr")
                           + f"_{datetime.now().strftime('%Y%m%d%H%M%S')}")
    os.makedirs(run_dir, exist_ok=True)
    prs, blocks, comments, maps, dirs, keys = [], [], [], {}, [run_dir], []
    for url in urls:
        try:
            project, repo, number = pr_review.parse_pr_url(url)
            pr = pr_review.pr_details(project, repo, number)
            diff, lines_map, files = pr_review.pr_diff(project, repo, number)
            existing = pr_review.existing_comments(project, repo, number)
        except (ValueError, pr_review.BitbucketError) as e:
            print(f"[REVUE PR] ❌ {url} : {e}")
            return 2
        label = pr_review.pr_label(pr)
        diff_file = os.path.join(run_dir, f"{label.replace('#', '_')}.diff")
        with open(diff_file, "w", encoding="utf-8") as f:
            f.write(diff)
        code, why = pr_review.prepare_code(project, repo, number, pr["commit"])
        print(f"[REVUE PR] {label} — {pr['titre']} ({pr['auteur']}, {len(files)} fichier(s)) : "
              + (f"code {code}" if code else f"⚠️ diff seul ({why})"))
        if code:
            dirs.append(code)
        blocks.append(f"- PR « {label} » : {project}/{repo} #{number} — {pr['titre']} ; auteur {pr['auteur']} ; "
                      f"{pr['source']} → {pr['cible']} ; {len(files)} fichier(s)\n  diff : '{diff_file}'\n  code : "
                      + (f"'{code}'" if code else "indisponible (diff seul : formule en question ce que tu ne peux "
                                                  "pas vérifier)")
                      + (f"\n  description : {' '.join(pr['description'].split())[:800]}" if pr["description"] else ""))
        comments += [{**c, "pr": label} for c in existing]
        maps[label] = lines_map
        keys += pr["jira"]
        prs.append({**pr, "repere": label, "fichiers": files})
    feature = feature or (keys[0] if keys else "")
    # Remarques déjà faites sur les PR FUSIONNÉES du même ticket (dans les projets des PR relues et REVIEW_PROJECTS)
    merged = []
    if feature:
        projects = [p["projet"] for p in prs] + [x.strip() for x in os.getenv("REVIEW_PROJECTS", "").split(",")]
        try:
            merged = pr_review.merged_feedback(feature, projects)
        except pr_review.BitbucketError as e:
            print(f"[REVUE PR] ⚠️ PR fusionnées de {feature} non lues : {e}")
        print(f"[REVUE PR] {len(merged)} PR fusionnée(s) de {feature} avec des remarques à reprendre.")
    tickets = []
    for key in list(dict.fromkeys(([feature] if feature else []) + keys))[:4]:
        try:
            tickets.append(f"{key} :\n{get_jira_issue_details(key)[:5000]}")
        except Exception as e:  # noqa: BLE001 — Jira facultatif pour une revue
            tickets.append(f"{key} : lecture impossible ({type(e).__name__})")
    conventions = os.getenv("DEV_CONVENTIONS_PATH", "").strip() or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "odatio-specific-code-conventions-dev.md")
    if os.path.exists(conventions):
        dirs.append(os.path.dirname(conventions))
    prompt = render_prompt(
        "pr_review", relecteur=os.getenv("JIRA_EMAIL", "").split("@")[0] or "l'utilisateur", nb_prs=len(prs),
        token_saving_rules="\n" + render_prompt("token_saving_rules"),
        fonctionnalite=(f"{feature} — " if feature else "") + prs[0]["titre"],
        jira="\n\n".join(tickets) or "(aucun ticket cité dans les titres, branches ou descriptions)",
        prs="\n".join(blocks),
        conventions=f"'{conventions}'" if os.path.exists(conventions) else "(fichier de conventions absent)",
        commentaires_existants="\n".join(f"- [{c['pr']}] {c['auteur']} — {c['fichier'] or 'général'}"
                                         + (f":{c['ligne']}" if c["ligne"] else "") + f" : {c['texte'][:200]}"
                                         for c in comments[:60]) or "(aucun)",
        commentaires_fusionnees="\n".join(
            f"- PR fusionnée « {m['pr']} » ({m['titre']}, {m['auteur']}) :\n" + "\n".join(
                f"  - {c['auteur']} — {c['fichier'] or 'général'}" + (f":{c['ligne']}" if c["ligne"] else "")
                + f" : {c['texte'][:300]}" for c in m["commentaires"][:25]) for m in merged)
        or "(aucune PR fusionnée commentée pour ce ticket)")
    model = os.getenv("CLAUDE_MODEL_REVIEW", "").strip() or "sonnet"
    effort = claude_effort("CLAUDE_EFFORT_REVIEW")
    turns = _env_int("REVIEW_MAX_TURNS", 40) + 15 * (len(prs) - 1)
    returncode, output, usage, limit_hit = run_claude(prompt, model, list(dict.fromkeys(dirs)), REVIEW_TOOLS,
                                                      extra_args=("--max-turns", str(turns)), cwd=run_dir,
                                                      effort=effort, lean=lean_session())
    report_token_usage(f"REVUE-{feature or prs[0]['repere']}", "review-pr", model, usage, len(prompt), returncode,
                       effort)
    review = pr_review.parse_review(output)
    if limit_hit or not (review["resume"] or review["commentaires"]):
        print("[REVUE PR] ❌ Pas de résultat exploitable" + (" (quota atteint)." if limit_hit else "."))
        return 1
    # Chaque commentaire est rattaché à sa PR ; ancrage vérifié en Python (ligne hors du diff : commentaire général)
    # Placement : la PR (repère donné, sinon celle dont le diff contient le fichier), le chemin exact du diff, puis la
    # ligne si elle est dans le diff, sinon le fichier ; un fichier hors des PR reste un commentaire général
    single = prs[0]["repere"] if len(prs) == 1 else ""
    for comment in review["commentaires"]:
        label = comment.get("pr") if comment.get("pr") in maps else single or next(
            (l for l, m in maps.items() if pr_review.diff_path(m, comment["fichier"])), prs[0]["repere"])
        resolved, where = pr_review.placement(maps[label], comment["fichier"], comment["ligne"])
        comment.update(pr=label, fichier=resolved or comment["fichier"], ancrage=where,
                       ancrable=where == "ligne", publie=False)
    identifier = pr_review.feature_id(prs, feature)
    pr_review.save_result(identifier, {
        **review, "fonctionnalite": feature, "prs": [{k: p[k] for k in ("repere", "projet", "depot", "id", "titre",
                                                                        "auteur", "commit", "url", "fichiers")}
                                                     for p in prs],
        "lignes": {l: {path: {str(k): v for k, v in m.items()} for path, m in lm.items()} for l, lm in maps.items()},
        "date": datetime.now().isoformat(timespec="seconds")})
    print(f"[REVUE PR] ✅ Analyse terminée ({len(prs)} PR) : {review['verdict']}, {len(review['commentaires'])} "
          f"commentaire(s) proposé(s).")
    # Revue déposée dans le répertoire de connaissances partagé (projet Jira du ticket, sinon projet Bitbucket)
    saved = pr_review.load_result(identifier)
    project = feature.split("-", 1)[0] if feature else prs[0]["projet"]
    share_knowledge(pr_review.review_markdown(saved), project, "revue-pr",
                    f"Revue {feature or prs[0]['repere']} — {prs[0]['titre']}"[:90],
                    tickets=[feature] if feature else [], composants=sorted({p["depot"] for p in prs}),
                    tags=["revue-pr"])
    return 0


def bitbucket_base_url(repo_url: str = "") -> str:
    """BITBUCKET_URL du .env, à défaut l'hôte de l'URL de dépôt fournie."""
    configured = os.getenv("BITBUCKET_URL", "").strip().rstrip("/")
    if configured or not repo_url.startswith("http"):
        return configured
    parts = urlsplit(repo_url)
    return f"{parts.scheme}://{parts.netloc}"


ODATIO_VERSION = re.compile(r"^\d{2}\.\d+(\.\d+)*$")


def odatio_version_from_pom(text: str) -> tuple:
    """
    (version d'Odatio, d'où elle vient) lue dans un pom.xml : propriété odatio / kis (…version), puis version du
    parent Odatio / KIS, puis version du projet — retenue seulement si elle a la forme d'une version Odatio (26.16,
    25.4.2) : la version propre d'un dépôt (1.0.0) n'est pas celle de la plateforme. ('', raison) sinon.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return "", f"pom.xml illisible ({e})"
    tag = lambda node: node.tag.rsplit("}", 1)[-1]  # noqa: E731
    clean = lambda v: (v or "").strip().replace("-SNAPSHOT", "")  # noqa: E731
    candidates = []
    for props in (n for n in root if tag(n) == "properties"):
        for prop in props:
            if re.search(r"(odatio|kis).*version|version.*(odatio|kis)", tag(prop), re.I):
                candidates.append((clean(prop.text), f"propriété <{tag(prop)}>"))
    parent = next((n for n in root if tag(n) == "parent"), None)
    if parent is not None:
        fields = {tag(n): (n.text or "") for n in parent}
        if re.search(r"odatio|kis", fields.get("groupId", "") + fields.get("artifactId", ""), re.I):
            candidates.append((clean(fields.get("version")), f"parent {fields.get('artifactId', '')}"))
    own = next((n for n in root if tag(n) == "version"), None)
    if own is not None:
        candidates.append((clean(own.text), "version du projet"))
    for version, origin in candidates:
        if ODATIO_VERSION.match(version or ""):
            return version, origin
    seen = ", ".join(f"{v or '?'} ({o})" for v, o in candidates) or "aucune"
    return "", f"aucune version au format Odatio (ex. 26.16) dans le pom ; vu : {seen}"


def detect_odatio_version(std_repo_path: str) -> str:
    """
    Version d'Odatio du dépôt Standard (pom.xml racine, voir odatio_version_from_pom) ; ODATIO_VERSION du .env
    prime. « dev » si elle est introuvable : aucune comparaison de version n'est alors faite.
    """
    forced = os.getenv("ODATIO_VERSION", "").strip()
    if forced:
        print(f"[VERSION ODATIO] {forced} (ODATIO_VERSION du .env)")
        return forced
    pom_path = os.path.join(std_repo_path, "pom.xml")
    if os.path.exists(pom_path):
        version, origin = odatio_version_from_pom(_read_text(pom_path))
        if version:
            print(f"[VERSION ODATIO] {version} ({origin} du pom du Standard)")
            return version
        print(f"[VERSION ODATIO] ⚠️ {origin} — renseignez ODATIO_VERSION (onglet Configuration) ; comparaisons de "
              f"version désactivées.")
    return "dev"


def _force_rmtree(path: str) -> None:
    """Supprime un dossier, y compris les fichiers en lecture seule d'un .git sous Windows."""
    def make_writable(func, target, _exc):
        os.chmod(target, 0o700)
        func(target)
    shutil.rmtree(path, **({"onexc": make_writable} if sys.version_info >= (3, 12) else {"onerror": make_writable}))


def _git_out(repo_dir: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True).stdout.strip()


def _same_git_url(a: str, b: str) -> bool:
    normalize = lambda url: re.sub(r"\.git$", "", url.strip().rstrip("/")).lower()
    return normalize(a) == normalize(b)


def cucumber_std_clone_dir(workspace_dir: str, clone_url: str) -> str:
    """Un clone par dépôt : .tmp/cucumber_std/<slug-projet>_<slug-dépôt>."""
    path = urlsplit(clone_url).path if "://" in clone_url else clone_url.split(":", 1)[-1]
    parts = [p for p in path.split("/") if p and p != "scm"]
    slug = lambda text: re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"
    repo = re.sub(r"\.git$", "", parts[-1]) if parts else "cucumber"
    project = parts[-2] if len(parts) >= 2 else "std"
    return os.path.join(workspace_dir, ".tmp", "cucumber_std", f"{slug(project)}_{slug(repo)}")


def checkout_cucumber_std_ref(repo_dir: str, odatio_version: str) -> str:
    """
    Détache le clone sur la première référence existante : branches distantes et tags de la version Odatio, puis
    origin/dev, origin/main, origin/master, origin/HEAD. Jamais une branche locale, qui peut être en retard sur le
    distant. Renvoie la référence retenue ('' si aucune).
    """
    clean_ver = odatio_version.replace("-SNAPSHOT", "").strip()
    candidates = [f"origin/release/{clean_ver}", f"origin/v{clean_ver}", f"origin/{clean_ver}",
                  f"refs/tags/v{clean_ver}", f"refs/tags/{clean_ver}",
                  "origin/dev", "origin/main", "origin/master", "origin/HEAD"]
    for ref in candidates:
        if not _git_out(repo_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
            continue
        res = subprocess.run(["git", "-C", repo_dir, "checkout", "-q", "--force", "--detach", ref],
                             capture_output=True, text=True)
        if res.returncode == 0:
            return ref.removeprefix("refs/tags/")
        print(f"⚠️ Checkout de '{ref}' impossible : {res.stderr.strip()[:300]}")
    return ""


def ensure_cucumber_std_repo(repo_url: str, odatio_version: str) -> tuple:
    """
    Prépare le dépôt Cucumber Standard et renvoie (dossier, "référence @ commit court") ; ("", "") si indisponible.
    - chemin local d'un dépôt git : utilisé tel quel (fetch seulement, aucun checkout) ;
    - URL (navigation Bitbucket, clonable ou projet Bitbucket) : un clone par dépôt, recloné si son origin diffère,
      puis fetch et checkout détaché de la référence distante correspondant à la version Odatio.
    """
    repo_url = (repo_url or "").strip()
    if not repo_url:
        return "", ""

    bitbucket_token = os.getenv("BITBUCKET_HTTP_TOKEN", "").strip()
    git_env = os.environ.copy()
    git_cmd_extra = ["-c", f"http.extraHeader=Authorization: Bearer {bitbucket_token}"] if bitbucket_token else []

    def fetch(repo_dir: str) -> None:
        res = subprocess.run(["git"] + git_cmd_extra + ["-C", repo_dir, "fetch", "--all", "--tags", "--force", "--prune"],
                             capture_output=True, text=True, env=git_env)
        if res.returncode != 0:
            print(f"⚠️ git fetch du Cucumber Standard en échec : {res.stderr.strip()[:300]}")

    def report(repo_dir: str, ref: str) -> tuple:
        label = f"{ref} @ {_git_out(repo_dir, 'rev-parse', '--short', 'HEAD')}"
        print(f"[CUCUMBER STD] Dossier : {repo_dir}")
        print(f"{cucumber_tools.STD_REF_LOG} {label}")
        print(f"==================================================\n")
        return repo_dir, label

    print(f"\n==================================================")
    print(f"[CUCUMBER STD] Préparation des tests Cucumber Standard (Version cible Odatio : {odatio_version})...")

    # Chemin local : le dépôt de l'utilisateur n'est ni cloné ni basculé de branche
    if not repo_url.startswith(("http://", "https://", "git@", "file://")):
        local_dir = os.path.abspath(repo_url)
        if not os.path.exists(os.path.join(local_dir, ".git")):
            print(f"❌ Dépôt Cucumber Standard '{repo_url}' : ni URL ni dossier git local.")
            return "", ""
        print(f"📂 Dépôt Cucumber Standard local utilisé tel quel : {local_dir}")
        fetch(local_dir)
        branch = _git_out(local_dir, "rev-parse", "--abbrev-ref", "HEAD")
        return report(local_dir, branch if branch and branch != "HEAD" else "HEAD détaché")

    clean_url = repo_url.split("/browse")[0].rstrip("/")
    # URL de navigation Bitbucket (.../projects/COMPONENT-QA/repos/cucumber) -> URL clonable (.../scm/component-qa/cucumber.git)
    web_repo = re.match(r"^(https?://[^/]+)/projects/([^/]+)/repos/([^/]+)$", clean_url)
    if web_repo:
        clean_url = f"{web_repo.group(1)}/scm/{web_repo.group(2).lower()}/{web_repo.group(3)}.git"
    bitbucket_url = bitbucket_base_url(clean_url)

    if "/projects/" in clean_url and "/repos/" not in clean_url and not clean_url.endswith(".git"):
        project_key = clean_url.split("/")[-1].upper()
        print(f"[CUCUMBER STD] Recherche du dépôt Cucumber dans le projet '{project_key}'...")
        api_url = f"{bitbucket_url}/rest/api/1.0/projects/{project_key}/repos?limit=100"
        headers = {"Authorization": f"Bearer {bitbucket_token}"} if bitbucket_token else {}
        try:
            res = requests.get(api_url, headers=headers, timeout=10)
            repos = res.json().get("values", []) if res.status_code == 200 else []
        except Exception as e:
            print(f"[CUCUMBER STD] Erreur API Bitbucket ({e})")
            return "", ""
        cuc_repo = (next((r["slug"] for r in repos if "cucumber" in r["slug"].lower()), None)
                    or next((r["slug"] for r in repos if "qa" in r["slug"].lower()), None))
        if not cuc_repo:
            print(f"[CUCUMBER STD] Aucun dépôt Cucumber trouvé dans le projet {project_key}.")
            return "", ""
        clean_url = f"{bitbucket_url}/scm/{project_key.lower()}/{cuc_repo}.git"
        print(f"[CUCUMBER STD] Dépôt trouvé via API Bitbucket : {clean_url}")

    if not clean_url.endswith(".git") and "/scm/" in clean_url:
        clean_url += ".git"
    print(f"URL Git : {clean_url}")

    workspace_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.join(workspace_dir, ".tmp", "cucumber_std")
    target_dir = cucumber_std_clone_dir(workspace_dir, clean_url)
    # Ancienne disposition : un clone unique directement dans .tmp/cucumber_std
    if os.path.exists(os.path.join(root_dir, ".git")):
        print(f"🧹 Suppression de l'ancien clone unique {root_dir}...")
        _force_rmtree(root_dir)

    if os.path.exists(os.path.join(target_dir, ".git")):
        origin = _git_out(target_dir, "remote", "get-url", "origin")
        if not _same_git_url(origin, clean_url):
            print(f"🧹 Clone {target_dir} issu d'un autre dépôt ({origin or 'origin absent'}) : reclonage.")
            _force_rmtree(target_dir)

    if os.path.exists(os.path.join(target_dir, ".git")):
        print(f"🔄 Mise à jour du dépôt Cucumber Standard dans {target_dir}...")
        fetch(target_dir)
    else:
        if os.path.exists(target_dir):
            _force_rmtree(target_dir)
        print(f"📥 Clonage du dépôt Cucumber Standard depuis {clean_url} dans {target_dir}...")
        os.makedirs(os.path.dirname(target_dir), exist_ok=True)
        res = subprocess.run(["git"] + git_cmd_extra + ["clone", "--no-checkout", clean_url, target_dir],
                             capture_output=True, text=True, env=git_env)
        if res.returncode != 0:
            print(f"❌ Échec du clonage Cucumber Standard : {res.stderr.strip()}")
            return "", ""

    ref = checkout_cucumber_std_ref(target_dir, odatio_version)
    if not ref:
        print("❌ Aucune référence distante exploitable (version Odatio, dev, main, master) dans le Cucumber Standard.")
        return "", ""
    return report(target_dir, ref)


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
    Dépôt Cucumber du client : correspondance POSTMERGE_CUCUMBER_REPOS ("CLIENT=client-cucumber-2,AUTRE=...")
    sinon le dépôt '*cucumber*' du projet (le plus récent par son nom, ex. client-cucumber-2 > client-cucumber).
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
    parser.add_argument("--issue", default="", help="Clé du ticket Jira (ex: PROJ-123) ; inutile pour generate-cucumber-client")
    parser.add_argument("--project", default="", help="Clé du projet Jira (ex: PROJ) ; inutile pour generate-cucumber-client")
    parser.add_argument("--client-repo", default="", help="Chemin local ou URL du dépôt client")
    parser.add_argument("--std-repo", default="", help="Chemin local ou URL du dépôt socle standard")
    parser.add_argument("--step", choices=["cadrage", "epic-cadrage", "epic-sync", "refine", "patch", "generate-cucumber-postmerge", "besoin", "cadrage-reponses", "generate-cucumber-client", "review-pr", "ask", "restore-config"], default="cadrage", help="Étape du traitement (epic-cadrage : découpe une épopée en tâches enfants analysées ; besoin : reformulation du besoin et questions ouvertes ; cadrage-reponses : complète le cadrage avec les réponses de l'onglet Intégration ; generate-cucumber-client : patch de tests Cucumber couvrant tous les spécifiques d'un client)")
    parser.add_argument("--pr-url", action="append", default=[],
                        help="review-pr : adresse d'une pull request de la fonctionnalité (option répétable : toutes les "
                             "PR de la fonctionnalité sont relues ensemble)")
    parser.add_argument("--feature", default="", help="review-pr : ticket Jira de la fonctionnalité relue")
    parser.add_argument("--source-schema", default="", help="restore-config : schéma de la config client (base client)")
    parser.add_argument("--target-schema", default="", help="restore-config : schéma des tests Cucumber à remplacer")
    parser.add_argument("--backup", default="", help="restore-config : sauvegarde à remettre en place (retour arrière)")
    parser.add_argument("--question", default="", help="ask : question posée à l'assistant (onglet Assistant)")
    parser.add_argument("--answer-file", default="", help="ask : fichier où écrire la réponse")
    parser.add_argument("--specifics", default="", help="generate-cucumber-client : ids des spécifiques à traiter, séparés par des virgules (défaut : non couverts et partiels de l'inventaire)")
    parser.add_argument("--resume", action="store_true", help="generate-cucumber-client : reprend les lots non terminés de la dernière génération")
    parser.add_argument("--complete", action="store_true", help="generate-cucumber-client : complète la dernière génération (worktree et lots conservés) par de nouveaux lots pour --specifics, à défaut pour les spécifiques non couverts faute de step, d'endpoint, d'interaction ou de vérification")
    parser.add_argument("--max-lots", type=int, default=0, help="generate-cucumber-client : nombre maximal de lots traités par cette exécution (0 = tous), les suivants restent à reprendre")
    parser.add_argument("--inventory-only", action="store_true", help="generate-cucumber-client : pré-calculs seuls (inventaire, catalogues, couverture), sans appel à Claude")
    parser.add_argument("--init-only", action="store_true", help="generate-cucumber-client : lot d'initialisation des données, en patch séparé, selon l'état détecté de l'init : absente, copie des Init_<MODULE> du Cucumber Standard pour ses modules ; incomplète, compléments seuls dans une nouvelle feature (jamais de réécriture ni de fichier protégé modifié) ; complète, rien. Sans appel à Claude, sauf steps du projet à écrire")
    parser.add_argument("--profile-only", action="store_true", help="generate-cucumber-client : détecte le profil client (racine et tag des features, init, clear, fichiers du module spécifique, runner, codes de référence, version de la bibliothèque) dans le dépôt <client>-cucumber, sans appel à Claude")
    parser.add_argument("--detect-only", action="store_true", help="generate-cucumber-client : détection automatique du projet (inventaire, profil client, faits de l'initialisation des données), mise en cache par empreinte des commits des dépôts et sautée si elle n'a pas changé ; lecture seule, sans appel à Claude")
    parser.add_argument("--fix-client-tags", action="store_true", help="generate-cucumber-client : règle rétroactive « tag client » : ajoute le tag client au-dessus des scénarios actifs qui ne l'ont pas (scénarios désactivés exclus), commite dans le worktree, contrôle de nouveau et régénère le patch, sans appel à Claude")
    parser.add_argument("--detect-force", action="store_true", help="generate-cucumber-client --detect-only : détection refaite même si l'empreinte des commits n'a pas changé (profil corrigé)")
    parser.add_argument("--health-only", action="store_true", help="generate-cucumber-client : bilan de santé du dépôt <client>-cucumber avant génération (emplacement et tags des features, fichiers de données manquants, compilation et dry-run de toutes ses features), sans appel à Claude")
    parser.add_argument("--import-run", nargs="*", default=None, help="generate-cucumber-client : import d'un rapport d'exécution (console Jenkins, rapport Cucumber JSON, exports de logs serveur CSV), secrets masqués avant stockage, analysé sans appel à Claude")
    parser.add_argument("--import-url", default="", help="generate-cucumber-client : import d'un build Jenkins (console et rapports Cucumber JSON lus en mémoire), analysé sans appel à Claude")
    parser.add_argument("--patch-only", action="store_true", help="generate-cucumber-client : régénère seulement le patch <client>-cucumber.patch depuis l'espace de travail de l'agent (un commit, écart net avec la branche d'intégration actuelle), sans appel à Claude")
    parser.add_argument("--fix-init-from-import", type=int, default=0, help="generate-cucumber-client : l'init du run importé n° N échoue dans les compléments écrits par l'agent : il corrige ces fichiers seuls (contrôlés), puis rejoue l'init si CUCUMBER_AGENT_JENKINS=1, sinon livre <client>-cucumber-init-correction.patch")
    parser.add_argument("--from-import", type=int, default=0, help="generate-cucumber-client --complete : lots « correction » des échecs « test » des scénarios de l'agent de l'import n° N")
    parser.add_argument("--run-jenkins", action="store_true", help="generate-cucumber-client : pousse le worktree sur la branche lue par le job (profil), contrôle le commit, lance le job Jenkins du client, suit le build puis importe sa console et ses rapports (bilan)")
    parser.add_argument("--run-dry", action="store_true", help="generate-cucumber-client --run-jenkins : aperçu sans push ni lancement (0 token) — job, branche, rôles des paramètres, paramètres envoyés (secrets masqués) et nombre de tests attendus pour --run-tags")
    parser.add_argument("--run-no-push", action="store_true", help="generate-cucumber-client --run-jenkins : rien n'est commité ni poussé, le job exécute la branche telle qu'elle est sur le dépôt client (--run-branch, à défaut celle du profil) ; lecture seule")
    parser.add_argument("--run-branch", default="", help="generate-cucumber-client --run-jenkins --run-no-push : branche du dépôt client à exécuter (défaut : branche de travail du profil)")
    parser.add_argument("--run-tags", default="", help="generate-cucumber-client --run-jenkins : expression de tags ajoutée aux arguments du job (-Dcucumber.filter.tags, vide : tous les scénarios actifs)")
    parser.add_argument("--server-log", default="", help="generate-cucumber-client : export de logs serveur à contrôler (avec --log-scenario) : couvre-t-il l'heure du step en échec ? (l'outil d'export peut garder les seules lignes les plus récentes), sans appel à Claude")
    parser.add_argument("--log-scenario", default="", help="generate-cucumber-client --server-log : identifiant ou tag @agent_… du scénario en échec (dernière génération)")
    parser.add_argument("--init-modules", default="", help="generate-cucumber-client --init-only : modules retenus parmi WMS, TMS, OMS, séparés par des virgules (défaut : détectés dans les dépôts et l'inventaire du client)")
    parser.add_argument("--client-name", default="", help="generate-cucumber-client : nom du client, qui donne le dépôt cible <client>-cucumber (défaut : déduit de --client-repo)")
    parser.add_argument("--cucumber-std-repo", default="", help="Dépôt du Cucumber Standard : URL Bitbucket (navigation ou clonable) ou chemin d'un clone local (défaut : BITBUCKET_CUCUMBER_STD_REPO)")
    parser.add_argument("--feedback", default="", help="Remarques de l'utilisateur pour la révision")
    # Les tests Cucumber sont désormais traités après merge, par le ticket SPEC [CUCUMBER] (generate-cucumber-postmerge)
    parser.add_argument("--generate-cucumber", action="store_true", default=False, help="Réactiver ponctuellement la génération des tests Cucumber avec le patch")
    parser.add_argument("--skip-cucumber", dest="generate_cucumber", action="store_false", help="Désactiver la génération des tests Cucumber (comportement par défaut)")
    parser.add_argument("--db-schema", default="", help="Schéma de la base client consultable en lecture seule par l'agent (vide = pas d'accès base)")
    parser.add_argument("--application", default="", help="Étapes besoin et generate-cucumber-client : Projet / Application dont les tickets servent de référence, séparés par des virgules (étape besoin, défaut : valeur du champ JIRA_APPLICATION_FIELD du ticket)")
    parser.add_argument("--epic-task-type", default=os.getenv("JIRA_EPIC_TASK_ISSUE_TYPE", ""), help="Type des tâches créées sous une épopée (défaut : premier type existant du projet parmi Tâche, Task, Development, Story)")

    postmerge = parser.add_argument_group("Post-merge (--step generate-cucumber-postmerge, déclenché par webhook_server.py)")
    postmerge.add_argument("--repo-slug", default="", help="Dépôt Bitbucket fusionné (ex: odatio-client)")
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
    if args.step == "review-pr":
        sys.exit(run_pr_review(args.pr_url, args.feature))
    if args.step == "ask":
        sys.exit(run_assistant(args.question, args.answer_file))
    if args.step == "restore-config":
        sys.exit(run_restore_config(args.client_name.strip().lower(), args.source_schema.strip(),
                                    args.target_schema.strip(), args.backup.strip()))
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
    flush_knowledge_outbox()

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
            cucumber_std_url = args.cucumber_std_repo.strip() or os.getenv("BITBUCKET_CUCUMBER_STD_REPO", "")
            local_cucumber_std_repo, _ = ensure_cucumber_std_repo(cucumber_std_url, odatio_version)
        else:
            print("\n[CUCUMBER STD] Génération des tests Cucumber désactivée par l'utilisateur.")

        # 3. Dépôt Client & Règles de branches
        if target_project.upper() in std_only_projects() or args.client_repo == "STD_ONLY":
            print(f"ℹ️ Projet {target_project} (STD_ONLY_PROJECTS) : socle Standard uniquement (branche dev).")
            local_client_repo = ""
            branch_rule = f"Projet {target_project} : utilise la branche de base 'dev' sur le socle Standard. Branche de travail : '{target_issue}_dev_...'."
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

    # Connaissances partagées : analyses antérieures du projet et du sujet, injectées comme données à vérifier
    # (la génération Cucumber d'un client consulte les siennes dans run_cucumber_client)
    shared = {"bloc": "", "documents": []} if is_cucumber_client else consult_knowledge(
        target_project, client=ticket_client(args, target_project), tickets=[target_issue],
        composants=jira_context.get("components", []),
        query_text=f"{jira_context.get('summary', '')}\n{jira_context.get('description', '')[:3000]}")
    knowledge_block = f"\n{shared['bloc']}\n" if shared["bloc"] else ""

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
        "knowledge_block": knowledge_block,
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
            print("❌ Dépôt Cucumber Standard indisponible : vérifiez --cucumber-std-repo ou BITBUCKET_CUCUMBER_STD_REPO (URL, ex. "
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
                  f"Renseignez POSTMERGE_CUCUMBER_REPOS (ex: CLIENT=client-cucumber-2).")
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

    # Modèle de prompt sans ${knowledge_block} (prompt personnalisé, besoin...) : le bloc est ajouté en fin de prompt
    if shared["bloc"] and shared["bloc"] not in prompt:
        prompt += knowledge_block
    print(f"=== Exécution Étape: {args.step.upper()} ===")
    print(f"Projet Jira          : {target_project}")
    print(f"Ticket Cible         : {target_issue}")
    print(f"Version Odatio       : {odatio_version}")
    print(f"Génération Cucumber  : {'Oui' if args.generate_cucumber else 'Non'}")
    print(f"Dépôt Cucumber Std   : {local_cucumber_std_repo if local_cucumber_std_repo else ('Désactivé' if not args.generate_cucumber else 'Non configuré')}")

    claude_model = os.getenv(CLAUDE_MODEL_ENV[args.step], "").strip()
    claude_effort_level = claude_effort(CLAUDE_EFFORT_ENV[args.step])
    print(f"Modèle Claude        : {claude_model or 'défaut de Claude Code'} | "
          f"Effort : {claude_effort_level or 'défaut de Claude Code'}")
    print(f"Taille du prompt     : {len(prompt)} caractères")

    add_dirs = [workspace_dir] + [d for d in (local_std_repo, local_client_repo, local_cucumber_std_repo)
                                  if d and os.path.exists(d)]
    returncode, stdout_output, token_usage, session_limit_hit = run_claude(
        prompt, claude_model, add_dirs,
        ["Read", "Write", "Edit", "Grep", "Glob", "Bash", f"Bash({python_exe}:*)", "Bash(git:*)"],
        extra_args=knowledge_tools.read_only_args() if shared["documents"] else (), effort=claude_effort_level)
    report_token_usage(target_issue, args.step, claude_model, token_usage, len(prompt), returncode,
                       claude_effort_level)

    if returncode == 0 and not session_limit_hit:
        print(f"\n=== Étape {args.step.upper()} terminée avec succès pour {target_issue} ===")
        
        # Enregistrement en mémoire / capitalisation d'expérience
        cadrage_content = _read_text(cadrage_file)
        # Connaissances partagées : champs communs des analyses du ticket
        knowledge = {"client": ticket_client(args, target_project), "tickets": [target_issue],
                     "composants": jira_context.get("components", []),
                     "depots": knowledge_tools.repo_commits(local_std_repo, local_client_repo),
                     "sources": shared["documents"]}
        knowledge_subject = jira_context.get("summary") or target_issue
        if args.step in ("cadrage", "refine", "cadrage-reponses") and produced(cadrage_file, run_started_at):
            share_knowledge(cadrage_content, target_project, "cadrage", knowledge_subject, **knowledge)
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
            task_keys = [t["key"] for t in load_epic_manifest(target_issue).get("tasks", []) if t.get("key")]
            share_knowledge(epic_markdown(target_issue), target_project, "decoupage-epopee", knowledge_subject,
                            **{**knowledge, "tickets": [target_issue] + task_keys})
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
                share_knowledge(patch_recap(target_issue, patch_paths, stdout_output), target_project, "recap-patch",
                                knowledge_subject, **knowledge)
                if produced(release_note_file, run_started_at):
                    share_file(release_note_file, target_project, "release-note", knowledge_subject, **knowledge)
        elif is_besoin:
            if not finalize_besoin(target_issue, target_project, besoin_output, previous_besoin, run_started_at):
                sys.exit(1)
            share_knowledge(integration_tools.render_section_markdown(integration_tools.load_state(target_issue)),
                            target_project, "besoin", knowledge_subject, **knowledge)
        elif is_postmerge:
            if not finalize_postmerge(args, target_issue, postmerge_summary, description_file,
                                      workspace_dir, run_started_at, stdout_output, knowledge):
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


CUCUMBER_TOOLS = ["Read", "Write", "Edit", "Grep", "Glob"]
DEFAULT_CUCUMBER_MAX_TURNS = 40


def agent_compilation_block(command: str) -> str:
    """Consigne de compilation par l'agent (CUCUMBER_AGENT_COMPILE), '' si désactivée."""
    if not command:
        return ""
    return ("=== COMPILATION PAR TOI (seul accès autorisé à Maven) ===\n"
            f"Commande : {command}\n"
            "- Lance-la dès qu'une première version des .feature et Step Definitions est écrite, puis après tes "
            "corrections : elle compile tout le worktree (test-compile, rien n'est exécuté) et liste les erreurs "
            "« fichier:ligne:colonne message », chemins relatifs au worktree.\n"
            "- Corrige chaque erreur d'après la signature exacte (Grep dans les catalogues Java, puis lecture ciblée des "
            "sources du Standard) ; ne conclus pas sur un lot qui ne compile pas.\n"
            "- Au plus 3 compilations par appel (chacune peut durer quelques minutes). « NON VÉRIFIABLE » : problème "
            "d'environnement, ne relance pas et poursuis.\n"
            "=== FIN COMPILATION ===")


def warning_turn(max_turns: int) -> int:
    """Tour (75 % de CUCUMBER_MAX_TURNS, arrondi au-dessus) où l'agent d'un lot doit cesser d'explorer et finaliser."""
    return max(1, -(-3 * max_turns // 4))
# Motifs du contrôle qui ne justifient pas un appel de revue : seulement un champ du JSON final non rempli
EMPTY_FIELD_MOTIFS = {"« source » vide dans le JSON", "« difference_standard » vide dans le JSON"}


def run_cucumber_claude(prompt: str, model: str, allowed_tools: list, max_turns: str, run_dir: str,
                        effort: str, system_file: str = "", db_log: list = None, db_step: str = "",
                        resume: str = "") -> tuple:
    """
    Appel Claude d'un lot Cucumber : session allégée (CLAUDE_LEAN_SESSION) et budget CUCUMBER_MAX_BUDGET_USD.
    `system_file` : partie fixe ajoutée au prompt système (--append-system-prompt-file), identique d'un lot à
    l'autre et donc lue depuis le cache ; un texte en tête du message ne l'est pas (un seul bloc par message).
    `db_log` : liste (lot["base"]) complétée par les commandes base réellement lancées pendant l'appel (journal
    AGENT_DB_LOG de db_tools), chacune marquée de l'étape `db_step`.
    """
    extra = ("--max-turns", max_turns) + (("--append-system-prompt-file", system_file) if system_file else ()) + (
        ("--resume", resume) if resume else ())
    journal = os.path.join(run_dir, f"base_journal_{os.getpid()}.jsonl") if db_log is not None else ""
    if journal:
        if os.path.exists(journal):
            os.remove(journal)
        os.environ["AGENT_DB_LOG"] = journal
    started = time.time() - 1
    try:
        return run_claude(prompt, model, [cucumber_tools.STD_CACHE_DIR], allowed_tools, extra_args=extra,
                          cwd=run_dir, effort=effort, lean=lean_session(), budget_usd=cucumber_budget())
    finally:
        # Fichiers de test écrits hors du dépôt <client>-cucumber (dossier parasite) : rapatriés dans le worktree
        for source, target in cucumber_stray.relocate(run_dir, os.path.join(run_dir, "worktree"), started):
            print(f"[CUCUMBER] ⚠️ Fichier écrit hors du dépôt {source} → rapatrié dans le dépôt : {target}")
        if journal:
            os.environ.pop("AGENT_DB_LOG", None)
            db_log.extend({**entry, "etape": db_step} for entry in read_db_journal(journal))
            if os.path.exists(journal):
                os.remove(journal)


def read_db_journal(path: str) -> list:
    """Commandes base journalisées par db_tools (une ligne JSON chacune) ; lignes illisibles ignorées."""
    entries = []
    for line in _read_text(path).splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def lot_tools(values: dict) -> list:
    """
    Outils d'un appel Cucumber : CUCUMBER_TOOLS, plus le python de l'agent si l'accès base est accordé ou si l'agent
    compile lui-même (CUCUMBER_AGENT_COMPILE : tools/cucumber_compile.py).
    """
    return CUCUMBER_TOOLS + ([f"Bash({sys.executable}:*)"]
                             if values.get("db_block") or values.get("logs_block") or values.get("compilation_agent")
                             else [])


def db_summary(entries: list) -> str:
    """Résumé des commandes base d'un lot : nombre, tables lues, erreurs."""
    if not entries:
        return "aucune commande"
    tables = list(dict.fromkeys(t for e in entries for t in e.get("tables") or []))
    errors = sum(1 for e in entries if not e.get("ok"))
    return (f"{len(entries)} commande(s)" + (f", tables : {', '.join(tables)}" if tables else "")
            + (f", {errors} en erreur" if errors else ""))


def resume_lot_session() -> bool:
    """CUCUMBER_RESUME_SESSION (défaut 1) : revue et corrections reprennent la session Claude du lot (--resume)."""
    return os.getenv("CUCUMBER_RESUME_SESSION", "1").strip() != "0"


def cucumber_followup(lot: dict, prompt: str, model: str, effort: str, tools: list, max_turns: str, run_dir: str,
                      db_log: list = None, db_step: str = "") -> tuple:
    """
    Appel de suite d'un lot (revue, correction de fichiers / compilation, correction après exécution). Si la session
    du lot est connue (lot["session"]), elle est reprise (--resume) avec le modèle, l'effort et le prompt système du
    lot : l'agent corrige en se souvenant du spécifique, des versions Standard et client, du code des steps et de ce
    qu'il a déjà essayé, sans relire. Sinon, ou si la reprise échoue, appel neuf avec `model` / `effort`.
    Renvoie (code retour, sortie, usage, quota atteint, modèle, effort).
    """
    session = (lot or {}).get("session", "") if resume_lot_session() else ""
    if session:
        lot_model, lot_effort = lot.get("modele") or model, lot.get("effort", effort)
        system_file = lot.get("systeme", "")
        system_file = system_file if system_file and os.path.exists(system_file) else ""
        returncode, output, usage, limit_hit = run_cucumber_claude(prompt, lot_model, tools, max_turns, run_dir,
                                                                   lot_effort, system_file, db_log, db_step,
                                                                   resume=session)
        if returncode == 0 or limit_hit or budget_hit(usage):
            if usage and usage.get("session_id"):
                lot["session"] = usage["session_id"]
            return returncode, output, usage, limit_hit, lot_model, lot_effort
        print("[CUCUMBER] Reprise de la session du lot impossible : appel neuf.")
        lot.pop("session", None)
    returncode, output, usage, limit_hit = run_cucumber_claude(prompt, model, tools, max_turns, run_dir, effort,
                                                               db_log=db_log, db_step=db_step)
    return returncode, output, usage, limit_hit, model, effort


def cucumber_fix_effort() -> str:
    """Effort des corrections et de la revue : CLAUDE_EFFORT_CUCUMBER_FIX, défaut low."""
    return claude_effort("CLAUDE_EFFORT_CUCUMBER_FIX") or "low"


def cucumber_fix_plan(lot_model: str, attempts: int) -> list:
    """
    (modèle, effort) de chaque correction de compilation / dry-run : `attempts` fois CLAUDE_MODEL_CUCUMBER_FIX
    (défaut haiku) avec cucumber_fix_effort(), puis, si ce modèle diffère de celui du lot, une dernière tentative
    avec le modèle et l'effort du lot.
    """
    fix_model = os.getenv("CLAUDE_MODEL_CUCUMBER_FIX", "").strip() or "haiku"
    plan = [(fix_model, cucumber_fix_effort())] * attempts
    if attempts and fix_model != lot_model:
        plan.append((lot_model, claude_effort("CLAUDE_EFFORT_CUCUMBER")))
    return plan


def review_cucumber_lot(lot: dict, lot_specs: list, label: str, worktree: str, files: list, results: list,
                        reference: dict, values: dict, model: str, work_key: str, run_dir: str, on_usage) -> None:
    """
    Contrôles Python d'un lot (doublons, assertions, réutilisation des steps, champs du JSON) ; s'il y a des
    scénarios signalés, un seul appel de revue (--max-turns CUCUMBER_REVIEW_MAX_TURNS) les corrige, sauf si leurs
    seuls motifs sont des champs du JSON vides (EMPTY_FIELD_MOTIFS : notés dans revue["sans_appel"]). La revue ne
    supprime qu'un doublon quasi exact signalé (RÈGLE 3a) : toute autre suppression ou affaiblissement est annulé
    (feature remise en l'état). Met à jour lot["indicateurs"], lot["revue"] et le JSON du lot (doublons retirés).
    """
    analysis = cucumber_verify.analyse_lot(worktree, files, results, reference)
    flagged = [s for s in analysis["scenarios"] if s["motifs"]]
    print(f"[LOT {label}] Contrôle : {analysis['nb_scenarios']} scénario(s), réutilisation des steps Standard "
          f"{analysis['taux_reutilisation'] if analysis['taux_reutilisation'] is not None else '—'} %, "
          f"{len(flagged)} signalé(s).")
    review = {"signales": [{k: s[k] for k in ("fichier", "titre", "motifs")} for s in flagged], "decisions": [],
              "supprimes": []}
    if flagged and all(set(s["motifs"]) <= EMPTY_FIELD_MOTIFS for s in flagged):
        review["sans_appel"] = review["signales"]
        print(f"[LOT {label}] Revue non lancée : seuls des champs « source » / « difference_standard » vides "
              f"({len(flagged)} scénario(s), noté(s) dans le récapitulatif).")
    elif flagged:
        prompt = render_prompt("cucumber_client_review", **values, lot_label=label,
                               scenarios_signales=cucumber_verify.review_block(lot_specs, analysis, results))
        effort = cucumber_fix_effort()
        before = cucumber_a_revoir.snapshot(worktree, files)
        duplicates = {cucumber_verify._title_key(s["titre"]) for s in flagged
                      if any(cucumber_verify.exact_duplicate(m) for m in s["motifs"])}
        returncode, output, usage, _, used_model, effort = cucumber_followup(
            lot, prompt, model, effort, lot_tools(values), str(_env_int("CUCUMBER_REVIEW_MAX_TURNS", 8)), run_dir)
        report_token_usage(work_key, "cucumber-review", used_model, usage, len(prompt), returncode, effort)
        on_usage(usage)
        review["decisions"] = cucumber_verify.parse_review(output)
        review["annulations"] = cucumber_a_revoir.check_corrections(worktree, before, duplicates)
        for error in review["annulations"]:
            print(f"[LOT {label}] ⛔ Revue : {error}")
        files = list(dict.fromkeys(files + cucumber_tools.lot_files(worktree, [])))
        after = cucumber_verify.analyse_lot(worktree, files, results, reference)
        review["supprimes"] = cucumber_verify.apply_review(results, analysis, after, review["decisions"])
        print(f"[LOT {label}] Revue : {len(review['supprimes'])} doublon(s) retiré(s), "
              f"{sum(1 for d in review['decisions'] if d.get('decision') == 'corrige')} corrigé(s).")
        analysis = after
    lot["indicateurs"] = analysis
    lot["revue"] = review


def keep_diagnostics(lot: dict, label: str, worktree: str, mode: str, conventions: dict, push) -> None:
    """
    Diagnostics temporaires (D7) en fin de vérification : avec un moyen d'exécution, ils ont été exécutés et sont
    retirés. Sans moyen d'exécution mais avec la branche lue par le job (profil), ils restent dans le worktree,
    poussés sur cette branche, pour s'exécuter au prochain run ; ils sont retirés après l'import de ce run. Sinon,
    retirés (ils ne s'exécuteraient jamais).
    """
    temporary = cucumber_diagnostic.temporary_steps(worktree)
    if temporary and not mode and conventions.get("verify_branch"):
        error = push()
        if not error:
            lot["diagnostics_en_attente"] = temporary
            print(f"[LOT {label}] ⏳ {len(temporary)} step(s) de diagnostic temporaire gardé(s) pour le prochain run "
                  f"(branche {conventions['verify_branch']}) : retirés automatiquement après son import.")
            return
        print(f"[LOT {label}] ⛔ Diagnostic temporaire non poussé ({error}) : retiré.")
    elif temporary and not mode:
        print(f"[LOT {label}] ⚠️ Diagnostic temporaire sans moyen d'exécution ni branche lue par le job (profil) : "
              f"il ne s'exécuterait jamais, retiré.")
    for rel in cucumber_diagnostic.remove_temporary(worktree):
        print(f"[LOT {label}] 🧹 Diagnostic temporaire retiré : {rel}")
    lot.pop("diagnostics_en_attente", None)


def hold_meaning_changes(client: str, worktree: str, before: dict, client_tag: str, label: str, lot: dict) -> set:
    """
    D2 : réalignement d'un attendu sur le code ou valeur d'un step existant changée par une correction — la
    modification est gardée à part dans une décision en attente, le scénario d'origine est désactivé
    provisoirement. Renvoie les identifiants des scénarios concernés.
    """
    held = set()
    for change in cucumber_decisions.held_changes(worktree, before, client_tag):
        identifier = cucumber_verify.scenario_id(change["fichier"], change["titre"])
        if change.get("applique") and change["type"] == "donnees":
            # Autonomie : données de test changées par la correction, appliquées et enregistrées (à relire)
            cucumber_decisions.record_agent_decision(
                client, "donnees", change["proposition"], fichier=change["fichier"], titre=change["titre"],
                scenario=identifier, lot=lot["numero"], avant=change["avant"], apres=change["apres"])
            print(f"[LOT {label}] 🤖 Décision de l'agent ({cucumber_decisions.TYPES['donnees']}) : "
                  f"{change['proposition']} — appliquée, à relire dans le bilan.")
            continue
        if change.get("applique"):
            if cucumber_decisions.autonomous():
                cucumber_decisions.record_agent_decision(
                    client, "realignement", change["proposition"], fichier=change["fichier"], titre=change["titre"],
                    scenario=identifier, lot=lot["numero"], avant=change["avant"], apres=change["apres"])
            # Le code fait foi : réalignement appliqué, écart consigné (bilan « Doutes sur le code fourni »)
            lot.setdefault("correction", {}).setdefault("constats", []).append({
                "type": "ecart_spec_code", "detail": f"{change['titre']} : attendu réaligné sur le code (le code fait "
                                                     f"foi), spécification à revoir",
                "fichier": change.get("code") or change["fichier"], "effet": "scénario aligné sur le comportement "
                                                                             "réel du code",
                "certitude": "probable", "preuve": ""})
            print(f"[LOT {label}] ↔ Réaligné sur le code (le code fait foi) : {change['proposition']} — écart "
                  f"spécification / code consigné.")
            continue
        cucumber_decisions.propose(client, change["type"], change["proposition"], fichier=change["fichier"],
                                   titre=change["titre"], scenario=identifier, lot=lot["numero"],
                                   avant=change["avant"], apres=change["apres"])
        held.add(identifier)
        print(f"[LOT {label}] ✋ Décision en attente ({cucumber_decisions.TYPES[change['type']]}) : "
              f"{change['proposition']} — scénario désactivé provisoirement, modification gardée à part.")
    lot.setdefault("decisions_en_attente", [])
    lot["decisions_en_attente"] = sorted(set(lot["decisions_en_attente"]) | held)
    return held


def verify_execution_lot(lot: dict, lot_specs: list, label: str, worktree: str, base: str, files: list,
                         results: list, mode: str, mode_message: str, client: str, cucumber_std_dir: str,
                         compile_cmd: str, dry_run_cmd: str, values: dict, model: str, work_key: str, run_dir: str,
                         on_usage, sources: dict = None, unverified_status: str = "non_verifie",
                         context: dict = None) -> None:
    """
    Niveaux 2 et 3 (cucumber_verify) pour les scénarios tagués du lot, sans token ; `sources` : dépôt et commits de
    base transmis au job Jenkins (cucumber_verify.job_sources) ; sans `mode`, tous les scénarios prennent le statut
    `unverified_status` (environnement si l'initialisation a échoué). Scénarios invalides, faibles, en cascade
    d'une mise en place en échec côté test, ou sur une donnée de référence absente : au plus
    CUCUMBER_VERIFY_FIX_MAX (4) appels de correction, arrêtés pour un scénario dont l'échec revient à l'identique
    (cucumber_fix_budget ; cucumber_client_fix.md), contrôlés (RÈGLE 3c : une
    correction interdite est annulée), recompilation, revérification de ces seuls scénarios. Puis RÈGLE 3 : rien
    n'est supprimé ; les scénarios bloqués par une cause hors test prouvée sont désactivés (cucumber_a_revoir), sauf
    seuil dépassé ou échec côté test persistant : lot["arret"] (décision humaine), rien n'est désactivé.
    Résultat dans lot["execution"], lot["a_revoir"], lot["faibles"].
    """
    scenarios, kinds = dict(lot["tags"]), lot.get("types") or {}
    number = lot["numero"]
    conventions = (context or {}).get("conventions") or {}

    def build():
        return cucumber_tools.build_dir(client, worktree, cucumber_std_dir)

    def on_branch(run_base: str, run_sources: dict) -> tuple:
        """
        Branche lue par le job (profil) : worktree commité et poussé dessus avant la vérification. Renvoie (erreur
        d'arrêt, base du patch, paramètres du job).
        """
        branch = conventions.get("verify_branch", "")
        if not branch:
            return "", run_base, run_sources
        error, committed = cucumber_checks.push_for_verification(
            worktree, branch, f"Cucumber coverage of {client} specifics - lot {number} (vérification)")
        if committed:
            lot["commit"] = True
        print(f"[LOT {label}] {'⛔' if error else '⬆️'} " + (error or f"Worktree commité et poussé sur {branch}."))
        head = cucumber_tools.rev_parse(worktree, "HEAD")
        return error, head, {**(run_sources or {}), "CUCUMBER_SPEC_COMMIT": head,
                             conventions.get("verify_branch_param") or "CUCUMBER_SPEC_BRANCH": branch}
    if mode:
        stop, base, sources = on_branch(base, sources)
        if stop:
            mode, mode_message = "", f"vérification arrêtée : {stop}"
            print(f"[LOT {label}] ⛔ {mode_message}")
    if not mode:
        result = cucumber_verify.not_verified(scenarios, kinds, mode_message, unverified_status)
        print(f"[LOT {label}] {'🌐' if unverified_status == 'environnement' else '⚪'} {len(scenarios)} scénario(s) : "
              f"{mode_message}.")
    else:
        result = cucumber_verify.verify_lot(mode, worktree, base, build, files, scenarios, kinds, f"lot{number}",
                                            sources)
    builds = list(result["builds"])
    tests = {s["id"]: s.get("tests_std") or [] for s in lot_specs}
    spec_of = {cucumber_verify.scenario_id(s["fichier"], s["titre"]): s["spec"]
               for s in (lot.get("indicateurs") or {}).get("scenarios", [])}
    violations, stuck = [], set()

    def to_fix() -> dict:
        # Cascade d'une mise en place en échec pour une raison de test (catégorie « test ») : à corriger aussi ;
        # donnée de référence absente : à créer dans le Background avant toute désactivation
        return {t: v for t, v in result["scenarios"].items()
                if v["statut"] in ("invalide", "faible", "donnee_reference")
                or (v["statut"] == "cascade" and cucumber_verify.failure_category(v.get("cause", "")) == "test")}
    # Plafond adaptatif (cucumber_fix_budget) : on continue tant que l'échec change, scénario par scénario
    for attempt in range(cucumber_fix_budget.max_attempts() if mode else 0):
        bad = {t: v for t, v in to_fix().items() if t not in stuck}
        if not bad:
            break
        sent = {t: dict(v) for t, v in bad.items()}
        print(f"[LOT {label}] Correction après exécution {attempt + 1} : {len(bad)} scénario(s) invalide(s) "
              f"ou faible(s)" + (f" ({len(stuck)} bloqué(s), même échec après correction)" if stuck else "") + ".")
        lines = []
        for tag, item in bad.items():
            linked = tests.get(spec_of.get(item["id"], ""), [])
            hint = cucumber_verify.failure_hint(item["motif"])
            lines.append(f"- {item['id']} [tag {tag}] : {item['motif']}" + (
                f" ; test Standard lié : '{linked[0].get('extrait', linked[0]['path'])}'" if linked else "")
                + (f" — piste : {hint}" if hint else ""))
            # Sortie d'un diagnostic temporaire exécuté au tour précédent
            lines += cucumber_diagnostic.diagnostic_lines(item)
        lines += [f"- {e}" for e in violations]
        prompt = render_prompt("cucumber_client_fix", **{"logs_block": "", "regle_spec_code": cucumber_decisions.spec_code_rule(), **values}, lot_label=label, etape="VÉRIFICATION PAR EXÉCUTION",
                               erreurs="\n".join(lines), fichiers="\n".join(f"- {f}" for f in files))
        before = cucumber_a_revoir.snapshot(worktree, files)
        effort = cucumber_fix_effort()
        returncode, output, usage, limit_hit, used_model, effort = cucumber_followup(
            lot, prompt, model, effort, lot_tools(values), str(_env_int("CUCUMBER_FIX_MAX_TURNS", 10)), run_dir,
            db_log=lot.setdefault("base", []), db_step="correction après exécution")
        report_token_usage(work_key, "cucumber-verify-fix", used_model, usage, len(prompt), returncode, effort)
        on_usage(usage)
        if limit_hit:
            break
        # D8 : déploiement retenu seulement si l'extrait cité par l'agent est dans le journal de l'exécution
        deployed, refused = cucumber_diagnostic.deployment_findings(
            cucumber_diagnostic.parse_fix_result(output), result["scenarios"])
        for tag, update in deployed.items():
            result["scenarios"][tag].update(update)
            print(f"[LOT {label}] 🚚 {result['scenarios'][tag]['id']} : {update['motif']}")
        for error in refused:
            print(f"[LOT {label}] ⛔ {error}")
        violations = refused + cucumber_a_revoir.check_corrections(worktree, before)
        for error in violations:
            print(f"[LOT {label}] ⛔ {error}")
        held = hold_meaning_changes(client, worktree, before, conventions.get("client_tag", ""), label, lot)
        files[:] = list(dict.fromkeys(files + cucumber_tools.lot_files(worktree, [])))
        check = verify_cucumber_lot(client, label, worktree, base, cucumber_std_dir, files, compile_cmd, dry_run_cmd,
                                    values, model, work_key, run_dir, on_usage, inspired_of(results), context,
                                    lot.setdefault("base", []), lot)
        if not check["ok"] and not check.get("environnement"):
            break
        bad = {t: v for t, v in bad.items() if t not in deployed and v["id"] not in held}
        present = cucumber_verify.tagged_scenarios(worktree, files, bad)
        for tag in set(bad) - set(present):
            result["scenarios"][tag].update(statut="supprime", motif="supprimé par la correction")
        stop, base, sources = on_branch(base, sources)
        if present and stop:
            print(f"[LOT {label}] ⛔ Revérification arrêtée : {stop}")
            break
        if present:
            again = cucumber_verify.verify_lot(mode, worktree, base, build, files,
                                               {t: scenarios[t] for t in present}, kinds,
                                               f"lot{number}-r{attempt + 1}", sources)
            builds += again["builds"]
            result["scenarios"].update(again["scenarios"])
        # D7 : un diagnostic temporaire ne s'exécute qu'une fois, sa sortie est gardée dans le journal du scénario
        temporary = cucumber_diagnostic.remove_temporary(worktree)
        for rel in temporary:
            print(f"[LOT {label}] 🧹 Diagnostic temporaire exécuté puis retiré : {rel}")
        # Même échec qu'avant la correction : ce scénario ne repart plus (sauf diagnostic dont la sortie est nouvelle)
        newly = set() if temporary else cucumber_fix_budget.stuck_after(sent, to_fix())
        for tag in sorted(newly):
            print(f"[LOT {label}] ⏹ {result['scenarios'][tag]['id']} : même échec après correction, arrêt des "
                  f"tentatives (cause à prouver au prochain run importé).")
        stuck |= newly
    keep_diagnostics(lot, label, worktree, mode, conventions, lambda: on_branch(base, sources)[0])
    # Scénarios dont une modification attend une décision humaine : désactivés provisoirement, hors jugement
    waiting = set(lot.get("decisions_en_attente") or [])
    for item in result["scenarios"].values():
        if item["id"] in waiting and "décision en attente" not in item.get("motif", ""):
            item["motif"] = (item.get("motif", "") + " — décision en attente").strip(" —")
    judged = {t: v for t, v in result["scenarios"].items() if v["id"] not in waiting}
    # RÈGLE 3 : désactivation des seuls blocages hors test prouvés, ou arrêt pour décision humaine
    autonomous = cucumber_decisions.autonomous()
    decision = cucumber_a_revoir.decide(judged, spec_of, autonomous=autonomous) if mode else {
        "desactiver": {}, "faibles": [], "bloquants": [], "arret": False, "motifs": [], "alertes": []}
    located = cucumber_verify.tagged_scenarios(worktree, files, result["scenarios"])
    # D2 : chaque désactivation est proposée (désactivation provisoire, décision en attente) ; autonomie : décidée
    # par l'agent, enregistrée, à relire
    disabled = cucumber_a_revoir.apply_decision(worktree, files, result["scenarios"], decision,
                                                conventions.get("client_tag", ""), located, pending=not autonomous)
    for entry in disabled:
        label_cause = cucumber_a_revoir.CAUSES[entry["cause"]][0]
        record = cucumber_decisions.record_agent_decision if autonomous else cucumber_decisions.propose
        record(client, "desactivation", f"{entry['fichier']} / {entry['titre']} : {label_cause} ("
               + (cucumber_a_revoir.PROVEN if entry["prouvee"] else "cause " + cucumber_a_revoir.UNPROVEN) + ")",
               fichier=entry["fichier"], titre=entry["titre"], scenario=entry["id"], lot=lot["numero"],
               cause=entry["cause"], preuve=entry["preuve"], reactivation=entry["reactivation"],
               prouvee=entry["prouvee"])
        print(f"[LOT {label}] 🔕 Désactivé, " + ("décision de l'agent" if autonomous else "décision en attente")
              + f" ({label_cause}) : {entry['fichier']} / {entry['titre']}")
    for alert in decision.get("alertes") or []:
        print(f"[LOT {label}] ⚠️ {alert} — l'agent poursuit (autonomie), à relire dans le bilan.")
    if decision.get("alertes"):
        lot["alertes"] = decision["alertes"]
    if disabled and not cucumber_a_revoir.runner_excludes_a_revoir(conventions.get("runner")):
        print(f"[LOT {label}] ⚠️ Le runner n'exclut pas {cucumber_a_revoir.a_revoir_tag()} : les scénarios désactivés "
              f"restent exécutés (le filtre de tags du job peut ne pas atteindre Cucumber) ; ajoute « not "
              f"{cucumber_a_revoir.a_revoir_tag()} » aux tags du runner.")
    lot["a_revoir"] = disabled
    lot["faibles"] = [{"fichier": located[t][0], "titre": located[t][1]["titre"],
                       "motif": result["scenarios"][t]["motif"]} for t in decision["faibles"] if t in located]
    if decision["arret"]:
        lot["arret"] = decision["motifs"]
        print(f"[LOT {label}] ✋ Décision humaine requise, rien n'est désactivé : " + " ; ".join(decision["motifs"]))
    else:
        lot.pop("arret", None)
    counts = {}
    for item in result["scenarios"].values():
        counts[item["statut"]] = counts.get(item["statut"], 0) + 1
    if mode:
        print(f"[LOT {label}] Vérification : " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) + ".")
    # Donnée de référence absente créée par un initializer serveur : elle ne survit sans doute pas au clear
    initialized = ((context or {}).get("conventions") or {}).get("server_initialized_data") or []
    for item in result["scenarios"].values():
        hint = (cucumber_initializers.reference_hint(item["motif"], initialized)
                if item["statut"] == "donnee_reference" else "")
        if hint:
            item["motif"] += f" — {hint}"
            print(f"[LOT {label}] ⚠️ {item['id']} : {hint}")
    lot["execution"] = {"mode": mode or "aucun", "message": mode_message, "builds": builds,
                        "scenarios": result["scenarios"], "desactives": disabled}
    if result.get("classes_executees") is not None:
        # CUCUMBER_JACOCO=1 : part des classes de chaque spécifique du lot exécutées pendant le build client
        lot["execution"]["couverture_code"] = cucumber_verify.specific_coverage(
            lot_specs, set(result["classes_executees"]))


CUCUMBER_ENV_PREFIX = "[CUCUMBER ENVIRONNEMENT] ❌"


def fix_client_tags(client: str, local_cucumber_std_repo: str) -> int:
    """
    Règle rétroactive « tag client » : ajoute le tag client au-dessus des scénarios actifs qui ne l'ont pas
    (scénarios désactivés exclus), commite dans le worktree, contrôle de nouveau et régénère le patch. 0 token.
    """
    worktree = cucumber_tools.worktree_dir(client)
    state = cucumber_tools.load_state(client)
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))
    if not os.path.isdir(worktree) or not profile.get("tag_client"):
        print("[CUCUMBER] Correction impossible : aucune génération (worktree) ou tag client absent du profil.")
        return 2
    changed = cucumber_checks.add_missing_client_tags(worktree, profile["features_root"], profile["tag_client"],
                                                      cucumber_a_revoir.a_revoir_tag())
    if changed:
        cucumber_tools._git(worktree, "add", "--", *changed)
        cucumber_tools._git(worktree, "commit", "-m", f"Cucumber: client tag {profile['tag_client']} on scenarios "
                                                      f"written before the rule")
    check_cucumber_selection(client, worktree, local_cucumber_std_repo, profile, state, "")
    remaining = len((state.get("selection") or {}).get("retroactif") or [])
    if cucumber_tools.write_patch(worktree, state["base"], cucumber_tools.patch_path(client), client):
        print(f"[CUCUMBER] Patch régénéré : {cucumber_tools.patch_path(client)}")
    print(f"[CUCUMBER] Tag client {profile['tag_client']} ajouté dans {len(changed)} feature(s) ; "
          f"{remaining} scénario(s) encore sans tag.")
    return 0 if not remaining else 1


def check_cucumber_selection(client: str, worktree: str, cucumber_std_dir: str, profile: dict, state: dict,
                             dry_run_cmd: str) -> bool:
    """
    Sélection des scénarios livrés (0 token) : chaque scénario actif des features du client porte le tag client,
    aucun scénario désactivé n'en hérite ; puis deux dry-runs stricts, sans filtre et avec le tag client, qui
    doivent sélectionner le même nombre de scénarios, enregistré comme « nombre attendu » (state["selection"]).
    Renvoie False sur une erreur (hors environnement Maven).
    """
    client_tag, root = profile["tag_client"], profile["features_root"]
    if not client_tag or not root:
        print("[CUCUMBER] ⚠️ Sélection des scénarios non contrôlée : tag client ou racine des features absent du "
              "profil.")
        return True
    texts = {p: cucumber_tools._read_text_file(os.path.join(worktree, *p.split("/")))
             for p in cucumber_tools._git(worktree, "ls-files", "-co", "--exclude-standard", "--",
                                          "*.feature").stdout.splitlines()
             if os.path.exists(os.path.join(worktree, *p.split("/")))
             and (p.startswith(root) or p in profile["init_features"])}
    # Règle « tag client » postérieure à certains scénarios : tag absent = correction proposée, pas un échec
    tag_errors = cucumber_checks.check_client_tags(texts, client_tag)
    retroactive = [e for e in tag_errors if cucumber_checks.MISSING_CLIENT_TAG in e]
    errors = [e for e in tag_errors if e not in retroactive]
    selection = {"attendu": cucumber_verify.expected_count(texts.values(), "", cucumber_a_revoir.a_revoir_tag()),
                 "nombre_attendu": 0, "environnement": False, "retroactif": retroactive}
    if retroactive:
        print(f"[CUCUMBER] ⚠️ {len(retroactive)} scénario(s) écrit(s) avant la règle « tag client » sans le tag "
              f"{client_tag} : correction automatique proposée (onglet Cucumber).")
    if dry_run_cmd and texts:
        try:
            dry = cucumber_checks.selection_check(cucumber_tools.build_dir(client, worktree, cucumber_std_dir),
                                                  dry_run_cmd, texts, client_tag)
        except RuntimeError as e:
            dry = {"erreurs": [str(e)], "environnement": True}
        selection.update({k: v for k, v in dry.items() if k != "erreurs"})
        # Dry-run impossible (environnement Maven) : signalé sans bloquer la livraison
        errors += [] if dry.get("environnement") else dry["erreurs"]
        for error in dry["erreurs"] if dry.get("environnement") else []:
            print(f"[CUCUMBER] ⚠️ Dry-runs de sélection non concluants (environnement) : {error}")
    selection["erreurs"] = errors
    state["selection"] = selection
    cucumber_tools.save_state(client, state)
    for error in errors:
        print(f"[CUCUMBER] ❌ Sélection : {error}")
    if selection["nombre_attendu"]:
        print(f"[CUCUMBER] ✅ Nombre attendu de la livraison : {selection['nombre_attendu']} scénario(s) (deux "
              f"dry-runs, sans filtre et avec {client_tag}).")
    return not errors


def check_cucumber_lot(client: str, label: str, worktree: str, base: str, cucumber_std_dir: str, files: list,
                       compile_cmd: str, dry_run_cmd: str, inspired: list = (), context: dict = None) -> dict:
    """
    Contrôles du worktree après un lot, sans token : fichiers de l'init globale modifiés (erreur bloquante, fichier
    restauré depuis la base), fichiers des features (emplacement, casse du tag client, fichiers de données, copiés
    du Standard s'il les a), puis, avec `context` ({init, java, conventions,
    reference}), ordre d'exécution après l'init du client, purge / init réécrites, codes de référence absents de
    l'init, colonnes de tables inconnues du Standard pour le même step, scénarios dépendant de l'état laissé par un
    autre (nouvelles features), runner doublé ou incomplet, fichiers encore utilisés supprimés, tag client des
    scénarios actifs et désactivés, colonnes de contrôle non mappées par le POJO de réponse du step ; enfin
    compilation (et dry-run) si `compile_cmd`. Renvoie un résultat au format de
    cucumber_tools.verify_build.
    """
    started = time.time()
    files_check = cucumber_checks.check_lot_files(worktree, base, files, client, cucumber_std_dir, inspired,
                                                  (context or {}).get("conventions"))
    for target, source in files_check["copies"]:
        print(f"[LOT {label}] Fichier de données copié du Standard : {target} (depuis {source})")
    for rel in files_check["restaures"]:
        print(f"[LOT {label}] ⛔ Fichier de l'init globale modifié, restauré depuis la base : {rel}")
    errors = list(files_check["erreurs"]) + cucumber_a_revoir.check_deletions(worktree, base)
    if context:
        new = cucumber_checks.new_features(worktree, base, [f for f in files if f.endswith(".feature")])
        errors += cucumber_init_data.check_lot_init(worktree, files, new, context.get("init"))
        if context.get("longueurs"):
            errors += cucumber_rules.check_code_lengths(worktree, sorted(new), context["longueurs"])
        errors += cucumber_verify.check_scenario_dependencies(worktree, sorted(
            f for f in new if os.path.exists(os.path.join(worktree, *f.split("/")))))
        # Règle du témoin positif : un scénario négatif sans scénario où l'action a lieu est une erreur de lot
        errors += cucumber_verify.check_positive_witness(worktree, sorted(
            f for f in new if os.path.exists(os.path.join(worktree, *f.split("/")))))
        errors += cucumber_verify.check_table_columns(worktree, base, [f for f in files if f.endswith(".feature")
                                                     and os.path.exists(os.path.join(worktree, *f.split("/")))],
                                                     context.get("reference"))
        errors += cucumber_tools.lot_runner_errors(worktree, base, files, context.get("java"),
                                                   context.get("conventions") or {})
        conventions = context.get("conventions") or {}
        present = [f for f in files if f.endswith(".feature") and os.path.exists(os.path.join(worktree, *f.split("/")))]
        if conventions.get("client_tag"):
            errors += cucumber_checks.check_client_tags(
                {f: cucumber_tools._read_text_file(os.path.join(worktree, *f.split("/"))) for f in present},
                conventions["client_tag"])
        errors += cucumber_versions.check_response_fields(worktree, base, present, cucumber_std_dir,
                                                          conventions.get("library_version", ""))
    if errors:
        print(f"[LOT {label}] Fichiers : {len(errors)} erreur(s) ({time.time() - started:.0f} s)")
        return {"ok": False, "etape": "fichiers", "erreurs": errors, "environnement": False}
    if not compile_cmd:
        return {"ok": True, "etape": "fichiers", "erreurs": [], "environnement": False}
    features = [f for f in files if f.endswith(".feature")]
    try:
        build = cucumber_tools.build_dir(client, worktree, cucumber_std_dir)
    except RuntimeError as e:
        return {"ok": False, "etape": "compilation", "erreurs": [str(e)], "environnement": True}
    check = cucumber_tools.verify_build(build, compile_cmd, features, dry_run_cmd)
    status = "OK" if check["ok"] else (
        "échec hors code (environnement)" if check["environnement"] else f"{len(check['erreurs'])} erreur(s)")
    print(f"[LOT {label}] {check['etape'].capitalize()} : {status} ({time.time() - started:.0f} s)")
    if check["environnement"]:
        print("\n".join(f"    {e}" for e in check["erreurs"]))
    return check


def inspired_of(results: list) -> list:
    """Scénarios Standard dont le lot s'inspire (« inspire_de » du JSON final) : guident la copie des fichiers."""
    return [str(p) for r in results or [] for p in r.get("inspire_de") or [] if p]


def verify_cucumber_lot(client: str, label: str, worktree: str, base: str, cucumber_std_dir: str, files: list,
                        compile_cmd: str, dry_run_cmd: str, values: dict, model: str, work_key: str, run_dir: str,
                        on_usage, inspired: list = (), context: dict = None, db_log: list = None,
                        lot: dict = None) -> dict:
    """
    Contrôles des fichiers (check_cucumber_lot, avec `context` : init du client et runner, fichiers de données du Standard copiés), puis compilation (et
    dry-run) si `compile_cmd`, en Python ; en cas d'erreurs dans les fichiers ou le code, au plus
    CUCUMBER_FIX_ATTEMPTS appels de correction limités (erreurs, fichiers du lot, catalogue Java) avec le modèle de
    correction, puis une dernière avec le modèle du lot (cucumber_fix_plan). Renvoie le dernier résultat de
    cucumber_tools.verify_build, avec "quota" si la limite de session a été atteinte. `db_log` : commandes base
    des corrections (lot["base"]).
    """
    plan = cucumber_fix_plan(model, _env_int("CUCUMBER_FIX_ATTEMPTS", 2))
    attempts, violations = len(plan), []
    fix_turns = str(_env_int("CUCUMBER_FIX_MAX_TURNS", 10))
    for attempt in range(attempts + 1):
        check = check_cucumber_lot(client, label, worktree, base, cucumber_std_dir, files, compile_cmd, dry_run_cmd,
                                   inspired, context)
        if check["ok"] or check["environnement"] or attempt == attempts:
            return check
        fix_model, effort = plan[attempt]
        resumed = bool(lot and lot.get("session") and resume_lot_session())
        print(f"[LOT {label}] Correction {attempt + 1}/{attempts} "
              + ("(reprise de la session du lot)..." if resumed else f"({fix_model}, effort {effort or 'défaut'})..."))
        prompt = render_prompt("cucumber_client_fix", **{"logs_block": "", "regle_spec_code": cucumber_decisions.spec_code_rule(), **values}, lot_label=label, etape=check["etape"].upper(),
                               erreurs="\n".join(f"- {e}" for e in check["erreurs"] + violations),
                               fichiers="\n".join(f"- {f}" for f in files))
        before = cucumber_a_revoir.snapshot(worktree, files)
        returncode, _, usage, limit_hit, fix_model, effort = cucumber_followup(
            lot if resumed else None, prompt, fix_model, effort, lot_tools(values), fix_turns, run_dir,
            db_log=db_log, db_step=f"correction ({check['etape']})")
        report_token_usage(work_key, "cucumber-fix", fix_model, usage, len(prompt), returncode, effort)
        on_usage(usage)
        if limit_hit:
            return {**check, "quota": True}
        violations = cucumber_a_revoir.check_corrections(worktree, before)
        for error in violations:
            print(f"[LOT {label}] ⛔ {error}")
        files = list(dict.fromkeys(files + cucumber_tools.lot_files(worktree, [])))
    return check


def print_profile(profile: dict) -> None:
    """Profil client appliqué, en résumé, et ses avertissements (champs vides, valeurs déduites)."""
    codes = ", ".join(f"{len(v)} {cucumber_init_data.LABELS.get(k, k)}"
                      for k, v in (profile.get("reference_codes") or {}).items() if v) or "aucun code"
    print(f"[CUCUMBER PROFIL] Racine {profile['features_root'] or '?'} ; tag {profile['tag_client'] or '?'} ; init "
          f"{', '.join(profile['init_features']) or 'aucune'} ({profile['init_tags'] or 'sans tag'}) ; clear "
          f"{profile['clear_name'] or '?'} ; {len(profile['spec_files'])} fichier(s) du module spécifique ; runner "
          f"{(profile['runner'] or {}).get('classe') or '?'} ; {codes} ; bibliothèque "
          f"{profile['library_version'] or '?'} ; serveur {profile['server_version'] or '?'}")
    for warning in profile["avertissements"]:
        print(f"[CUCUMBER PROFIL] ⚠️ {warning}")


def detect_cucumber_profile(client: str, cucumber_repo_dir: str, base: str, local_cucumber_std_repo: str,
                            odatio_version: str) -> dict:
    """Profil client détecté dans le dépôt <client>-cucumber à `base` (profil.json pour l'IHM) ; {} en cas d'erreur."""
    try:
        runners = cucumber_tools.standard_java_catalogue(local_cucumber_std_repo, odatio_version).get("runners", [])
        profile = cucumber_profile.detect_in_base(client, cucumber_repo_dir, base, local_cucumber_std_repo, runners,
                                                  odatio_version)
    except RuntimeError as e:
        print(f"[CUCUMBER PROFIL] ❌ Détection impossible : {e}")
        return {}
    print_profile(profile)
    print(f"[CUCUMBER PROFIL] Profil : {cucumber_profile.profile_path(client)}")
    return profile


def std_features_dir(local_cucumber_std_repo: str) -> str:
    return os.path.join(local_cucumber_std_repo, *cucumber_init.RESOURCES_DIR.split("/"), "features") \
        if local_cucumber_std_repo else ""


def write_known_values(client: str, profile: dict, inventory: dict, worktree: str, local_cucumber_std_repo: str) -> str:
    """valeurs_connues.md des lots (index des 6 sources recalculé sur le worktree), chemin renvoyé."""
    index = cucumber_testability.known_values(client, profile, inventory, worktree,
                                              std_features_dir(local_cucumber_std_repo),
                                              cucumber_testability.document_paths(client))
    cucumber_testability.save_index(client, index)
    path = os.path.join(cucumber_tools.cache_dir(client), "valeurs_connues.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(cucumber_testability.index_markdown(index))
    print(f"[CUCUMBER] Valeurs connues (6 sources) : {len(index)} valeur(s) — {path}")
    return path


def detect_init_facts(client: str, cucumber_repo_dir: str, base: str, local_cucumber_std_repo: str,
                      odatio_version: str) -> dict:
    """
    Profil client puis faits de l'initialisation des données, relevés dans le worktree détaché du dépôt client à
    `base` (lecture seule). Lève RuntimeError si le profil ne peut pas être détecté.
    """
    if not detect_cucumber_profile(client, cucumber_repo_dir, base, local_cucumber_std_repo, odatio_version):
        raise RuntimeError("profil client non détecté (worktree du dépôt client impossible à créer)")
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))
    return cucumber_detection.init_facts(cucumber_profile.base_worktree_dir(client), client, profile,
                                         cucumber_tools.load_inventory(client))


def current_init_status(client: str, facts: dict) -> dict:
    """État de l'init d'après les faits, le profil appliqué et le dernier run importé."""
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))
    index = cucumber_import.load_index(client)
    numero = max((e["numero"] for e in index.get("imports") or []), default=0)
    analysis = cucumber_tools._read_json(os.path.join(cucumber_import.imports_dir(client), str(numero),
                                                      "analyse.json"), {}) if numero else {}
    return cucumber_detection.init_status(facts, profile, client, analysis or {}, cucumber_import.imports_dir(client))


def print_init_status(client: str, facts: dict) -> dict:
    status = current_init_status(client, facts)
    print(f"[CUCUMBER DÉTECTION] Initialisation des données : {cucumber_detection.state_label(status)}")
    for finding in status["constats"]:
        print(f"  - {finding['libelle']} : {finding['message']}")
        for proof in finding["preuves"][:3]:
            print(f"      {cucumber_detection.proof_line(proof)}")
    return status


def initializer_sources(client_root: str, items: list, target_dir: str) -> dict:
    """Sources des initializers des données à recréer, copiées dans `target_dir` pour être lues : {classe: copie}."""
    repos, out = cucumber_tools.git_repos(client_root), {}
    for item in items:
        classe = item["donnee"].get("classe", "")
        repo = repos.get(item["donnee"].get("depot", ""))
        if not classe or classe in out or not repo:
            continue
        found = cucumber_tools._git(repo, "ls-files", f"*/{classe}.java").stdout.splitlines()
        if found:
            target = os.path.join(target_dir, f"{classe}.java")
            os.makedirs(target_dir, exist_ok=True)
            shutil.copyfile(os.path.join(repo, *found[0].split("/")), target)
            out[classe] = target
    return out


def recreate_initializer_data(client: str, worktree: str, items: list, client_root: str, cucumber_std_dir: str,
                              profile: dict) -> list:
    """
    Steps du projet de recréation des données d'initializers (point 5) : un appel Claude (cucumber_init_recreation)
    puis au plus CUCUMBER_FIX_ATTEMPTS corrections, chacune contrôlée en Python (cucumber_recreation.check_project_steps,
    puis compilation et dry-run des features d'init si CUCUMBER_COMPILE_CMD). Renvoie les erreurs restantes.
    """
    project = [e for e in items if e["mode"] == "projet"]
    run_dir = cucumber_tools.cache_dir(client)
    sources = initializer_sources(client_root, project, os.path.join(run_dir, "initializers"))
    std_java = cucumber_tools.standard_java_catalogue(cucumber_std_dir, profile.get("server_version", ""))
    std_expressions = cucumber_versions.std_snapshot(cucumber_std_dir, "HEAD")["steps"]
    features = init_features(worktree)
    lines = [f"- « {e['expression']} » : {e['donnee'].get('entite')} {e['donnee']['code']} — initializer "
             f"{e['donnee'].get('classe')} ({e['donnee'].get('depot')}) ; source : "
             f"'{sources.get(e['donnee'].get('classe'), 'introuvable : Grep dans le dépôt ' + e['donnee'].get('depot', '?'))}'"
             for e in project]
    values = {"client_name": client, "worktree": worktree, "java_path": cucumber_recreation.java_path(client),
              "package": f"cucumber.features.{cucumber_tools.java_package_segment(client)}",
              "clear_step_java": os.path.join(worktree, *cucumber_init.clear_step_path(client).split("/")),
              "java_std": std_java["java_std"], "steps_example": std_java["steps_example"] or "aucune",
              "donnees": "\n".join(lines)}
    return write_init_steps(client, worktree, "cucumber_init_recreation", values,
                            lambda: cucumber_recreation.check_project_steps(worktree, items, std_expressions),
                            features, cucumber_std_dir, "Steps de recréation", "cucumber-init-recreation")


def init_features(worktree: str) -> list:
    """Features d'init du worktree d'initialisation (chemins relatifs)."""
    return [os.path.relpath(os.path.join(d, n), worktree).replace("\\", "/")
            for d, _, names in os.walk(os.path.join(worktree, *cucumber_init.RESOURCES_DIR.split("/"), "features"))
            for n in names if n.endswith(".feature") and cucumber_init.INIT_DIR in d]


# Points restants et dépendances non vérifiables par l'API déclarés par Claude à la dernière écriture de steps
# d'init, par libellé (résultat de la préparation)
INIT_REMAINING, INIT_UNVERIFIABLE = {}, {}


def write_init_steps(client: str, worktree: str, prompt_name: str, values: dict, check, features: list,
                     cucumber_std_dir: str, label: str, usage_step: str) -> list:
    """
    Steps du projet de l'init écrits par un appel Claude (`prompt_name`), puis au plus CUCUMBER_FIX_ATTEMPTS
    corrections, chacune contrôlée en Python (`check()`, puis compilation et dry-run des features d'init si
    CUCUMBER_COMPILE_CMD). Renvoie les erreurs restantes.
    """
    run_dir = cucumber_tools.cache_dir(client)
    compile_cmd = os.getenv("CUCUMBER_COMPILE_CMD")
    compile_cmd = cucumber_tools.DEFAULT_COMPILE_CMD if compile_cmd is None else compile_cmd.strip()
    model = os.getenv("CLAUDE_MODEL_CUCUMBER", "").strip() or "sonnet"
    errors = []
    for attempt in range(1 + _env_int("CUCUMBER_FIX_ATTEMPTS", 2)):
        prompt = render_prompt(prompt_name, **values,
                               erreurs="\n".join(f"- {e}" for e in errors) or "aucune (première tentative)")
        effort = cucumber_fix_effort() if attempt else claude_effort("CLAUDE_EFFORT_CUCUMBER")
        returncode, output, usage, limit_hit = run_cucumber_claude(
            prompt, model, CUCUMBER_TOOLS, str(_env_int("CUCUMBER_FIX_MAX_TURNS", 10) * 2), run_dir, effort)
        report_token_usage(f"{client}-init", usage_step, model, usage, len(prompt), returncode, effort)
        final = cucumber_diagnostic.parse_fix_result(output)
        INIT_REMAINING[label] = final.get("restantes") or []
        INIT_UNVERIFIABLE[label] = final.get("non_verifiables") or []
        if limit_hit:
            return errors + [f"quota ou budget atteint pendant l'écriture ({label.lower()})"]
        errors = check()
        if not errors and compile_cmd:
            try:
                build = cucumber_tools.verify_build(cucumber_tools.build_dir(client, worktree, cucumber_std_dir),
                                                    compile_cmd, features, cucumber_tools.DEFAULT_DRY_RUN_CMD)
            except RuntimeError as e:
                build = {"ok": False, "environnement": True, "erreurs": [str(e)]}
            if not build["ok"] and not build.get("environnement"):
                errors = build["erreurs"]
            elif not build["ok"]:
                print(f"[CUCUMBER INIT] ⚠️ {label} : compilation non vérifiée (environnement Maven) : "
                      f"{build['erreurs'][0] if build['erreurs'] else '?'}")
        print(f"[CUCUMBER INIT] {label}, tentative {attempt + 1} : "
              + ("OK" if not errors else f"{len(errors)} erreur(s) — {errors[0]}"))
        if not errors:
            return []
    return errors


def write_environment_steps(client: str, worktree: str, checks: list, recreations: list, cucumber_std_dir: str,
                            profile: dict) -> list:
    """
    Steps du projet de l'init qui recréent les corrections manuelles de l'environnement et contrôlent ses
    dépendances en lecture seule (points 9g, 9h, cucumber_environment). Renvoie les erreurs restantes.
    """
    std_java = cucumber_tools.standard_java_catalogue(cucumber_std_dir, profile.get("server_version", ""))
    std_expressions = cucumber_versions.std_snapshot(cucumber_std_dir, "HEAD")["steps"]
    controls, manual = cucumber_environment.prompt_items(checks, recreations)
    values = {"client_name": client, "worktree": worktree, "java_path": cucumber_environment.java_path(client),
              "package": f"cucumber.features.{cucumber_tools.java_package_segment(client)}",
              "clear_step_java": os.path.join(worktree, *cucumber_init.clear_step_path(client).split("/")),
              "java_std": std_java["java_std"], "steps_example": std_java["steps_example"] or "aucune",
              "check_expression": cucumber_environment.check_expression(client),
              "recreation_expression": cucumber_environment.recreation_expression(client),
              "controles": controls, "recreations": manual}
    label = "Steps d'environnement"
    INIT_UNVERIFIABLE.pop(label, None)
    errors = write_init_steps(client, worktree, "cucumber_init_environnement", values,
                              lambda: cucumber_environment.check_steps(worktree, client, checks, recreations,
                                                                       std_expressions)
                              + cucumber_environment.unconfirmed_failures(worktree, client),
                              init_features(worktree), cucumber_std_dir, label, "cucumber-init-environnement")
    # Dépendances que l'API ne permet pas de vérifier : retirées du contrôle, avertissement non bloquant
    return {"erreurs": errors, "non_verifiables": INIT_UNVERIFIABLE.get(label) or []}


def run_cucumber_init(args, local_client_repo: str, local_cucumber_std_repo: str, cucumber_repo_dir: str,
                      base: str, odatio_version: str = "") -> int:
    """
    Lot d'initialisation des données (patch séparé), selon l'état détecté de l'init du client : absente,
    Init_<MODULE> du Cucumber Standard copiées pour les modules détectés dans les dépôts et l'inventaire du client
    (ou --init-modules, qui force cette copie) ; incomplète, compléments seuls (run_cucumber_init_complements) ;
    complète, rien (0 token). Seule exception : les steps du projet (données d'initializers effacées par le clear
    sans step du Standard reconnu, contrôle de l'environnement), écrits par un appel Claude borné. Renvoie le code
    de sortie.
    """
    code = _run_cucumber_init(args, local_client_repo, local_cucumber_std_repo, cucumber_repo_dir, base,
                              odatio_version)
    if code and not (cucumber_tools.load_state(args.client_name).get("preparation") or {}).get("date", "") >= \
            INIT_STARTED[0]:
        cucumber_results.record_preparation(args.client_name, False, "La préparation a échoué : voir le journal "
                                            "technique (mode expert).")
    return code


INIT_STARTED = [""]


def _run_cucumber_init(args, local_client_repo: str, local_cucumber_std_repo: str, cucumber_repo_dir: str,
                       base: str, odatio_version: str = "") -> int:
    client = args.client_name
    INIT_STARTED[0] = datetime.now().isoformat(timespec="seconds")
    if not args.init_modules.strip():
        # Lot d'initialisation selon l'état détecté : init absente -> init complète, incomplète -> compléments seuls
        subprocess.run(["git", "-C", cucumber_repo_dir, "fetch", "origin", "--prune"], capture_output=True)
        try:
            facts = detect_init_facts(client, cucumber_repo_dir, base, local_cucumber_std_repo, odatio_version)
        except RuntimeError as e:
            print(f"[CUCUMBER INIT] ❌ {e}")
            cucumber_results.record_preparation(client, False, f"La préparation a échoué : {e}")
            return 1
        status = print_init_status(client, facts)
        if status["etat"] == "complete":
            print("[CUCUMBER INIT] Initialisation complète : aucun lot d'initialisation à générer.")
            cucumber_results.record_preparation(client, True, "Les données de test sont déjà complètes : rien à "
                                                "préparer.")
            return 0
        if status["etat"] == "incomplete":
            return run_cucumber_init_complements(client, local_client_repo, local_cucumber_std_repo,
                                                 cucumber_repo_dir, base, facts, status)
    if args.init_modules.strip():
        modules = {m.strip().upper() for m in args.init_modules.split(",") if m.strip()}
        unknown = modules - set(cucumber_init.INIT_MODULES)
        if unknown:
            print(f"❌ Module(s) inconnu(s) : {', '.join(sorted(unknown))} (attendus : "
                  f"{', '.join(cucumber_init.INIT_MODULES)}).")
            return 2
        print(f"[CUCUMBER INIT] Modules choisis : {', '.join(sorted(modules))}")
    else:
        modules = cucumber_init.client_modules(local_client_repo, cucumber_tools.load_inventory(client))
        print(f"[CUCUMBER INIT] Modules détectés : {', '.join(sorted(modules)) or 'aucun'}")
    if not modules:
        print("[CUCUMBER INIT] ❌ Aucun module WMS, TMS ou OMS : cochez les modules du client. Aucun patch produit.")
        return 2
    subprocess.run(["git", "-C", cucumber_repo_dir, "fetch", "origin", "--prune"], capture_output=True)
    try:
        profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))
        # Points 9g, 9h : dépendances d'environnement contrôlées et corrections manuelles recréées par l'init
        environment = {"controles": cucumber_environment.dependencies(profile, cucumber_import.imports_dir(client)),
                       "recreations": [m["description"] for m in cucumber_bilan.manual_corrections(client)],
                       "writer": lambda worktree, checks, manual: write_environment_steps(
                           client, worktree, checks, manual, local_cucumber_std_repo, profile)}
        result = cucumber_init.build_client_init(
            cucumber_repo_dir, local_cucumber_std_repo, client, base, modules, profile,
            lambda worktree, items: recreate_initializer_data(client, worktree, items, local_client_repo,
                                                              local_cucumber_std_repo, profile), environment)
    except RuntimeError as e:
        print(f"[CUCUMBER INIT] ❌ {e}")
        return 1
    for warning in result.get("avertissements", []):
        print(f"[CUCUMBER INIT] ⚠️ {warning}")
    for note in result.get("todo_version", []):
        print(f"[CUCUMBER INIT] ⚠️ {cucumber_versions.TODO_MARKER} {note}")
    recreation = result.get("recreation") or {}
    if recreation.get("standard") or recreation.get("projet"):
        print(f"[CUCUMBER INIT] Données d'initializers recréées : {recreation['standard']} par un step du Standard, "
              f"{recreation['projet'] - len(recreation['non_livrees'])} par un step du projet"
              + (f", {len(recreation['non_livrees'])} non livrée(s) (commentées)" if recreation["non_livrees"] else ""))
    env = result.get("environnement") or {}
    if env.get("controles") or env.get("recreations"):
        print(f"[CUCUMBER INIT] Environnement : {env['controles']} dépendance(s) contrôlée(s) en lecture seule, "
              f"{env['recreations']} correction(s) manuelle(s) recréée(s) — "
              + ("steps livrés" if env.get("livres") else f"non livrés ({cucumber_environment.TODO_MARKER})"))
    if result["absents"]:
        print(f"[CUCUMBER INIT] ⚠️ Pas d'initialisation Standard pour : {', '.join(result['absents'])}")
    if not result["modules"]:
        print("[CUCUMBER INIT] ❌ Aucune initialisation Standard pour ces modules. Aucun patch produit.")
        return 1
    print(f"[CUCUMBER INIT] Modules retenus : {', '.join(result['modules'])} ({len(result['fichiers'])} fichier(s))")
    if not result["patch"]:
        print(f"[CUCUMBER INIT] Le dépôt {args.cucumber_repo} contient déjà cette initialisation : aucun patch produit.")
        cucumber_results.record_preparation(client, True, "Le dépôt contient déjà cette initialisation : rien à "
                                            "ajouter.", remaining=result.get("avertissements") or [])
        return 0
    tags = ", ".join(f"@INIT_{cucumber_tools.java_package_segment(client).upper()}_{m}" for m in result["modules"])
    cucumber_results.record_preparation(
        client, True, f"Initialisation des données de test écrite pour {', '.join(result['modules'])} "
                      f"({len(result['fichiers'])} fichier(s)) ; à appliquer au dépôt puis à lancer module par module "
                      f"({tags}).", result["fichiers"], result["patch"],
        (result.get("avertissements") or []) + [r for notes in INIT_REMAINING.values() for r in notes])
    print(f"[CUCUMBER INIT] Patch : {result['patch']}")
    print(f"[CUCUMBER INIT] Une initialisation par module, chacune purge l'environnement : lancez-les séparément "
          f"({tags}).")
    return 0


def run_cucumber_init_complements(client: str, local_client_repo: str, local_cucumber_std_repo: str,
                                  cucumber_repo_dir: str, base: str, facts: dict, status: dict) -> int:
    """
    Lot « Compléments d'initialisation » (init incomplète) : éléments manquants livrables dans une nouvelle feature,
    patch séparé ; les autres constats sont affichés avec leur preuve, à traiter à la main. Renvoie le code de sortie.
    """
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))
    try:
        result = cucumber_init.build_init_complements(
            cucumber_repo_dir, local_cucumber_std_repo, client, base, profile, status,
            [f["chemin"] for f in facts.get("features") or []], cucumber_import.imports_dir(client),
            lambda worktree, items: recreate_initializer_data(client, worktree, items, local_client_repo,
                                                              local_cucumber_std_repo, profile),
            lambda worktree, checks, manual: write_environment_steps(client, worktree, checks, manual,
                                                                     local_cucumber_std_repo, profile))
    except RuntimeError as e:
        print(f"[CUCUMBER INIT] ❌ {e}")
        cucumber_results.record_preparation(client, False, f"La préparation a échoué : {e}")
        return 1
    remaining = (result["avertissements"] + [r for notes in INIT_REMAINING.values() for r in notes]
                 + [f"À traiter à la main — {f['libelle']} : {f['message']}" for f in result["manuels"]])
    for warning in result["avertissements"]:
        print(f"[CUCUMBER INIT] ⚠️ {warning}")
    for finding in result["manuels"]:
        print(f"[CUCUMBER INIT] À traiter à la main (fichier protégé ou preuve de run) — {finding['libelle']} : "
              f"{finding['message']}")
        for proof in finding["preuves"][:3]:
            print(f"    {cucumber_detection.proof_line(proof)}")
    if not result["patch"]:
        print("[CUCUMBER INIT] Aucun complément livrable : aucun patch produit.")
        cucumber_results.record_preparation(client, True, "Aucun complément ne peut être ajouté automatiquement : "
                                            "les points restants sont à traiter à la main.", remaining=remaining)
        return 0
    cucumber_results.record_preparation(
        client, True, f"Compléments des données de test écrits ({len(result['fichiers'])} fichier(s)) ; l'init "
                      f"existante n'est pas modifiée. À appliquer au dépôt (patch).", result["fichiers"],
        result["patch"], remaining)
    print(f"[CUCUMBER INIT] Compléments d'initialisation : {', '.join(result['fichiers'])}")
    print(f"[CUCUMBER INIT] Patch : {result['patch']}")
    return 0


def run_cucumber_import(client: str, paths: list, build_url: str = "", project: str = "") -> int:
    """
    Import d'un rapport d'exécution (0 token, cucumber_import) : fichiers lus et masqués en mémoire, ou build
    Jenkins ; analyse affichée, écrite dans imports/<n>/analyse.md et déposée dans les connaissances partagées.
    Renvoie 0 si l'import est analysé, 1 sinon, 2 si rien n'a été importé.
    """
    files = {}
    for path in paths:
        try:
            with open(path, "rb") as f:
                files[os.path.basename(path)] = f.read()
        except OSError as e:
            print(f"[CUCUMBER IMPORT] ❌ {path} illisible : {e}")
    if build_url:
        fetched = cucumber_import.build_files(build_url)
        print(f"[CUCUMBER IMPORT] Build {build_url} : {', '.join(fetched) or 'rien de lisible'}")
        files.update(fetched)
    imported = cucumber_import.import_files(client, files, build_url)
    for message in imported["messages"]:
        print(f"[CUCUMBER IMPORT] {message}")
    if not imported["numero"]:
        print("[CUCUMBER IMPORT] Rien de nouveau à importer.")
        return 2
    analysis = cucumber_import.analyse(client, imported["numero"], cucumber_tools.load_state(client),
                                       cucumber_tools.worktree_dir(client))
    print_import(analysis)
    # Bilan (défauts en tête, corrigés, désactivés, doutes, corrections manuelles, prochaine action) : IHM,
    # A_REVOIR.md du worktree et répertoire partagé
    bilan = cucumber_bilan.after_import(client, analysis, cucumber_tools.load_state(client),
                                        cucumber_tools.worktree_dir(client))
    print(f"[CUCUMBER IMPORT] Bilan : {bilan['fichier']}" + (" ; A_REVOIR.md mis à jour et commité"
                                                             if bilan["a_revoir"] else ""))
    print(f"[CUCUMBER IMPORT] Prochaine action : {bilan['prochaine_action']}")
    if knowledge_tools.enabled():
        print(knowledge_tools.publish_line(cucumber_bilan.share_bilan(client, bilan, project)))
    return 1 if analysis["non_analyse"] else 0


def preview_cucumber_jenkins(client: str, tags: str, repository: str = "", base_branch: str = "", push: bool = True,
                             branch: str = "") -> int:
    """Aperçu du run Jenkins (cucumber_run.preview), sans push ni lancement ; 0 si le run serait accepté, sinon 2."""
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))
    out = cucumber_run.preview(client, cucumber_tools.worktree_dir(client), profile, tags, repository, base_branch,
                               push, branch)
    if not push:
        print("[RUN JENKINS — APERÇU] Sans push : la branche est exécutée telle qu'elle est sur le dépôt client.")
    print(f"[RUN JENKINS — APERÇU] Job : {out['job'] or '(non renseigné)'} | branche de travail : "
          f"{out['branche'] or '(non renseignée)'} | tags : {tags or '(aucun : tous les scénarios actifs)'}")
    if out.get("cible_tags"):
        print(f"[RUN JENKINS — APERÇU] Filtre de tags envoyé dans : {out['cible_tags']}")
    print("[RUN JENKINS — APERÇU] Rôles des paramètres : "
          + (", ".join(f"{role} → {name}" for role, name in out["roles"].items()) or "aucun détecté"))
    for name, value in out["parametres"].items():
        print(f"    {name} = {value}" + ("   (imposé par le profil)" if name in out.get("imposes", []) else ""))
    if out["attendus"] is not None:
        print(f"[RUN JENKINS — APERÇU] Tests attendus pour ces tags dans le worktree : {out['attendus']}")
    for warning in out["avertissements"]:
        print(f"[RUN JENKINS — APERÇU] ⚠️ {warning}")
    for error in out["erreurs"]:
        print(f"[RUN JENKINS — APERÇU] ⛔ {error}")
    print(f"[RUN JENKINS — APERÇU] {'✅ Le run serait lancé avec ces paramètres' if out['ok'] else '⛔ Le run serait refusé'}"
          " (rien n'a été poussé ni lancé).")
    return 0 if out["ok"] else 2


def run_cucumber_jenkins(client: str, tags: str, project: str = "", repository: str = "", base_branch: str = "",
                         client_root: str = "", push: bool = True, branch: str = "") -> int:
    """
    Run Jenkins automatique (cucumber_run) : push du worktree sur la branche de travail lue par le job (jamais la
    branche d'intégration `base_branch`), contrôle du commit, lancement avec le dépôt client `repository` et le
    filtre de tags, suivi, version testée comparée aux commits des tâches (dépôts de `client_root`), puis import de
    la console et des rapports du build (analyse, bilan). Renvoie le code de run_cucumber_import, 2 si rien n'a été
    lancé ou si le build n'a pas abouti.
    """
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))
    run = cucumber_run.launch(client, cucumber_tools.worktree_dir(client), profile, tags, repository=repository,
                              base_branch=base_branch, inventory=cucumber_tools.load_inventory(client),
                              client_root=client_root, push=push, branch=branch)
    print(f"[RUN JENKINS] {'✅' if run['ok'] else '⛔'} {run['message']}" + (f" — {run['build']}" if run["build"] else ""))
    if not run["ok"]:
        return 2
    return run_cucumber_import(client, [], run["build"], project)


def print_import(analysis: dict) -> None:
    """Résumé console d'une analyse d'import."""
    label = f"[CUCUMBER IMPORT] Import n° {analysis['numero']}"
    if analysis["non_analyse"]:
        print(f"{label} ⛔ non analysé : {analysis['non_analyse']}")
        return
    failures = sum(len(g["scenarios"]) for g in analysis["groupes"])
    print(f"{label} : {analysis['tests']} test(s), {failures} échec(s), {len(analysis['groupes'])} cause(s) racine.")
    for group in analysis["groupes"][:10]:
        print(f"    {len(group['scenarios'])} × {group['libelle']} ({group['categorie']}) — {group['step']} — "
              f"{group['message'][:160]}")
    init = analysis.get("init_en_echec") or {}
    if init:
        print(f"{label} ⛔ initialisation en échec, aucun scénario jugé : {init['id']} — {init['step']} — "
              f"{init['message'][:200]}")
    for warning in analysis.get("avertissements") or []:
        print(f"{label} ⚠️ {warning}")
    comparison = analysis.get("comparaison") or {}
    if comparison:
        print(f"{label} : par rapport à l'import n° {comparison['import']} — {len(comparison['corriges'])} corrigé(s), "
              f"{len(comparison['nouveaux'])} nouveau(x), {len(comparison.get('regressions', []))} régression(s), "
              f"{len(comparison['inchanges'])} inchangé(s), {len(comparison['disparus'])} disparu(s).")
    for slow in ((analysis.get("durees") or {}).get("lents") or [])[:10]:
        print(f"{label} ⏱️ {slow['id']} : {slow['motif']}")
    for entry in (e for e in analysis["logs"] if e["a_exporter"]):
        print(f"{label} : logs à exporter pour {entry['id']} : {entry['a_exporter']}")
    if analysis["propositions"]:
        print(f"{label} : {len(analysis['propositions'])} lot(s) de correction proposé(s) ("
              f"{sum(len(p['scenarios']) for p in analysis['propositions'])} scénario(s)) : --complete --from-import "
              f"{analysis['numero']} pour les lancer.")
    print(f"{label} : rapport analyse.md dans le dossier de l'import.")


def enforce_db_proofs(worktree: str, lot: dict, data: list, fixed: dict, before: dict, db_start: int,
                      label: str) -> list:
    """
    Corrections d'échecs de données sans preuve en base (cucumber_db_proof.check) : features rétablies, scénario
    gardé dans « restantes ». Preuves acceptées enregistrées dans lot["correction"]["preuves_base"] (bilan).
    Renvoie les scénarios refusés.
    """
    changed = {rel for rel, text in before.items()
               if cucumber_tools._read_text_file(os.path.join(worktree, *rel.split("/"))) != text}
    result = cucumber_db_proof.check(data, fixed, (lot.get("base") or [])[db_start:], changed)
    for rel in result["fichiers"]:
        with open(os.path.join(worktree, *rel.split("/")), "w", encoding="utf-8", newline="") as f:
            f.write(before[rel])
    correction = lot.setdefault("correction", {})
    correction["preuves_base"] = list(result["preuves"].values())[:30]
    for s in result["refuses"]:
        reason = (f"{s.get('id')} : correction refusée, échec lié aux données sans preuve en base (requête de la "
                  f"donnée cherchée par le step, tables réellement lues) — {s.get('message', '')[:160]}")
        correction.setdefault("restantes", []).append(reason)
        print(f"[LOT {label}] ⛔ {reason}")
    if result["preuves"]:
        print(f"[LOT {label}] {len(result['preuves'])} correction(s) d'échec de données prouvée(s) en base.")
    return result["refuses"]


def run_correction_lot(lot: dict, label: str, worktree: str, values: dict, model: str, work_key: str,
                       run_dir: str, on_usage) -> list:
    """
    Lot « correction » (import d'un rapport d'exécution) : un appel de correction avec le message réel de chaque
    scénario (cucumber_client_fix.md), contrôlé (RÈGLE 3c). La compilation, la vérification par exécution et la
    RÈGLE 3 suivent comme pour un lot généré. Renvoie les fichiers du lot.
    """
    files = sorted({s["fichier"] for s in lot["corrections"]})
    lot["avant"] = cucumber_tools.rev_parse(worktree, "HEAD")
    # Scénarios écrits hors de l'agent (sans tag) : pris en charge, tag du lot de correction ajouté
    untagged = [s for s in lot["corrections"] if not s.get("tag")]
    if untagged:
        tagged = cucumber_verify.tag_scenarios(worktree, [{**s, "tags": []} for s in untagged],
                                               values.get("client_name", ""), lot["numero"])
        by_id = {identifier: tag for tag, identifier in tagged.items()}
        for s in untagged:
            s["tag"] = by_id.get(s["id"], "")
        lot["tags"] = {**(lot.get("tags") or {}), **tagged}
        print(f"[LOT {label}] {len(tagged)} scénario(s) écrit(s) hors de l'agent pris en charge (tag ajouté).")
    lines = []
    server_logs = os.path.join(cucumber_import.imports_dir(values.get("client_name", "")), str(lot.get("import")),
                               cucumber_server_logs.EXTRACT_NAME)
    if os.path.exists(server_logs):
        lines.append(f"Logs applicatifs du serveur pendant le run (fenêtre de chaque échec, lignes « [fichier] "
                     f"horodatage … ») : '{server_logs}' — Grep de l'heure du step en échec (HH:MM:SS) ou de la valeur "
                     f"cherchée, puis Read offset / limit, jamais en entier. Une cause prouvée par ces logs cite la "
                     f"ligne mot pour mot.")
    for s in lot["corrections"]:
        lines.append(f"- {s['id']} [tag {s['tag']}] : en échec dans le run importé n° {lot['import']} — {s['step']} "
                     f"— {s['message']}" + (f" (correction n° {s['tentatives'] + 1} de cette cause : change "
                                            f"d'approche)" if s.get("tentatives") else ""))
        hint = cucumber_verify.failure_hint(s["message"])
        lines += [f"  - piste : {hint}"] if hint else []
        lines += [f"  - extrait des logs serveur : {e}" for e in s.get("preuves") or []]
        lines += [f"  - sortie du diagnostic temporaire : {s['diagnostic']}"] if s.get("diagnostic") else []
        lines += [f"  - décision humaine refusée : « {c} »" for c in s.get("refus") or []]
    print(f"[LOT {label}] Correction de {len(lines)} scénario(s) en échec dans l'import n° {lot['import']}.")
    # Échecs liés aux données : preuve en base exigée avant toute correction (si l'accès base est accordé)
    data = cucumber_db_proof.data_scenarios(lot["corrections"]) if cucumber_db_proof.enabled() else []
    db_available = bool(values.get("db_block"))
    if data:
        print(f"[LOT {label}] {len(data)} échec(s) lié(s) aux données : "
              + ("preuve en base exigée." if db_available else "aucun accès base, causes NON PROUVÉES."))
    prompt = render_prompt("cucumber_client_fix", **{"logs_block": "", "regle_spec_code": cucumber_decisions.spec_code_rule(), **values}, lot_label=label, etape="RAPPORT D'EXÉCUTION IMPORTÉ",
                           erreurs="\n".join(lines) + cucumber_db_proof.prompt_block(data, db_available),
                           fichiers="\n".join(f"- {f}" for f in files))
    before = cucumber_a_revoir.snapshot(worktree, files)
    db_start = len(lot.setdefault("base", []))
    effort = cucumber_fix_effort()
    returncode, output, usage, _ = run_cucumber_claude(prompt, model, lot_tools(values),
                                                       str(_env_int("CUCUMBER_FIX_MAX_TURNS", 10)), run_dir, effort,
                                                       db_log=lot.setdefault("base", []),
                                                       db_step="correction d'un run importé")
    report_token_usage(work_key, "cucumber-import-fix", model, usage, len(prompt), returncode, effort)
    # Ce que la correction a fait (bilan après le prochain import) : corrections, restantes, réalignés, constats
    fixed = cucumber_diagnostic.parse_fix_result(output)
    cucumber_tools.normalize_constats(fixed)
    lot["correction"] = {"corrections": [c for c in fixed.get("corrections") or [] if isinstance(c, dict)][:30],
                         "restantes": [str(r) for r in fixed.get("restantes") or []][:30],
                         "realignes": [str(r) for r in fixed.get("realignes") or []][:30],
                         "constats": fixed["constats"]}
    on_usage(usage)
    if data and db_available:
        enforce_db_proofs(worktree, lot, data, fixed, before, db_start, label)
    for error in cucumber_a_revoir.check_corrections(worktree, before):
        print(f"[LOT {label}] ⛔ {error}")
    hold_meaning_changes(values.get("client_name", ""), worktree, before, values.get("client_tag", ""), label, lot)
    return list(dict.fromkeys(files + cucumber_tools.lot_files(worktree, [])))


def fix_own_init(client: str, worktree: str, init: dict, verify_mode: str, init_tags: str, verify_sources: dict,
                 profile: dict, state: dict, cucumber_std_dir: str, std_java: dict, step_code: dict,
                 token_saving_rules: str, python_exe: str, workspace_dir: str, work_key: str,
                 console: str = "") -> dict:
    """
    Correction autonome des compléments d'init de l'agent (cucumber_init_fix) : seulement si l'init échoue dans ses
    propres compléments. Chaque tentative : un appel Claude borné aux fichiers autorisés, contrôles Python
    (fichiers hors liste rétablis, aucun échec sur une dépendance non confirmée, lecture seule, aucun scénario ni
    tag retiré, compilation), commit dédié, push sur la branche de travail, init rejouée seule. Renvoie le dernier
    résultat de l'init ; state["init_corrigee"] : fichiers corrigés (livrés dans le patch, à relire en premier).
    """
    files = cucumber_init_fix.own_files(worktree, client)
    if not (cucumber_init_fix.enabled() and cucumber_init_fix.fixable(init.get("echecs") or [], files)):
        if init.get("echecs") and cucumber_init_fix.enabled():
            print("[CUCUMBER INIT] Échec hors des compléments écrits par l'agent : init du client à corriger à la main.")
        return init
    run_dir = cucumber_tools.cache_dir(client)
    model = os.getenv("CLAUDE_MODEL_CUCUMBER", "").strip() or "sonnet"
    effort = claude_effort("CLAUDE_EFFORT_CUCUMBER")
    compile_cmd = os.getenv("CUCUMBER_COMPILE_CMD")
    compile_cmd = cucumber_tools.DEFAULT_COMPILE_CMD if compile_cmd is None else compile_cmd.strip()
    agent_compile = ""
    if compile_cmd and os.getenv("CUCUMBER_AGENT_COMPILE", "1").strip() != "0":
        cucumber_compile.write_context(client, worktree, cucumber_std_dir, compile_cmd)
        agent_compile = f'"{python_exe}" "{os.path.join(workspace_dir, "tools", "cucumber_compile.py")}" {client}'
    values = {"client_name": client, "token_saving_rules": token_saving_rules, "worktree": worktree,
              "java_std": std_java["java_std"], "sources_std": step_code["sources"],
              "check_expression": cucumber_environment.check_expression(client),
              "recreation_expression": cucumber_environment.recreation_expression(client),
              "compilation_agent": agent_compilation_block(agent_compile),
              "console": console or os.path.join(os.path.dirname(worktree), "verification", "init-console.txt"),
              "fichiers": "\n".join(f"- {f}" for f in files), "tentatives": cucumber_init_fix.attempts(),
              # Lecture seule de la base de test : confirmer une dépendance au lieu de la laisser « non confirmée »
              "db_block": build_db_block("", python_exe, workspace_dir, step="generate-cucumber-client",
                                         profile="cucumber"),
              "logs_block": build_logs_block(python_exe, workspace_dir)}
    errors, corrected = [], state.setdefault("init_corrigee", {"fichiers": [], "tentatives": []})
    for attempt in range(1, cucumber_init_fix.attempts() + 1):
        print(f"[CUCUMBER INIT] Correction des compléments d'init par l'agent, tentative {attempt} : "
              f"{', '.join(files)}")
        before = cucumber_init_fix.snapshot(worktree, files)
        prompt = render_prompt("cucumber_init_correction", **values, tentative=attempt,
                               echecs=cucumber_init_fix.failure_lines(init.get("echecs") or []),
                               erreurs="\n".join(f"- {e}" for e in errors) or "aucune (première tentative)")
        returncode, output, usage, limit_hit = run_cucumber_claude(prompt, model, lot_tools(values),
                                                                   str(_env_int("CUCUMBER_FIX_MAX_TURNS", 10) * 2),
                                                                   run_dir, effort,
                                                                   db_log=corrected.setdefault("base", []),
                                                                   db_step="correction de l'initialisation")
        report_token_usage(work_key, "cucumber-init-fix", model, usage, len(prompt), returncode, effort)
        if limit_hit:
            print("[CUCUMBER INIT] ⛔ Quota ou budget atteint pendant la correction de l'init.")
            break
        result = cucumber_diagnostic.parse_fix_result(output)
        errors = cucumber_init_fix.check_correction(worktree, client, files, before)
        if not errors and compile_cmd:
            try:
                build = cucumber_tools.verify_build(cucumber_tools.build_dir(client, worktree, cucumber_std_dir),
                                                    compile_cmd)
            except RuntimeError as e:
                build = {"ok": False, "environnement": True, "erreurs": [str(e)]}
            if not build["ok"] and not build.get("environnement"):
                errors = build["erreurs"]
        if errors:
            print(f"[CUCUMBER INIT] Correction refusée par les contrôles : {errors[0]}")
            # Fichiers autorisés remis à leur état d'avant l'appel : la tentative suivante repart proprement
            for path, text in before.items():
                with open(os.path.join(worktree, *path.split("/")), "w", encoding="utf-8", newline="") as f:
                    f.write(text)
            continue
        if not cucumber_init_fix.commit(worktree, files, client, attempt):
            print("[CUCUMBER INIT] Aucune modification proposée par l'agent : arrêt.")
            break
        corrected["fichiers"] = sorted(set(corrected["fichiers"]) | {
            p for p in files if cucumber_init_fix.snapshot(worktree, [p])[p] != before.get(p)})
        corrected["tentatives"].append({"tentative": attempt, "cause": result.get("cause", ""),
                                        "corrections": result.get("corrections") or [],
                                        "restantes": result.get("restantes") or []})
        cucumber_tools.save_state(client, state)
        if not verify_mode:
            # Sans exécution par l'agent (correction depuis un import) : correction commitée, à vérifier par le run
            return {"ok": None, "message": "compléments d'init corrigés, à vérifier par un run", "build": "",
                    "corrige": True}
        push_error = cucumber_checks.push_for_verification(
            worktree, profile["verify_branch"], f"Cucumber : correction de l'initialisation ({attempt})")[0] \
            if profile.get("verify_branch") else ""
        if push_error:
            # Correction commitée mais non vérifiable sur Jenkins (réseau, droits) : livrée en patch, à vérifier
            print(f"[CUCUMBER INIT] ⚠️ Init non rejouée : {push_error}")
            return {"ok": None, "message": f"compléments d'init corrigés, non vérifiés ({push_error})", "build": "",
                    "corrige": True}
        sources = verify_sources
        if profile.get("verify_branch"):
            head = cucumber_tools.rev_parse(worktree, "HEAD")
            sources = {**verify_sources, "CUCUMBER_SPEC_COMMIT": head,
                       profile.get("verify_branch_param") or cucumber_profile.DEFAULT_VERIFY_BRANCH_PARAM:
                           profile["verify_branch"]}
        init = cucumber_verify.run_init(verify_mode, worktree, cucumber_tools.rev_parse(worktree, "HEAD"),
                                        lambda: cucumber_tools.build_dir(client, worktree, cucumber_std_dir),
                                        init_tags, sources)
        print(f"[CUCUMBER INIT] {'✅' if init['ok'] else '🌐'} Après correction {attempt} : {init['message']}")
        if init["ok"]:
            return init
        if not cucumber_init_fix.fixable(init.get("echecs") or [], files):
            print("[CUCUMBER INIT] L'échec restant est hors des compléments de l'agent : init à corriger à la main.")
            break
        errors = [f"l'init échoue encore : {init['message']}"]
    return init


def fix_init_from_import(args, numero: int, local_cucumber_std_repo: str, odatio_version: str,
                         token_saving_rules: str, python_exe: str, workspace_dir: str) -> int:
    """
    Init en échec dans un run importé (n° `numero`), uniquement dans les compléments de l'agent : correction de ces
    fichiers (fix_own_init), commitée dans le worktree, puis — si l'agent exécute les tests (CUCUMBER_AGENT_JENKINS=1
    et branche de travail) — init rejouée sur Jenkins ; sinon patch <client>-cucumber-init-correction.patch à
    appliquer, pousser et vérifier par un run. Renvoie 0 si corrigé (et vérifié le cas échéant), 1 sinon, 2 si rien
    n'est corrigeable.
    """
    client = args.client_name
    analysis = cucumber_tools._read_json(os.path.join(cucumber_import.imports_dir(client), str(numero),
                                                      "analyse.json"), {}) or {}
    init = analysis.get("init_en_echec") or {}
    worktree = cucumber_tools.worktree_dir(client)
    if not init or not os.path.exists(os.path.join(worktree, ".git")):
        print(f"[CUCUMBER INIT] Import n° {numero} : " + ("aucun échec d'initialisation." if not init else
                                                         "aucun espace de travail : lancez d'abord une génération."))
        return 2
    rel, title = (init["id"].split("::", 1) + [""])[:2]
    result = (analysis.get("resultats") or {}).get(init["id"]) or {}
    failure = {"fichier": rel, "titre": title, "step": init.get("step", ""), "message": init.get("message", ""),
               "journal": result.get("journal", "")}
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))
    state = cucumber_tools.load_state(client)
    std_java = cucumber_tools.standard_java_catalogue(local_cucumber_std_repo, odatio_version)
    step_code = cucumber_tools.std_step_code(local_cucumber_std_repo, odatio_version)
    mode, message = cucumber_verify.execution_mode(profile, args.cucumber_base_branch)
    if mode and mode != "branche":
        mode = ""
    print(f"[CUCUMBER INIT] Import n° {numero} : init en échec dans {rel} — {init.get('step')} — "
          f"{(init.get('message') or '')[:160]}")
    folder = os.path.join(cucumber_import.imports_dir(client), str(numero))
    consoles = sorted(n for n in os.listdir(folder) if "console" in n.lower()) if os.path.isdir(folder) else []

    def agent_commits() -> list:
        base = state.get("base") or ""
        log = _git_out(worktree, "log", "--format=%H %s", f"{base}..HEAD" if base else "-20")
        return [c for c in log.splitlines() if "compléments d'initialisation" in c and "par l'agent" in c]
    files = cucumber_init_fix.own_files(worktree, client)
    # Correction déjà faite (tentative précédente non poussée) et conforme : patch seulement, sans nouvel appel
    already = agent_commits() and not cucumber_init_fix.check_correction(
        worktree, client, files, cucumber_init_fix.snapshot(worktree, files))
    if already:
        print("[CUCUMBER INIT] Correction déjà présente dans l'espace de travail (non poussée) : patch régénéré.")
    out = {"ok": None, "corrige": True, "message": ""} if already else fix_own_init(client, worktree, {"ok": False, "message": "", "echecs": [failure]}, mode,
                       profile.get("init_tags") or cucumber_verify.init_tags(),
                       cucumber_verify.branch_sources(profile, args.cucumber_repo), profile, state,
                       local_cucumber_std_repo, std_java, step_code, token_saving_rules, python_exe, workspace_dir,
                       cucumber_tools.work_id(client), os.path.join(folder, consoles[0]) if consoles else "")
    # Import traité (écran minimal : la correction n'est plus proposée pour ce run)
    state.setdefault("init_corrigee", {"fichiers": [], "tentatives": []})["import"] = numero
    cucumber_tools.save_state(client, state)
    if out.get("ok") is False and not out.get("corrige"):
        if not (state.get("init_corrigee") or {}).get("tentatives"):
            return 2
        print(f"[CUCUMBER INIT] ⛔ Init toujours en échec : {out.get('message')}")
        return 1
    patch = os.path.join(cucumber_tools.WORKSPACE_DIR, f"{client}-cucumber-init-correction.patch")
    commits = agent_commits()
    if commits and cucumber_tools.format_patch(worktree, f"{commits[-1].split()[0]}~1..{commits[0].split()[0]}",
                                               patch):
        print(f"[CUCUMBER INIT] Patch de la correction de l'init (à relire, appliquer et pousser) : {patch}")
    print("[CUCUMBER INIT] " + ("✅ Init vérifiée sur Jenkins après correction." if out.get("ok") else
                                "Correction faite, non vérifiée sur Jenkins : relisez et appliquez le patch, poussez et "
                                "relancez votre job, puis importez le résultat."))
    # Correction de l'init déposée dans le répertoire de connaissances partagé
    corrected = state.get("init_corrigee") or {}
    body = (f"# Correction de l'initialisation de {client} (run importé n° {numero})\n\n"
            f"- Échec : {rel} — {init.get('step', '')} — {(init.get('message') or '')[:400]}\n"
            f"- Résultat : " + ("vérifiée sur Jenkins après correction" if out.get("ok") else
                                "corrigée, non vérifiée sur Jenkins") + "\n"
            + "".join(f"- Fichier corrigé : {f}\n" for f in corrected.get("fichiers") or [])
            + "".join(f"- Tentative : {json.dumps(t, ensure_ascii=False)[:600]}\n"
                      for t in (corrected.get("tentatives") or [])[-3:])
            + (f"\n## Patch\n\n```diff\n{_read_text(patch)[:20000]}\n```\n" if os.path.exists(patch) else ""))
    share_knowledge(body, (getattr(args, "project", "") or "").strip().upper() or client.upper(), "cucumber-correction-init",
                    f"Correction de l'initialisation {client} (run n° {numero})", client=client, tags=["cucumber"])
    return 0


def check_server_log(client: str, path: str, scenario: str) -> int:
    """
    Import d'un export de logs serveur (0 token) : l'export couvre-t-il l'heure du step en échec du scénario
    `scenario` (identifiant ou tag) de la dernière génération ? Rappelle la fenêtre et le filtre conseillés.
    Renvoie 0 si l'export couvre le step, 1 sinon, 2 si le scénario ou le fichier est introuvable.
    """
    items = [item for lot in cucumber_tools.load_state(client).get("lots", [])
             for tag, item in ((lot.get("execution") or {}).get("scenarios") or {}).items()
             if scenario in (tag, item.get("id"))]
    if not items or not os.path.exists(path):
        print(f"[CUCUMBER LOGS] ❌ {'Scénario ' + scenario if not items else 'Fichier ' + path} introuvable.")
        return 2
    item = items[-1]
    print(f"[CUCUMBER LOGS] {item['id']} : {item.get('step') or 'step inconnu'} — heure "
          f"{item.get('heure') or 'inconnue'} (UTC)")
    print(f"[CUCUMBER LOGS] Export conseillé : {cucumber_verify.log_request(item)}")
    with open(path, encoding="utf-8", errors="replace") as f:
        covered, message = cucumber_verify.log_coverage(f.read(), item.get("heure", ""))
    print(f"[CUCUMBER LOGS] {'✅' if covered else '❌'} {message}")
    return 0 if covered else 1


def run_cucumber_health(client: str, local_cucumber_std_repo: str, cucumber_repo_dir: str, base: str,
                        project: str = "") -> int:
    """
    Bilan de santé du dépôt client (0 token, cucumber_checks.health_report) : compilation et dry-run avec les
    commandes de la génération (CUCUMBER_COMPILE_CMD, dry-run toujours lancé). Renvoie 0 si le dépôt est sain, 1 sinon.
    """
    compile_cmd = os.getenv("CUCUMBER_COMPILE_CMD")
    compile_cmd = cucumber_tools.DEFAULT_COMPILE_CMD if compile_cmd is None else compile_cmd.strip()
    subprocess.run(["git", "-C", cucumber_repo_dir, "fetch", "origin", "--prune"], capture_output=True)
    print(f"[CUCUMBER SANTÉ] Bilan de {client}-cucumber à {base}" + ("" if compile_cmd else
                                                                     " (sans compilation : CUCUMBER_COMPILE_CMD vide)"))
    try:
        profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))
        report = cucumber_checks.health_report(client, cucumber_repo_dir, base, local_cucumber_std_repo, compile_cmd,
                                               cucumber_tools.DEFAULT_DRY_RUN_CMD, cucumber_profile.conventions(profile))
    except RuntimeError as e:
        print(f"[CUCUMBER SANTÉ] ❌ {e}")
        return 2
    for title, errors in report["sections"].items():
        print(f"[CUCUMBER SANTÉ] {'✅' if not errors else '⚠️'} {title} : {len(errors)}")
        print("\n".join(f"    {e}" for e in errors[:10]))
    if report["ecart_version"]:
        print(f"[CUCUMBER SANTÉ] ⚠️ Bibliothèque de steps : {report['ecart_version']}")
    steps_report = report["ecarts_steps"]
    if steps_report["avertissement"]:
        print(f"[CUCUMBER SANTÉ] ⚠️ {steps_report['avertissement']}")
    elif steps_report["ref_bibliotheque"]:
        print(f"[CUCUMBER SANTÉ] {'⚠️' if steps_report['ecarts'] else '✅'} Steps utilisés : "
              f"{len(steps_report['ecarts'])} écart(s) entre {steps_report['ref_bibliotheque']} et le Standard aligné")
        print("\n".join(f"    {cucumber_versions.difference_line(d)}" for d in steps_report["ecarts"][:10]))
    if report["todo_version"]:
        print(f"[CUCUMBER SANTÉ] {len(report['todo_version'])} marqueur(s) {cucumber_versions.TODO_MARKER} dans le "
              f"dépôt client")
    if report["decale"]:
        print("[CUCUMBER SANTÉ] ⚠️ Features existantes en échec de compilation ou de dry-run (steps non définis ou "
              "ambigus) : détail dans le rapport (pas un problème de génération).")
    print(f"[CUCUMBER SANTÉ] Rapport : {report['fichier']}")
    share_file(report["fichier"], project or client.upper(), "cucumber-sante", f"Santé du dépôt {client}-cucumber",
               client=client, tags=["cucumber"], depots=knowledge_tools.repo_commits(cucumber_repo_dir))
    return 0 if report["ok"] else 1


def deliver_std_data(client: str, worktree: str, cucumber_std_dir: str, profile: dict) -> list:
    """
    Avant le patch : fichiers de données lus par TOUTES les features du client (de l'agent ou écrites à la main)
    absents du worktree. Présents au Standard : copiés et commités (livrés) ; sinon signalés. Renvoie les copies.
    """
    root = profile.get("features_root") or f"{cucumber_checks.FEATURES_ROOT}{client}"
    result = cucumber_checks.check_data_files(worktree, cucumber_checks.client_feature_files(worktree, root),
                                              cucumber_std_dir)
    copied = [target for target, _ in result["copies"]]
    if copied:
        cucumber_tools._git(worktree, "add", "-f", "--", *copied)
        cucumber_tools._git(worktree, "commit", "-q", "-m",
                            f"Cucumber : fichiers de données du Standard lus par les features de {client}")
        print(f"[CUCUMBER] {len(copied)} fichier(s) de données manquant(s) copié(s) depuis le Standard : "
              f"{', '.join(copied[:5])}")
    if result["erreurs"]:
        print(f"[CUCUMBER] ⚠️ {len(result['erreurs'])} fichier(s) de données introuvable(s), y compris au Standard :")
        print("\n".join(f"    {e}" for e in result["erreurs"][:20]))
    return copied


def run_cucumber_client(args, work_key: str, local_client_repo: str, local_std_repo: str,
                        local_cucumber_std_repo: str, odatio_version: str, cucumber_repo_dir: str,
                        token_saving_rules: str, python_exe: str, workspace_dir: str) -> int:
    """
    Couverture Cucumber des spécifiques d'un client. Python fait tout le déterministe (inventaire, catalogues,
    extraits Jira, git, récapitulatif) ; Claude n'écrit que les tests, par lots, avec un appel neuf par lot.
    Reprise : les lots terminés (etat.json) ne sont pas refaits. Complément (--complete) : nouveaux lots ajoutés à
    la génération existante, sans repartir de la base. Renvoie le code de sortie.
    """
    client = args.client_name
    project = args.project.strip().upper()
    if args.server_log:
        return check_server_log(client, args.server_log, args.log_scenario)
    if args.import_run is not None or args.import_url:
        return run_cucumber_import(client, args.import_run or [], args.import_url, project)
    if args.patch_only:
        worktree = cucumber_tools.worktree_dir(client)
        base = cucumber_tools.base_ref(cucumber_repo_dir, args.cucumber_base_branch)
        subprocess.run(["git", "-C", worktree, "fetch", "-q", "origin"], capture_output=True)
        if not base or not os.path.exists(os.path.join(worktree, ".git")):
            print("[CUCUMBER] Aucun espace de travail ou branche d'intégration introuvable : rien à livrer.")
            return 2
        relocated = cucumber_stray.relocate(cucumber_tools.cache_dir(client), worktree)
        for source, target in relocated:
            print(f"[CUCUMBER] ⚠️ Fichier écrit hors du dépôt {source} → rapatrié dans le dépôt : {target}")
        if relocated:
            cucumber_tools._git(worktree, "add", "--", *[t for _, t in relocated])
            cucumber_tools._git(worktree, "commit", "-q", "-m",
                                f"Cucumber : {len(relocated)} fichier(s) de l'agent rapatrié(s) dans le dépôt")
        if local_cucumber_std_repo:
            deliver_std_data(client, worktree, local_cucumber_std_repo,
                             cucumber_profile.effective_profile(cucumber_profile.load_profile(client)))
        if cucumber_tools.write_patch(worktree, base, cucumber_tools.patch_path(client), client):
            print(f"[CUCUMBER] Patch régénéré (un commit, écart net avec {base}) : {cucumber_tools.patch_path(client)}")
            return 0
        print(f"[CUCUMBER] Rien à livrer par rapport à {base}.")
        return 2
    if args.fix_init_from_import:
        return fix_init_from_import(args, args.fix_init_from_import, local_cucumber_std_repo, odatio_version,
                                    token_saving_rules, python_exe, workspace_dir)
    if args.run_jenkins and args.run_dry:
        return preview_cucumber_jenkins(client, args.run_tags, args.cucumber_repo, args.cucumber_base_branch,
                                        not args.run_no_push, args.run_branch)
    if args.run_jenkins:
        return run_cucumber_jenkins(client, args.run_tags, project, args.cucumber_repo, args.cucumber_base_branch,
                                    local_client_repo, not args.run_no_push, args.run_branch)
    base = cucumber_tools.base_ref(cucumber_repo_dir, args.cucumber_base_branch)
    if not base:
        print(f"❌ Branche '{args.cucumber_base_branch}' introuvable dans {cucumber_repo_dir}.")
        if args.detect_only:
            cucumber_detection.save_error(client, f"branche '{args.cucumber_base_branch}' introuvable dans "
                                                  f"{args.cucumber_repo}")
        return 2
    if args.detect_only:
        subprocess.run(["git", "-C", cucumber_repo_dir, "fetch", "origin", "--prune"], capture_output=True)
        base = cucumber_tools.base_ref(cucumber_repo_dir, args.cucumber_base_branch)
        commits = cucumber_detection.repo_commits(local_client_repo, cucumber_repo_dir, base, local_cucumber_std_repo)
        if not args.detect_force and cucumber_detection.up_to_date(client, cucumber_detection.fingerprint(commits)):
            print(f"[CUCUMBER DÉTECTION] Commits inchangés ({cucumber_detection.fingerprint(commits)}) : détection "
                  f"en cache conservée.")
            return 0
    if args.init_only:
        return run_cucumber_init(args, local_client_repo, local_cucumber_std_repo, cucumber_repo_dir, base,
                                 odatio_version)
    if args.health_only:
        return run_cucumber_health(client, local_cucumber_std_repo, cucumber_repo_dir, base, project)
    if args.profile_only:
        subprocess.run(["git", "-C", cucumber_repo_dir, "fetch", "origin", "--prune"], capture_output=True)
        return 0 if detect_cucumber_profile(client, cucumber_repo_dir, base, local_cucumber_std_repo,
                                            odatio_version) else 1
    complete_state = cucumber_tools.load_state(client) if args.complete else {}
    if args.complete and not complete_state.get("lots"):
        print("❌ Aucune génération à compléter : lancez d'abord une génération.")
        return 2
    resume_state = cucumber_tools.load_state(client) if args.resume and not args.complete else {}
    if args.resume and not args.complete and not cucumber_tools.pending_lots(resume_state):
        print("[CUCUMBER] Aucun lot à reprendre.")
        return 0
    if args.fix_client_tags:
        return fix_client_tags(client, local_cucumber_std_repo)
    if not (args.inventory_only or args.detect_only):
        # Environnement de l'agent contrôlé avant tout lot, sans rien modifier : un problème = aucun lot lancé
        compile_cmd = os.getenv("CUCUMBER_COMPILE_CMD")
        compile_cmd = cucumber_tools.DEFAULT_COMPILE_CMD if compile_cmd is None else compile_cmd.strip()
        problems = cucumber_preflight.check(local_client_repo, compile_cmd, local_cucumber_std_repo,
                                            cucumber_tools.outside_inventory)
        if problems:
            for problem in problems:
                print(f"{CUCUMBER_ENV_PREFIX} {problem}")
            requested = ([i.strip() for i in args.specifics.split(",") if i.strip()] if args.specifics else
                         [i for lot in cucumber_tools.pending_lots(resume_state) for i in lot["ids"]] if resume_state
                         else cucumber_tools.completable_specifics(complete_state) if complete_state else [])
            cucumber_results.record_interruption(client, requested, problems[0], "corriger l'environnement puis "
                                                 "relancer" if len(problems) == 1 else
                                                 f"corriger les {len(problems)} problèmes d'environnement signalés, "
                                                 f"puis relancer")
            print(f"[CUCUMBER] ⛔ Génération interrompue avant tout lot : {len(problems)} problème(s) "
                  f"d'environnement ; aucun lot lancé, l'état des tâches est inchangé.")
            return 3

    # 1. Pré-calculs (0 token). Reprise ou sélection faite sur un inventaire : celui-ci est réutilisé tel quel
    subprocess.run(["git", "-C", cucumber_repo_dir, "fetch", "origin", "--prune"], capture_output=True)
    inventory = cucumber_tools.load_inventory(client) if (resume_state or complete_state or args.specifics) else {}
    started = time.time()
    if inventory.get("specifiques"):
        print(f"[CUCUMBER] Inventaire du {inventory['genere_le'].replace('T', ' ')} réutilisé.")
        # Inventaire antérieur au rapprochement avec les tests et endpoints Standard : complété sans le refaire ;
        # complément : rapprochement refait (synonymes, CUCUMBER_STD_MATCH_MIN à jour)
        if complete_state or any("tests_std" not in s for s in inventory["specifiques"]):
            cucumber_tools.attach_std_tests(inventory, local_cucumber_std_repo, odatio_version)
        # Versions Standard des fichiers overridés (inventaire antérieur : complétées si la référence est connue)
        cucumber_tools.export_sources(inventory)
        if "initialiseurs" not in inventory:
            inventory["initialiseurs"] = cucumber_initializers.find_initializers(inventory)
        if any("endpoints_std" not in s for s in inventory["specifiques"]):
            cucumber_tools.attach_endpoints(inventory, local_std_repo, cucumber_tools.standard_endpoints(
                local_cucumber_std_repo, odatio_version))
        cucumber_tools.save_inventory(client, inventory)
    else:
        print("[CUCUMBER] Inventaire des spécifiques, catalogues et extraits Jira...")
        inventory = cucumber_tools.prepare(client, local_client_repo, local_std_repo, local_cucumber_std_repo,
                                           odatio_version, cucumber_repo_dir, base,
                                           cucumber_jira_tickets(client, args.application))
    # Recettes de test applicables et steps Standard candidats de chaque spécifique (0 token), recalculés à chaque
    # exécution
    with_recipes = cucumber_tools.attach_recipes(inventory, local_cucumber_std_repo)
    std_endpoints = cucumber_tools.standard_endpoints(local_cucumber_std_repo, odatio_version)
    with_candidates = cucumber_tools.attach_candidate_steps(inventory, inventory["catalogue_standard"]["steps_std"],
                                                            std_endpoints)
    # Parties modifiées des customized.xml, injectées dans le prompt des lots (0 token)
    with_studio = cucumber_tools.attach_studio_excerpts(
        inventory, _env_int("CUCUMBER_INLINE_STUDIO_LINES", cucumber_tools.DEFAULT_INLINE_STUDIO_LINES))
    cucumber_tools.save_inventory(client, inventory)
    specifics = {s["id"]: s for s in inventory["specifiques"]}
    counts = {}
    for spec in specifics.values():
        counts[spec["couverture"]] = counts.get(spec["couverture"], 0) + 1
    print(f"[CUCUMBER] {len(specifics)} spécifique(s) en {time.time() - started:.0f} s : "
          + ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
          + f" — {cucumber_tools.inventory_path(client)}")
    recipes_file = cucumber_tools.recipes_path()
    print(f"[CUCUMBER] Recettes de test : {with_recipes} spécifique(s) concerné(s)"
          + (f" ({recipes_file})" if recipes_file else " (aucun fichier de recettes)"))
    print(f"[CUCUMBER] Steps Standard candidats : {with_candidates} spécifique(s) concerné(s) ; parties modifiées "
          f"de processus Studio : {with_studio} fichier(s)")
    if args.detect_only:
        try:
            facts = detect_init_facts(client, cucumber_repo_dir, base, local_cucumber_std_repo, odatio_version)
        except RuntimeError as e:
            print(f"[CUCUMBER DÉTECTION] ❌ {e}")
            cucumber_detection.save_error(client, str(e))
            return 1
        cucumber_detection.save_result(client, commits, facts)
        print_init_status(client, facts)
        # Valeurs connues des 6 sources, puis rétro-analyse de la testabilité des tâches « non couvertes »
        profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))
        index = cucumber_testability.known_values(
            client, profile, cucumber_tools.load_inventory(client), cucumber_profile.base_worktree_dir(client),
            std_features_dir(local_cucumber_std_repo), cucumber_testability.document_paths(client))
        cucumber_testability.save_index(client, index)
        # Liens ticket ↔ tâche vérifiés (commit qui modifie ses fichiers, référence explicite) : titres métier
        links = cucumber_coverage.verify_ticket_links(client, inventory, local_client_repo,
                                                      cucumber_tools.cached_tickets(client, max_age_s=10 ** 9) or [])
        print(f"[CUCUMBER DÉTECTION] Tickets Jira liés de façon vérifiée : {sum(map(len, links.values()))} "
              f"lien(s) sur {len(links)} tâche(s)")
        # Tous les tests du dépôt client (écrits ou non par l'agent), lus sur la branche du job, et leurs rattachements
        tests = cucumber_links.refresh(client, cucumber_repo_dir, profile, inventory,
                                       {i: [l["cle"] for l in ls] for i, ls in links.items()},
                                       args.cucumber_base_branch)
        resolved = cucumber_links.resolve(tests["features"], cucumber_links.load_confirmations(client))
        print(f"[CUCUMBER DÉTECTION] Tests existants sur {tests['branche'] or '?'} : {len(tests['features'])} "
              f"feature(s), {sum(f['scenarios'] for f in tests['features'])} scénario(s) actif(s) ; "
              f"{len(resolved['rattachees'])} rattachée(s) à {len(resolved['par_tache'])} tâche(s), "
              f"{len(resolved['non_rattachees'])} non rattachée(s)")
        if cucumber_tools.load_state(client).get("lots"):
            retro = cucumber_testability.retro(client, index=index, profile=profile)
            print(f"[CUCUMBER TESTABILITÉ] {cucumber_testability.retro_line(retro)}")
        return 0
    if args.inventory_only:
        detect_cucumber_profile(client, cucumber_repo_dir, base, local_cucumber_std_repo, odatio_version)
        share_knowledge(inventory_markdown(client, specifics), project, "cucumber-inventaire",
                        f"Inventaire des spécifiques {client}", client=client, tags=["cucumber"],
                        depots=knowledge_tools.repo_commits(cucumber_repo_dir))
        return 0

    # Tâches « partielles » de la vue Couverture (rapprochement partiel, scénarios faibles), calculées sur la
    # génération en cours : leur lot doit tester l'EFFET, pas la configuration
    partial = cucumber_coverage.partial_ids(cucumber_coverage.coverage_rows(client, inventory))
    # 2. Lots : complément, reprise, sélection explicite ou, par défaut, tous les spécifiques non couverts ou partiels
    if complete_state and args.from_import:
        state = complete_state
        analysis = cucumber_tools._read_json(os.path.join(cucumber_import.imports_dir(client), str(args.from_import),
                                                          "analyse.json"), {}) or {}
        if analysis.get("non_analyse") or not analysis.get("propositions"):
            print(f"[CUCUMBER] Import n° {args.from_import} : " + (analysis.get("non_analyse") or
                  "aucune correction proposée (ou import inconnu)") + ".")
            return 0
        new_lots = cucumber_import.add_correction_lots(state, analysis)
        print(f"[CUCUMBER] Import n° {args.from_import} : {len(new_lots)} lot(s) « correction » ajouté(s) ("
              f"{sum(len(lot['corrections']) for lot in new_lots)} scénario(s)), lots précédents conservés.")
    elif complete_state:
        state = complete_state
        cucumber_tools.drop_excluded(state, specifics)
        wanted = [i.strip() for i in args.specifics.split(",") if i.strip()] if args.specifics else \
            cucumber_tools.completable_specifics(state)
        unknown = [i for i in wanted if i not in specifics]
        if unknown:
            print(f"⚠️ Spécifiques inconnus ignorés : {', '.join(unknown)}")
        wanted = [i for i in wanted if i in specifics]
        if not wanted:
            print("[CUCUMBER] Aucun spécifique à compléter.")
            return 0
        previous = cucumber_tools.latest_results(state)
        print(f"[CUCUMBER] Complément : {len(wanted)} spécifique(s) à reprendre :")
        for i in wanted:
            reason = " ".join(((previous.get(i) or {}).get("raison") or "").split())
            print(f"  - {i} — {specifics[i]['libelle']}" + (f" : {reason[:160]}" if reason else ""))
        # Tri sans token : un élément Studio sans logique propre ne part jamais dans un lot Claude
        wanted, sorted_out = cucumber_tools.presort(wanted, specifics)
        if sorted_out:
            sorted_lot = cucumber_tools.add_sorted_lot(state, sorted_out)
            cucumber_results.record_lot(state, sorted_lot)
            print(f"[CUCUMBER] Tri sans appel Claude (lot {sorted_lot['numero']}) : " + " ; ".join(
                f"{r['id']} — {r['raison']}" for r in sorted_out))
        if not wanted:
            cucumber_tools.save_state(client, state)
            print("[CUCUMBER] Aucun spécifique à confier à un lot Claude.")
            return 0
        new_lots = cucumber_tools.add_completion_lots(state, wanted, _env_int("CUCUMBER_BATCH_SIZE", 3), specifics)
        others = len(cucumber_tools.pending_lots(state)) - len(new_lots)
        print(f"[CUCUMBER] Complément : lot(s) {new_lots[0]['numero']} à {new_lots[-1]['numero']} ajouté(s), lots "
              f"précédents et commits conservés" + (f" ; {others} lot(s) non terminé(s) repris aussi." if others else "."))
    elif resume_state:
        state = resume_state
        dropped = cucumber_tools.drop_excluded(state, specifics)
        if dropped:
            print(f"[CUCUMBER] {len(dropped)} spécifique(s) absent(s) de l'inventaire actuel retiré(s) des lots.")
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
        # Tri Python (0 token) : listeners / événements sans logique propre, spécifiques purement techniques
        remaining, sorted_out = cucumber_tools.presort(wanted, specifics)
        if sorted_out:
            indirect = sum(1 for r in sorted_out if r["statut"] == "couvert_indirectement")
            print(f"[CUCUMBER] Tri sans appel Claude : {indirect} couvert(s) indirectement (listener / événement "
                  f"sans logique propre), {len(sorted_out) - indirect} technique(s) non couvert(s).")
        # Lots regroupés par test Standard principal puis par domaine ; libellés réunis en un seul lot
        lots = cucumber_tools.make_lots(remaining, _env_int("CUCUMBER_BATCH_SIZE", 3), specifics)
        if sorted_out:
            lots.append(cucumber_tools.presort_lot(sorted_out, len(lots) + 1))
        state = {"client": client, "debut": datetime.now().isoformat(timespec="seconds"), "base": base,
                 "lots": lots}
        for sorted_lot in (lot for lot in lots if lot.get("tri")):
            cucumber_results.record_lot(state, sorted_lot)
        if cucumber_tools.load_state(client).get("lots"):
            print("⚠️ Nouvelle génération : les lots précédents (worktree, commits, etat.json) sont écrasés.")
    state["partiels"] = sorted(set(state.get("partiels") or []) | {i for lot in cucumber_tools.pending_lots(state)
                                                                    for i in lot["ids"] if i in partial})
    try:
        worktree = cucumber_tools.prepare_worktree(cucumber_repo_dir, client, state["base"], reset=not (resume_state or complete_state))
    except RuntimeError as e:
        print(f"❌ {e}")
        return 2
    if args.from_import or resume_state or complete_state:
        # Correction d'un run, reprise ou complément : l'espace de travail est remis sur la branche réelle lue par
        # Jenkins (scénarios écrits ou corrigés hors de l'agent compris) ; sur un conflit, la branche l'emporte
        subprocess.run(["git", "-C", worktree, "fetch", "-q", "origin"], capture_output=True)
        problem = cucumber_failures.sync_with_branch(
            worktree, state["base"],
            lambda kept: print(f"[CUCUMBER] Version de {state['base']} retenue sur conflit (corrections faites "
                               f"depuis, jamais écrasées) : {', '.join(kept)}"))
        print(f"[CUCUMBER] ⚠️ Espace de travail non synchronisé avec {state['base']} : {problem}" if problem else
              f"[CUCUMBER] Espace de travail synchronisé avec {state['base']} (branche lue par Jenkins).")
    # Fichiers égarés par l'agent hors du dépôt lors des générations précédentes : rapatriés s'ils sont plus récents
    for source, target in cucumber_stray.relocate(cucumber_tools.cache_dir(client), worktree):
        print(f"[CUCUMBER] ⚠️ Fichier écrit hors du dépôt {source} → rapatrié dans le dépôt : {target}")
    # Autonomie : décisions restées en attente (d'avant l'autonomie) prises par l'agent, scénarios réactivés
    for done in cucumber_decisions.auto_resolve_pending(client, worktree, state["base"]):
        print(f"[CUCUMBER] 🤖 Décision en attente prise par l'agent ({cucumber_decisions.TYPES[done['type']]}) : "
              f"{done['proposition']}")
        cucumber_decisions.sync_state(state, done)
    cucumber_tools.save_state(client, state)

    catalogue = inventory["catalogue_standard"]
    std_java = cucumber_tools.standard_java_catalogue(local_cucumber_std_repo, odatio_version)
    # Code des Step Definitions du Standard (injecté par step candidat) et copie lisible de ses sources de test
    step_code = cucumber_tools.std_step_code(local_cucumber_std_repo, odatio_version)
    # Référence des contrôles de pertinence (steps, scénarios et endpoints du Standard), calculée une fois
    reference = cucumber_verify.std_reference(local_cucumber_std_repo, std_endpoints)
    # Profil client (0 token) : conventions détectées dans le worktree, surchargées par la saisie et le dépôt
    profile = cucumber_profile.client_profile(client, worktree, state["base"], local_cucumber_std_repo,
                                              std_java.get("runners", []), odatio_version)
    print_profile(profile)
    # Moyen d'exécution des niveaux 2 et 3 (job Jenkins avec patch, job lisant la branche de travail, commande
    # locale, ou aucun)
    verify_mode, verify_message = cucumber_verify.execution_mode(profile, args.cucumber_base_branch)
    print(f"[CUCUMBER] Vérification par exécution : {verify_message}.")
    unverified_status = "non_verifie"
    # Job Jenkins : même dépôt client et mêmes commits de base que l'exécution locale ; job sans patch : branche de
    # travail du profil et dépôt client
    verify_sources = (cucumber_verify.branch_sources(profile, args.cucumber_repo) if verify_mode == "branche" else
                      cucumber_verify.job_sources(
                          args.cucumber_repo, args.cucumber_base_branch,
                          cucumber_tools.rev_parse(worktree, state["base"]),
                          cucumber_tools.rev_parse(local_cucumber_std_repo, "HEAD")))
    # Bibliothèque de steps plus ancienne que le serveur : écarts du catalogue aligné, interrogés par Grep par les lots
    gap_file = ""
    if profile["library_version"] and profile["server_version"] and cucumber_checks.compare_versions(
            profile["library_version"], profile["server_version"]) < 0:
        gap_file = cucumber_versions.library_gap_file(local_cucumber_std_repo, profile["library_version"])
        print(f"[CUCUMBER PROFIL] Écarts du catalogue avec la bibliothèque {profile['library_version']} : "
              f"{gap_file or 'référence de la bibliothèque introuvable dans le dépôt Standard'}")
    init_tags = profile["init_tags"] or cucumber_verify.init_tags()
    # Initialisation du client (init_tags du profil), une fois par génération : en échec, les lots ne sont pas jugés
    if profile["verify_branch"]:
        print(f"[CUCUMBER] Vérification sur la branche {profile['verify_branch']} (paramètre "
              f"{profile['verify_branch_param'] or cucumber_profile.DEFAULT_VERIFY_BRANCH_PARAM}) : le worktree y est "
              f"commité et poussé avant chaque vérification.")
    branch_error = (cucumber_checks.push_for_verification(
        worktree, profile["verify_branch"], f"Cucumber coverage of {client} specifics - initialisation")[0]
                    if verify_mode and profile["verify_branch"] and init_tags and cucumber_tools.pending_lots(state)
                    else "")
    if branch_error:
        print(f"[CUCUMBER] ⛔ Initialisation non vérifiée : {branch_error}")
    if verify_mode and init_tags and cucumber_tools.pending_lots(state) and not branch_error:
        init_base, init_sources = state["base"], verify_sources
        if profile["verify_branch"]:
            init_base = cucumber_tools.rev_parse(worktree, "HEAD")
            init_sources = {**verify_sources, "CUCUMBER_SPEC_COMMIT": init_base,
                            profile["verify_branch_param"] or cucumber_profile.DEFAULT_VERIFY_BRANCH_PARAM:
                                profile["verify_branch"]}
        init = cucumber_verify.run_init(verify_mode, worktree, init_base,
                                        lambda: cucumber_tools.build_dir(client, worktree, local_cucumber_std_repo),
                                        init_tags, init_sources)
        print(f"[CUCUMBER] {'✅' if init['ok'] else '🌐'} {init['message']}" + (f" ({init['build']})" if init["build"]
                                                                               else ""))
        if not init["ok"]:
            # Init en échec dans les compléments écrits par l'agent : il les corrige lui-même (et eux seuls), puis
            # rejoue l'init seule ; au plus CUCUMBER_INIT_FIX_ATTEMPTS tentatives
            init = fix_own_init(client, worktree, init, verify_mode, init_tags, verify_sources, profile, state,
                                local_cucumber_std_repo, std_java, step_code, token_saving_rules, python_exe,
                                workspace_dir, work_key)
        # Dernier résultat connu de l'init (parcours de l'onglet, étape Projet)
        state["init"] = {**init, "date": datetime.now().isoformat(timespec="seconds")}
        cucumber_tools.save_state(client, state)
        if not init["ok"]:
            verify_mode, verify_message, unverified_status = "", init["message"], "environnement"
    # Connaissances partagées du client (couverture, santé, à revoir, runs importés...), dans la partie fixe des lots
    shared = consult_knowledge(project, client=client, tags=["cucumber"])
    fixed_values = {
        "client_name": client, "token_saving_rules": token_saving_rules,
        "knowledge_block": f"\n{shared['bloc']}\n" if shared["bloc"] else "",
        # Accès base en lecture seule de tous les lots et de leurs corrections (--db-schema), journalisé par lot
        # Base de l'environnement où Jenkins joue les tests (CUCUMBER_DB_*), à défaut la base DB_*
        "db_block": build_db_block(args.db_schema.strip(), python_exe, workspace_dir,
                                   step="generate-cucumber-client", profile="cucumber"),
        # Logs applicatifs de l'environnement de test (groundcover), en lecture seule
        "logs_block": build_logs_block(python_exe, workspace_dir),
        "catalogue_std": catalogue["catalogue_std"], "steps_std": catalogue["steps_std"],
        "features_std": catalogue["features_std"],
        "examples": ", ".join(f"'{e}'" for e in catalogue["examples"]) or "aucun",
        "couverture_existante": inventory["couverture_existante"], "cucumber_repo": args.cucumber_repo,
        "inventaire": cucumber_tools.inventory_path(client),
        "worktree": worktree, "java_std": std_java["java_std"], "endpoints_std": std_endpoints["fichier"],
        "consignes": cucumber_verify.project_guidelines(client),
        "steps_example": std_java["steps_example"] or "aucune", "runner_example": std_java["runner_example"] or "aucun",
        # Conventions du dépôt client, tirées du profil (contrôlées après chaque lot)
        "features_root": profile["features_root"], "client_tag": profile["tag_client"],
        "tag_a_revoir": cucumber_a_revoir.a_revoir_tag(),
        # Seul clear autorisé pour purger l'environnement (profil client)
        "clear_name": cucumber_init.clear_name(profile) or "(non renseigné dans le profil client)",
        # Valeurs connues des 6 sources (profil, init, initializers, dépôt client, Standard, documents) : Grep avant
        # de déclarer une donnée manquante
        "valeurs_connues": write_known_values(client, profile, inventory, worktree, local_cucumber_std_repo),
        "sources_std": step_code["sources"],
    }
    # Initialisation du client vue par les lots : features d'init et codes de référence du profil
    client_init = cucumber_profile.init_view(profile)
    conventions = cucumber_profile.conventions(profile)
    # Compléments d'init corrigés par l'agent pendant cette génération : sortis des fichiers protégés (livrés)
    own_init = set((state.get("init_corrigee") or {}).get("fichiers") or [])
    if own_init:
        conventions["protected_files"] = [p for p in conventions.get("protected_files") or [] if p not in own_init]
    # Longueur maximale du code de chaque entité (modèles des dépôts serveur), contrôlée après chaque lot (9b)
    code_lengths = cucumber_rules.code_length_index(client, [local_std_repo, local_client_repo])
    print(f"[CUCUMBER] Longueurs des codes : {len(code_lengths)} entité(s) trouvée(s) dans les modèles "
          f"(défaut {cucumber_rules.DEFAULT_CODE_LENGTH} caractères sinon).")
    # Runner contrôlé avant le premier lot : sa correction éventuelle est livrée avec le premier lot
    runner_check = cucumber_tools.client_java_context(client, worktree, std_java, conventions)
    print(f"[CUCUMBER] Runner et glue : {runner_check['runner_status']}")
    # Contrôle du runner avant le premier lot, repris dans le récapitulatif avec sa modification éventuelle (point 8)
    state["runner"] = {"consigne": runner_check["runner_status"],
                       "chemin": (runner_check.get("runner") or {}).get("path", "")}
    # Contenu injecté : partie fixe (en tête, identique pour tous les lots) et extraits du lot, chacun borné
    inline_max = _env_int("CUCUMBER_INLINE_MAX_CHARS", cucumber_tools.DEFAULT_INLINE_MAX_CHARS)
    recipe_examples = [r["exemple_local"] for s in specifics.values() for r in s.get("recettes") or []
                       if r.get("exemple_local")]
    model = os.getenv("CLAUDE_MODEL_CUCUMBER", "").strip() or "sonnet"
    effort = claude_effort("CLAUDE_EFFORT_CUCUMBER")
    fix_plan = cucumber_fix_plan(model, 1)
    budget = cucumber_budget()
    print(f"[CUCUMBER] Modèle Claude : {model} | Effort : {effort or 'défaut de Claude Code'} "
          f"(revue : {cucumber_fix_effort()} ; correction de compilation : {fix_plan[0][0]}, effort "
          f"{fix_plan[0][1]}" + (f", dernière tentative {model}" if len(fix_plan) > 1 else "") + ")"
          f" | Session allégée : {'oui' if lean_session() else 'non (CLAUDE_LEAN_SESSION=0)'}"
          f" | Budget par appel : {budget + ' $' if budget else 'aucun'}")
    max_turns = str(_env_int("CUCUMBER_MAX_TURNS", DEFAULT_CUCUMBER_MAX_TURNS))
    run_dir = cucumber_tools.cache_dir(client)
    # Variable absente : compilation par défaut ; présente mais vide : pas de compilation
    compile_cmd = os.getenv("CUCUMBER_COMPILE_CMD")
    compile_cmd = cucumber_tools.DEFAULT_COMPILE_CMD if compile_cmd is None else compile_cmd.strip()
    # Dry-run strict par défaut : un step non défini est une erreur de lot (CUCUMBER_DRY_RUN=0 pour le désactiver)
    dry_run_cmd = "" if os.getenv("CUCUMBER_DRY_RUN", "1").strip() == "0" else cucumber_tools.DEFAULT_DRY_RUN_CMD
    # Compilation par l'agent lui-même pendant le lot (CUCUMBER_AGENT_COMPILE, défaut 1 dès qu'on compile)
    agent_compile = ""
    if compile_cmd and os.getenv("CUCUMBER_AGENT_COMPILE", "1").strip() != "0":
        cucumber_compile.write_context(client, worktree, local_cucumber_std_repo, compile_cmd)
        agent_compile = f'"{python_exe}" "{os.path.join(workspace_dir, "tools", "cucumber_compile.py")}" {client}'
    fixed_values["compilation_agent"] = agent_compilation_block(agent_compile)
    print(f"[CUCUMBER] Compilation par l'agent pendant les lots : "
          f"{'oui' if agent_compile else 'non (CUCUMBER_AGENT_COMPILE=0 ou pas de CUCUMBER_COMPILE_CMD)'} | "
          f"code de {len(step_code['steps'])} step(s) Standard disponible ; sources : {step_code['sources']}")
    total = {"tokens_entree": 0, "cache_ecrit": 0, "cache_lu": 0, "tokens_sortie": 0, "cout_usd": 0.0}
    lots = state["lots"]
    exit_code = 0

    def add_usage(lot: dict, usage: dict) -> None:
        tokens = lot.setdefault("tokens", {})
        for key in total:
            value = (usage or {}).get(key)
            if isinstance(value, (int, float)):
                total[key] += value
                tokens[key] = (tokens.get(key) or 0) + value

    # 3. Un appel Claude neuf par lot, puis compilation (et correction) en Python ; commit après chaque lot
    processed = 0
    for lot in lots:
        if lot.get("statut") in ("termine", "exclu"):
            continue
        if args.max_lots and processed >= args.max_lots:
            print(f"\n[CUCUMBER] --max-lots {args.max_lots} atteint : {len(cucumber_tools.pending_lots(state))} lot(s) "
                  f"restant(s), à reprendre.")
            break
        processed += 1
        lot_specs = [{**specifics[i], "partiel": i in (state.get("partiels") or [])} for i in lot["ids"] if i in specifics]
        label = f"{lot['numero']}/{len(lots)}"
        print(f"\n[LOT {label}] " + " ; ".join(s["libelle"] for s in lot_specs))
        java = cucumber_tools.client_java_context(client, worktree, std_java, conventions)
        context = {"init": client_init, "java": java, "conventions": conventions, "reference": reference,
                   "longueurs": code_lengths}
        if lot.get("type") == "correction" and lot.get("resultats") is None:
            # Lot « correction » d'un import : scénarios existants, aucune génération
            results = []
            lot.setdefault("tokens", {})
            files = run_correction_lot(lot, label, worktree, {**fixed_values, **java}, model, work_key, run_dir,
                                       lambda usage: add_usage(lot, usage))
        elif lot.get("statut") in ("erreur_compilation", "decision_humaine") and lot.get("resultats") is not None:
            # Reprise d'un lot écrit mais qui ne compilait pas, ou arrêté pour décision humaine : correction et
            # vérification seules
            results, files = lot["resultats"], lot.get("fichiers") or []
            print(f"[LOT {label}] Reprise de la correction et de la vérification ({len(files)} fichier(s)).")
        else:
            # Exemple du lot : la feature Standard la mieux classée, à défaut les exemples globaux
            example = cucumber_tools.lot_example(lot_specs)
            values = {**fixed_values, "examples": f"'{example}'"} if example else fixed_values
            # Partie fixe (prompt système, en cache d'un lot à l'autre) puis partie propre au lot (message)
            references, inlined = cucumber_tools.fixed_references(
                catalogue["catalogue_std"], std_java["steps_example"], recipe_examples, inline_max)
            java_values = {k: java[k] for k in ("java_client", "package_steps", "package_support", "class_suffix")}
            system_prompt = render_prompt("cucumber_client_lot_systeme", **fixed_values, **java_values,
                                          references_injectees=references, max_turns=max_turns,
                                          tour_alerte=warning_turn(int(max_turns)))
            system_file = os.path.join(run_dir, f"prompt_systeme_lot_{lot['numero']}.md")
            with open(system_file, "w", encoding="utf-8") as f:
                f.write(system_prompt)
            prompt = render_prompt("cucumber_client_lot", **values, **java_values, runner_status=java["runner_status"],
                                   runner_block=cucumber_tools.runner_block(std_java["runner_example"],
                                                                            java["runner_status"], inline_max),
                                   lot_block=cucumber_tools.lot_block(
                                       lot_specs, inline_max, inlined, step_code,
                                       _env_int("CUCUMBER_INLINE_STEP_CODE_CHARS",
                                                cucumber_tools.DEFAULT_INLINE_STEP_CODE_CHARS)),
                                   profil_block="\n".join(filter(None, [
                                       cucumber_profile.profile_block(profile, gap_file=gap_file,
                                                                      known=cucumber_testability.load_index(client)),
                                       cucumber_testability.answers_block(profile)])),
                                   lot_label=label)
            # Accès limité au dossier de travail du client (worktree, sources, extraits Jira) et aux catalogues
            returncode, output, usage, limit_hit = run_cucumber_claude(prompt, model, lot_tools(fixed_values),
                                                                       max_turns, run_dir, effort, system_file,
                                                                       lot.setdefault("base", []), "écriture")
            report_token_usage(work_key, "cucumber-lot", model, usage, len(system_prompt) + len(prompt), returncode,
                               effort)
            lot["tokens"] = {}
            add_usage(lot, usage)
            # Session du lot, reprise par la revue et les corrections (CUCUMBER_RESUME_SESSION)
            lot.update(session=(usage or {}).get("session_id", ""), modele=model, effort=effort, systeme=system_file)
            results = cucumber_tools.parse_lot_result(output)
            if returncode != 0 or limit_hit or results is None:
                # Rien n'est commité : le worktree revient au dernier lot, la reprise repart d'un état propre
                cucumber_tools.discard_lot(worktree)
                lot.update(statut="echec", commit=lot.get("commit", False),
                           fin=datetime.now().isoformat(timespec="seconds"),
                           erreur=("quota atteint" if limit_hit else "budget atteint" if budget_hit(usage)
                                   else f"code {returncode}" if returncode != 0 else "JSON final absent ou invalide"))
                cucumber_tools.save_state(client, state)
                print(f"[LOT {label}] ❌ Échec ({lot['erreur']}) : fichiers du lot annulés, relancez avec « Reprendre ».")
                exit_code = 1
                # Budget : propre à cet appel, les lots suivants sont tentés ; sinon (quota, erreur) arrêt
                if lot["erreur"] == "budget atteint":
                    continue
                break
            files = cucumber_tools.lot_files(worktree, results)
            for result in results:
                for constat in result["constats"]:
                    print(f"[LOT {label}] Constat {result['id']} — {cucumber_tools.constat_line(constat)}")
                    # Test jugé tautologique (et écart spécification / code si le code ne fait pas foi) : soumis à
                    # une décision humaine ; sinon l'écart reste un constat du bilan
                    if not cucumber_decisions.autonomous() and (
                            constat["type"] == "tautologique" or (constat["type"] == "ecart_spec_code"
                                                                  and not cucumber_decisions.code_wins())):
                        cucumber_decisions.propose(
                            client, constat["type"], f"{specifics.get(result['id'], {}).get('libelle', result['id'])}"
                            f" : {constat['detail']}", spec=result["id"], lot=lot["numero"],
                            fichier=constat.get("fichier", ""))
            # Contrôles de pertinence en Python, puis une revue bornée des seuls scénarios signalés
            review_cucumber_lot(lot, lot_specs, label, worktree, files, results, reference,
                                {**fixed_values, **java}, model, work_key, run_dir, lambda usage: add_usage(lot, usage))
            # Identifiant stable et tags @agent_<client>_<lot>[_<n>] de chaque scénario, pour l'exécuter seul
            new_scenarios = lot["indicateurs"]["scenarios"]
            lot["tags"] = cucumber_verify.tag_scenarios(worktree, new_scenarios, client, lot["numero"])
            spec_of = {cucumber_verify.scenario_id(s["fichier"], s["titre"]): s["spec"] for s in new_scenarios}
            lot["types"] = {tag: specifics.get(spec_of.get(identifier), {}).get("type", "")
                            for tag, identifier in lot["tags"].items()}
            files = list(dict.fromkeys(files + cucumber_tools.lot_files(worktree, [])))

        check = {"ok": True}
        if files:
            # Fichiers des features (0 token, données du Standard copiées), puis compilation si configurée
            check = verify_cucumber_lot(client, label, worktree, state["base"], local_cucumber_std_repo, files,
                                        compile_cmd, dry_run_cmd, {**fixed_values, **java}, model, work_key, run_dir,
                                        lambda usage: add_usage(lot, usage), inspired_of(results), context,
                                        lot.setdefault("base", []), lot)
            files = list(dict.fromkeys(files + cucumber_tools.lot_files(worktree, [])))
        # Niveaux 2 et 3 : exécution sur le client, contre-épreuves, correction bornée, retrait des scénarios faibles
        if lot.get("tags") and (check["ok"] or check.get("environnement")):
            verify_execution_lot(lot, lot_specs, label, worktree, state["base"], files, results, verify_mode,
                                 verify_message, client, local_cucumber_std_repo, compile_cmd, dry_run_cmd,
                                 {**fixed_values, **java}, model, work_key, run_dir,
                                 lambda usage: add_usage(lot, usage), verify_sources, unverified_status, context)
            files = list(dict.fromkeys(files + cucumber_tools.lot_files(worktree, [])))
        # A_REVOIR.md du dépôt client (défauts détectés, scénarios désactivés ou faibles, arrêts), livré avec le lot
        if lot.get("arret"):
            lot["statut"] = "decision_humaine"
        if cucumber_a_revoir.write_a_revoir(worktree, state, specifics):
            print(f"[LOT {label}] {cucumber_a_revoir.A_REVOIR_FILE} mis à jour.")
        # Commit même en erreur de compilation, pour ne rien perdre ; fichiers de test ignorés par le .gitignore du
        # client ajoutés de force, puis aucun fichier ne doit rester hors du commit
        forced = cucumber_checks.ignored_test_files(worktree)
        for rel in forced:
            print(f"[LOT {label}] ⚠️ Fichier ignoré par le .gitignore du dépôt client, ajouté de force au commit : {rel}")
        committed = cucumber_tools.commit_lot(worktree, client, lot["numero"], forced)
        outside = cucumber_checks.uncommitted_files(worktree)
        if outside:
            print(f"[LOT {label}] ❌ {len(outside)} fichier(s) hors du commit (absents du patch) : "
                  f"{', '.join(outside[:10])}")
        lot["hors_commit"] = outside
        # Empreinte des entrées de chaque tâche (code, profil client, réponses) : pas de relance inutile
        for result in results:
            if result.get("id") in specifics:
                result["entrees"] = cucumber_testability.task_inputs(specifics[result["id"]], profile)
        lot.update(resultats=results, fichiers=files, commit=committed or lot.get("commit", False),
                   fin=datetime.now().isoformat(timespec="seconds"),
                   verification={k: check.get(k) for k in ("ok", "etape", "erreurs", "environnement")} if files
                   else None)
        # Résultat obligatoire de la génération de chaque tâche du lot (carte de la tâche)
        cucumber_results.record_lot(state, lot)
        if lot.get("type") == "correction":
            # Résultat explicite de chaque échec corrigé : correction (diff, patch) ou diagnostic nécessaire
            for item in cucumber_failures.record_correction(cucumber_import.imports_dir(client), lot, worktree):
                print(f"[LOT {label}] {cucumber_failures.CATEGORIES[item['categorie']]} — {item['titre']} : "
                      f"{item['cause']}")
        if not check["ok"] and not check.get("environnement"):
            lot.update(statut="erreur_compilation", erreur=f"{check['etape']} : {check['erreurs'][0]}"[:300])
            cucumber_tools.save_state(client, state)
            print(f"[LOT {label}] ⚠️ Erreur de {check['etape']} persistante ({len(check['erreurs'])} erreur(s)), "
                  f"commit fait quand même : « Reprendre » relancera la correction.")
            if check.get("quota"):
                break
            continue
        if lot.get("arret"):
            cucumber_tools.save_state(client, state)
            print(f"[LOT {label}] ✋ Génération arrêtée pour décision humaine (voir "
                  f"{cucumber_a_revoir.A_REVOIR_FILE}) : « Reprendre » relancera la correction et la vérification "
                  f"de ce lot.")
            exit_code = 1
            break
        if check["ok"] and compile_cmd and files:
            # Tout le worktree compile : les lots précédents restés en erreur de compilation sont corrigés
            for other in lots:
                if other.get("statut") == "erreur_compilation" and (other.get("verification") or {}).get(
                        "etape") == "compilation":
                    other.update(statut="termine", verification={**other["verification"], "ok": True, "erreurs": []})
                    other.pop("erreur", None)
        lot.pop("erreur", None)
        lot["statut"] = "termine"
        cucumber_tools.save_state(client, state)
        done = sum(1 for r in results if r.get("statut") == "couvert")
        indirect = sum(1 for r in results if r.get("statut") == "couvert_indirectement")
        verified = ("" if not files else " — compilation non vérifiée (environnement Maven)"
                    if check.get("environnement") else f" — {check['etape']} OK")
        print(f"[LOT {label}] ✅ {done}/{len(lot_specs)} spécifique(s) couvert(s)"
              + (f", {indirect} indirectement" if indirect else "")
              + f"{' — commit créé' if committed else ' — aucun fichier écrit'}{verified}.")
        if fixed_values["db_block"]:
            print(f"[LOT {label}] Base consultée : {db_summary(lot.get('base') or [])}.")

    # 4. Patch et récapitulatif, produits en Python ; « couvert_indirectement » vérifié sur l'ensemble des lots
    requalified = cucumber_tools.requalify_indirect(state, inventory)
    if requalified:
        print(f"[CUCUMBER] {len(requalified)} spécifique(s) « couvert_indirectement » requalifié(s) non couvert(s) "
              f"(spécifique cité non couvert) : {', '.join(requalified)}")
    if not check_cucumber_selection(client, worktree, local_cucumber_std_repo, profile, state,
                                    dry_run_cmd if compile_cmd else ""):
        exit_code = 1
    patch_file = cucumber_tools.patch_path(client)
    # Fichiers de l'init globale modifiés par la génération : erreur bloquante, aucun patch livré
    own_init = set((state.get("init_corrigee") or {}).get("fichiers") or [])
    protected = cucumber_checks.check_protected_files(worktree, state["base"],
                                                      [p for p in profile["protected_files"] if p not in own_init],
                                                      restore=False)
    if own_init:
        print(f"[CUCUMBER] ⚠️ Compléments d'initialisation corrigés par l'agent (dans le patch, à relire en premier) : "
              f"{', '.join(sorted(own_init))}")
    state["fichiers_proteges_touches"] = [e.split(" : ", 1)[0] for e in protected["erreurs"]]
    temporary = cucumber_diagnostic.temporary_steps(worktree)
    state["steps_temporaires"] = temporary
    cucumber_tools.save_state(client, state)
    waiting = [t for lot in lots for t in lot.get("diagnostics_en_attente") or []]
    if temporary and set(temporary) <= set(waiting):
        print(f"\n[CUCUMBER] ⏳ Patch non mis à jour : {len(temporary)} step(s) de diagnostic temporaire en attente "
              f"du prochain run (retirés après son import) ; le patch précédent, sans diagnostic, est conservé.")
    elif temporary:
        if os.path.exists(patch_file):
            os.remove(patch_file)
        print(f"\n[CUCUMBER] ⛔ Patch non produit : {len(temporary)} step(s) "
              f"{cucumber_diagnostic.TEMP_PREFIX} encore présent(s) (diagnostic temporaire non retiré) :")
        print("\n".join(f"    {t}" for t in temporary))
        exit_code = 1
    elif protected["erreurs"]:
        if os.path.exists(patch_file):
            os.remove(patch_file)
        print(f"\n[CUCUMBER] ⛔ Patch non produit : {len(protected['erreurs'])} fichier(s) de l'init globale "
              f"modifié(s).")
        print("\n".join(f"    {e}" for e in protected["erreurs"]))
        exit_code = 1
    elif deliver_std_data(client, worktree, local_cucumber_std_repo, profile) is not None and \
            cucumber_tools.write_patch(worktree, state["base"], patch_file, client):
        print(f"\n[CUCUMBER] Patch généré : {patch_file}")
        missing = cucumber_checks.check_patch_files(worktree, state["base"], patch_file, client)
        if missing:
            print(f"[CUCUMBER] ❌ {len(missing)} fichier(s) absent(s) du patch :")
            print("\n".join(f"    {e}" for e in missing))
            exit_code = 1
    else:
        print("\n[CUCUMBER] Aucun test écrit : pas de patch.")
    coverage_file = cucumber_tools.write_coverage_summary(client, inventory, state)
    print(f"[CUCUMBER] Récapitulatif : {coverage_file}")
    # Connaissances partagées : un document de couverture par génération (pas par lot) et l'A_REVOIR.md livré
    cucumber_knowledge = {"client": client, "tags": ["cucumber"], "sources": shared["documents"],
                          "depots": knowledge_tools.repo_commits(worktree, local_cucumber_std_repo)}
    share_file(coverage_file, project, "cucumber-couverture", f"Couverture Cucumber {client}", **cucumber_knowledge)
    share_file(os.path.join(worktree, cucumber_a_revoir.A_REVOIR_FILE), project, "cucumber-a-revoir",
               f"À revoir Cucumber {client}", **cucumber_knowledge)
    not_compiling = [str(lot["numero"]) for lot in lots if lot.get("statut") == "erreur_compilation"]
    if not_compiling:
        print(f"[CUCUMBER] ⚠️ Lot(s) en erreur de compilation, commités tels quels : {', '.join(not_compiling)} "
              f"(« Reprendre » relance leur correction).")
        exit_code = 1
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
                       workspace_dir: str, run_started_at: float, agent_output: str, knowledge: dict = None) -> bool:
    """
    Contrôle le résultat de l'agent : ticket SPEC créé, patch attaché. Rattrape ce que l'agent
    n'a pas fait (création depuis la description, pièce jointe manquante) puis capitalise et dépose la description
    dans les connaissances partagées (`knowledge` : champs communs de l'analyse).
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
    if knowledge is not None:
        share_knowledge(description, args.jira_target_project, "postmerge-cucumber", summary,
                        **{**knowledge, "tickets": [source_issue, new_key]})

    print(f"\n[POST-MERGE] ✅ Ticket {new_key} prêt pour {source_issue} : {summary}")
    print(f"POSTMERGE_RESULT source={source_issue} jira={new_key} patches={len(patch_paths)}")
    return True


if __name__ == "__main__":
    main()