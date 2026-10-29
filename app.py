import difflib
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import streamlit as st
import logging

from dotenv import dotenv_values, load_dotenv

from tools import env_tools
from tools import cucumber_init_fix
from tools import (cucumber_a_revoir, cucumber_audit, cucumber_bilan, cucumber_checks, cucumber_coverage, cucumber_job,
                   cucumber_decisions, cucumber_detection, cucumber_environment, cucumber_import, cucumber_preflight,
                   cucumber_init_data, cucumber_initializers, cucumber_journey, cucumber_profile, cucumber_run,
                   cucumber_links, cucumber_results, cucumber_studio, cucumber_testability, cucumber_tools,
                   cucumber_verify, integration_tools, jenkins_tools, knowledge_tools)
from tools.epic_tools import load_epic_manifest
from tools.jira_tools import (
    JiraSearchError, fetch_tickets_data, fetch_tickets_with_epic_context, get_jira_issue_summary,
    set_jira_description_markdown, std_only_projects,
)
from tools.db_tools import DB_TYPES, test_connection
from tools import pr_review
from tools.jenkins_tools import trigger_cucumber_job
from tools import jenkins_tools
from tools.memory_tools import (
    find_similar_tickets, get_project_lessons, load_ticket_memory, record_manual_edit,
    get_all_memories, clear_memory, delete_ticket_memory, import_existing_deliverables,
)
from tools.poller_control import force_stop, get_poller_status, request_stop, start_poller, tail_log

st.set_page_config(page_title="Scrum Master IA Dashboard", layout="centered")

# Variables indispensables : leur absence est signalée dans l'onglet Lancement
REQUIRED_ENV = ["JIRA_DOMAIN", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY",
                "BITBUCKET_URL", "BITBUCKET_HTTP_TOKEN", "BITBUCKET_STD_REPO_PATH"]


def format_ticket_option(key: str) -> str:
    """Formatte l'affichage d'un ticket Jira sous la forme 'CLÉ - Titre/Summary'."""
    if not key:
        return ""
    jira_titles = st.session_state.get("jira_titles", {})
    summary = jira_titles.get(key)
    if not summary:
        summary = get_jira_issue_summary(key)
        if summary:
            if "jira_titles" not in st.session_state:
                st.session_state["jira_titles"] = {}
            st.session_state["jira_titles"][key] = summary
    return f"{key} - {summary}" if summary else key


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def claude_config_dirs() -> list:
    """Dossiers de configuration Claude Code du poste (un par compte) : ~/.claude et ~/.claude-*."""
    home = os.path.expanduser("~")
    dirs = [os.path.join(home, ".claude")] + sorted(glob.glob(os.path.join(home, ".claude-*")))
    return [d for d in dirs if os.path.isdir(d)]


@st.cache_data(ttl=300, show_spinner=False)
def claude_account(config_dir: str) -> str:
    """Compte connecté dans ce dossier de configuration (`claude auth status`), ou '' s'il est inconnu."""
    env = os.environ.copy()
    env.pop("CLAUDE_CONFIG_DIR", None)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config_dir)
    try:
        result = subprocess.run([shutil.which("claude") or "claude", "auth", "status"], capture_output=True,
                                text=True, encoding="utf-8", errors="replace", env=env, timeout=20,
                                shell=(os.name == "nt"))
        status = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    if not status.get("loggedIn"):
        return "non connecté"
    return " · ".join(v for v in (status.get("email"), status.get("orgName")) if v)


def show_release_note(ticket: str, since: float) -> None:
    """Affiche la release note rédigée avec le patch (ajoutée par main.py en fin de description Jira)."""
    path = f"{ticket}-release-note.md"
    if not os.path.exists(path) or os.path.getmtime(path) < since:
        st.warning(f"Aucune release note produite pour {ticket} lors de cette exécution.", icon=":material/campaign:")
        return
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        note = f.read().strip()
    with st.container(border=True):
        st.markdown("**:material/campaign: Release note** :gray[(ajoutée à la fin de la description Jira)]")
        st.markdown(note or "_Fichier vide._")


# Préfixe des problèmes d'environnement rédigés par main.py (contrôle avant tout lot)
CUCUMBER_ENV_PREFIX = "[CUCUMBER ENVIRONNEMENT] ❌"
JOURNAL_PATH = os.path.join(".tmp", "journal_technique.log")
JOURNAL_MAX_BYTES = 5_000_000


def run_main_step(cmd: list, on_line=None, quiet: bool = True) -> int:
    """
    Lance main.py (`on_line` : appelé à chaque ligne). Toute la sortie va dans le journal technique
    (.tmp/journal_technique.log, panneau « Journal technique » replié de l'en-tête) ; par défaut (`quiet`), rien de
    brut n'est affiché : seul un indicateur d'avancement, et la dernière exécution est gardée pour en tirer des
    phrases (journal_phrases). `quiet=False` réservé au diagnostic. Renvoie le code retour.
    """
    os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
    if os.path.exists(JOURNAL_PATH) and os.path.getsize(JOURNAL_PATH) > JOURNAL_MAX_BYTES:
        os.replace(JOURNAL_PATH, JOURNAL_PATH + ".1")
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    output_container = st.empty() if not quiet else None
    spinner = st.spinner("Traitement en cours… (sortie détaillée : « Journal technique »)") \
        if quiet else None
    if spinner:
        spinner.__enter__()
    logs, lines = "", []
    with open(JOURNAL_PATH, "a", encoding="utf-8") as journal:
        journal.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} — {' '.join(cmd[1:])[:300]}\n")
        for line in process.stdout:
            journal.write(line)
            lines.append(line.rstrip("\n"))
            if output_container is not None:
                logs += line
                output_container.code(logs, language="text")
            if on_line:
                on_line(line)
        process.wait()
        journal.write(f"===== code retour {process.returncode}\n")
    if spinner:
        spinner.__exit__(None, None, None)
    st.session_state["last_run_lines"] = lines
    return process.returncode


def journal_phrases(prefix: str) -> list:
    """Phrases rédigées par main.py dans la dernière exécution, sous un préfixe (ex. problèmes d'environnement)."""
    return [line.split(prefix, 1)[1].strip() for line in st.session_state.get("last_run_lines") or []
            if prefix in line]


def interruption_cause(returncode: int) -> str:
    """Cause d'une interruption, en une phrase : problème d'environnement, sinon dernière erreur signalée."""
    environment = journal_phrases(CUCUMBER_ENV_PREFIX)
    if environment:
        return environment[0]
    errors = [re.sub(r"^\s*(\[[^\]]*\]\s*)+", "", line).replace("❌", "").replace("⛔", "").strip()
              for line in st.session_state.get("last_run_lines") or [] if "❌" in line or "⛔" in line]
    errors = [e for e in errors if e and not e.startswith(("Traceback", "File "))]
    return (errors[-1][:240] if errors else
            f"le traitement s'est arrêté avant la fin (code retour {returncode}), voir le journal technique")


def render_technical_journal() -> None:
    """Panneau « Journal technique », replié : sortie brute des traitements (jamais dans une carte)."""
    with st.expander("Journal technique", icon=":material/terminal:"):
        if not os.path.exists(JOURNAL_PATH):
            st.caption("Aucun traitement lancé pour l'instant.")
            return
        with open(JOURNAL_PATH, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-400:]
        st.caption(f"Sortie brute des derniers traitements (400 dernières lignes) — {JOURNAL_PATH}")
        st.code("".join(tail), language="text")


def run_epic_patches(epic_key: str, project: str, client_repo: str, db_schema: str) -> bool:
    """Génère le patch de chaque tâche enfant de l'épopée, dans l'ordre du découpage. S'arrête au premier échec."""
    tasks = [t for t in load_epic_manifest(epic_key).get("tasks", []) if t.get("key")]
    if not tasks:
        st.error(f"Aucune tâche enfant créée pour {epic_key} : lancez d'abord l'analyse de l'épopée.")
        return False
    for index, task in enumerate(tasks, 1):
        st.info(f"[{index}/{len(tasks)}] Génération du patch de {task['key']} — {task.get('summary', '')}...")
        patch_started_at = time.time()
        returncode = run_main_step([
            sys.executable, "main.py",
            "--issue", task["key"],
            "--project", project,
            "--client-repo", client_repo,
            "--step", "patch",
            "--db-schema", db_schema
        ])
        if returncode != 0:
            remaining = ", ".join(t["key"] for t in tasks[index:]) or "aucune"
            st.error(f"La génération du patch de {task['key']} a échoué (code {returncode}). Tâches non traitées : {remaining}.")
            return False
        st.success(f"Patch généré et attaché au ticket Jira {task['key']}.")
        show_release_note(task["key"], patch_started_at)
    return True


def search_criteria_form(key_prefix: str) -> dict:
    """Critères de recherche Jira, mêmes champs et valeurs par défaut dans les onglets Lancement et Intégration."""
    # Recherche rapide « épopées ouvertes de l'équipe » (onglet Configuration)
    open_epics_team = os.getenv("JIRA_OPEN_EPICS_TEAM", "").strip()
    open_epics_status = os.getenv("JIRA_OPEN_EPICS_STATUS", "").strip()
    open_epics_only = st.checkbox(
        f"Épopées au statut « {open_epics_status} » de l'équipe {open_epics_team}"
        if open_epics_team and open_epics_status else "Épopées ouvertes de mon équipe",
        key=f"{key_prefix}open_epics_only",
        disabled=not (open_epics_team and open_epics_status),
        help="Si coché, la recherche porte sur les épopées du projet cible à ce statut pour cette équipe : "
             "les critères Équipe, Statuts et Labels ci-dessous sont ignorés. "
             "Équipe et statut : JIRA_OPEN_EPICS_TEAM et JIRA_OPEN_EPICS_STATUS (onglet Configuration)."
    ) and bool(open_epics_team and open_epics_status)

    col1, col2 = st.columns(2)
    with col1:
        jira_project_input = st.text_input(
            "Projet Jira cible :",
            value=os.getenv("JIRA_PROJECT_KEY", ""),
            key=f"{key_prefix}search_project",
            help="Clé du projet Jira (ex. PROJ)."
        )
        jira_team_input = st.text_input(
            "Équipe / Composant (Optionnel) :",
            value=os.getenv("JIRA_SEARCH_TEAM", ""),
            key=f"{key_prefix}search_team",
            help="Composant Jira ou équipe Atlassian (ex. WMS, Socle...)",
            disabled=open_epics_only
        )

    with col2:
        jira_status_input = st.text_input(
            "États / Statuts (Séparés par une virgule) :",
            value=os.getenv("JIRA_SEARCH_STATUSES", ""),
            key=f"{key_prefix}search_statuses",
            help="Ex: Analysée, En cours, A traiter (laissez vide pour tous les états)",
            disabled=open_epics_only
        )
        jira_label_input = st.text_input(
            "Labels (Séparés par une virgule) :",
            value=os.getenv("JIRA_SEARCH_LABELS", ""),
            key=f"{key_prefix}search_labels",
            help="Ex. le label d'attente de l'équipe (laissez vide si aucun label requis)",
            disabled=open_epics_only
        )

    return {
        "project_key": jira_project_input.strip().upper(),
        "statuses": [s.strip() for s in jira_status_input.split(",") if s.strip()],
        "labels": [l.strip() for l in jira_label_input.split(",") if l.strip()],
        "team": jira_team_input.strip(),
        "open_epics_only": open_epics_only,
        "open_epics_team": open_epics_team,
        "open_epics_status": open_epics_status,
    }


def search_jira_tickets(criteria: dict) -> list:
    """Tickets correspondant aux critères de search_criteria_form. Lève JiraSearchError."""
    if criteria["open_epics_only"]:
        return fetch_tickets_data(
            project_key=criteria["project_key"],
            statuses=[criteria["open_epics_status"]],
            epics_only=True,
            atlassian_team=criteria["open_epics_team"]
        )
    return fetch_tickets_data(
        project_key=criteria["project_key"],
        statuses=criteria["statuses"],
        labels=criteria["labels"],
        team=criteria["team"]
    )


ENV_PATH = ".env"
if not os.path.exists(ENV_PATH):
    open(ENV_PATH, "w", encoding="utf-8").close()

# override=True : Streamlit ré-exécute ce script à chaque interaction ; on recharge donc la
# configuration enregistrée (onglet ⚙️) au lieu de garder les valeurs du démarrage.
# CLAUDE_CONFIG_DIR vide dans le .env : on garde celui du shell de lancement (mémorisé au premier passage).
os.environ.setdefault("AGENT_SHELL_CLAUDE_CONFIG_DIR", os.environ.get("CLAUDE_CONFIG_DIR", ""))
# Lignes du .env en défaut : un message clair au démarrage (numéros de ligne) au lieu des avertissements bruts
logging.getLogger("dotenv.main").setLevel(logging.ERROR)
load_dotenv(ENV_PATH, override=True)
if not os.getenv("CLAUDE_CONFIG_DIR", "").strip():
    os.environ["CLAUDE_CONFIG_DIR"] = os.environ["AGENT_SHELL_CLAUDE_CONFIG_DIR"]

st.title("🤖 Tableau de bord - Agent Scrum Master")

env_problems = env_tools.check_env(ENV_PATH)
if env_problems:
    with st.container(border=True):
        st.warning(env_tools.check_line(env_problems), icon=":material/warning:")
        if st.button("Neutraliser les lignes invalides", icon=":material/comment:", key="env_neutralize",
                     help="Met ces lignes en commentaire (rien n'est supprimé) ; copie de l'ancien fichier : .env.bak"):
            st.success(f"{env_tools.neutralize_invalid_lines(ENV_PATH)} ligne(s) mise(s) en commentaire ; copie : "
                       f".env.bak.")
            st.rerun()

tab_integration, tab_execution, tab_cucumber, tab_review, tab_merges, tab_knowledge, tab_config = st.tabs(
    ["🧩 Intégration", "🚀 Lancement", "🥒 Cucumber", "🔍 Revue de PR", "🔔 Écoute des merges",
     "📚 Connaissances partagées", "⚙️ Configuration (.env)"])

# Initialisation de l'état
if "workflow_step" not in st.session_state:
    st.session_state["workflow_step"] = "INIT"

# ---------------------------------------------------------
# ONGLET 1 : LANCEMENT & RECHERCHE PARAMÉTRABLE
# ---------------------------------------------------------
with tab_execution:
    render_technical_journal()
    missing_env = [name for name in REQUIRED_ENV if not os.getenv(name, "").strip()]
    if missing_env:
        st.warning("Configuration incomplète : renseignez " + ", ".join(missing_env)
                   + " dans l'onglet « ⚙️ Configuration ».", icon=":material/settings:")

    st.markdown("### 1. Critères de Recherche Jira")

    search_criteria = search_criteria_form("")
    project_key = search_criteria["project_key"]

    search_clicked = st.button("🔍 Rechercher les tickets", type="primary", use_container_width=True)

    if search_clicked:
        with st.spinner(f"Recherche Jira sur le projet {project_key}..."):
            try:
                tickets_data = search_jira_tickets(search_criteria)
                search_error = None
            except JiraSearchError as e:
                tickets_data, search_error = [], str(e)

            if search_error:
                st.session_state["jira_tickets"] = []
                st.session_state["jira_titles"] = {}
                st.error(f"❌ {search_error}")
            elif tickets_data:
                found_keys = [t["key"] for t in tickets_data]
                jira_titles = {t["key"]: t["summary"] for t in tickets_data if t.get("summary")}
                st.session_state["jira_tickets"] = found_keys
                st.session_state["jira_titles"] = jira_titles
                st.session_state["jira_context"] = {t["key"]: t for t in tickets_data}
                st.session_state["selected_index"] = 0
                st.session_state["selected_jira_project"] = project_key
                st.session_state["workflow_step"] = "INIT"
                
                formatted_list = [f"{t['key']} - {t['summary']}" if t.get('summary') else t['key'] for t in tickets_data]
                st.success(f"{len(found_keys)} ticket(s) trouvé(s) :\n" + "\n".join([f"- {item}" for item in formatted_list]))
            else:
                st.session_state["jira_tickets"] = []
                st.session_state["jira_titles"] = {}
                st.warning(f"Aucun ticket trouvé avec ces critères sur le projet {project_key}.")

    if "jira_tickets" in st.session_state and st.session_state["jira_tickets"]:
        tickets = st.session_state["jira_tickets"]
        current_project = st.session_state.get("selected_jira_project", os.getenv("JIRA_PROJECT_KEY", ""))
        
        st.markdown("---")
        st.markdown("### 2. Sélection du Ticket & Mode d'Exécution")
        
        std_only = (current_project or "").upper() in std_only_projects()
        if std_only:
            st.info(f"ℹ️ Projet {current_project} (STD_ONLY_PROJECTS) : analyse basée uniquement sur le socle "
                    f"Standard (branche dev).")
            client_repo_path = "STD_ONLY"
        else:
            client_repo_path = st.text_input(
                "Chemin local ou URL Bitbucket du dépôt Client :",
                value=os.getenv("BITBUCKET_CLIENT_REPO_PATH", ""),
                help="Ex: https://git.kiscloud.io/projects/PHEACR ou C:\\development\\PHEACR"
            )

        db_schema_input = st.text_input(
            "Schéma BDD cible :",
            value=os.getenv("DB_SCHEMA", ""),
            key="db_schema_input",
            help="Schéma de la base client consultée en lecture seule par l'agent (structure des tables, paramétrage, "
                 "données du cas). Laissez vide pour que l'agent n'utilise pas la base."
        ).strip()

        if "selected_index" not in st.session_state:
            st.session_state["selected_index"] = 0

        selected_ticket = st.selectbox(
            "Sélectionne le ticket Jira à traiter :",
            options=tickets,
            index=st.session_state["selected_index"],
            key="ticket_selectbox",
            format_func=format_ticket_option
        )
        
        st.session_state["selected_index"] = tickets.index(selected_ticket)

        # Indicateur de capitalisation : mêmes critères que ceux utilisés par main.py
        ticket_ctx = st.session_state.get("jira_context", {}).get(selected_ticket, {})
        similar = find_similar_tickets(
            current_project,
            component_name=ticket_ctx.get("components", []),
            query_text=f"{ticket_ctx.get('summary', '')}\n{ticket_ctx.get('description', '')}",
            exclude_issue_key=selected_ticket,
        )
        lessons = get_project_lessons(current_project, exclude_issue_key=selected_ticket)
        own_feedbacks = load_ticket_memory(selected_ticket).get("feedbacks", [])

        with st.container(border=True):
            st.markdown("**:material/psychology: Mémoire de l'agent**")
            with st.container(horizontal=True):
                st.metric("Tickets similaires", len(similar))
                st.metric("Consignes projet", len(lessons))
                st.metric("Retours sur ce ticket", len(own_feedbacks))
            if similar:
                with st.expander("Analyses et patchs similaires injectés dans le prompt"):
                    for item in similar:
                        mem = item["memory"]
                        st.markdown(f"**{mem['issue_key']}** — {mem.get('summary', '')}  \n"
                                    f":gray[{mem.get('status')} · score {item['score']} · {', '.join(item['reasons'])}]")
                        files = mem.get("patch_info", {}).get("modified_files", [])
                        if files:
                            st.caption("Fichiers du patch : " + ", ".join(files[:8]))
            if lessons:
                with st.expander("Consignes récurrentes appliquées"):
                    st.markdown("\n".join(f"- {l}" for l in lessons))
            if not similar and not lessons:
                st.caption("Aucune expérience mémorisée pertinente pour ce ticket.")

        # Vérification si une analyse existe déjà localement pour ce ticket
        cadrage_file = f"{selected_ticket}-cadrage.md"
        # Épopée : l'analyse la découpe en tâches enfants, chacune avec sa propre analyse puis son patch
        is_epic = bool(ticket_ctx.get("is_epic"))
        epic_manifest_tasks = load_epic_manifest(selected_ticket).get("tasks", []) if is_epic else []
        epic_tasks = [t for t in epic_manifest_tasks if t.get("key")]
        # Découpage rédigé mais tâches pas (toutes) créées dans Jira : on les crée sans relancer l'agent
        epic_pending = len(epic_manifest_tasks) - len(epic_tasks)
        has_existing_cadrage = bool(epic_tasks) and not epic_pending if is_epic else os.path.exists(cadrage_file)

        if is_epic:
            if epic_pending:
                st.warning(f"Le découpage de {selected_ticket} est rédigé ({len(epic_manifest_tasks)} tâche(s)) mais "
                           f"{epic_pending} tâche(s) ne sont pas encore créées dans Jira.")
            elif epic_tasks:
                st.success(f"💡 {format_ticket_option(selected_ticket)} est déjà découpée en {len(epic_tasks)} tâche(s) : "
                           + ", ".join(t["key"] for t in epic_tasks) + ".")
            else:
                st.info("ℹ️ Épopée : l'analyse crée la ou les tâches enfants nécessaires (une seule si un découpage "
                        "n'est pas utile), avec leur analyse fonctionnelle et technique en description.")
        elif has_existing_cadrage:
            st.success(f"💡 Une analyse préalable existe déjà pour {format_ticket_option(selected_ticket)} (`{cadrage_file}`).")

        col_cadrage, col_direct_patch, col_next = st.columns([2, 2, 1])

        with col_cadrage:
            if is_epic and epic_pending:
                btn_cadrage_label = "📌 Créer les tâches dans Jira (analyse prête)"
            elif is_epic:
                btn_cadrage_label = "📝 Relire les analyses des tâches" if has_existing_cadrage else "⚡ 1. Analyser l'épopée & créer les tâches"
            else:
                btn_cadrage_label = "🔄 Revoir l'analyse" if has_existing_cadrage else "⚡ 1. Lancer l'analyse"
            run_cadrage_btn = st.button(btn_cadrage_label, use_container_width=True)

        with col_direct_patch:
            if is_epic:
                btn_patch_label = "🛠️ Générer les Patchs des tâches"
            else:
                btn_patch_label = "🛠️ Générer le Patch (Analyse prête)" if has_existing_cadrage else "🚀 Générer le Patch directement"
            run_direct_patch_btn = st.button(btn_patch_label, type="primary", use_container_width=True,
                                             disabled=is_epic and not has_existing_cadrage)

        with col_next:
            has_next = st.session_state["selected_index"] < len(tickets) - 1
            if st.button("➡️ Suivant", disabled=not has_next, use_container_width=True):
                st.session_state["selected_index"] += 1
                st.session_state["workflow_step"] = "INIT"
                st.rerun()

        # Épopée : analyse avec création des tâches enfants, relecture, ou patchs de toutes les tâches
        if is_epic and (run_cadrage_btn or run_direct_patch_btn):
            if not std_only and not client_repo_path.strip():
                st.error("Veuillez renseigner le chemin local ou l'URL Bitbucket du dépôt client.")
            elif run_direct_patch_btn:
                if run_epic_patches(selected_ticket, current_project, client_repo_path.strip(), db_schema_input):
                    st.success(f"🎉 Patchs générés pour toutes les tâches de l'épopée {selected_ticket} !")
                    st.session_state["workflow_step"] = "EPIC_FINISHED"
                    st.session_state["current_ticket"] = selected_ticket
                    st.session_state["current_client_repo"] = client_repo_path.strip()
            else:
                returncode = 0
                if not has_existing_cadrage:
                    st.info(f"Création des tâches enfants de {selected_ticket} dans Jira..." if epic_pending
                            else f"Analyse de l'épopée {selected_ticket} et création des tâches enfants en cours...")
                    returncode = run_main_step([
                        sys.executable, "main.py",
                        "--issue", selected_ticket,
                        "--project", current_project,
                        "--client-repo", client_repo_path.strip(),
                        "--step", "epic-sync" if epic_pending else "epic-cadrage",
                        "--db-schema", db_schema_input
                    ])
                if returncode == 0:
                    st.session_state["workflow_step"] = "REVIEW_EPIC"
                    st.session_state["current_ticket"] = selected_ticket
                    st.session_state["current_client_repo"] = client_repo_path.strip()
                    st.rerun()
                else:
                    st.error("Une erreur est survenue lors de l'analyse de l'épopée : consultez le journal technique (en haut de l'onglet) "
                             "(les tâches déjà créées sont conservées et réutilisées à la relance).")
            run_cadrage_btn = run_direct_patch_btn = False

        # Option A : Lancer ou revoir l'analyse
        if run_cadrage_btn:
            if not std_only and not client_repo_path.strip():
                st.error("Veuillez renseigner le chemin local ou l'URL Bitbucket du dépôt client.")
            else:
                st.info(f"Analyse/Cadrage en cours pour {selected_ticket}...")
                cmd_cadrage = [
                    sys.executable, "main.py",
                    "--issue", selected_ticket,
                    "--project", current_project,
                    "--client-repo", client_repo_path.strip(),
                    "--step", "cadrage",
                    "--db-schema", db_schema_input
                ]

                returncode = run_main_step(cmd_cadrage)

                if returncode == 0:
                    st.success("Analyse terminée et sauvegardée.")
                    st.session_state["workflow_step"] = "REVIEW_ANALYSIS"
                    st.session_state["current_ticket"] = selected_ticket
                    st.session_state["current_client_repo"] = client_repo_path.strip()
                    st.rerun()
                else:
                    st.error("Une erreur est survenue lors du cadrage.")

        # Option B : Génération directe du Patch (sans passer par la relecture)
        if run_direct_patch_btn:
            if not std_only and not client_repo_path.strip():
                st.error("Veuillez renseigner le chemin local ou l'URL Bitbucket du dépôt client.")
            else:
                st.info(f"Génération directe du patch et envoi sur Jira pour {selected_ticket}...")
                cmd_patch = [
                    sys.executable, "main.py",
                    "--issue", selected_ticket,
                    "--project", current_project,
                    "--client-repo", client_repo_path.strip(),
                    "--step", "patch",
                    "--db-schema", db_schema_input
                ]
                patch_started_at = time.time()

                returncode = run_main_step(cmd_patch)

                if returncode == 0:
                    st.success(f"🎉 Patch généré et attaché avec succès au ticket Jira {selected_ticket} !")
                    show_release_note(selected_ticket, patch_started_at)
                    st.session_state["workflow_step"] = "FINISHED"
                else:
                    st.error("Une erreur est survenue lors de la génération du patch.")

    # ---------------------------------------------------------
    # PHASE 2 : RELECTURE, REVISION ET VALIDATION DU PATCH
    # ---------------------------------------------------------
    if st.session_state.get("workflow_step") in ["REVIEW_ANALYSIS", "FINISHED"]:
        active_ticket = st.session_state.get("current_ticket", "")
        active_repo = st.session_state.get("current_client_repo", "")
        active_project = st.session_state.get("selected_jira_project", os.getenv("JIRA_PROJECT_KEY", ""))

        st.markdown("---")
        st.subheader(f"📝 Relecture & Ajustement de l'Analyse ({active_ticket})")

        cadrage_file = f"{active_ticket}-cadrage.md"
        cadrage_content = ""
        if os.path.exists(cadrage_file):
            with open(cadrage_file, "r", encoding="utf-8") as f:
                cadrage_content = f.read()

        user_analysis = st.text_area(
            "Analyse enregistrée (Modifiable directement avant de générer le patch) :",
            value=cadrage_content,
            height=300
        )

        user_feedback = st.text_input(
            "Consignes de révision pour l'agent (Optionnel) :",
            placeholder="Ex: Préciser l'impact sur les retours, ajuster le composant..."
        )

        col_review, col_validate = st.columns(2)

        with col_review:
            if st.button("🔄 Demander une révision à l'agent", use_container_width=True):
                st.info("Révision du cadrage par l'agent...")

                if record_manual_edit(active_ticket, cadrage_content, user_analysis):
                    st.toast("Vos corrections manuelles ont été mémorisées.", icon=":material/edit_note:")

                with open(cadrage_file, "w", encoding="utf-8") as f:
                    f.write(user_analysis)

                cmd_refine = [
                    sys.executable, "main.py",
                    "--issue", active_ticket,
                    "--project", active_project,
                    "--client-repo", active_repo,
                    "--step", "refine",
                    "--feedback", user_feedback.strip(),
                    "--db-schema", st.session_state.get("db_schema_input", "").strip()
                ]

                returncode = run_main_step(cmd_refine)

                if returncode == 0:
                    st.toast("Analyse révisée et mise à jour !", icon=":material/check_circle:")
                    st.rerun()
                else:
                    # Pas de rerun : les logs restent affichés pour diagnostiquer l'échec
                    st.error(f"La révision a échoué (code {returncode}). Rien n'a été enregistré en mémoire : consultez le journal technique (en haut de l'onglet).")

        with col_validate:
            if st.button("✅ Valider l'analyse & Générer le Patch", type="primary", use_container_width=True):
                st.info("Génération du patch Git et envoi sur Jira...")

                if record_manual_edit(active_ticket, cadrage_content, user_analysis):
                    st.toast("Vos corrections manuelles ont été mémorisées.", icon=":material/edit_note:")

                with open(cadrage_file, "w", encoding="utf-8") as f:
                    f.write(user_analysis)

                cmd_val_patch = [
                    sys.executable, "main.py",
                    "--issue", active_ticket,
                    "--project", active_project,
                    "--client-repo", active_repo,
                    "--step", "patch",
                    "--db-schema", st.session_state.get("db_schema_input", "").strip()
                ]
                patch_started_at = time.time()

                returncode = run_main_step(cmd_val_patch)

                if returncode == 0:
                    st.success(f"🎉 Traitement terminé avec succès ! Le patch est attaché au ticket {active_ticket}.")
                    show_release_note(active_ticket, patch_started_at)
                    st.session_state["workflow_step"] = "FINISHED"
                else:
                    st.error("Une erreur est survenue lors de la création du patch.")
                    
    # ---------------------------------------------------------
    # PHASE 2 (ÉPOPÉE) : RELECTURE DES TÂCHES ENFANTS ET PATCHS
    # ---------------------------------------------------------
    if st.session_state.get("workflow_step") in ["REVIEW_EPIC", "EPIC_FINISHED"]:
        active_epic = st.session_state.get("current_ticket", "")
        active_repo = st.session_state.get("current_client_repo", "")
        active_project = st.session_state.get("selected_jira_project", os.getenv("JIRA_PROJECT_KEY", ""))
        active_schema = st.session_state.get("db_schema_input", "").strip()
        epic_manifest = load_epic_manifest(active_epic)
        review_tasks = [t for t in epic_manifest.get("tasks", []) if t.get("key")]

        st.markdown("---")
        st.subheader(f"📝 Relecture des tâches de l'épopée ({active_epic})")
        if epic_manifest.get("justification"):
            st.caption(f"Découpage en {len(review_tasks)} tâche(s) — {epic_manifest['justification']}")

        task_edits = {}
        for task in review_tasks:
            task_file = f"{task['key']}-cadrage.md"
            task_content = ""
            if os.path.exists(task_file):
                with open(task_file, "r", encoding="utf-8") as f:
                    task_content = f.read()
            with st.expander(f"{task['key']} — {task.get('summary', '')}", expanded=len(review_tasks) == 1):
                task_edits[task["key"]] = (task_file, task_content, st.text_area(
                    f"Analyse de {task['key']} (Modifiable directement avant de générer les patchs) :",
                    value=task_content,
                    height=300
                ))

        def save_task_edits():
            """Enregistre les corrections manuelles (fichier local, mémoire, description Jira de la tâche)."""
            for task_key, (task_file, before, after) in task_edits.items():
                with open(task_file, "w", encoding="utf-8") as f:
                    f.write(after)
                if record_manual_edit(task_key, before, after):
                    st.toast(f"Vos corrections sur {task_key} ont été mémorisées.", icon=":material/edit_note:")
                    try:
                        set_jira_description_markdown(task_key, after)
                    except Exception as e:
                        st.warning(f"Description Jira de {task_key} non mise à jour : {e}")

        epic_feedback = st.text_input(
            "Consignes de révision pour l'agent (Optionnel) :",
            placeholder="Ex: Regrouper les tâches 1 et 2, séparer la partie Studio, préciser l'impact sur les retours...",
            key="epic_feedback"
        )

        col_epic_review, col_epic_validate = st.columns(2)

        with col_epic_review:
            if st.button("🔄 Demander une révision à l'agent", use_container_width=True, key="epic_refine_btn"):
                st.info("Révision du découpage et des analyses par l'agent...")
                save_task_edits()
                returncode = run_main_step([
                    sys.executable, "main.py",
                    "--issue", active_epic,
                    "--project", active_project,
                    "--client-repo", active_repo,
                    "--step", "epic-cadrage",
                    "--feedback", epic_feedback.strip(),
                    "--db-schema", active_schema
                ])
                if returncode == 0:
                    st.toast("Analyses révisées et tâches Jira mises à jour !", icon=":material/check_circle:")
                    st.rerun()
                else:
                    st.error(f"La révision a échoué (code {returncode}) : consultez le journal technique (en haut de l'onglet).")

        with col_epic_validate:
            if st.button("✅ Valider les analyses & Générer les Patchs", type="primary", use_container_width=True,
                         key="epic_validate_btn", disabled=not review_tasks):
                save_task_edits()
                if run_epic_patches(active_epic, active_project, active_repo, active_schema):
                    st.success(f"🎉 Traitement terminé : un patch est attaché à chaque tâche de l'épopée {active_epic}.")
                    st.session_state["workflow_step"] = "EPIC_FINISHED"

    # Dans app.py, sous le bouton de validation du patch :
    if st.session_state.get("workflow_step") == "FINISHED":
        st.markdown("---")
        st.subheader("🧪 Validation Cucumber / Jenkins")
        
        job_name = st.text_input("Nom du Job Jenkins Cucumber :", value=os.getenv("JENKINS_CUCUMBER_JOB", ""))
        
        if st.button("🚀 Lancer les tests Cucumber sur Jenkins", use_container_width=True):
            # Un seul build Cucumber à la fois (runs et vérifications de l'onglet Cucumber compris)
            busy = jenkins_tools.busy_builds(jenkins_tools.exclusive_jobs(job_name))
            if busy:
                st.warning("Un build Cucumber est déjà en cours (un seul à la fois) : " + " ; ".join(busy)
                           + ". Relancez une fois terminé.")
                st.stop()
            with st.spinner("Déclenchement du build Jenkins..."):
                build_url = trigger_cucumber_job(
                    job_name=job_name,
                    parameters={
                        "ISSUE_KEY": active_ticket,
                        "PROJECT": active_project,
                        "PATCH_APPLIED": "true"
                    }
                )
                if build_url:
                    st.success(f"Job lancé ! Suivi du build : [{build_url}]({build_url})")
                else:
                    st.error("Échec du lancement du job Jenkins.")


# ---------------------------------------------------------
# ONGLET 2 : INTÉGRATION — BESOIN REFORMULÉ ET QUESTIONS OUVERTES
# ---------------------------------------------------------
def run_besoin_step(issue_key: str, project: str, application: str) -> int:
    """Rédaction (ou complément) du besoin par l'agent, journal affiché au fil de l'eau."""
    return run_main_step([
        sys.executable, "main.py",
        "--issue", issue_key,
        "--project", project,
        "--step", "besoin",
        "--application", application
    ])


def integration_option_label(ticket: dict) -> str:
    """'CLÉ - Titre [statut]', suivi du nombre de questions en attente s'il y en a."""
    label = f"{ticket['key']} - {ticket.get('summary', '')} [{ticket.get('status', '?')}]"
    state = integration_tools.load_state(ticket["key"])
    if state:
        pending = len(state.get("questions", [])) - len(integration_tools.answered(state))
        label += f" · ❓ {pending} en attente" if pending else " · ✅ besoin rédigé"
    return label


def render_cucumber_run(ctx: dict, state: dict) -> None:
    """
    Étape 5, run Jenkins automatique (cucumber_run) : push du worktree sur la branche lue par le job, contrôle du
    commit, lancement avec la branche et les tags, suivi, puis import de la console et des rapports (bilan).
    """
    client = ctx["client"]
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))
    job, branch = cucumber_run.run_job(profile), profile["verify_branch"]
    with st.container(border=True):
        st.markdown("##### :material/rocket_launch: Lancer un run Jenkins")
        missing = [label for label, ok in (("JENKINS_URL (onglet Configuration)", jenkins_tools.jenkins_configured()),
                                           ("job Jenkins du client (profil)", job),
                                           ("branche lue par le job (profil)", branch)) if not ok]
        if missing:
            st.caption("Lancement automatique indisponible, à renseigner : " + ", ".join(missing) + ". Sinon, "
                       "importez la console et le rapport ci-dessous.")
            return
        st.caption(f"Job **{job}** sur la branche de travail **{branch}** : le worktree y est commité et poussé "
                   f"(jamais de push forcé), le commit est contrôlé en tête de la branche distante avant le lancement "
                   f"(configuration actuelle du job, attente décochée, filtre de tags dans ses arguments), puis le "
                   f"build est suivi et sa console et ses rapports Cucumber JSON sont importés.")
        tested = cucumber_job.deployment_line(cucumber_job.load_deployment(client))
        if tested:
            st.caption(f":material/inventory_2: {tested}")
        with st.container(horizontal=True, vertical_alignment="bottom"):
            tags = st.text_input("Tags (filtre -Dcucumber.filter.tags ajouté aux arguments du job, vide : tous "
                                 "les scénarios actifs)",
                                 key=f"cucumber_run_tags_{client}", placeholder="Ex. @agent_<client>_3")
            launch = st.button("Pousser et lancer le run", icon=":material/play_arrow:", type="primary",
                               disabled=not ctx["ready"])
        if launch:
            st.info("Run Jenkins en cours : push, lancement, suivi du build puis import…")
            code = run_main_step(ctx["cmd"] + ["--run-jenkins", "--run-tags", tags.strip()])
            (st.success if code in (0, 1) else st.error)(
                "Run importé : voir le résultat ci-dessous et le bilan." if code in (0, 1) else
                "Run non lancé ou non abouti : voir le journal technique (en haut de l'onglet).")
            state.update(cucumber_tools.load_state(client))
        last = cucumber_run.last_run(state)
        if last:
            st.caption(f"Dernier lancement : {last['date'].replace('T', ' ')[:16]} · commit {last['commit'][:10]} · "
                       f"{last['message']}" + (f" · [build]({last['build']})" if last.get("build") else ""))
            if last.get("selection"):
                st.warning(f"Filtre de tags : {last['selection']}", icon=":material/filter_alt:")
            elif last.get("ok") and last.get("tags"):
                st.caption(f":material/check: Filtre « {last['tags']} » vérifié : le nombre de tests exécutés est "
                           f"celui attendu.")


def render_cucumber_import(client: str, state: dict, cmd: list = None) -> None:
    """
    Étape 5, import d'un rapport d'exécution (0 token) : fichiers déposés ou build Jenkins, secrets masqués en
    mémoire, analyse puis bilan (A_REVOIR.md, répertoire partagé) ; résultat du dernier import (tests, échecs,
    comparaison, initialisation en échec, scénarios anormalement longs) et logs serveur à exporter.
    """
    st.markdown("#### :material/upload_file: Import d'un rapport d'exécution")
    with st.form(f"cucumber_import_{client}", border=True):
        uploaded = st.file_uploader(
            "Console Jenkins, rapport Cucumber JSON, exports de logs serveur (CSV)", accept_multiple_files=True,
            type=["txt", "log", "json", "csv"],
            help="Les secrets (-Dpassword, -DuserName, jetons, valeurs base64) sont masqués avant tout stockage ; "
                 "seule l'empreinte du fichier brut est gardée.")
        build_url = st.text_input("Ou URL d'un build Jenkins", placeholder="https://jenkins/job/…/123/",
                                  help="Console et rapports Cucumber JSON du build, lus en mémoire.")
        submitted = st.form_submit_button("Importer et analyser", icon=":material/analytics:")
    if submitted:
        files = {f.name: f.getvalue() for f in uploaded or []}
        if build_url.strip():
            files.update(cucumber_import.build_files(build_url.strip()))
        imported = cucumber_import.import_files(client, files, build_url.strip())
        for message in imported["messages"]:
            st.info(message, icon=":material/info:")
        if imported["numero"]:
            refresh_bilan(client, cucumber_import.analyse(client, imported["numero"], state,
                                                          cucumber_tools.worktree_dir(client)), state)
        elif not imported["messages"]:
            st.warning("Aucun fichier à importer.")
    numero, analysis = latest_import_analysis(client)
    if not analysis:
        st.caption("Aucun import pour ce client.")
        return
    st.caption(f"Import n° {numero} — run {analysis.get('run') or '?'}"
               + (f", build #{analysis['build']}" if analysis.get("build") else "")
               + " · " + ", ".join(f"{f['nom']} ({f['type']})" for f in analysis["fichiers"]))
    if analysis["non_analyse"]:
        st.error(f"Non analysé : {analysis['non_analyse']}", icon=":material/block:")
        return
    init = analysis.get("init_en_echec") or {}
    if init:
        st.error(f"**Initialisation en échec : aucun scénario n'est jugé.** {init['id']} — premier step en échec : "
                 f"`{init['step'] or '—'}` — cause : {init['message']}", icon=":material/block:")
        # Échec dans les compléments d'init écrits par l'agent : il peut les corriger lui-même (ces fichiers seuls)
        if cmd and cucumber_init_fix.enabled() and cucumber_init_fix.is_complements(init["id"].split("::", 1)[0]):
            st.caption("Cet échec est dans les compléments d'initialisation écrits par l'agent : il peut les corriger "
                       "lui-même (ces fichiers seuls, contrôlés), puis rejouer l'init si CUCUMBER_AGENT_JENKINS est "
                       "coché, sinon livrer un patch de correction à appliquer.")
            if st.button("Corriger les compléments d'init", icon=":material/build:", type="primary",
                         key=f"cucumber_fix_init_{client}_{numero}"):
                code = run_main_step(cmd + ["--fix-init-from-import", str(numero)])
                patch = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     f"{client}-cucumber-init-correction.patch")
                if code == 0 and os.path.exists(patch):
                    st.success("Compléments d'init corrigés : patch à relire, appliquer et pousser.")
                    with open(patch, "rb") as f:
                        st.download_button("Télécharger le patch de correction de l'init", f.read(),
                                           file_name=os.path.basename(patch), icon=":material/download:",
                                           key=f"cucumber_fix_init_patch_{client}_{numero}")
                elif code == 0:
                    st.success("Init corrigée et vérifiée sur Jenkins.")
                else:
                    st.error("Correction impossible ou init toujours en échec : voir le journal technique.")
    for warning in analysis.get("avertissements") or []:
        st.warning(warning, icon=":material/schedule:")
    failures = sum(len(g["scenarios"]) for g in analysis["groupes"])
    comparison = analysis.get("comparaison") or {}
    with st.container(horizontal=True):
        st.metric("Tests", analysis["tests"], border=True)
        st.metric("Échecs", failures, border=True)
        st.metric("Causes racine", len(analysis["groupes"]), border=True)
        if comparison:
            st.metric(f"Corrigés depuis l'import n° {comparison['import']}", len(comparison["corriges"]), border=True)
            st.metric("Nouveaux", len(comparison["nouveaux"]), border=True)
            st.metric("Régressions", len(comparison.get("regressions", [])), border=True,
                      help="Scénarios verts au run précédent, en échec dans celui-ci")
            st.metric("Inchangés", len(comparison["inchanges"]), border=True)
    durations = analysis.get("durees") or {}
    if durations.get("lents"):
        with st.expander(f"Scénarios anormalement longs : {len(durations['lents'])} (médiane "
                         f"{durations['mediane']} s, run de {durations['total']} s)", icon=":material/timer:"):
            st.dataframe([{"Scénario": e["id"].split("::", 1)[-1], "Feature": e["id"].split("::", 1)[0],
                           "Durée (s)": e["secondes"], "Motif": e["motif"]} for e in durations["lents"]],
                         hide_index=True, width="stretch")
    uncovered = [e for e in analysis["logs"] if e["a_exporter"]]
    if uncovered:
        with st.expander(f"Logs serveur à exporter ({len(uncovered)})", icon=":material/schedule:"):
            for entry in uncovered:
                st.markdown(f"- `{entry['id']}` : {entry['a_exporter']}")


def refresh_bilan(client: str, analysis: dict, state: dict) -> None:
    """Bilan d'un import (A_REVOIR.md commité, bilan.md), déposé dans le répertoire partagé ; résultat affiché."""
    bilan = cucumber_bilan.after_import(client, analysis, state, cucumber_tools.worktree_dir(client))
    shared = cucumber_bilan.share_bilan(client, bilan)
    st.success(f"Bilan du run n° {bilan['import']} écrit" + (" et ajouté à A_REVOIR.md (commité)" if bilan["a_revoir"]
                                                              else "") + f". Prochaine action : "
               f"{bilan['prochaine_action']}", icon=":material/summarize:")
    if shared:
        st.caption(knowledge_tools.publish_line(shared))


def latest_import_analysis(client: str) -> tuple:
    """(numéro, analyse) du dernier import ; (0, {}) sans import."""
    index = cucumber_import.load_index(client)
    if not index["imports"]:
        return 0, {}
    numero = max(e["numero"] for e in index["imports"])
    return numero, cucumber_tools._read_json(os.path.join(cucumber_import.imports_dir(client), str(numero),
                                                          "analyse.json"), {}) or {}


def refresh_a_revoir(client: str, state: dict, decision: dict) -> None:
    """A_REVOIR.md régénéré après une décision (scénarios désactivés à jour), commité dans le worktree."""
    worktree = cucumber_tools.worktree_dir(client)
    if decision.get("statut") == "en_attente" or not os.path.isdir(worktree):
        return
    specifics = {s["id"]: s for s in cucumber_tools.load_inventory(client).get("specifiques") or []}
    if cucumber_a_revoir.write_a_revoir(worktree, state, specifics):
        cucumber_decisions.commit_decision(worktree, client, state.get("base", ""), [cucumber_a_revoir.A_REVOIR_FILE],
                                           f"A_REVOIR.md : décision {decision['id']} prise")


def render_decision(client: str, decision: dict, state: dict, client_tag: str) -> None:
    """Une décision en attente : proposition, preuve, modification gardée à part ; Valider, Refuser, Commenter."""
    with st.container(border=True):
        st.markdown(f"**{cucumber_decisions.TYPES[decision['type']].capitalize()}** :gray[({decision['id']}, "
                    f"lot {decision.get('lot', '?')})] — {decision['proposition']}")
        details = [f"preuve : `{decision['preuve']}`" if decision.get("preuve") else "",
                   f"réactivation : {decision['reactivation']}" if decision.get("reactivation") else ""]
        if any(details):
            st.caption(" · ".join(d for d in details if d))
        if decision.get("apres"):
            with st.expander("Modification gardée à part", icon=":material/difference:"):
                st.code("\n".join(difflib.unified_diff(decision["avant"], decision["apres"], "avant", "proposé",
                                                        lineterm="")), language="diff")
        comments = [e for e in decision["historique"] if e["commentaire"]]
        for event in comments:
            st.caption(f"{event['auteur']}, {event['date'][:16].replace('T', ' ')} : « {event['commentaire']} »")
        with st.container(horizontal=True, vertical_alignment="bottom"):
            comment = st.text_input("Commentaire (pourquoi)", key=f"decision_comment_{decision['id']}")
            for action, label, icon in (("valider", "Valider", ":material/check:"),
                                        ("refuser", "Refuser", ":material/close:"),
                                        ("commenter", "Commenter", ":material/comment:")):
                if st.button(label, key=f"decision_{action}_{decision['id']}", icon=icon,
                             disabled=action == "commenter" and not comment.strip()):
                    done = cucumber_decisions.resolve(client, decision["id"], action, comment,
                                                      cucumber_tools.worktree_dir(client), state.get("base", ""),
                                                      client_tag)
                    cucumber_decisions.sync_state(state, done)
                    cucumber_tools.save_state(client, state)
                    refresh_a_revoir(client, state, done)
                    st.rerun()


def render_cucumber_decisions(client: str, state: dict) -> None:
    """File des décisions humaines (point 7) : en attente, puis historique (qui, quand, pourquoi)."""
    decisions = cucumber_decisions.load_decisions(client)
    waiting = [d for d in decisions if d["statut"] == "en_attente"]
    if cucumber_decisions.autonomous():
        taken = [d for d in decisions if d.get("auto")]
        st.markdown(f"##### :material/smart_toy: Choix faits par l'agent : {len(taken)}")
        st.caption("Autonomie : l'agent applique lui-même ce qui change le sens d'un test (réalignement sur le code, "
                   "données d'un step, désactivation) et l'enregistre ici, avec l'avant et l'après, pour relecture.")
    else:
        st.markdown(f"##### :material/gavel: Décisions en attente : {len(waiting)}")
        st.caption("Ce qui change le sens d'un test attend votre décision ; le scénario concerné est désactivé "
                   "provisoirement. Valider applique la modification, Refuser rétablit le scénario et transmet votre "
                   "commentaire à sa prochaine correction.")
    client_tag = cucumber_profile.effective_profile(cucumber_profile.load_profile(client))["tag_client"]
    for decision in waiting:
        render_decision(client, decision, state, client_tag)
    decided = [d for d in decisions if d["statut"] != "en_attente"]
    if decided:
        with st.expander(f"Historique ({len(decided)})", icon=":material/history:",
                         expanded=cucumber_decisions.autonomous()):
            st.dataframe([{"Décision": cucumber_decisions.TYPES[d["type"]], "Proposition": d["proposition"],
                           "Statut": cucumber_decisions.STATUSES[d["statut"]], "Par": d["historique"][-1]["auteur"],
                           "Le": d["historique"][-1]["date"][:16].replace("T", " "),
                           "Pourquoi": d["historique"][-1]["commentaire"]} for d in decided],
                         hide_index=True, width="stretch")


def render_cucumber_corrections(client: str, cucumber_cmd: list, rows: list, state: dict) -> None:
    """
    Étape 6 : décisions en attente, causes racine du dernier import, lots de correction proposés (échecs « test »
    des scénarios de l'agent), causes à prouver, diagnostics en attente du prochain run et tâches en échec.
    """
    render_cucumber_decisions(client, state)
    waiting = [t for lot in state.get("lots") or [] for t in lot.get("diagnostics_en_attente") or []]
    if waiting:
        st.info(f"{len(waiting)} step(s) de diagnostic temporaire en attente du prochain run (poussés sur la branche "
                f"lue par le job) : leur sortie sera lue à l'import, puis ils seront retirés.",
                icon=":material/troubleshoot:")
    failing = [r for r in rows if r["etat"] == "echec"]
    if failing:
        st.markdown(f"**{len(failing)} tâche(s) en échec**")
        st.dataframe([{"Tâche": r["tache"], "Cause": r["cause"], "Dernière exécution": r["derniere"]}
                      for r in failing], hide_index=True, width="stretch")
    numero, analysis = latest_import_analysis(client)
    if not analysis or analysis.get("non_analyse"):
        st.caption("Aucun import analysé : importe le résultat du run (écran principal, cadre « 3 »).")
        return
    init = analysis.get("init_en_echec") or {}
    if init:
        st.error(f"Initialisation en échec au run n° {numero} : aucun scénario n'est jugé ni corrigé. Corrigez "
                 f"l'initialisation ({init['step'] or init['id']} — {init['message']}), puis relancez le run.",
                 icon=":material/block:")
    if analysis["groupes"]:
        st.dataframe([{"Scénarios": len(g["scenarios"]), "Cause": g["libelle"], "Catégorie": g["categorie"],
                       "Step": g["step"], "Message": g["message"]} for g in analysis["groupes"]], hide_index=True)
    for item in analysis.get("a_prouver") or []:
        with st.container(border=True):
            st.markdown(f"**Cause à prouver** — `{item['id']}` :gray[({len(item['tentatives'])} correction(s) sans "
                        f"effet : l'agent arrête de deviner)]")
            st.caption(f"Message : {item['message'][:300]}")
            st.markdown("Essayé :\n" + "\n".join(f"- lot {t['lot']} : {t['correction']}" for t in item["tentatives"])
                        + "\n\nMéthodes de preuve :\n" + "\n".join(f"- {m}" for m in item["methodes"]))
    if analysis["propositions"]:
        count = sum(len(p["scenarios"]) for p in analysis["propositions"])
        st.markdown(f"**{len(analysis['propositions'])} lot(s) de correction proposé(s)** ({count} scénario(s) de "
                    f"l'agent en échec pour une cause « test ») ; les autres échecs relèvent d'actions humaines.")
        if st.button("Lancer la correction ciblée", icon=":material/build:", key=f"cucumber_import_fix_{numero}"):
            st.info(f"Correction de l'import n° {numero} en cours...")
            run_main_step(cucumber_cmd + ["--complete", "--from-import", str(numero)])


with tab_integration:
    render_technical_journal()
    st.markdown("### 1. Critères de Recherche Jira")
    integration_criteria = search_criteria_form("integ_")

    if st.button("🔍 Rechercher les tickets", type="primary", use_container_width=True, key="integ_search"):
        with st.spinner(f"Recherche Jira sur le projet {integration_criteria['project_key']}..."):
            try:
                integration_found, integration_error = search_jira_tickets(integration_criteria), None
            except JiraSearchError as e:
                integration_found, integration_error = [], str(e)
        st.session_state["integ_tickets"] = [
            {"key": t["key"], "summary": t.get("summary", ""), "status": t.get("status", "")} for t in integration_found
        ]
        st.session_state["integ_project"] = integration_criteria["project_key"]
        if integration_error:
            st.error(f"❌ {integration_error}")
        elif not integration_found:
            st.warning(f"Aucun ticket trouvé avec ces critères sur le projet {integration_criteria['project_key']}.")

    integration_tickets = st.session_state.get("integ_tickets", [])
    if integration_tickets:
        st.markdown("---")
        st.markdown(f"### 2. Tickets trouvés ({len(integration_tickets)})")
        tickets_by_key = {t["key"]: t for t in integration_tickets}
        integ_ticket = st.selectbox(
            "Ticket à intégrer :",
            list(tickets_by_key),
            format_func=lambda key: integration_option_label(tickets_by_key[key]),
            key="integ_selected",
        )
        integ_project = st.session_state.get("integ_project") or os.getenv("JIRA_PROJECT_KEY", "")
        jira_domain = os.getenv("JIRA_DOMAIN", "").replace("https://", "").replace("http://", "").strip("/")
        if jira_domain:
            st.caption(f"[Ouvrir {integ_ticket} dans Jira](https://{jira_domain}/browse/{integ_ticket})")

        application_field = os.getenv("JIRA_APPLICATION_FIELD", "").strip()
        integ_application = st.text_input(
            "Projet / Application des tickets de référence (optionnel) :",
            key=f"integ_application_{integ_ticket}",
            placeholder="Vide = valeur du champ Projet / Application du ticket",
            disabled=not application_field,
            help="L'agent consulte tous les tickets Jira (tous projets) de ce Projet / Application pour rédiger le "
                 "besoin. Ex: APPLI. Plusieurs valeurs séparées par des virgules. "
                 + ("" if application_field else "Renseignez JIRA_APPLICATION_FIELD dans l'onglet Configuration."),
        ).strip()

        has_state = bool(integration_tools.load_state(integ_ticket))
        if st.button("📝 Rédaction du besoin" if not has_state else "📝 Rédiger à nouveau le besoin (avec les réponses)",
                     type="primary", use_container_width=True, key="integ_redaction",
                     help="L'agent reformule le besoin et liste les questions sans réponse dans l'expression initiale. "
                          "Le résultat est ajouté à la description Jira (section « Intégration du besoin »)."):
            st.info(f"Rédaction du besoin de {integ_ticket} en cours...")
            returncode = run_besoin_step(integ_ticket, integ_project, integ_application)
            if returncode == 0:
                st.success(f"Besoin reformulé et questions ajoutés à la description Jira de {integ_ticket}.")
            else:
                st.error(f"La rédaction du besoin a échoué (code {returncode}).")

        integ_state = integration_tools.load_state(integ_ticket)
        if integ_state:
            st.markdown("### 3. Besoin reformulé")
            with st.container(border=True):
                st.markdown(integ_state.get("besoin", "") or "_Vide._")

            questions = integ_state.get("questions", [])
            pending_count = len(questions) - len(integration_tools.answered(integ_state))
            st.markdown(f"### 4. Questions ({pending_count} en attente)" if questions else "### 4. Questions")
            if not questions:
                st.success("Aucune question ouverte : le besoin est complet.")
            else:
                # La clé dépend de la date de l'état : les champs repartent des valeurs enregistrées après chaque mise à jour
                version = integ_state.get("updated_at", "")
                with st.form(f"integ_answers_{integ_ticket}"):
                    answers = {}
                    for q in questions:
                        st.markdown(f"**Q{q['id']}.** {q['question']}")
                        if q.get("pourquoi"):
                            st.caption(f"Pourquoi : {q['pourquoi']}")
                        answers[q["id"]] = st.text_area(
                            f"Réponse Q{q['id']}", value=q.get("reponse", ""), height=80,
                            key=f"integ_answer_{integ_ticket}_{q['id']}_{version}",
                            label_visibility="collapsed", placeholder="Votre réponse…",
                        )
                    save_answers = st.form_submit_button(
                        "📨 Envoyer les réponses et compléter le besoin", type="primary", use_container_width=True,
                        help="Les réponses sont ajoutées à la description Jira, puis l'agent complète le besoin "
                             "reformulé avec ces réponses et ne garde que les questions restantes.")
                if save_answers:
                    changed = [q for q in questions if answers[q["id"]].strip() != q.get("reponse", "").strip()]
                    for q in questions:
                        q["reponse"] = answers[q["id"]].strip()
                    if not changed:
                        st.info("Aucune réponse nouvelle ou modifiée : rien à envoyer.")
                    else:
                        integration_tools.save_state(integ_ticket, integ_state)
                        # Réponses publiées d'abord : elles restent dans Jira même si l'agent échoue ensuite
                        try:
                            with st.spinner(f"Ajout des réponses à la description de {integ_ticket}..."):
                                integration_tools.publish_state(integ_ticket, integ_state)
                            publish_error = None
                        except Exception as e:
                            publish_error = e
                        if publish_error:
                            st.error(f"Réponses enregistrées localement, mais la mise à jour Jira a échoué : {publish_error}")
                        else:
                            st.info(f"{len(changed)} réponse(s) ajoutée(s) à Jira. L'agent complète le besoin...")
                            returncode = run_besoin_step(integ_ticket, integ_project, integ_application)
                            cadrage_done = None
                            if returncode == 0 and os.path.exists(f"{integ_ticket}-cadrage.md"):
                                st.info(f"Besoin complété. L'agent complète maintenant le cadrage de {integ_ticket}...")
                                cadrage_done = run_main_step([
                                    sys.executable, "main.py",
                                    "--issue", integ_ticket,
                                    "--project", integ_project,
                                    "--step", "cadrage-reponses"
                                ]) == 0
                            if returncode == 0 and cadrage_done is not False:
                                st.session_state["integ_completed"] = (integ_ticket, cadrage_done)
                                st.rerun()
                            if returncode != 0:
                                st.error(f"Les réponses sont dans Jira, mais l'agent n'a pas pu compléter le besoin "
                                         f"(code {returncode}). Relancez la rédaction ci-dessus.")
                            else:
                                st.error(f"Besoin complété, mais le cadrage {integ_ticket}-cadrage.md n'a pas pu être "
                                         "complété avec les réponses : voir le journal technique (en haut de l'onglet).")
                st.caption("Les réponses déjà envoyées restent dans la description Jira, sous leur question.")
            completed = st.session_state.get("integ_completed")
            if completed and completed[0] == integ_ticket:
                del st.session_state["integ_completed"]
                remaining = len(questions) - len(integration_tools.answered(integ_state))
                st.success(f"Besoin de {integ_ticket} complété avec les réponses"
                           + (" ainsi que le cadrage" if completed[1] else "") + ", et mis à jour dans Jira. "
                           + (f"{remaining} question(s) restent ouvertes." if remaining else "Plus aucune question ouverte."))
                if completed[1] is None:
                    st.caption(f"Pas encore de cadrage pour {integ_ticket} : l'analyse lancée depuis l'onglet "
                               "Lancement tiendra compte des réponses présentes dans la description.")


NEW_PROJECT = "__nouveau__"
PROJECT_KEYS = {"client_repo": "cucumber_repo", "client_name": "cucumber_client_name",
                "db_schema": "cucumber_db_schema", "application": "cucumber_application",
                "std_repo": "cucumber_std_repo"}


def load_cucumber_project() -> None:
    """Projet choisi dans la liste : ses réglages enregistrés remplissent « Client et dépôts » (vides : nouveau)."""
    choice = st.session_state.get("cucumber_project")
    settings = cucumber_detection.known_projects(cucumber_tools.CACHE_ROOT).get(choice) or {}
    if choice != NEW_PROJECT:
        settings = {"client_name": choice, **{k: v for k, v in settings.items() if v}}
    for key, state_key in PROJECT_KEYS.items():
        st.session_state[state_key] = settings.get(key) or (
            os.getenv("BITBUCKET_CUCUMBER_STD_REPO", "") if key == "std_repo" else "")
    # Nom saisi conservé : le nom déduit du dépôt ne le remplace pas
    st.session_state["cucumber_client_derived"] = cucumber_tools.client_name_from_repo(
        st.session_state["cucumber_repo"])


def cucumber_header() -> dict:
    """
    Projet choisi dans la liste des projets connus (ou nouveau). Les réglages du projet (dépôts, schéma, application)
    ne sont affichés ici que pour un projet nouveau ou incomplet ; sinon ils sont dans le mode expert
    (render_project_settings). Renvoie le contexte commun : client, commande main.py, prêt ou non, capture de la
    référence Git du Standard, réglages déjà affichés ou non.
    """
    # Valeurs par défaut du .env, puis dernières valeurs saisies (conservées dans la session)
    st.session_state.setdefault("cucumber_repo", os.getenv("BITBUCKET_CLIENT_REPO_PATH", ""))
    st.session_state.setdefault("cucumber_db_schema", os.getenv("CUCUMBER_DB_SCHEMA", "") or os.getenv("DB_SCHEMA", ""))
    st.session_state.setdefault("cucumber_application", "")
    st.session_state.setdefault("cucumber_std_repo", os.getenv("BITBUCKET_CUCUMBER_STD_REPO", ""))
    projects = cucumber_detection.known_projects(cucumber_tools.CACHE_ROOT)
    options = list(projects) + [NEW_PROJECT]
    current = st.session_state.get("cucumber_project")
    typed = (st.session_state.get("cucumber_client_name") or "").strip().lower()
    if current == NEW_PROJECT and typed in projects:
        # Nouveau projet enregistré : il rejoint la liste
        st.session_state["cucumber_project"] = typed
    elif current not in options:
        env_client = cucumber_tools.client_name_from_repo(os.getenv("BITBUCKET_CLIENT_REPO_PATH", ""))
        st.session_state["cucumber_project"] = env_client if env_client in projects else options[0]
        load_cucumber_project()
    st.selectbox("Projet", options, key="cucumber_project", on_change=load_cucumber_project,
                 format_func=lambda k: "Nouveau projet…" if k == NEW_PROJECT else k.capitalize(),
                 help="Projets déjà travaillés (leurs réglages sont gardés).")
    # Réglages non affichés pendant un passage (écran simplifié) : Streamlit efface l'état de leurs champs ; un
    # projet connu dont le dépôt est vide retrouve donc ses réglages enregistrés
    chosen = st.session_state.get("cucumber_project")
    if chosen in projects and not (st.session_state.get("cucumber_repo") or "").strip() \
            and (projects.get(chosen) or {}).get("client_repo"):
        load_cucumber_project()
    # Nom du client recalculé quand le dépôt change, sauf s'il a été modifié à la main depuis
    repo_input = (st.session_state.get("cucumber_repo") or "").strip()
    derived_client = cucumber_tools.client_name_from_repo(repo_input)
    if st.session_state.get("cucumber_client_derived") != derived_client:
        st.session_state["cucumber_client_derived"] = derived_client
        st.session_state["cucumber_client_name"] = derived_client
    client = (st.session_state.get("cucumber_client_name") or "").strip().lower()
    schema = (st.session_state.get("cucumber_db_schema") or "").strip()
    application = (st.session_state.get("cucumber_application") or "").strip()
    std_input = (st.session_state.get("cucumber_std_repo") or "").strip()

    def capture_std_ref(line: str) -> None:
        if line.startswith(cucumber_tools.STD_REF_LOG):
            st.session_state["cucumber_std_ref"] = {"repo": std_input,
                                                    "ref": line[len(cucumber_tools.STD_REF_LOG):].strip()}

    ready = bool(repo_input and client)
    if ready:
        settings = {"client_repo": repo_input, "client_name": client, "db_schema": schema,
                    "application": application, "std_repo": std_input}
        if projects.get(client) != settings:
            cucumber_detection.save_project(client, settings)
    cucumber_cmd = [
        sys.executable, "main.py",
        "--step", "generate-cucumber-client",
        "--client-repo", repo_input,
        "--client-name", client,
        "--application", application,
        "--db-schema", schema,
        "--cucumber-std-repo", std_input,
    ]
    ctx = {"client": client, "cmd": cucumber_cmd, "ready": ready, "capture": capture_std_ref,
           "reglages_affiches": not ready or st.session_state["cucumber_project"] == NEW_PROJECT}
    with st.expander("Données du projet", icon=":material/settings:", expanded=ctx["reglages_affiches"]):
        render_project_settings(ctx)
    return ctx


def render_project_settings(ctx: dict) -> None:
    """Réglages du projet : dépôt du client, nom, schéma BDD, application Jira, dépôt du Cucumber Standard."""
    with st.container(border=False):
        st.text_input("Dépôt Bitbucket du projet client :", key="cucumber_repo",
                      help="URL du projet Bitbucket (ex. https://git.kiscloud.io/projects/CLIENT) ou chemin d'un clone "
                           "local.")
        col_client, col_schema, col_app = st.columns(3)
        with col_client:
            st.text_input("Nom du client :", key="cucumber_client_name",
                          help="Déduit du dépôt (clé du projet Bitbucket). Le dépôt cible est <client>-cucumber.")
        with col_schema:
            st.text_input("Schéma BDD (optionnel) :", key="cucumber_db_schema",
                          help="Schéma consultable en lecture seule dans la base de l'environnement de test Cucumber "
                               "(CUCUMBER_DB_*, onglet Configuration ; à défaut la base DB_*). Vide = CUCUMBER_DB_SCHEMA "
                               "si cette base est renseignée, sinon l'agent n'utilise pas la base.")
        with col_app:
            st.text_input("Projet / Application Jira (optionnel) :", key="cucumber_application",
                          disabled=not os.getenv("JIRA_APPLICATION_FIELD", "").strip(),
                          help="Ex: APPLI. Les tickets Jira ayant cette valeur (champ JIRA_APPLICATION_FIELD) servent de "
                               "référence fonctionnelle sur les spécifiques.")
        if ctx["client"]:
            st.caption(f"Dépôt cible : **{ctx['client']}-cucumber** · identifiant de travail "
                       f"**{cucumber_tools.work_id(ctx['client'])}**")
        std_input = st.text_input(
            "Dépôt Bitbucket du Cucumber Standard :", key="cucumber_std_repo",
            help="URL de navigation (.../projects/X/repos/Y/browse), URL clonable (.../scm/x/y.git) ou chemin d'un "
                 "clone local, utilisé tel quel. Défaut : BITBUCKET_CUCUMBER_STD_REPO (onglet Configuration).").strip()
        last = st.session_state.get("cucumber_std_ref") or {}
        if last.get("repo") == std_input and last.get("ref"):
            st.caption(f"Référence Git utilisée lors de la dernière exécution : **{last['ref']}**")


def render_cucumber_detection(ctx: dict) -> None:
    """
    Détection automatique en arrière-plan (inventaire, profil, faits de l'init ; 0 token, lecture seule) : lancée
    quand le projet est prêt puis, à chaque contrôle périodique, si les commits des dépôts ont changé. Indicateur
    discret pendant l'analyse ; la page est rechargée à la fin.
    """
    client = ctx["client"]
    if not ctx["ready"]:
        return
    cucumber_detection.start(client, ctx["cmd"])
    active = cucumber_detection.running(client)

    def watch() -> None:
        if cucumber_detection.running(client):
            st.session_state[f"cucumber_detecting_{client}"] = True
            st.caption(":material/progress_activity: Analyse du projet en cours…")
            return
        if st.session_state.pop(f"cucumber_detecting_{client}", False):
            cucumber_detection.finish(client)
            st.rerun()
        if cucumber_detection.start(client, ctx["cmd"]):
            st.rerun()

    st.fragment(watch, run_every="3s" if active else f"{cucumber_detection.interval()}s")()


def render_cucumber_findings(status: dict) -> None:
    """Constats de l'init, chacun avec ses preuves (fichier, ligne, extrait, ou résultat de run)."""
    for finding in status.get("constats") or []:
        st.markdown(f"- **{finding['libelle']}** : {finding['message']}"
                    + ("" if finding["livrable"] or finding["type"] == "absente" else
                       " :gray[(à traiter à la main : fichier protégé ou preuve de run)]"))
        st.caption("  \n".join(cucumber_detection.proof_line(p) for p in finding["preuves"][:5])
                   + (f"  \n… {len(finding['preuves']) - 5} autre(s)" if len(finding["preuves"]) > 5 else ""))


PROFILE_TEXT_FIELDS = ("features_root", "tag_client", "init_tags", "clear_name", "library_version", "server_version",
                       "verify_branch", "verify_branch_param", "run_job", "run_tags_target")


def profile_field_inputs(client: str, fields: list, profile: dict, profile_doc: dict) -> dict:
    """Champs du profil à corriger (ceux de `fields` seulement) ; renvoie les valeurs saisies."""
    values = {}
    for key in (k for k in PROFILE_TEXT_FIELDS if k in fields):
        values[key] = st.text_input(cucumber_profile.LABELS[key], value=profile[key],
                                    placeholder={"verify_branch_param": cucumber_profile.DEFAULT_VERIFY_BRANCH_PARAM,
                                                 "run_job": "DOSSIER/JOB",
                                                 "run_tags_target": "arguments | parametre | les_deux"}.get(key, ""))
    for key in (k for k in ("init_features", "protected_files", "job_params") if k in fields):
        values[key] = [line.strip() for line in st.text_area(
            f"{cucumber_profile.LABELS[key]}, un par ligne", value="\n".join(profile[key]),
            height=100).splitlines() if line.strip()]
    if "spec_files" in fields:
        options = list(dict.fromkeys(profile["spec_files"] + (profile_doc.get("suggestions") or {}).get("spec_files",
                                                                                                        [])))
        values["spec_files"] = st.multiselect(
            f"{cucumber_profile.LABELS['spec_files']}, dans l'ordre d'intégration", options,
            default=profile["spec_files"], help="Intégrés par l'init après l'initialisation globale, dans l'ordre de "
                                                "sélection. Suggestions : fichiers de données du dépôt client hors "
                                                "initialisation Standard.")
    if "reference_codes" in fields:
        codes = {}
        with st.container(horizontal=True):
            for category in cucumber_profile.REFERENCE_CATEGORIES:
                codes[category] = [c.strip() for c in st.text_input(
                    cucumber_init_data.LABELS.get(category, category),
                    value=", ".join((profile["reference_codes"] or {}).get(category) or [])).split(",") if c.strip()]
        values["reference_codes"] = codes
    if "initializers_survive_clear" in fields:
        values["initializers_survive_clear"] = st.segmented_control(
            cucumber_profile.LABELS["initializers_survive_clear"], list(cucumber_profile.SURVIVE_CLEAR),
            format_func=cucumber_profile.SURVIVE_CLEAR.get, default=profile["initializers_survive_clear"] or "",
            help="Réponse globale, appliquée aux données dont le statut est « inconnu ».") or ""
    if "server_initialized_data" in fields and profile["server_initialized_data"]:
        import pandas as pd
        initialized = pd.DataFrame(profile["server_initialized_data"])
        values["server_initialized_data"] = st.data_editor(
            initialized, hide_index=True, num_rows="fixed",
            column_order=("statut", "code", "entite", "classe", "depot", "mode", "certitude"),
            disabled=[c for c in initialized.columns if c != "statut"],
            column_config={"statut": st.column_config.SelectboxColumn(
                "Statut après un clear", options=list(cucumber_initializers.STATUSES),
                format_func=cucumber_initializers.STATUSES.get, required=True)},
            key=f"cucumber_initialized_{client}").to_dict("records")
    if "environment_checks" in fields:
        import pandas as pd
        checks = pd.DataFrame(profile["environment_checks"] or [], columns=["type", "entite", "code"])
        edited = st.data_editor(
            checks, hide_index=True, num_rows="dynamic",
            column_config={"type": st.column_config.SelectboxColumn(
                "Type", options=list(cucumber_environment.TYPES), format_func=cucumber_environment.TYPES.get,
                required=True), "entite": st.column_config.TextColumn("Entité"),
                "code": st.column_config.TextColumn("Code", required=True)},
            key=f"cucumber_env_checks_{client}")
        values["environment_checks"] = [{k: str(v or "").strip() for k, v in row.items()}
                                        for row in edited.to_dict("records")
                                        if row.get("type") and str(row.get("code") or "").strip()]
    if "job_roles" in fields:
        st.markdown(f"**{cucumber_profile.LABELS['job_roles']}**")
        st.caption("Nom du paramètre du job qui porte chaque rôle (détecté sur le job, vide : aucun). Les valeurs "
                   "des arguments ne sont jamais affichées ni enregistrées.")
        roles = profile.get("job_roles") or {}
        values["job_roles"] = {}
        with st.container(horizontal=True):
            for role, label in cucumber_job.ROLES.items():
                values["job_roles"][role] = st.text_input(role, value=roles.get(role, ""), help=label,
                                                          key=f"cucumber_job_role_{client}_{role}").strip()
    if "runner" in fields:
        values["runner"] = st.text_area(f"{cucumber_profile.LABELS['runner']} (JSON)",
                                        value=json.dumps(profile["runner"], ensure_ascii=False, indent=1), height=150)
    return values


def render_cucumber_profile(ctx: dict, profile_doc: dict, detection: dict, status: dict) -> None:
    """
    Profil client en lecture seule, replié : projet, racine des features, tag client, init, runner, versions. Un
    seul indicateur (vert : complet, orange : champs manquants, rouge : détection impossible). « Corriger » ouvre
    l'édition des seuls champs manquants (ou choisis comme faux) ; l'enregistrement relance la détection.
    """
    client = ctx["client"]
    profile = cucumber_profile.effective_profile(profile_doc)
    indicator = cucumber_detection.profile_indicator(detection, profile)
    missing = [cucumber_profile.LABELS[k] for k in indicator["manquants"]]
    title = {"vert": "🟢 Profil client — complet",
             "orange": f"🟠 Profil client — {len(missing)} champ(s) manquant(s) : {', '.join(missing)}",
             "rouge": f"🔴 Profil client — détection impossible : {indicator['cause']}",
             "attente": "⚪ Profil client — en attente de la première détection"}[indicator["couleur"]]
    with st.expander(title, expanded=False):
        runner = profile["runner"]
        st.markdown("\n".join([
            f"- **Projet** : {client.capitalize()} (dépôt `{client}-cucumber`)",
            f"- **Racine des features** : `{profile['features_root'] or '—'}`",
            f"- **Tag client** : `{profile['tag_client'] or '—'}`",
            f"- **Init** : {cucumber_detection.state_label(status)}"
            + (f" ({', '.join(f'`{p}`' for p in profile['init_features'][:3])})" if profile["init_features"] else ""),
            f"- **Runner** : " + (f"`{runner['classe']}`" + (" (surcouche du Standard)" if runner.get("surcouche")
                                                              else "") if runner.get("classe") else "—"),
            f"- **Versions** : bibliothèque de steps {profile['library_version'] or '—'}, serveur "
            f"{profile['server_version'] or '—'}"]))
        render_cucumber_findings(status)
        for warning in profile["avertissements"]:
            st.caption(f":orange[⚠️ {warning}]")
        if detection.get("date"):
            st.caption(f"Détecté le {detection['date'].replace('T', ' ')[:16]} (commits {detection.get('empreinte')}),"
                       f" contrôlé le {(detection.get('verifie_le') or '').replace('T', ' ')[:16]}.")
        if not profile_doc:
            return
        editing_key = f"cucumber_profile_edit_{client}"
        if st.button("Corriger", type="tertiary", icon=":material/edit:", key=f"cucumber_profile_fix_{client}"):
            st.session_state[editing_key] = not st.session_state.get(editing_key, False)
        if not st.session_state.get(editing_key):
            return
        depot = profile_doc.get("depot") or {}
        editable = [k for k in cucumber_profile.FIELDS if k not in depot and k not in ("workarounds", "reponses")]
        extra = st.multiselect("Autres champs faux à corriger", [k for k in editable if k not in indicator["manquants"]],
                               format_func=cucumber_profile.LABELS.get, key=f"cucumber_profile_extra_{client}")
        fields = [k for k in editable if k in indicator["manquants"] or k in extra]
        if not fields:
            st.caption("Aucun champ manquant : choisissez ci-dessus les champs à corriger.")
            return
        with st.form(f"cucumber_profile_{client}"):
            values = profile_field_inputs(client, fields, profile, profile_doc)
            if st.form_submit_button("Enregistrer et relancer la détection", icon=":material/save:"):
                if "runner" in values:
                    try:
                        values["runner"] = json.loads(values["runner"])
                    except ValueError:
                        st.error("Runner : JSON invalide, profil non enregistré.")
                        return
                # Saisie complète : champs non affichés repris du profil appliqué (la détection est filtrée)
                current = {k: profile[k] for k in editable + ["reponses"]}
                cucumber_profile.save_user_fields(client, {**current, **values})
                cucumber_detection.start(client, ctx["cmd"], force=True)
                st.session_state[editing_key] = False
                st.rerun()


def render_cucumber_health(ctx: dict) -> None:
    """Bilan de santé du dépôt client (compilation et dry-run : plusieurs minutes), à la demande, replié."""
    client, capture = ctx["client"], ctx["capture"]
    health_file = cucumber_checks.health_path(client) if client else ""
    health = ""
    if health_file and os.path.exists(health_file):
        with open(health_file, "r", encoding="utf-8", errors="replace") as f:
            health = f.read()
    drifted = "décalé de sa bibliothèque" in health or "⚠️ le client utilise la bibliothèque" in health
    verdict = cucumber_journey.health_verdict(client) if client else ""
    with st.expander(("⚠️ Projet client décalé de sa bibliothèque — " if drifted else "") + "Bilan de santé du dépôt "
                     "client" + (f" : {verdict} " + time.strftime("(%d/%m/%Y %H:%M)", time.localtime(
                         os.path.getmtime(health_file))) if health else " : non lancé"),
                     icon=":material/health_and_safety:"):
        if st.button("Lancer le bilan de santé", icon=":material/play_arrow:", disabled=not ctx["ready"],
                     help="Contrôle le dépôt <client>-cucumber tel quel : emplacement et casse du tag client de ses "
                          "features, fichiers de données manquants (le dépôt client n'a pas ceux du Standard à "
                          "l'exécution), puis compilation et dry-run de toutes ses features (steps non définis ou "
                          "ambigus). Plusieurs minutes. Aucun appel à Claude."):
            code = run_main_step(ctx["cmd"] + ["--health-only"], on_line=capture)
            if code == 0:
                st.success("Dépôt client sain.")
            elif code == 1:
                st.warning("Le dépôt client a des défauts : voir le rapport. Les échecs qu'ils provoquent à "
                           "l'exécution ne viendront pas des tests générés.", icon=":material/warning:")
            else:
                st.error("Le bilan de santé a échoué : voir le journal technique (en haut de l'onglet).")
            st.rerun()
        if health:
            st.markdown(health)


def render_init_patch(client: str) -> None:
    """Patch du lot d'initialisation (séparé des tests), téléchargeable."""
    init_patch = cucumber_tools.init_patch_path(client) if client else ""
    if not (init_patch and os.path.exists(init_patch)):
        return
    generated_at = time.strftime("%d/%m/%Y %H:%M", time.localtime(os.path.getmtime(init_patch)))
    with st.container(border=True):
        st.markdown(f"**:material/description: Patch d'initialisation** `{init_patch}` :gray[(généré le "
                    f"{generated_at})]")
        with open(init_patch, "rb") as f:
            st.download_button("Télécharger le patch d'initialisation", f.read(), file_name=os.path.basename(init_patch),
                               mime="text/x-diff", icon=":material/download:", key=f"cucumber_init_patch_{client}")


COVERAGE_COLUMNS = {
    "Générer": st.column_config.CheckboxColumn("Générer", width="small",
                                               help="Tâches à générer ensemble (testables, réponses données)"),
    "Tâche": st.column_config.TextColumn("Tâche", width="large"),
    "Risque": st.column_config.TextColumn("Risque", width="small", help="Risque métier proposé (module, type de "
                                                                        "règle), modifiable dans les détails"),
    "Réponses attendues": st.column_config.TextColumn("Réponses attendues", width="small"),
}
# États dont la tâche se génère
GENERABLE_STATES = ("non_couvert", "partiel", "a_confirmer")


def generable(row: dict) -> bool:
    """
    Tâche à générer : non couverte, partielle ou à confirmer, sans réponse attendue, et pas de relance inutile
    (dernier lot sans rien écrit, entrées inchangées).
    """
    return (row["etat"] in GENERABLE_STATES and not (row.get("evaluation") or {}).get("attendues")
            and not row.get("relance_inutile"))


def generate_tasks(ctx: dict, ids: list) -> None:
    """Génère les tests des tâches `ids` : ajoutées à la génération en cours, sinon première génération."""
    existing = bool(cucumber_tools.load_state(ctx["client"]).get("lots")) and os.path.isdir(
        cucumber_tools.worktree_dir(ctx["client"]))
    run_cucumber_generation(ctx, (["--complete"] if existing else []) + ["--specifics", ",".join(ids)])


def record_answer(client: str, row: dict, question: str, answer: str, key: str, status: str) -> None:
    """Enregistrement automatique d'une confirmation, correction ou réponse : profil client, connaissances partagées."""
    if not (answer or "").strip():
        return
    cucumber_testability.save_answer(client, question, answer, row["id"], key=key, status=status)
    if knowledge_tools.enabled():
        knowledge_tools.publish_knowledge(f"- {question} → {answer.strip()}", client.upper(), "cucumber-reponses",
                                          f"Réponse de l'utilisateur ({row['titre']})", client=client,
                                          tags=["cucumber"])


def render_proposal(client: str, row: dict, proposal: dict) -> None:
    """Réponse proposée avec sa source : « Confirmer » / « Corriger » (champ de saisie seulement après « Corriger »)."""
    values = ", ".join(proposal["valeurs"])
    question = f"{proposal['libelle']} : {values}"
    editing = f"cucumber_fix_{client}_{row['id']}_{proposal['cle']}"
    answer = proposal.get("reponse") or {}
    st.markdown(f"**Réponse proposée** — {proposal['libelle']} : `{values}`")
    st.caption(f"Source : {proposal['source']}")
    if answer and not st.session_state.get(editing):
        st.caption(f":green[:material/check_circle: {'Confirmée' if answer.get('statut') == 'confirmee' else 'Corrigée'}"
                   f" : {answer['reponse']}]")
    if st.session_state.get(editing):
        def save_correction() -> None:
            record_answer(client, row, question, st.session_state[f"{editing}_texte"], proposal["cle"], "corrigee")
            st.session_state[editing] = False
        st.text_input("Valeur correcte", value=(answer or {}).get("reponse") or values, key=f"{editing}_texte",
                      on_change=save_correction, help="Enregistrée dès la validation (Entrée).")
        return
    with st.container(horizontal=True):
        if not answer:
            st.button("Confirmer", icon=":material/check:", key=f"{editing}_ok", on_click=record_answer,
                      args=(client, row, question, values, proposal["cle"], "confirmee"))
        st.button("Corriger", icon=":material/edit:", type="tertiary", key=f"{editing}_edit",
                  on_click=lambda: st.session_state.update({editing: True}))


def render_question(client: str, row: dict, question: dict) -> None:
    """Question précise sans réponse proposée : champ de saisie, enregistré dès la validation."""
    key = f"cucumber_answer_{client}_{row['id']}_{question['cle']}"
    st.markdown(f"**À renseigner** — {question['texte']}")
    if question.get("candidats"):
        st.caption("Candidats proches trouvés : " + ", ".join(f"`{c}`" for c in question["candidats"]))
    answer = question.get("reponse") or {}

    def save() -> None:
        record_answer(client, row, question["texte"], st.session_state[key], question["cle"], "saisie")
    st.text_input("Réponse", value=answer.get("reponse", ""), key=key, on_change=save, label_visibility="collapsed",
                  placeholder="Valeur à utiliser, ou « créer dans le test »", help="Enregistrée dès la validation (Entrée).")


def render_card_details(ctx: dict, row: dict) -> None:
    """Volet « Détails » : identifiant, raison et recherches, facteurs détectés, risque, Studio, scénarios, documents."""
    client, evaluation = ctx["client"], row.get("evaluation") or {}
    st.caption(f"Identifiant : {row['id']} · {row['tache']} · point d'entrée : {row['point_entree']}"
               + (f" · tickets : {', '.join(row['tickets'])}" if row["tickets"] else ""))
    for link in row.get("tickets_verifies") or []:
        st.caption(f"Ticket lié (vérifié) : {link['cle']} — {link['titre']} ({link['preuve']})")
    if row.get("chaine"):
        st.caption(f"Couverture indirecte : {' → '.join(row['chaine'])} · {row['cause']}")
    if evaluation.get("raison"):
        st.caption(f"Raison technique de l'agent : {evaluation['raison']}")
    for search in evaluation.get("recherches") or []:
        st.caption(f"- {dict(cucumber_testability.SOURCES).get(search.get('source'), search.get('source'))} : "
                   f"« {search.get('cherche', '')} » → {search.get('trouve') or 'rien'}")
    for value, places in (evaluation.get("trouvees") or {}).items():
        st.caption(f"- {value} trouvé : {cucumber_testability.source_line(places)}")
    for rejection in evaluation.get("rejets") or []:
        st.caption(f":orange[Contrôle : {rejection}]")
    st.markdown("**Facteurs de testabilité** (détectés)\n" + "\n".join(
        f"- {cucumber_coverage.CRITERIA[k].capitalize()} : **{answer}** — {proof or '—'}"
        for k, (answer, proof) in row["criteres"].items()))

    def save_risk() -> None:
        risk = st.session_state.get(f"cucumber_risk_{client}_{row['id']}")
        previous = cucumber_coverage.load_overrides(client).get(row["id"], {})
        cucumber_coverage.save_override(client, row["id"], {
            **previous, "risque": risk if risk and risk != row["risque_propose"] else ""})
    st.segmented_control(f"Risque métier (proposé : {row['risque_propose']})", cucumber_coverage.RISKS,
                         default=row["risque"], key=f"cucumber_risk_{client}_{row['id']}", on_change=save_risk)
    if row["studio_a_calculer"]:
        st.caption("Personnalisation Studio : ses comportements à couvrir seront listés à la prochaine analyse "
                   "du projet.")
    elif row["studio_total"]:
        st.markdown(f"**Comportements Studio à couvrir** : {cucumber_studio.counts_line(row['studio_total'])}\n"
                    + "\n".join(f"- {cucumber_studio.behaviour_line(b)}" for b in row["studio"]))
    if row["scenarios"]:
        st.dataframe([{"Scénario": s["id"].split("::", 1)[-1], "Feature": s["id"].split("::", 1)[0],
                       "État": cucumber_coverage.STATES[s["etat"]][1] + " " + cucumber_coverage.STATES[s["etat"]][0]
                       + (" (faible)" if s["faible"] else ""),
                       "Cause": s["cause"], "Dernière exécution": s["derniere"],
                       "Audit": cucumber_audit.VERDICTS.get(s.get("audit"), "—"),
                       "Constats de l'audit": " ; ".join(s.get("audit_motifs") or [])} for s in row["scenarios"]],
                     hide_index=True, width="stretch")
    if row["constats"]:
        st.markdown("**Défauts et constats de l'agent**\n" + "\n".join(
            f"- {cucumber_tools.constat_line(c)}" for c in row["constats"]))
    render_coverage_knowledge(client, row)


def render_modifications(client: str, spec_id: str, place: str, extra=None) -> None:
    """
    « Voir les modifications » et « Télécharger » pour une fonctionnalité dont le test est écrit, sur une rangée
    (`extra` : bouton ajouté à la rangée), puis les modifications sur toute la largeur, en dessous.
    """
    state = cucumber_tools.load_state(client)
    mods = cucumber_results.modifications(state, spec_id, cucumber_tools.worktree_dir(client))
    if not mods["diff"]:
        with st.container(horizontal=True, vertical_alignment="center"):
            st.caption("Modifications non disponibles : l'espace de travail de l'agent a été vidé depuis.")
            if extra:
                extra()
        return
    with st.container(horizontal=True, vertical_alignment="center"):
        show = st.toggle("Voir les modifications", key=f"cucumber_voir_{place}_{client}_{spec_id}")
        st.download_button("Télécharger", mods["diff"], icon=":material/download:", mime="text/x-diff",
                           file_name=f"{client}-{spec_id}-modifications.diff",
                           key=f"cucumber_telecharger_{place}_{client}_{spec_id}")
        st.caption(f"{len(mods['fichiers'])} fichier(s)"
                   + (" — écrits en même temps que d'autres fonctionnalités" if mods["partage"] else ""))
        if extra:
            extra()
    if show:
        lines = mods["diff"].splitlines()
        st.code("\n".join(lines[:600]) + (f"\n… {len(lines) - 600} ligne(s) de plus dans le fichier téléchargeable"
                                          if len(lines) > 600 else ""), language="diff")


def render_generation_result(ctx: dict, row: dict) -> None:
    """Résultat de la dernière écriture de la fonctionnalité, en une phrase, et ses modifications."""
    client, generation = ctx["client"], row.get("generation") or {}
    if not generation:
        return
    kind, date = generation.get("type", ""), (generation.get("date") or "").replace("T", " ")[:16]
    label = cucumber_results.KINDS.get(kind, kind)
    if kind == "interrompue":
        st.warning(f"**{label}** ({date}) : {generation.get('message', '')}"
                   + (f" Que faire : {generation['action']}." if generation.get("action") else "")
                   + " L'état de la fonctionnalité n'a pas changé.", icon=":material/block:")
        return
    st.markdown(f"**{cucumber_results.ICONS.get(kind, '')} {label}** :gray[({date})] — {generation.get('message', '')}")
    if generation.get("couvert_par") and row["etat"] != "en_attente":
        st.button("Ouvrir la fonctionnalité qui la couvre", icon=":material/arrow_forward:", type="tertiary",
                  key=f"cucumber_result_open_{client}_{row['id']}", on_click=st.session_state.update,
                  kwargs={f"cucumber_card_{client}": generation["couvert_par"]})
    if kind == "test_ecrit":
        render_modifications(client, row["id"], "detail")


def blocked_repositories(client_root: str) -> list:
    """Dépôts locaux bloqués (fusion inachevée, branche non suivie, retard) : contrôle git rapide, sans Maven."""
    if not client_root or not os.path.isdir(client_root):
        return []
    cache = st.session_state.setdefault("cucumber_blocked_cache", {})
    cached = cache.get(client_root)
    if cached and time.time() - cached[0] < 120:
        return cached[1]
    problems = cucumber_preflight.check(client_root, "", "", cucumber_tools.outside_inventory)
    cache[client_root] = (time.time(), problems)
    return problems


def render_todo_banner(ctx: dict, rows: list, links: dict, init_status: dict, detected: dict) -> None:
    """
    Bandeau « À faire », seulement s'il y a quelque chose à faire : une phrase et un bouton par problème (dépôt
    bloqué, analyse en échec, données de test incomplètes, réponses attendues, tests existants non reliés, décisions,
    anciens tests sans le tag du client).
    """
    client = ctx["client"]
    state = cucumber_tools.load_state(client)
    inventory_root = cucumber_tools.load_inventory(client).get("depot_client", "")
    items = []
    for problem in blocked_repositories(inventory_root):
        what, _, action = problem.partition(". Action : ")
        items.append(("repo", f"Un dépôt local est bloqué — {what}.", action))
    if detected.get("statut") == "erreur":
        items.append(("analyse", "L'analyse automatique du projet a échoué.", detected.get("cause", "")))
    init_lot = cucumber_detection.init_lot(init_status)
    if init_lot:
        prepared = cucumber_tools.load_state(client).get("preparation") or {}
        items.append(("init", "Les données de test du projet sont " + ("absentes." if init_lot["mode"] == "init" else
                                                                        "incomplètes.")
                      + (f" Préparation faite le {prepared['date'][:16].replace('T', ' à ')} : voir « Résultats »."
                         if prepared.get("ok") and prepared.get("patch") else ""), init_lot))
    answers = sum(1 for r in rows if cucumber_coverage.in_filter(r, "reponses"))
    if answers:
        items.append(("reponses", f"{answers} fonctionnalité{'s' if answers > 1 else ''} attend"
                                  f"{'ent' if answers > 1 else ''} une réponse pour que son test soit écrit.", None))
    unlinked = len((links or {}).get("non_rattachees") or [])
    if unlinked:
        items.append(("relier", f"{unlinked} test{'s' if unlinked > 1 else ''} existant{'s' if unlinked > 1 else ''} "
                                f"ne {'sont' if unlinked > 1 else 'est'} relié{'s' if unlinked > 1 else ''} à aucune "
                                f"fonctionnalité.", None))
    numero, analysis = latest_import_analysis(client)
    # Corrections déjà lancées pour cet import : plus proposées (un nouvel import dira si elles ont suffi)
    launched = {str(lot.get("import")) for lot in state.get("lots") or [] if lot.get("type") == "correction"}
    fixable = 0 if str(numero) in launched else sum(len(p["scenarios"]) for p in analysis.get("propositions") or [])
    if fixable and not analysis.get("non_analyse"):
        items.append(("corriger", f"{fixable} test{'s' if fixable > 1 else ''} en échec au dernier résultat "
                                  f"importé {'peuvent' if fixable > 1 else 'peut'} être corrigé{'s' if fixable > 1 else ''}"
                                  f" automatiquement.", (numero, [p["spec"] for p in analysis["propositions"]])))
    decisions = len(cucumber_decisions.pending(client))
    if decisions:
        items.append(("decider", f"{decisions} décision{'s' if decisions > 1 else ''} attend"
                                 f"{'ent' if decisions > 1 else ''} ton avis.", None))
    retroactive = len((state.get("selection") or {}).get("retroactif") or [])
    if retroactive:
        items.append(("tag", f"{retroactive} ancien{'s' if retroactive > 1 else ''} test{'s' if retroactive > 1 else ''}"
                             f" ne porte{'nt' if retroactive > 1 else ''} pas le tag du client : un job filtré sur ce "
                             f"tag ne les exécuterait pas.", None))
    if not items:
        return
    with st.container(border=True):
        st.markdown(":material/notification_important: **À faire**")
        for index, (kind, text, extra) in enumerate(items):
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown(f":orange[⚠] {text}")
                if kind in ("repo", "analyse"):
                    with st.popover("Voir comment le débloquer" if kind == "repo" else "Voir le détail"):
                        st.markdown(extra or "Cause inconnue.")
                elif kind == "init":
                    st.button("Les préparer", key=f"cucumber_todo_init_{client}", disabled=not ctx["ready"],
                              on_click=st.session_state.update, kwargs={f"cucumber_init_demande_{client}": True})
                elif kind == "reponses":
                    st.button("Répondre", key=f"cucumber_todo_reponses_{client}", on_click=st.session_state.update,
                              kwargs={f"cucumber_filtre_{client}": "reponses"})
                elif kind == "relier":
                    with st.popover("Les relier"):
                        render_unlinked_tests(ctx, rows, links)
                elif kind == "corriger":
                    if st.button("Corriger", type="primary", key=f"cucumber_todo_corriger_{client}",
                                 disabled=not ctx["ready"]):
                        run_cucumber_generation(ctx, ["--complete", "--from-import", str(extra[0])], extra[1])
                elif kind == "decider":
                    # Décisions : dans le mode expert (corrections), ouvert par ce bouton
                    st.button("Décider", key=f"cucumber_todo_decider_{client}", on_click=st.session_state.update,
                              kwargs={f"cucumber_section_expert_{client}": True})
                elif kind == "tag":
                    if st.button("Corriger automatiquement", key=f"cucumber_todo_tag_{client}",
                                 disabled=not ctx["ready"]):
                        code = run_main_step(ctx["cmd"] + ["--fix-client-tags"])
                        (st.success if code == 0 else st.error)(
                            "Tag du client ajouté aux anciens tests." if code == 0 else
                            f"Correction incomplète : {interruption_cause(code)}.")
    # Préparation demandée : lancée sous le bandeau (pleine largeur), son résultat reste affiché dans « Résultats »
    init_item = [extra for kind, _, extra in items if kind == "init"]
    if st.session_state.pop(f"cucumber_init_demande_{client}", False) and init_item and ctx["ready"]:
        run_cucumber_init_lot(ctx, init_item[0])


def render_simple_import(client: str, place: str = "simple") -> None:
    """Import d'un résultat Jenkins (console, rapport JSON ou URL du build), sans le détail de l'analyse."""
    with st.form(f"cucumber_{place}_import_{client}", border=False):
        uploaded = st.file_uploader("Fichiers du résultat Jenkins (console, rapport JSON, journaux)",
                                    accept_multiple_files=True, type=["txt", "log", "json", "csv"],
                                    help="Les mots de passe et jetons sont masqués avant tout enregistrement.")
        build_url = st.text_input("Ou adresse du build Jenkins", placeholder="https://jenkins/job/…/123/")
        submitted = st.form_submit_button("Importer", icon=":material/upload_file:", type="primary")
    if not submitted:
        return
    files = {f.name: f.getvalue() for f in uploaded or []}
    if build_url.strip():
        files.update(cucumber_import.build_files(build_url.strip()))
    imported = cucumber_import.import_files(client, files, build_url.strip())
    if not imported["numero"]:
        st.session_state[f"cucumber_import_message_{client}"] = (
            "Rien de nouveau à importer : ces fichiers ont déjà été importés ou ne contiennent pas de résultat.")
        st.rerun()
    state = cucumber_tools.load_state(client)
    analysis = cucumber_import.analyse(client, imported["numero"], state, cucumber_tools.worktree_dir(client))
    cucumber_bilan.after_import(client, analysis, state, cucumber_tools.worktree_dir(client))
    failures = sum(len(g["scenarios"]) for g in analysis.get("groupes") or [])
    fixable = sum(len(p["scenarios"]) for p in analysis.get("propositions") or [])
    st.session_state[f"cucumber_import_message_{client}"] = (
        f"Résultat non exploitable : {analysis['non_analyse']}" if analysis.get("non_analyse") else
        "Résultat importé : l'initialisation est en échec — le bouton « Lancer » la corrige."
        if analysis.get("init_en_echec") else
        f"Résultat importé : {analysis.get('tests', 0)} test(s), {failures} en échec"
        + (f" ; {fixable} peut être corrigé automatiquement (bouton « Lancer »)." if fixable else "."))
    st.rerun()


def render_zone_status(ctx: dict, rows: list) -> None:
    """Zone « Où en est-on » : résumé en 2 ou 3 phrases, nombres cliquables, un bouton « Importer un résultat Jenkins »."""
    client = ctx["client"]
    run = cucumber_coverage.last_run(client)
    st.markdown("#### Où en est-on")
    with st.container(horizontal=True, vertical_alignment="center", gap="small"):
        for text, key in cucumber_coverage.status_sentences(rows, run):
            if key:
                st.button(text, type="tertiary", key=f"cucumber_filtre_{key}_{client}",
                          on_click=st.session_state.update, kwargs={f"cucumber_filtre_{client}": key},
                          help="Afficher ces fonctionnalités dans la liste")
            else:
                st.markdown(f"**{text}**")
    message = st.session_state.pop(f"cucumber_import_message_{client}", "")
    if message:
        st.info(message, icon=":material/info:")
    with st.popover("Importer un résultat Jenkins", icon=":material/upload_file:"):
        render_simple_import(client)


def render_task_row(client: str, row: dict) -> None:
    """Une ligne de la liste : case à cocher, module, titre (ouvre le détail), état en un ou deux mots."""
    cols = st.columns([0.5, 1.4, 6, 2.6], vertical_alignment="center")
    cols[0].checkbox("Choisir", key=f"cucumber_pick_{client}_{row['id']}", label_visibility="collapsed")
    cols[1].caption(row["module"] or "—")
    cols[2].button(row["titre"], type="tertiary", key=f"cucumber_open_{client}_{row['id']}",
                   on_click=st.session_state.update, kwargs={f"cucumber_card_{client}": row["id"],
                                                             f"cucumber_card_origine_{client}": "liste"})
    cols[3].markdown(cucumber_coverage.short_state(row))


def render_zone_tasks(ctx: dict, rows: list) -> None:
    """
    Zone « À tester » : une ligne par fonctionnalité (module, titre métier, état), filtres module et recherche,
    cases à cocher et un seul bouton « Écrire les tests de la sélection » ; un clic sur un titre ouvre son détail.
    """
    client = ctx["client"]
    st.markdown("#### À tester")
    active = st.session_state.get(f"cucumber_filtre_{client}")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        module = st.selectbox("Module", ["Tous"] + sorted({r["module"] for r in rows if r["module"]}),
                              key=f"cucumber_module_{client}")
        search = st.text_input("Recherche", key=f"cucumber_recherche_{client}",
                               placeholder="Mot du titre ou ticket").strip().casefold()
        if active:
            st.button(f"Fonctionnalités qui {cucumber_coverage.FILTERS[active]} — afficher tout", type="tertiary",
                      icon=":material/filter_alt_off:", key=f"cucumber_filtre_off_{client}",
                      on_click=st.session_state.pop, args=(f"cucumber_filtre_{client}", None))
    shown = [r for r in rows if (not active or cucumber_coverage.in_filter(r, active))
             and (module == "Tous" or r["module"] == module)
             and (not search or search in (r["titre"] + " " + " ".join(r["tickets"])).casefold())]
    shown.sort(key=lambda r: (r["module"], r["titre"].casefold()))
    page_size = 25
    pages = max(1, -(-len(shown) // page_size))
    page = st.session_state.get(f"cucumber_page_{client}", 1)
    page = min(max(1, page), pages)
    with st.container(border=True):
        if not shown:
            st.caption("Aucune fonctionnalité ne correspond.")
        for row in shown[(page - 1) * page_size:page * page_size]:
            render_task_row(client, row)
    if pages > 1:
        st.segmented_control("Page", list(range(1, pages + 1)), default=page, key=f"cucumber_page_{client}",
                             label_visibility="collapsed")
    by_id = {r["id"]: r for r in rows}
    picked = [r["id"] for r in rows if st.session_state.get(f"cucumber_pick_{client}_{r['id']}")]
    wanted, substitutions = cucumber_coverage.substitute_dependents(rows, picked)
    ready_ids = [i for i in wanted if i in by_id and generable(by_id[i])]
    with st.container(horizontal=True, vertical_alignment="center"):
        clicked = st.button(f"Écrire les tests de la sélection ({len(ready_ids)})", type="primary",
                            icon=":material/edit_note:", disabled=not (ctx["ready"] and ready_ids),
                            key=f"cucumber_ecrire_{client}")
        notes = [f"« {by_id[d]['titre']} » est couverte par « {by_id[t]['titre']} » : c'est son test qui sera écrit."
                 for d, t in substitutions if d in by_id and t in by_id]
        skipped = len(wanted) - len(ready_ids)
        if skipped:
            notes.append(f"{skipped} fonctionnalité(s) choisie(s) laissée(s) de côté : déjà testée(s), réponse "
                         f"attendue, bloquée(s) ou sans objet.")
        if notes:
            st.caption(" ".join(notes))
    if clicked:
        generate_tasks(ctx, ready_ids)
        for spec_id in picked:
            st.session_state.pop(f"cucumber_pick_{client}_{spec_id}", None)
    opened = st.session_state.get(f"cucumber_card_{client}")
    from_results = st.session_state.get(f"cucumber_card_origine_{client}") == "resultats" and opened in (
        cucumber_tools.load_state(client).get("generations") or {})
    if opened in by_id and not from_results:
        render_task_card(ctx, by_id[opened])
        st.button("Fermer le détail", type="tertiary", icon=":material/close:", key=f"cucumber_close_{client}",
                  on_click=st.session_state.pop, args=(f"cucumber_card_{client}", None))


def render_preparation_result(client: str, preparation: dict) -> None:
    """Résultat de la dernière préparation des données de test : phrase, points restants, modifications, patch."""
    if not preparation:
        return
    with st.container(border=True):
        date = preparation.get("date", "")[:16].replace("T", " à ")
        st.markdown(f"**Préparation des données de test** ({date}) — {'✅' if preparation.get('ok') else '⛔'} "
                    f"{preparation.get('message', '')}")
        for path in preparation.get("fichiers") or []:
            st.caption(f"📄 `{path}`")
        remaining = preparation.get("restantes") or []
        if remaining:
            st.markdown(f"**{len(remaining)} point(s) non fait(s), à traiter à la main :**")
            st.markdown("\n".join(f"- {r}" for r in remaining))
        patch = preparation.get("patch") or ""
        if patch and os.path.exists(patch):
            text = open(patch, encoding="utf-8", errors="replace").read()
            with st.container(horizontal=True):
                show = st.toggle("Voir les modifications", key=f"cucumber_preparation_diff_{client}")
                st.download_button("Télécharger le patch", text, file_name=os.path.basename(patch),
                                   icon=":material/download:", key=f"cucumber_preparation_patch_{client}")
            if show:
                st.code(text, language="diff")


def render_zone_results(ctx: dict, rows: list) -> None:
    """
    Zone « Résultats » : une ligne par fonctionnalité traitée (la plus récente en tête), avec son résultat (test
    écrit, pas de test et pourquoi, en attente d'écriture, interrompu et que faire) et ses actions.
    """
    client = ctx["client"]
    state = cucumber_tools.load_state(client)
    generations = state.get("generations") or {}
    by_id = {r["id"]: r for r in rows}
    waiting = [i for lot in cucumber_tools.pending_lots(state) for i in lot["ids"]
               if (generations.get(i) or {}).get("type") != "interrompue"]
    st.markdown("#### Résultats")
    render_preparation_result(client, state.get("preparation") or {})
    if not generations and not waiting and not state.get("preparation"):
        st.caption("Aucune fonctionnalité traitée pour l'instant : cochez-en dans la liste et écrivez leurs tests.")
        return
    for spec_id in waiting:
        with st.container(border=True):
            st.markdown(f"**{(by_id.get(spec_id) or {}).get('titre', spec_id)}** — ⏳ En attente d'écriture")
    ordered = sorted(generations.items(), key=lambda item: item[1].get("date", ""), reverse=True)
    show_all = st.session_state.get(f"cucumber_resultats_tout_{client}")
    for spec_id, generation in ordered if show_all else ordered[:15]:
        kind = generation.get("type", "")
        with st.container(border=True):
            title = (by_id.get(spec_id) or {}).get("titre", spec_id)
            text = f"**{title}** — {cucumber_results.ICONS.get(kind, '')} {cucumber_results.KINDS.get(kind, kind)}"
            text += f" : {generation.get('message', '')}" if generation.get("message") else ""
            if kind == "interrompue" and generation.get("action"):
                text += f" Que faire : {generation['action']}."
            st.markdown(text)

            def open_button(spec_id=spec_id):
                st.button("Ouvrir", icon=":material/open_in_new:", type="tertiary",
                          key=f"cucumber_result_card_{client}_{spec_id}", on_click=st.session_state.update,
                          kwargs={f"cucumber_card_{client}": spec_id, f"cucumber_card_origine_{client}": "resultats"})
            if kind == "test_ecrit":
                render_modifications(client, spec_id, "resultat", open_button)
            else:
                open_button()
        if st.session_state.get(f"cucumber_card_origine_{client}") == "resultats" and \
                st.session_state.get(f"cucumber_card_{client}") == spec_id and spec_id in by_id:
            render_task_card(ctx, by_id[spec_id])
            st.button("Fermer le détail", type="tertiary", icon=":material/close:",
                      key=f"cucumber_close_resultat_{client}", on_click=st.session_state.pop,
                      args=(f"cucumber_card_{client}", None))
    if len(ordered) > 15:
        st.toggle(f"Afficher les {len(ordered)} résultats", key=f"cucumber_resultats_tout_{client}")


def main_action(ctx: dict, rows: list, init_status: dict) -> dict:
    """
    Ce que fait le bouton « Lancer », de la plus bloquante à la première couverture : corriger l'initialisation en
    échec au dernier run importé, préparer l'initialisation absente ou incomplète, corriger les tests en échec au
    dernier run importé, écrire la première couverture. {"type", "libelle", "detail", ...} ; type "" : rien à faire.
    """
    client = ctx["client"]
    state = cucumber_tools.load_state(client)
    numero, analysis = latest_import_analysis(client)
    launched = {str(lot.get("import")) for lot in state.get("lots") or [] if lot.get("type") == "correction"}
    init_fixed = str((state.get("init_corrigee") or {}).get("import", "")) == str(numero)
    if analysis.get("init_en_echec") and not analysis.get("non_analyse") and not init_fixed:
        init = analysis["init_en_echec"]
        return {"type": "init_echec", "numero": numero, "libelle": "Corriger l'initialisation",
                "detail": f"l'initialisation a échoué au dernier run importé (n° {numero}) : "
                          f"{(init.get('step') or init.get('id', ''))[:120]}."}
    init_lot = cucumber_detection.init_lot(init_status)
    first = [r["id"] for r in rows if generable(r)] if not state.get("lots") else []
    fixable = [] if str(numero) in launched or analysis.get("non_analyse") else analysis.get("propositions") or []
    follow = {}
    if fixable:
        count = sum(len(p["scenarios"]) for p in fixable)
        follow = {"type": "corriger", "numero": numero, "specs": [p["spec"] for p in fixable],
                  "libelle": f"Corriger les {count} test(s) en échec",
                  "detail": f"tests en échec au dernier run importé (n° {numero})."}
    elif first:
        follow = {"type": "couverture", "ids": first, "libelle": f"Écrire la première couverture ({len(first)})",
                  "detail": f"{len(first)} tâche(s) sans test."}
    # Préparation déjà réussie : ce qui reste incomplet est listé dans son résultat, on passe aux tests
    prepared = (state.get("preparation") or {}).get("ok")
    if init_lot and not prepared:
        what = "absentes" if init_lot["mode"] == "init" else "incomplètes"
        return {"type": "init", "lot": init_lot, "suite": follow,
                "libelle": "Préparer l'initialisation" + (" puis " + follow["libelle"][0].lower()
                                                         + follow["libelle"][1:] if follow else ""),
                "detail": f"les données de test du projet sont {what} ; l'agent les complète"
                          + (f", puis : {follow['detail']}" if follow else ".")}
    if follow:
        return follow
    return {"type": "", "libelle": "Lancer",
            "detail": "rien à faire pour l'instant : importe le résultat du prochain run Jenkins, ou choisis des "
                      "tâches ci-dessous."}


def fix_action(ctx: dict) -> dict:
    """
    Bouton « Corriger les tests en échec » : toujours proposé d'après le dernier run importé, même si une correction
    de ce run a déjà été lancée (nouvelle tentative) — l'initialisation en échec d'abord, sinon les tests en erreur
    ou en échec que l'agent sait corriger. {"type": "", ...} si rien n'est corrigeable.
    """
    numero, analysis = latest_import_analysis(ctx["client"])
    if not numero:
        return {"type": "", "libelle": "Corriger les tests en échec",
                "detail": "aucun résultat Jenkins importé : dépose-le dans « 3 »."}
    if analysis.get("non_analyse"):
        return {"type": "", "libelle": "Corriger les tests en échec",
                "detail": f"le dernier résultat importé n'est pas exploitable : {analysis['non_analyse']}"}
    if analysis.get("init_en_echec"):
        return {"type": "init_echec", "numero": numero, "libelle": "Corriger l'initialisation en échec",
                "detail": f"run importé n° {numero} : l'initialisation a échoué, aucun test n'a été jugé."}
    fixable = cucumber_import.remaining_proposals(cucumber_tools.load_state(ctx["client"]), analysis)
    failures = sum(len(g["scenarios"]) for g in analysis.get("groupes") or [])
    count = sum(len(p["scenarios"]) for p in fixable)
    if not count and analysis.get("propositions"):
        return {"type": "", "libelle": "Corriger les tests en échec",
                "detail": f"run importé n° {numero} : tous les tests corrigeables ont été corrigés et vérifiés verts "
                          f"par l'agent — télécharge les modifications, puis relance le job Jenkins."}
    if not count:
        return {"type": "", "libelle": "Corriger les tests en échec",
                "detail": f"run importé n° {numero} : " + (f"{failures} échec(s), aucun corrigeable dans les tests "
                                                           f"(causes hors test, voir le bilan)." if failures else
                                                           "aucun test en échec.")}
    return {"type": "corriger", "numero": numero, "specs": [p["spec"] for p in fixable],
            "libelle": f"Corriger les tests en échec ({count})",
            "detail": f"run importé n° {numero} : {count} test(s) en erreur ou en échec à corriger"
                      + (f" sur {failures}" if failures > count else "") + "."}


def run_main_action(ctx: dict, action: dict) -> None:
    client = ctx["client"]
    if action["type"] == "init_echec":
        st.info("Correction de l'initialisation en cours…", icon=":material/build:")
        code = run_main_step(ctx["cmd"] + ["--fix-init-from-import", str(action["numero"])], on_line=ctx["capture"])
        (st.success if code == 0 else st.error)(
            "Initialisation corrigée : les modifications sont prêtes ci-dessous." if code == 0 else
            f"Correction de l'initialisation incomplète : {interruption_cause(code)}.")
    elif action["type"] == "init":
        # Puis, dans le même clic, la correction des tests en échec ou la première couverture
        if run_cucumber_init_lot(ctx, action["lot"]) == 0 and action.get("suite"):
            run_main_action(ctx, action["suite"])
            return
    elif action["type"] == "corriger":
        run_cucumber_generation(ctx, ["--complete", "--from-import", str(action["numero"])], action["specs"])
    elif action["type"] == "couverture":
        generate_tasks(ctx, action["ids"])
    st.session_state.pop(f"cucumber_simple_taches_{client}", None)


def render_deliverable(client: str) -> None:
    """Modifications à appliquer (tests, initialisation), avec ce qui n'a pas pu y entrer."""
    found = False
    for path, label in ((cucumber_tools.patch_path(client), "Télécharger les tests"),
                        (cucumber_tools.init_patch_path(client), "Télécharger l'initialisation")):
        if not (path and os.path.exists(path)):
            continue
        found = True
        when = time.strftime("%d/%m/%Y %H:%M", time.localtime(os.path.getmtime(path)))
        with st.container(horizontal=True, vertical_alignment="center"):
            with open(path, "rb") as f:
                st.download_button(f"{label} ({when})", f.read(), file_name=os.path.basename(path),
                                   mime="text/x-diff", icon=":material/download:",
                                   key=f"cucumber_simple_dl_{client}_{os.path.basename(path)}")
            for suffix, what in ((".ecarts.txt", "modification(s) écartée(s)"),
                                 (".donnees-manquantes.txt", "fichier(s) de données introuvable(s)")):
                if os.path.exists(path + suffix):
                    with open(path + suffix, encoding="utf-8", errors="replace") as f:
                        detail = f.read()
                    count = sum(1 for line in detail.splitlines() if line.startswith("- "))
                    with st.popover(f"⚠️ {count} {what}"):
                        st.markdown(detail)
    if not found:
        st.caption("Rien à appliquer pour l'instant : les modifications apparaissent ici dès que des tests sont "
                   "écrits ou corrigés. Applique-les dans ton dépôt (git am), relis, pousse, puis lance le job "
                   "Jenkins.")
    decisions = len(cucumber_decisions.pending(client))
    if decisions:
        st.caption(f"✋ {decisions} décision(s) attendent ton avis (changement du sens d'un test) : mode expert, "
                   f"« Corrections ».")
    taken = [d for d in cucumber_decisions.load_decisions(client) if d.get("auto")]
    if taken:
        with st.popover(f"🤖 {len(taken)} choix faits par l'agent, à relire"):
            st.markdown("\n".join(cucumber_decisions.summary_lines(taken, limit=50)))


def render_cucumber_simple(ctx: dict, rows: list, init_status: dict) -> None:
    """
    Écran minimal : bouton « Lancer » (init, première couverture, corrections), choix de tâches non testées, import
    du résultat Jenkins, patch à appliquer.
    """
    client = ctx["client"]
    action = main_action(ctx, rows, init_status)
    st.markdown(" ".join(text for text, _ in cucumber_coverage.status_sentences(rows, cucumber_coverage.last_run(
        client))))
    with st.container(border=True):
        st.markdown("**1. Lancer**")
        with st.container(horizontal=True, vertical_alignment="center"):
            clicked = st.button(action["libelle"], type="primary", icon=":material/play_arrow:",
                                disabled=not (ctx["ready"] and action["type"]), key=f"cucumber_simple_go_{client}")
            st.caption(action["detail"][:1].upper() + action["detail"][1:])
        fix = fix_action(ctx)
        same = fix["type"] and fix["type"] == action["type"] and fix.get("numero") == action.get("numero")
        fix_clicked = False
        if not same:
            with st.container(horizontal=True, vertical_alignment="center"):
                fix_clicked = st.button(fix["libelle"], icon=":material/build:",
                                        disabled=not (ctx["ready"] and fix["type"]), key=f"cucumber_simple_fix_{client}")
                st.caption(fix["detail"][:1].upper() + fix["detail"][1:])
        if clicked:
            run_main_action(ctx, action)
        elif fix_clicked:
            run_main_action(ctx, fix)
    with st.container(border=True):
        st.markdown("**2. Écrire les tests de tâches choisies**")
        candidates = sorted((r for r in rows if generable(r)), key=lambda r: (r["module"], r["titre"].casefold()))
        titles = {r["id"]: (f"{r['module']} — " if r["module"] else "") + r["titre"] for r in candidates}
        picked = st.multiselect("Tâches non testées", list(titles), format_func=titles.get,
                                key=f"cucumber_simple_taches_{client}", placeholder="Choisis une ou plusieurs tâches",
                                label_visibility="collapsed")
        wanted, _ = cucumber_coverage.substitute_dependents(rows, picked)
        by_id = {r["id"]: r for r in rows}
        ready_ids = [i for i in wanted if i in by_id and generable(by_id[i])]
        if st.button(f"Écrire les tests ({len(ready_ids)})", icon=":material/edit_note:",
                     disabled=not (ctx["ready"] and ready_ids), key=f"cucumber_simple_write_{client}"):
            generate_tasks(ctx, ready_ids)
        if not candidates:
            st.caption("Toutes les tâches testables ont un test.")
    with st.container(border=True):
        st.markdown("**3. Résultat du job Jenkins**")
        st.caption("Après un run Jenkins en erreur ou en échec : dépose sa console (ou son rapport JSON), ou colle "
                   "l'adresse du build. Le bouton « Lancer » proposera ensuite la correction.")
        message = st.session_state.pop(f"cucumber_import_message_{client}", "")
        if message:
            st.info(message, icon=":material/info:")
        render_simple_import(client, "minimal")
    st.markdown("**Modifications à appliquer**")
    render_deliverable(client)


def render_expert(ctx: dict, data: dict) -> None:
    """
    Mode expert, en onglets : Projet (profil, santé du dépôt), Couverture (résumé, audit), Génération (plan, lots,
    vérification), Résultats (exécution, imports, bilan, A_REVOIR.md), Décisions (choix de l'agent, corrections
    proposées), Journal. Le téléchargement des modifications et le tag client rétroactif sont sur l'écran principal.
    """
    client = ctx["client"]
    st.caption(cucumber_journey.overview_line(
        client, cucumber_detection.state_label(data["init_status"]), data["rows"], data["analysis"]))
    tabs = st.tabs(["Projet", "Couverture", "Génération", "Résultats", "Décisions", "Journal"])
    with tabs[0]:
        render_cucumber_profile(ctx, data["profile_doc"], data["detected"], data["init_status"])
        render_cucumber_health(ctx)
    with tabs[1]:
        if data["rows"]:
            render_coverage_summary(ctx, data["rows"], data["links"],
                                    cucumber_journey.next_action(client, data["rows"], data["links"]))
        render_cucumber_audit(client, data["inventory"], data["rows"])
    with tabs[2]:
        render_cucumber_selection(ctx, data["inventory"], data["state"], data["rows"], data["init_status"],
                                  data["dependencies"])
        render_cucumber_generation(ctx, data["state"], data["dependencies"])
    with tabs[3]:
        if cucumber_verify.agent_runs_tests():
            render_cucumber_run(ctx, data["state"])
        render_cucumber_import(client, data["state"], ctx["cmd"])
        render_cucumber_review(client, data["state"])
    with tabs[4]:
        render_cucumber_corrections(client, ctx["cmd"], data["rows"], data["state"])
    with tabs[5]:
        render_technical_journal()


def render_task_card(ctx: dict, row: dict) -> None:
    """
    Carte d'une tâche, même gabarit pour tous les états : titre métier et badge, une phrase métier, réponses
    proposées (Confirmer / Corriger) et questions sans réponse proposée (champ de saisie), UNE action « Générer le
    test » (désactivée tant qu'une réponse est attendue), puis le volet « Détails » replié.
    """
    client, evaluation = ctx["client"], row.get("evaluation") or {}
    label, badge, _ = cucumber_coverage.STATES[row["etat"]]
    expected = evaluation.get("attendues") or 0
    with st.container(border=True):
        st.markdown(f"#### {row['titre']}  \n{badge} {label}")
        st.markdown(row["phrase"])
        render_generation_result(ctx, row)
        if row["etat"] == "a_confirmer":
            for proposal in evaluation.get("proposees") or []:
                render_proposal(client, row, proposal)
            for question in evaluation.get("questions") or []:
                render_question(client, row, question)
        relaunch = row.get("relance_inutile") or {}
        with st.container(horizontal=True, vertical_alignment="center"):
            if row["etat"] == "en_attente" and row.get("couvert_par"):
                # Une seule action : ouvrir la tâche dont elle dépend (son test couvrira celle-ci)
                st.button(f"Ouvrir « {row['chaine_titre']} »", type="primary", icon=":material/arrow_forward:",
                          key=f"cucumber_card_open_{client}_{row['id']}", on_click=st.session_state.update,
                          kwargs={f"cucumber_card_{client}": row["couvert_par"]})
            elif relaunch:
                st.caption(f":material/block: Dernière écriture sans test : {relaunch['raison']} Le code, les "
                           f"réglages du projet et les réponses n'ont pas changé depuis : la relancer donnerait le "
                           f"même résultat.")
            else:
                if st.button("Écrire le test", type="primary", icon=":material/edit_note:",
                             key=f"cucumber_card_generate_{client}_{row['id']}",
                             disabled=not (ctx["ready"] and generable(row))):
                    generate_tasks(ctx, [row["id"]])
                if expected:
                    st.caption(f"{expected} réponse(s) attendue(s)")
        with st.expander("Détail technique", icon=":material/info:"):
            render_card_details(ctx, row)


def render_coverage_knowledge(client: str, row: dict) -> None:
    """Documents partagés liés à la tâche (le partage n'est lu qu'à la demande)."""
    if not knowledge_tools.enabled():
        return
    if st.button("Documents partagés liés", icon=":material/library_books:", key=f"cucumber_docs_{row['id']}"):
        with st.spinner("Lecture des en-têtes du répertoire partagé…"):
            st.session_state["knowledge_index"] = knowledge_tools.load_index()
    index = st.session_state.get("knowledge_index")
    if index is None:
        return
    found = knowledge_tools.find_knowledge(index["documents"], projet=client, client=client, tickets=row["tickets"],
                                           composants=[row["module"]], query_text=row["tache"])[:5]
    if not found:
        st.caption("Aucun document partagé lié.")
    for item in found:
        meta = item["doc"]["meta"]
        st.markdown(f"- {meta.get('sujet', '')} :gray[({meta.get('type', '')}, {str(meta.get('date', ''))[:10]}, "
                    f"{', '.join(item['raisons'])})] — `{item['doc']['chemin']}`")


def section(title: str, icon: str, key: str, expanded: bool) -> bool:
    """
    Section repliable de l'onglet (interrupteur dans un cadre : les volets « Détails » y restent possibles, Streamlit
    n'imbriquant pas les volets). Ouverte par défaut seulement quand elle a quelque chose à montrer ou est demandée.
    """
    state_key = f"cucumber_section_{key}"
    if expanded and not st.session_state.get(f"{state_key}_vu"):
        st.session_state[state_key] = True
        st.session_state[f"{state_key}_vu"] = True
    return st.toggle(f"{icon} **{title}**", key=state_key)


def render_coverage_summary(ctx: dict, rows: list, links: dict, action: dict) -> None:
    """
    Un seul bloc de résumé, en phrases, issu d'un seul calcul (cucumber_coverage.summary_groups) : chaque nombre filtre
    la liste ; puis l'action suivante, et les indicateurs détaillés dans un volet replié, chacun expliqué.
    """
    client = ctx["client"]
    run = cucumber_coverage.last_run(client)
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center", gap="small"):
            for text, group in cucumber_coverage.summary_sentence(rows, run):
                if group:
                    st.button(text, type="tertiary", key=f"cucumber_group_{group}_{client}",
                              on_click=st.session_state.update, kwargs={f"cucumber_group_{client}": group},
                              help="Filtre la liste des tâches")
                else:
                    st.markdown(f"**{text}**")
        document = (links or {}).get("document") or {}
        if document:
            st.caption(f"Tests lus sur la branche {document.get('branche') or '?'} (commit {document.get('commit')}"
                       + (f", {document['date'][8:10]}/{document['date'][5:7]} {document['date'][11:16]}"
                          if document.get("date") else "") + ")"
                       + ("" if document.get("branche_profil") else " — branche du job Jenkins non renseignée dans "
                                                                   "le profil : branche de base lue"))
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(f":material/arrow_forward: **Action suivante** : {action['texte']}")
            if action["cle"]:
                st.button({"import": "Importer un log Jenkins", "rattacher": "Rattacher les tests",
                           "generer": "Générer les tests"}[action["cle"]], type="primary",
                          key=f"cucumber_next_{client}", on_click=st.session_state.update,
                          kwargs={f"cucumber_open_{client}": action["cle"]})
        with st.expander("Indicateurs détaillés", icon=":material/insights:"):
            rates = cucumber_coverage.rates(rows)
            states = rates["par_etat"]
            tests = document.get("features") or []
            st.markdown("\n".join([
                f"- **Couverture réelle** : " + (f"{rates['reelle']} %" if run else "non mesurée")
                + " — part des tâches dont les scénarios actifs sont verts au dernier run importé"
                + ("." if run else " ; aucun run importé et analysé."),
                f"- **Couverture écrite** : {rates['ecrite']} % — part des tâches qui ont des scénarios écrits (actifs "
                f"ou désactivés, exécutés ou non), y compris par une autre tâche qui les couvre.",
                f"- **Tests existants** : {len(tests)} feature(s), {sum(f['scenarios'] for f in tests)} scénario(s) "
                f"actif(s) dans le dépôt client, écrits ou non par l'agent ; "
                f"{len((links or {}).get('non_rattachees') or [])} non rattaché(s) à une tâche.",
                f"- **Partielles** : {states['partiel']} — les tests ne vérifient qu'une configuration ou la présence "
                f"d'une donnée, pas l'effet du spécifique.",
                f"- **À confirmer** : {states['a_confirmer']} — testables, mais une donnée, une clé ou une valeur est à "
                f"confirmer (carte de la tâche).",
                f"- **En attente d'une autre tâche** : {states['en_attente']} — couvertes par une tâche qui n'a pas "
                f"encore de test.",
                f"- **Non testables aujourd'hui** : {states['non_testable']} — blocage prouvé hors du test.",
                f"- **Sans objet** : {states['sans_objet']} — aucun comportement propre au client à tester."]))
            st.caption(cucumber_coverage.legend())


def render_unlinked_tests(ctx: dict, rows: list, links: dict) -> None:
    """Tests existants non rattachés : proposition de rattachement à confirmer, ou autre tâche, ou aucune."""
    client = ctx["client"]
    unlinked = (links or {}).get("non_rattachees") or []
    if not unlinked:
        st.caption("Tous les tests existants du dépôt client sont rattachés à une tâche.")
        return
    titles = {r["id"]: r["titre"] for r in rows}
    for item in unlinked:
        with st.container(border=True):
            st.markdown(f"**{item['titre'] or item['feature'].rsplit('/', 1)[-1]}** :gray[({item['scenarios']} "
                        f"scénario(s) — `{item['feature']}`)]")
            proposal = item.get("proposition")
            if proposal:
                st.caption(f"Proposition : « {titles.get(proposal['spec'], proposal['spec'])} » — "
                           + " ; ".join(proposal["raisons"][:3]))
            with st.container(horizontal=True, vertical_alignment="center"):
                if proposal:
                    st.button("Confirmer", icon=":material/link:", key=f"cucumber_link_ok_{item['feature']}",
                              on_click=cucumber_links.confirm, args=(client, item["feature"], proposal["spec"]))
                other = st.selectbox("Autre tâche", [""] + list(titles), key=f"cucumber_link_other_{item['feature']}",
                                     format_func=lambda i: "Rattacher à une autre tâche…" if not i else titles[i],
                                     label_visibility="collapsed")
                if other:
                    cucumber_links.confirm(client, item["feature"], other)
                    st.rerun()
                st.button("Aucune tâche", type="tertiary", key=f"cucumber_link_none_{item['feature']}",
                          on_click=cucumber_links.confirm, args=(client, item["feature"], ""),
                          help="Test sans tâche de l'inventaire : il n'apparaîtra plus ici.")


def render_cucumber_audit(client: str, inventory: dict, rows: list) -> None:
    """
    Action « Auditer » (point 9i, 0 token) : chaque scénario lié à une tâche détecte-t-il l'absence du spécifique,
    qualité de ses assertions, robustesse, durée ; les verdicts « partiel » alimentent l'état partiel.
    """
    audit = cucumber_audit.load_audit(client)
    with st.container(horizontal=True, vertical_alignment="center"):
        if st.button("Auditer les tests", icon=":material/policy:", key=f"cucumber_audit_{client}",
                     help="Pour chaque scénario lié à une tâche : détecte-t-il l'absence du spécifique (exécution, "
                          "sinon éléments propres cités), qualité des assertions, robustesse (attente fixe, date en "
                          "dur, comptage global, dépendance à un autre scénario), durée au dernier run. Aucun appel "
                          "à Claude."):
            state = cucumber_tools.load_state(client)
            verified = {k: v["statut"] for k, v in cucumber_coverage.verified_results(state).items()}
            audit = cucumber_audit.run_audit(client, inventory, rows, verified)
            st.rerun()
        st.caption(cucumber_audit.summary_line(audit) + (
            f" · {len(audit['introuvables'])} scénario(s) introuvable(s) dans les features"
            if audit.get("introuvables") else "") if audit else
                   "Aucun audit : les tests existants n'ont pas encore été évalués.")


def selection_candidates(rows: list) -> list:
    """Lignes proposées à la sélection (non couvertes, partielles, non testables), dans l'ordre proposé."""
    return [r for r in rows if r["etat"] in cucumber_coverage.SELECTABLE_STATES]


def selection_key(client: str, inventory: dict) -> str:
    return f"cucumber_selection_{client}_{inventory.get('genere_le', '')}"


def current_selection(candidates: list, key: str) -> list:
    """
    Tâches cochées : cases par défaut (non couvertes et partielles) puis modifications du tableau, lues dans la
    session pour que le résumé du parcours, affiché avant le tableau, soit à jour.
    """
    edits = (st.session_state.get(key) or {}).get("edited_rows") or {}
    selected = []
    for index, row in enumerate(candidates):
        checked = generable(row)
        checked = (edits.get(index) or edits.get(str(index)) or {}).get("Couvrir", checked)
        if checked:
            selected.append(row["id"])
    return selected


def run_cucumber_generation(ctx: dict, extra: list, requested: list = None) -> int:
    """
    Lance une génération (nouvelle, reprise, complément) avec la progression des lots, sans aucun journal brut.
    Chaque tâche demandée finit avec un résultat enregistré : celui de son lot, sinon « Génération interrompue »
    (cause en une phrase) ; son état ne change pas.
    """
    client = ctx["client"]
    state = cucumber_tools.load_state(client)
    requested = requested or (extra[extra.index("--specifics") + 1].split(",") if "--specifics" in extra else
                 [i for lot in cucumber_tools.pending_lots(state) for i in lot["ids"]] if "--resume" in extra else
                 cucumber_tools.completable_specifics(state) if "--complete" in extra else [])
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    progress = st.progress(0.0, text="Préparation…")
    # Avancement par fonctionnalité (le découpage interne en lots n'est pas affiché)
    titles = {r["id"]: r["titre"] for r in ctx.get("rows") or []}
    libelles = {s["libelle"]: titles.get(s["id"], s["libelle"])
                for s in cucumber_tools.load_inventory(client).get("specifiques") or []}
    done = []

    def show_lot_progress(line: str) -> None:
        ctx["capture"](line)
        match = re.match(r"\[LOT (\d+)/(\d+)\] (.*)$", line.strip())
        if match:
            current = [libelles.get(name.strip(), name.strip()) for name in match.group(3).split(" ; ")]
            total = max(len(requested), len(done) + len(current))
            progress.progress(min(len(done) / total, 1.0) if total else 0.0,
                              text="Écriture des tests en cours : " + ", ".join(current))
            done.extend(current)
    returncode = run_main_step(ctx["cmd"] + extra, on_line=show_lot_progress)
    progress.progress(1.0, text="Terminé" if returncode == 0 else "Interrompu")
    missing = cucumber_results.missing_results(cucumber_tools.load_state(client), requested, started)
    if missing:
        cucumber_results.record_interruption(client, missing, interruption_cause(returncode),
                                             "corriger la cause indiquée, puis relancer l'écriture", started)
    environment = journal_phrases(CUCUMBER_ENV_PREFIX)
    if environment:
        st.error("**L'écriture des tests n'a pas pu commencer** : l'état des fonctionnalités est inchangé.\n\n"
                 + "\n".join(f"- {p}" for p in environment), icon=":material/block:")
    elif missing:
        st.error(f"Le test n'a pas pu être écrit pour {len(missing)} fonctionnalité(s) : "
                 f"{interruption_cause(returncode)}. Leur état est inchangé ; voir « Résultats ».",
                 icon=":material/block:")
    else:
        st.success("Écriture terminée : le résultat de chaque fonctionnalité est dans « Résultats ».",
                   icon=":material/task_alt:")
    return returncode


def run_cucumber_init_lot(ctx: dict, lot: dict) -> int:
    """Lot d'initialisation (patch séparé, avant les lots de tests) : init complète ou compléments selon l'état."""
    st.info(f"{lot['titre']} {ctx['client']} en cours...")
    returncode = run_main_step(ctx["cmd"] + ["--init-only"], on_line=ctx["capture"])
    if returncode == 0:
        st.success(f"{lot['titre']} : terminé ; le résultat est dans « Résultats » (Préparation des données de test).")
    else:
        st.error(f"{lot['titre']} en échec (code {returncode}) : voir le journal technique (en haut de l'onglet).")
    return returncode


def generation_plan(inventory: dict, selected: list, dependencies: dict) -> list:
    """Lots de tests prévus pour les tâches cochées (aperçu, tri sans token compris) et leur dépendance à l'init."""
    specifics = {s["id"]: s for s in inventory.get("specifiques") or []}
    remaining, sorted_out = cucumber_tools.presort([i for i in selected if i in specifics], specifics)
    lots = cucumber_tools.make_lots(remaining, max(1, env_int("CUCUMBER_BATCH_SIZE", 3)), specifics)
    plan = [{"Lot": f"Tests {n}", "Tâches": ", ".join(specifics[i]["libelle"] for i in lot["ids"]),
             "Initialisation": cucumber_detection.lot_dependency(lot["ids"], dependencies)}
            for n, lot in enumerate(lots, 1)]
    if sorted_out:
        plan.append({"Lot": "Tri sans appel Claude", "Tâches": f"{len(sorted_out)} tâche(s) (listener, technique)",
                     "Initialisation": ""})
    return plan


def render_cucumber_selection(ctx: dict, inventory: dict, state: dict, rows: list, status: dict,
                              dependencies: dict) -> None:
    """
    Étape 3 : plan de génération — lot d'initialisation proposé selon l'état de l'init (coché par défaut), choix
    des tâches à couvrir ou à compléter, lots de tests et leur dépendance à l'init — puis génération.
    """
    client, ready = ctx["client"], ctx["ready"]
    init_lot = cucumber_detection.init_lot(status)
    run_init = False
    if init_lot:
        with st.container(border=True):
            run_init = st.checkbox(f"**Lot 0 — {init_lot['titre']}** (patch séparé, avant les lots de tests)",
                                   value=True, key=f"cucumber_init_lot_{client}_{status['etat']}")
            st.caption("Init absente : copie de l'initialisation du Cucumber Standard pour les modules du client "
                       "(clear de l'environnement, intégration globale, fichiers du module spécifique, recréation des "
                       "données d'initializers, contrôle de l'environnement)." if init_lot["mode"] == "init" else
                       "Init incomplète : seuls les éléments manquants livrables sont ajoutés, dans une nouvelle "
                       "feature à côté de l'init ; l'init existante et les fichiers protégés ne sont jamais "
                       "modifiés. Les autres constats restent à traiter à la main, avec leur preuve.")
            with st.expander(f"Éléments du lot ({len(init_lot['constats'])} constat(s))"):
                render_cucumber_findings(status)
    candidates = selection_candidates(rows)
    selected, accepted = [], True
    if not candidates:
        st.info("Aucune tâche non couverte, partielle ou non testable.", icon=":material/task_alt:")
    else:
        st.caption("Ordre proposé : risque métier, puis testabilité, puis effort. Les tâches « à confirmer » sont "
                   "cochées dès que leurs réponses sont données (carte de la tâche, vue Couverture) ; les tâches « non testables aujourd'hui » "
                   "(blocage prouvé) et « sans objet » ne le sont pas : sélectionnez-les en connaissance de cause. "
                   "Pour une tâche "
                   "partielle, le lot teste l'EFFET (document généré, message envoyé, statut, champ enregistré), pas "
                   "la configuration.")
        key = selection_key(client, inventory)
        table = [{"Couvrir": generable(r), "État": cucumber_coverage.state_label(r),
                  "Tâche": r["tache"], "Module": r["module"], "Risque": r["risque"], "Testabilité": r["testabilite"],
                  "Effort": r["effort"], "Tickets": ", ".join(r["tickets"])} for r in candidates]
        st.data_editor(table, key=key, hide_index=True, width="stretch", height=360,
                       disabled=[c for c in table[0] if c != "Couvrir"],
                       column_config={**COVERAGE_COLUMNS,
                                      "Couvrir": st.column_config.CheckboxColumn("Couvrir", width="small"),
                                      "Effort": st.column_config.NumberColumn(
                                          "Effort", width="small", help="Estimation : fichiers, absence de test "
                                                                        "Standard ou de step candidat, comportements "
                                                                        "Studio")})
        selected = current_selection(candidates, key)
        warnings = cucumber_coverage.selection_warnings(rows, selected)
        if warnings:
            st.warning(f"**{len(warnings)} tâche(s) non testable(s) aujourd'hui sélectionnée(s)** : un blocage "
                       "prouvé, hors du test, empêche aujourd'hui leur test.\n\n" + "\n".join(f"- {w}" for w in warnings[:10]),
                       icon=":material/warning:")
            accepted = st.checkbox("Je génère aussi ces tâches", key=f"cucumber_untestable_{client}")
    batch_size = max(1, env_int("CUCUMBER_BATCH_SIZE", 3))
    lot_count = -(-len(selected) // batch_size)
    average = cucumber_tools.lot_cost_average(os.path.join(".tmp", "token_usage.csv"))
    estimate = (f"≈ {lot_count * average['tokens'] / 1e6:.1f} M tokens, ≈ {lot_count * average['cout_usd']:.2f} $ "
                f"(moyenne de {average['lots']} lot(s) précédent(s))" if average["lots"] else
                "coût inconnu (aucun lot exécuté jusqu'ici)")
    if selected:
        st.caption(f"{len(selected)} tâche(s) cochée(s) → {lot_count} lot(s) de {batch_size}, regroupés par test "
                   f"Standard et par module · {estimate}")
        plan = generation_plan(inventory, selected, dependencies)
        if any(p["Initialisation"] for p in plan):
            st.dataframe(plan, hide_index=True, width="stretch")
    existing = bool(state.get("lots")) and os.path.isdir(cucumber_tools.worktree_dir(client))
    ids = ",".join(selected)
    tests_ok = bool(selected) and accepted
    if st.button("Générer les tests", type="primary", icon=":material/science:",
                 disabled=not (ready and (tests_ok or run_init)),
                 help=("Lot d'initialisation d'abord (patch séparé), puis " if run_init else "")
                 + ("ajoute des lots à la génération en cours, sans rien effacer." if existing else
                    "crée les lots des tâches cochées.")):
        if run_init:
            run_cucumber_init_lot(ctx, init_lot)
        if tests_ok:
            run_cucumber_generation(ctx, ["--complete", "--specifics", ids] if existing else ["--specifics", ids])
    render_init_patch(client)
    if existing:
        with st.expander("Nouvelle génération (repart de la base)", icon=":material/restart_alt:"):
            st.warning(f"Une génération existe déjà ({len(state['lots'])} lot(s)) : une nouvelle génération repart "
                       f"de la base et **les lots précédents seront écrasés** (commits, patch, récapitulatif).",
                       icon=":material/warning:")
            overwrite = st.checkbox("Je confirme : les lots précédents seront écrasés",
                                    key=f"cucumber_overwrite_{client}")
            if st.button("Nouvelle génération", icon=":material/delete_sweep:",
                         disabled=not (ready and tests_ok and overwrite)):
                if run_init:
                    run_cucumber_init_lot(ctx, init_lot)
                run_cucumber_generation(ctx, ["--specifics", ids])


def lot_table(state: dict, dependencies: dict = None) -> list:
    """Lignes du tableau des lots de la dernière génération (dépendance au lot d'initialisation comprise)."""
    lot_rows = []
    for lot in state["lots"]:
        tokens = lot.get("tokens") or {}
        total_tokens = sum(v for k, v in tokens.items() if k != "cout_usd" and isinstance(v, (int, float)))
        results = lot.get("resultats") or []
        indicators = cucumber_tools.lot_indicators(lot)
        lot_rows.append({
            "Lot": lot["numero"], "Statut": lot.get("statut", ""), "Spécifiques": len(lot["ids"]),
            "Couverts": sum(1 for r in results if r.get("statut") == "couvert"),
            "Indirects": sum(1 for r in results if r.get("statut") == "couvert_indirectement"),
            "Scénarios": indicators.get("scenarios"),
            "Steps Standard (%)": indicators.get("reutilisation"),
            "Steps créés": indicators.get("steps_crees"),
            "Signalés": indicators.get("signales"), "Supprimés": indicators.get("supprimes"),
            # Commandes base réellement lancées (journal de db_tools) et tables lues
            "Base": (f"{len(lot['base'])} cmd : " + ", ".join(dict.fromkeys(
                t for e in lot["base"] for t in e.get("tables") or [])) if lot.get("base") else None),
            "Tokens": total_tokens or None,
            "Coût ($)": round(tokens["cout_usd"], 3) if isinstance(tokens.get("cout_usd"), (int, float)) else None,
            "Erreur": lot.get("erreur", "") or ("tri Python, sans appel Claude" if lot.get("tri") else ""),
            "Initialisation": "" if lot.get("tri") or lot.get("statut") == "termine" else
            cucumber_detection.lot_dependency(lot["ids"], dependencies or {}),
        })
    return lot_rows


def render_cucumber_lots(state: dict, pending: list, dependencies: dict = None) -> None:
    """Tableau des lots de la dernière génération et indicateurs de contrôle."""
    lot_rows = lot_table(state, dependencies)
    with st.expander(f"Lots de la dernière génération ({sum(1 for r in lot_rows if r['Statut'] == 'termine')}"
                     f"/{len(lot_rows)} terminés)", expanded=bool(pending)):
        controlled = [c for c in (cucumber_tools.lot_indicators(lot) for lot in state["lots"]) if c]
        std_steps = sum(c["steps_std"] for c in controlled)
        all_steps = sum(c["steps_std"] + c["steps_client"] + c["non_definis"] for c in controlled)
        if controlled:
            st.caption(f"{sum(c['scenarios'] for c in controlled)} scénario(s) contrôlé(s) · réutilisation des "
                       f"steps Standard : {round(100 * std_steps / all_steps) if all_steps else '—'} % · "
                       f"{sum(c['signales'] for c in controlled)} signalé(s), "
                       f"{sum(c['supprimes'] for c in controlled)} supprimé(s) à la revue")
        st.dataframe(lot_rows, hide_index=True, width="stretch", column_config={
            "Indirects": st.column_config.NumberColumn(
                "Indirects", help="Spécifiques couverts indirectement (listener, événement) par le test d'un "
                                  "autre spécifique couvert"),
            "Steps Standard (%)": st.column_config.NumberColumn(
                "Steps Standard (%)", help="Part des steps des nouveaux scénarios définis par le Cucumber Standard"),
            "Signalés": st.column_config.NumberColumn(
                "Signalés", help="Scénarios signalés par les contrôles (doublon, sans assertion métier, "
                                 "différence ou source absente), envoyés à la revue"),
        })


def render_cucumber_verification(client: str, state: dict) -> None:
    """Vérification par exécution des scénarios générés et revue humaine d'un échantillon."""
    verified_rows = cucumber_verify.scenario_rows(state)
    if not verified_rows:
        return
    indicators = cucumber_verify.verification_indicators(state)
    st.markdown("##### :material/fact_check: Vérification des scénarios")
    if indicators["hors_test"]:
        st.warning(f"**{indicators['hors_test']} échec(s) hors test**, ni corrigés ni retirés : "
                   f"🌐 {indicators['environnement']} environnement (route, champ exigé, version, init en échec) · "
                   f"📭 {indicators['donnee_reference']} donnée de référence absente · "
                   f"🐞 {indicators['bug_applicatif']} bug applicatif probable · "
                   f"⛓️ {indicators['cascade']} en cascade d'une mise en place en échec · "
                   f"🚚 {indicators['deploiement']} déploiement en retard (extrait vérifié). Corrigez l'environnement "
                   f"(profil et initialisation du client, bilan de santé) ou signalez le bug, puis relancez.",
                   icon=":material/public:")
        with st.expander("Scénarios non jugés : environnement, donnée de référence, bug applicatif, cascade, "
                         "déploiement"):
            st.dataframe([{k: row[k] for k in ("Lot", "Scénario", "Statut", "Motif", "Build")}
                          for row in verified_rows if row["Statut"] in cucumber_verify.NOT_TEST_STATUSES],
                         hide_index=True, width="stretch",
                         column_config={"Build": st.column_config.LinkColumn("Build", display_text="Jenkins")})
    if not indicators["verifies"] and not indicators["hors_test"]:
        message = next((lot["execution"]["message"] for lot in state["lots"] if lot.get("execution")), "")
        st.warning(f"**Pertinence non vérifiée par exécution** : {message or 'aucun moyen d’exécution configuré'}. "
                   "Configurez CUCUMBER_VERIFY_JOB ou CUCUMBER_VERIFY_LOCAL_CMD (onglet Configuration).",
                   icon=":material/help:")
    elif indicators["verifies"]:
        def pct(value):
            return "—" if value is None else f"{value} %"
        st.caption(f"Pertinents : {pct(indicators['taux_pertinent'])} des {indicators['verifies']} vérifié(s) · "
                   f"mutations détectées : {pct(indicators['taux_mutation'])} · overrides en échec sur le "
                   f"Standard : {pct(indicators['taux_echec_standard'])} · {indicators['desactives']} désactivé(s) "
                   f"(A_REVOIR.md) · {indicators['faibles']} faible(s) · validation humaine : "
                   f"{pct(indicators['taux_validation_humaine'])}")
    st.dataframe([{k: v for k, v in row.items() if k != "tag"} for row in verified_rows], hide_index=True,
                 width="stretch", column_config={
                     "Build": st.column_config.LinkColumn("Build", display_text="Jenkins"),
                     "Statut": st.column_config.TextColumn(
                         "Statut", help="pertinent : passe sur le client, échoue sur le Standard (override) et "
                                        "mutation détectée ; faible : mutation non détectée ou passe sur le "
                                        "Standard ; invalide : échoue sur le client ; non_verifie : pas "
                                        "d'exécution (jamais compté pertinent) ; environnement : route absente, "
                                        "champ exigé ou version ; donnee_reference : code absent de "
                                        "l'environnement ; bug_applicatif : erreur serveur ; cascade : une mise "
                                        "en place de la feature a échoué avant lui ; deploiement : code ou "
                                        "personnalisation déployés en retard (extrait vérifié dans le log). "
                                        "Seuls invalide et faible sont corrigés ; rien n'est supprimé, les "
                                        "blocages hors test prouvés sont désactivés (A_REVOIR.md)."),
                     "Client": st.column_config.TextColumn("Client", width="small"),
                     "Standard": st.column_config.TextColumn("Standard", width="small"),
                     "Mutation": st.column_config.TextColumn("Mutation", width="small"),
                     "Statique": st.column_config.TextColumn("Statique", width="small"),
                 })
    # Autonomie : pas de jugement demandé (les choix de l'agent sont dans l'onglet « Décisions » du mode expert)
    sample = [] if cucumber_decisions.autonomous() else cucumber_verify.review_sample(state)
    if not sample:
        return
    unverified = sum(1 for item in sample if item["statut"] == "non_verifie")
    with st.expander(f"Revue humaine : échantillon de {len(sample)} scénario(s), dont {unverified} non "
                     f"vérifié(s) par l'exécution", expanded=True):
        by_tag = {row["tag"]: row for row in verified_rows}
        for item in sample:
            row = by_tag.get(item["tag"], {})
            with st.container(border=True):
                st.markdown(f"**{row.get('Scénario', item['id'])}** :gray[(lot {item['lot']}, {item['tag']}, "
                            f"{item['statut'].replace('_', ' ')})]")
                if item["statut"] == "non_verifie" and item.get("motif"):
                    st.caption(f"Non vérifié : {item['motif']}")
                st.caption(f"Différence avec le Standard : {row.get('Différence avec le Standard') or '—'} · "
                           f"Source : {row.get('Source') or '—'} · "
                           f"Inspiré de : {row.get('Inspiré du Standard') or '—'}")
                verdict = (state.get("verdicts") or {}).get(item["tag"])
                if verdict:
                    st.markdown(f":gray[Verdict : **{verdict['verdict'].replace('_', ' ')}**"
                                + (f" — {verdict['motif']}" if verdict.get("motif") else "") + "]")
                with st.container(horizontal=True, vertical_alignment="bottom"):
                    reason = st.text_input("Motif", key=f"verdict_motif_{item['tag']}",
                                           placeholder="Obligatoire si non pertinent")
                    if st.button("Pertinent", key=f"verdict_ok_{item['tag']}", icon=":material/thumb_up:"):
                        cucumber_verify.record_verdict(client, state, item, "pertinent", reason)
                        cucumber_tools.save_state(client, state)
                        st.rerun()
                    if st.button("Non pertinent", key=f"verdict_ko_{item['tag']}",
                                 icon=":material/thumb_down:", disabled=not reason.strip()):
                        cucumber_verify.record_verdict(client, state, item, "non_pertinent", reason)
                        cucumber_tools.save_state(client, state)
                        st.rerun()
        st.caption("Un verdict « Non pertinent » et son motif deviennent une consigne du projet, injectée "
                   "dans les lots des prochaines générations.")


def render_cucumber_generation(ctx: dict, state: dict, dependencies: dict = None) -> None:
    """Étape 4 : lots, contrôles, vérification par exécution, reprise, complément et patch."""
    client, ready = ctx["client"], ctx["ready"]
    if not state.get("lots"):
        st.info("Aucune génération : lance-la depuis l'écran principal (« Lancer » ou « Écrire les tests »).",
                icon=":material/science:")
        return
    pending = cucumber_tools.pending_lots(state)
    completable = cucumber_tools.completable_specifics(state)
    with st.container(horizontal=True):
        resume = st.button("Reprendre", icon=":material/play_arrow:", disabled=not (ready and pending),
                           help=f"{len(pending)} lot(s) non terminé(s) lors de la dernière génération."
                           if pending else "Aucun lot à reprendre.")
        complete = st.button("Compléter la génération", icon=":material/playlist_add:",
                             disabled=not (ready and completable),
                             help=f"{len(completable)} spécifique(s) non couvert(s) faute de step, d'endpoint, "
                                  f"d'interaction ou de vérification : repris dans de nouveaux lots (recettes de "
                                  f"test, synonymes), sans effacer les lots ni les commits précédents."
                             if completable else "Aucun spécifique à compléter.")
    if resume or complete:
        run_cucumber_generation(ctx, ["--resume"] if resume else ["--complete"])
        state = cucumber_tools.load_state(client)
        pending = cucumber_tools.pending_lots(state)
    render_cucumber_lots(state, pending, dependencies)
    render_cucumber_verification(client, state)
    # Patch d'initialisation : affiché une seule fois, avec le plan de génération (même section)
    if os.path.exists(cucumber_tools.patch_path(client)):
        patch_file = cucumber_tools.patch_path(client)
        generated_at = time.strftime("%d/%m/%Y %H:%M", time.localtime(os.path.getmtime(patch_file)))
        with st.container(border=True):
            st.markdown(f"**:material/description: Patch** `{patch_file}` :gray[(généré le {generated_at})]")
            with open(patch_file, "rb") as f:
                st.download_button("Télécharger le patch", f.read(), file_name=os.path.basename(patch_file),
                                   mime="text/x-diff", icon=":material/download:",
                                   key=f"cucumber_generation_patch_{client}")


def render_manual_correction(client: str, state: dict) -> None:
    """Signalement d'une correction faite à la main sur l'environnement : historisée, reprise dans le bilan."""
    with st.form(f"cucumber_manual_{client}", border=True):
        st.markdown("**:material/build_circle: Signaler une correction manuelle de l'environnement**")
        st.caption("Interdite dans la durée : le bilan indique comment la recréer dans l'init du projet.")
        description = st.text_area("Correction faite (donnée créée, paramétrage modifié…)", height=80)
        if st.form_submit_button("Signaler", icon=":material/flag:") and description.strip():
            cucumber_bilan.add_manual_correction(client, description)
            numero, analysis = latest_import_analysis(client)
            if analysis:
                refresh_bilan(client, analysis, state)
            st.rerun()


def render_cucumber_review(client: str, state: dict) -> None:
    """
    Étape 7 : bilan du dernier run importé (défauts en tête, corrigés, désactivés, doutes, corrections manuelles,
    prochaine action), signalement d'une correction manuelle, A_REVOIR.md et récapitulatif de couverture.
    """
    bilan = cucumber_bilan.latest_bilan(client)
    if bilan:
        with st.container(border=True):
            st.markdown(cucumber_bilan.bilan_markdown(bilan, level=4))
    render_manual_correction(client, state)
    review_file = os.path.join(cucumber_tools.worktree_dir(client), "A_REVOIR.md")
    coverage_file = cucumber_tools.coverage_path(client)
    if not bilan and not os.path.exists(review_file) and not os.path.exists(coverage_file):
        st.info("Aucun bilan : il est écrit après chaque import de run et à la fin d'une génération.",
                icon=":material/summarize:")
        return
    if os.path.exists(review_file):
        with st.expander("A_REVOIR.md (livré dans le patch)", icon=":material/rule:", expanded=not bilan):
            with open(review_file, "r", encoding="utf-8", errors="replace") as f:
                st.markdown(f.read())
    if os.path.exists(coverage_file):
        with st.expander("Récapitulatif de couverture"):
            with open(coverage_file, "r", encoding="utf-8", errors="replace") as f:
                st.markdown(f.read())


# ---------------------------------------------------------
# ONGLET CUCUMBER : PARCOURS EN 7 ÉTAPES, DU CHOIX DES TÂCHES AU PATCH VERT
# ---------------------------------------------------------
with tab_cucumber:
    st.markdown("### Tests Cucumber des fonctionnalités spécifiques")
    st.caption("Choisissez le projet : l'agent analyse le reste tout seul, sans rien modifier dans les dépôts. Les "
               "tests écrits sont livrés comme des modifications à télécharger : rien n'est envoyé.")
    cucumber_ctx = cucumber_header()
    cucumber_client = cucumber_ctx["client"]
    render_cucumber_detection(cucumber_ctx)
    cucumber_inventory = cucumber_tools.load_inventory(cucumber_client) if cucumber_client else {}
    cucumber_profile_doc = cucumber_profile.load_profile(cucumber_client) if cucumber_client else {}
    cucumber_state = cucumber_tools.load_state(cucumber_client) if cucumber_client else {}
    cucumber_detected = cucumber_detection.load_detection(cucumber_client) if cucumber_client else {}
    cucumber_analysis = cucumber_journey.last_analysis(cucumber_client) if cucumber_client else {}
    cucumber_init_status = cucumber_detection.init_status(
        cucumber_detected.get("faits") or {}, cucumber_profile.effective_profile(cucumber_profile_doc),
        cucumber_client, cucumber_analysis,
        cucumber_import.imports_dir(cucumber_client) if cucumber_client else "")
    cucumber_dependencies = cucumber_detection.init_dependencies(cucumber_init_status,
                                                                 cucumber_detected.get("faits") or {})
    cucumber_rows = (cucumber_coverage.coverage_rows(cucumber_client, cucumber_inventory, cucumber_state)
                     if cucumber_inventory.get("specifiques") else [])
    cucumber_selected = current_selection(selection_candidates(cucumber_rows),
                                          selection_key(cucumber_client, cucumber_inventory))
    cucumber_ctx["rows"] = cucumber_rows
    cucumber_links_now = cucumber_links.current_links(cucumber_client) if cucumber_client else {}
    if cucumber_client and cucumber_rows:
        render_cucumber_simple(cucumber_ctx, cucumber_rows, cucumber_init_status)
    elif cucumber_client:
        st.info("Les fonctionnalités spécifiques du projet apparaîtront ici dès la fin de son analyse automatique.",
                icon=":material/hourglass_top:")
    if cucumber_client:
        st.divider()
        # Ouvert de lui-même quand une fiche, un filtre ou une préparation y est demandé
        detail_wanted = any(st.session_state.get(f"{k}_{cucumber_client}") for k in (
            "cucumber_card", "cucumber_filtre", "cucumber_init_demande"))
        if section("Détail du projet", ":material/list_alt:", f"detail_{cucumber_client}", detail_wanted):
            render_todo_banner(cucumber_ctx, cucumber_rows, cucumber_links_now, cucumber_init_status,
                               cucumber_detected)
            if cucumber_rows:
                render_zone_status(cucumber_ctx, cucumber_rows)
                render_zone_tasks(cucumber_ctx, cucumber_rows)
                render_zone_results(cucumber_ctx, cucumber_rows)
        if section("Mode expert", ":material/engineering:", f"expert_{cucumber_client}", False):
            render_expert(cucumber_ctx, {
                "rows": cucumber_rows, "links": cucumber_links_now, "inventory": cucumber_inventory,
                "state": cucumber_state, "profile_doc": cucumber_profile_doc, "detected": cucumber_detected,
                "init_status": cucumber_init_status, "dependencies": cucumber_dependencies,
                "analysis": cucumber_analysis})


def render_knowledge_outbox() -> None:
    """
    Documents en file locale (partage indisponible lors du dépôt) : nombre affiché, nouvel essai au premier
    affichage de la session puis à la demande.
    """
    if not knowledge_tools.enabled():
        return
    retry = not st.session_state.get("knowledge_outbox_tried") and knowledge_tools.outbox_count()
    with st.container(horizontal=True, vertical_alignment="center"):
        retry = st.button("Déposer les documents en attente", icon=":material/cloud_upload:",
                          disabled=not knowledge_tools.outbox_count()) or retry
        pending_caption = st.empty()
    if retry:
        st.session_state["knowledge_outbox_tried"] = True
        with st.spinner("Dépôt des documents en attente…"):
            flushed = knowledge_tools.flush_outbox()
        for message in flushed["messages"]:
            st.caption(message)
    pending = knowledge_tools.outbox_count()
    pending_caption.caption(f":orange[{pending} document(s) en attente de dépôt] (partage indisponible lors de "
                            f"l'analyse, file locale .tmp/knowledge_outbox)" if pending else
                            "Aucun document en attente de dépôt.")


# ---------------------------------------------------------
# ONGLET 3 : ÉCOUTE DES MERGES BITBUCKET (post-merge Cucumber)
# ---------------------------------------------------------
@st.fragment(run_every="10s")
def merge_listener_status():
    """Statut et journal rafraîchis toutes les 10 s sans recharger toute la page."""
    status = get_poller_status()
    # Passage actif <-> arrêté détecté par le rafraîchissement : recharge complète pour réactiver le formulaire
    running = bool(status)
    previous = st.session_state.get("merge_listener_running")
    st.session_state["merge_listener_running"] = running
    if previous is not None and previous != running:
        st.rerun()

    with st.container(border=True):
        if not status:
            st.markdown(":gray-badge[:material/pause_circle: Écoute arrêtée]")
        elif status.get("state") == "stopping":
            st.markdown(":orange-badge[:material/hourglass_top: Arrêt en cours] "
                        "— l'écoute finit le traitement en cours avant de s'arrêter.")
        else:
            st.markdown(f":green-badge[:material/sensors: Écoute active] sur **{status['projects']}**")
            started = status["started_at"].replace("T", " ")
            last_poll = status.get("last_poll", "").replace("T", " ") or "en cours…"
            st.caption(f"Depuis {started} · toutes les {status['interval'] // 60} min · "
                       f"dernier passage : {last_poll} · en file : {status.get('queued', 0)}")
            if status.get("repositories"):
                st.caption("Dépôts surveillés : " + ", ".join(status["repositories"]))

        if status and status.get("state") != "stopping":
            with st.container(horizontal=True):
                if st.button("Arrêter l'écoute", icon=":material/stop_circle:",
                             help="Plus aucun nouveau merge n'est pris ; un traitement déjà lancé va jusqu'au bout."):
                    request_stop()
                    st.rerun()
                if st.button("Forcer l'arrêt", icon=":material/dangerous:",
                             help="Arrête immédiatement, y compris l'agent s'il est en train de traiter un merge."):
                    st.toast(force_stop())
                    st.rerun()

    with st.expander("Journal de l'écoute", icon=":material/receipt_long:"):
        st.code(tail_log(40) or "Journal vide.", language="text")
    return status


# ---------------------------------------------------------
# ONGLET REVUE DE PR : RELIRE LES PULL REQUESTS DES COLLABORATEURS (BITBUCKET)
# ---------------------------------------------------------
REVIEW_VERDICTS = {"approuver": "✅ Peut être approuvée", "changements_demandes": "⛔ Changements à demander",
                   "a_discuter": "💬 À discuter avec l'auteur"}
REVIEW_SEVERITIES = {"bloquant": "🔴 Bloquant", "majeur": "🟠 Majeur", "mineur": "🟡 Mineur", "suggestion": "💡 Suggestion"}


def review_label(pr: dict) -> str:
    return f"{pr['projet']}/{pr['depot']} #{pr['id']} — {pr['titre']} ({pr['auteur']})"


def render_review_result(pr: dict, result: dict) -> None:
    """Résultat d'une analyse : verdict, résumé, commentaires à cocher, modifier et publier."""
    project, repo, number = pr["projet"], pr["depot"], pr["id"]
    stale = result.get("commit") and pr.get("commit") and result["commit"] != pr["commit"]
    st.markdown(f"#### {REVIEW_VERDICTS.get(result['verdict'], result['verdict'])} · risque {result['risque']}")
    if stale:
        st.warning("De nouveaux commits ont été poussés depuis cette analyse : relance-la pour les prendre en compte.",
                   icon=":material/update:")
    st.markdown(result["resume"])
    if result.get("conformite_ticket"):
        st.caption(f"Ticket : {result['conformite_ticket']}")
    if result.get("points_positifs"):
        with st.expander(f"Points positifs ({len(result['points_positifs'])})"):
            st.markdown("\n".join(f"- {p}" for p in result["points_positifs"]))
    if result.get("questions"):
        with st.expander(f"Questions à poser à l'auteur ({len(result['questions'])})"):
            st.markdown("\n".join(f"- {q}" for q in result["questions"]))
    if result.get("tests"):
        st.caption(f"Tests : {result['tests']}")
    comments = result.get("commentaires") or []
    st.markdown(f"**Commentaires proposés ({len(comments)})** — coche ceux à publier, modifie-les si besoin.")
    key = f"review_{project}_{repo}_{number}"
    for index, comment in enumerate(comments):
        with st.container(border=True):
            where = comment["fichier"] + (f":{comment['ligne']}" if comment.get("ligne") else "")
            head = f"{REVIEW_SEVERITIES.get(comment['gravite'], comment['gravite'])} · `{where}`"
            if comment.get("publie"):
                st.markdown(f"{head} — ✔️ publié")
                st.caption(comment["message"])
                continue
            st.checkbox(head + ("" if comment.get("ancrable") else " (publié en commentaire général : ligne hors "
                                                                     "du diff)"),
                        key=f"{key}_pick_{index}", value=comment["gravite"] in ("bloquant", "majeur"))
            st.text_area("Commentaire", comment["message"] + (f"\n\n```\n{comment['suggestion']}\n```"
                                                               if comment.get("suggestion") else ""),
                         key=f"{key}_text_{index}", label_visibility="collapsed", height=110)
    picked = [i for i, c in enumerate(comments) if not c.get("publie") and st.session_state.get(f"{key}_pick_{i}")]
    with st.container(horizontal=True, vertical_alignment="center"):
        publish = st.button(f"Publier sur Bitbucket ({len(picked)})", type="primary", icon=":material/send:",
                            disabled=not picked, key=f"{key}_publish")
        st.caption("Publiés sous ton nom, sur les lignes concernées de la PR. Rien n'est approuvé ni refusé "
                   "automatiquement.")
    if publish:
        lines_map = {p: {int(k): v for k, v in m.items()} for p, m in (result.get("lignes") or {}).items()}
        done, errors = 0, []
        for index in picked:
            comment = comments[index]
            text = (st.session_state.get(f"{key}_text_{index}") or comment["message"]).strip()
            try:
                pr_review.post_comment(project, repo, number, text, comment["fichier"], comment.get("ligne"),
                                       lines_map)
                comment["publie"], comment["message"] = True, text
                done += 1
            except pr_review.BitbucketError as e:
                errors.append(f"{comment['fichier']}:{comment.get('ligne')} — {e}")
        pr_review.save_result(project, repo, number, result)
        if done:
            st.success(f"{done} commentaire(s) publié(s) sur la PR.", icon=":material/task_alt:")
        for error in errors:
            st.error(error)
        if done and not errors:
            st.rerun()


def render_review_tab() -> None:
    st.markdown("### Relire les pull requests de tes collaborateurs")
    st.caption("L'agent lit la PR (diff, code complet, ticket Jira, conventions de l'équipe, commentaires déjà "
               "posés) et propose un avis et des commentaires ligne par ligne. Tu choisis ce qui est publié.")
    if not pr_review.configured():
        st.warning("Renseigne BITBUCKET_URL et BITBUCKET_HTTP_TOKEN dans l'onglet Configuration.",
                   icon=":material/key:")
        return
    with st.container(horizontal=True, vertical_alignment="bottom"):
        role = st.segmented_control("PR", ["À relire", "Où je participe", "Les miennes"], default="À relire",
                                    key="review_role")
        refresh = st.button("Actualiser", icon=":material/refresh:", key="review_refresh")
    roles = {"À relire": "REVIEWER", "Où je participe": "PARTICIPANT", "Les miennes": "AUTHOR"}
    cache_key = f"review_inbox_{roles.get(role or 'À relire')}"
    if refresh or cache_key not in st.session_state:
        try:
            st.session_state[cache_key] = pr_review.review_inbox(roles.get(role or "À relire", "REVIEWER"))
        except pr_review.BitbucketError as e:
            st.error(str(e))
            st.session_state[cache_key] = []
    prs = {f"{p['projet']}/{p['depot']}/{p['id']}": p for p in st.session_state.get(cache_key) or []}
    url = st.text_input("Ou adresse d'une PR", key="review_url",
                        placeholder="https://git.kiscloud.io/projects/APPAREL/repos/odatio-app/pull-requests/42")
    chosen = None
    if url.strip():
        try:
            project, repo, number = pr_review.parse_pr_url(url)
            chosen = pr_review.pr_details(project, repo, number)
        except (ValueError, pr_review.BitbucketError) as e:
            st.error(str(e))
    elif prs:
        choice = st.selectbox(f"Pull requests ({len(prs)})", list(prs), format_func=lambda k: review_label(prs[k]),
                              key="review_choice")
        chosen = prs.get(choice)
    else:
        st.info("Aucune pull request ouverte dans cette liste.", icon=":material/inbox:")
    if not chosen:
        return
    with st.container(border=True):
        st.markdown(f"**{chosen['titre']}** — {chosen['auteur']} · `{chosen['source']}` → `{chosen['cible']}`")
        details = [f"[Ouvrir dans Bitbucket]({chosen['url']})"] if chosen.get("url") else []
        if chosen.get("jira"):
            details.append("Jira : " + ", ".join(chosen["jira"]))
        if chosen.get("relecteurs"):
            details.append("Relecteurs : " + ", ".join(f"{r['nom']} ({r['statut'].lower()})"
                                                      for r in chosen["relecteurs"]))
        st.caption(" · ".join(details))
        result = pr_review.load_result(chosen["projet"], chosen["depot"], chosen["id"])
        analysed = bool(result) and result.get("commit") == chosen.get("commit")
        if st.button("Relancer l'analyse" if result else "Analyser la PR", type="secondary" if analysed else "primary",
                     icon=":material/troubleshoot:", key=f"review_run_{chosen['projet']}_{chosen['depot']}_{chosen['id']}"):
            pr_url = chosen.get("url") or (f"{pr_review.base_url()}/projects/{chosen['projet']}/repos/"
                                           f"{chosen['depot']}/pull-requests/{chosen['id']}")
            with st.spinner("Analyse de la PR en cours (lecture du code, du ticket et des conventions)…"):
                code = run_main_step([sys.executable, "main.py", "--step", "review-pr", "--pr-url", pr_url])
            if code == 0:
                st.rerun()
            st.error(f"Analyse interrompue : {interruption_cause(code)}.")
        if result:
            render_review_result(chosen, result)


with tab_review:
    render_review_tab()


with tab_merges:
    st.subheader("Écoute des merges Bitbucket")
    postmerge_project = os.getenv("POSTMERGE_JIRA_PROJECT", "").strip() or os.getenv("JIRA_PROJECT_KEY", "").strip()
    target_branches = " ou ".join(b.strip() for b in os.getenv("WEBHOOK_TARGET_BRANCHES", "integration,dev").split(",")
                                  if b.strip()) or "n'importe quelle branche"
    st.caption(f"À chaque Pull Request fusionnée vers {target_branches}, l'agent analyse le code fusionné, "
               f"crée le ticket {postmerge_project} « [CUCUMBER] Adaptation des tests post-merge » et y attache le patch Cucumber.")

    listener_status = merge_listener_status()

    with st.form("merge_listener_form"):
        listen_projects = st.text_input(
            "Projets Bitbucket à écouter",
            value=os.getenv("POLL_REPOSITORIES", ""),
            help="Clé de projet (tous ses dépôts) ou PROJET/dépôt, séparés par des virgules. "
                 "Ex : CLIENT, AUTRE/odatio-autre. Les dépôts *cucumber* sont toujours ignorés."
        )
        listen_interval = st.number_input(
            "Fréquence de vérification (minutes)", min_value=1, max_value=60,
            value=max(int(os.getenv("POLL_INTERVAL_SECONDS", "300")) // 60, 1)
        )
        listen_catch_up = st.checkbox(
            "Traiter aussi les merges survenus pendant l'arrêt de l'écoute", value=False,
            help="Décoché : seuls les merges postérieurs au démarrage sont traités."
        )
        start_listening = st.form_submit_button(
            "Démarrer l'écoute", type="primary", icon=":material/play_circle:",
            disabled=bool(listener_status),
            help="Arrêtez l'écoute pour en modifier les paramètres." if listener_status else None
        )

    if start_listening:
        projects_value = ",".join(p.strip().upper() if "/" not in p else p.strip()
                                  for p in listen_projects.split(",") if p.strip())
        if not projects_value:
            st.error("Renseignez au moins un projet Bitbucket.")
        else:
            env_tools.write_env(ENV_PATH, {"POLL_REPOSITORIES": projects_value,
                                           "POLL_INTERVAL_SECONDS": str(int(listen_interval) * 60)})
            os.environ["POLL_REPOSITORIES"] = projects_value
            os.environ["POLL_INTERVAL_SECONDS"] = str(int(listen_interval) * 60)
            with st.spinner("Démarrage de l'écoute…"):
                ok, message = start_poller(projects_value, int(listen_interval) * 60, listen_catch_up)
            if ok:
                st.toast(message, icon=":material/sensors:")
                st.rerun()
            else:
                st.error(message)


# ---------------------------------------------------------
# ONGLET CONNAISSANCES PARTAGÉES : RECHERCHE, APERÇU, STATUT, IMPORT
# ---------------------------------------------------------
KNOWLEDGE_ALL_TYPES = "Tous les types"


def open_document(path: str) -> None:
    """Ouvre un document avec l'application associée du poste (l'IHM tourne sur le poste de l'utilisateur)."""
    if os.name == "nt":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


def render_knowledge_import() -> None:
    """Import ponctuel de .md existants (A_REVOIR.md, AUDIT.md, COUVERTURE.md...) : projet, type et tickets choisis."""
    with st.expander("Importer des analyses Markdown existantes", icon=":material/upload_file:"):
        with st.form("knowledge_import", clear_on_submit=True):
            uploaded = st.file_uploader("Fichiers .md", type=["md"], accept_multiple_files=True)
            with st.container(horizontal=True):
                import_project = st.text_input("Projet Jira", placeholder="PROJ")
                import_client = st.text_input("Client (dossier, facultatif)",
                                              help="Dossier du document : le client s'il est renseigné, sinon le "
                                                   "projet Jira.")
                import_type = st.selectbox("Type", knowledge_tools.KNOWLEDGE_TYPES,
                                           index=knowledge_tools.KNOWLEDGE_TYPES.index("autre"))
            import_tickets = st.text_input("Tickets", placeholder="PROJ-1, PROJ-2")
            import_subject = st.text_input("Sujet", placeholder="Vide = nom du fichier")
            import_clicked = st.form_submit_button("Importer", icon=":material/upload:")
        if not import_clicked:
            return
        if not uploaded or not import_project.strip():
            st.error("Choisissez au moins un fichier et renseignez le projet Jira.", icon=":material/error:")
            return
        for file in uploaded:
            result = knowledge_tools.import_text(
                file.name, file.getvalue().decode("utf-8-sig", errors="replace"), import_project.strip().upper(),
                import_type, [t for t in import_tickets.split(",")], import_client.strip(), import_subject.strip())
            ok = result["statut"] in (knowledge_tools.PUBLISHED, knowledge_tools.QUEUED)
            (st.success if ok else st.error)(f"{file.name} : {result['message']}",
                                             icon=":material/check_circle:" if ok else ":material/error:")
        st.session_state.pop("knowledge_index", None)


def render_knowledge_status(doc: dict, meta: dict, documents: list) -> None:
    """Action « marquer obsolète / remplacé par » : en-tête seul modifié, par écriture atomique."""
    folder = doc["chemin"].rsplit("/", 1)[0]
    # Documents du même projet et du même type d'abord
    others = sorted((d["chemin"] for d in documents if d["chemin"] != doc["chemin"]),
                    key=lambda p: (p.rsplit("/", 1)[0] != folder, p))
    current = meta.get("remplace_par", "")
    with st.form(f"knowledge_status_{doc['chemin']}"):
        new_status = st.segmented_control("Statut", knowledge_tools.STATUSES,
                                          default=meta.get("statut", knowledge_tools.STATUS_ACTIVE), required=True)
        replacement = st.selectbox("Remplacé par (statut « remplacé »)", [""] + others,
                                   index=([""] + others).index(current) if current in others else 0)
        save_clicked = st.form_submit_button("Enregistrer le statut", icon=":material/save:")
    if save_clicked:
        result = knowledge_tools.set_status(doc["chemin"], new_status or knowledge_tools.STATUS_ACTIVE, replacement)
        if result["ok"]:
            st.session_state["knowledge_index"] = knowledge_tools.load_index()
            st.toast(result["message"], icon=":material/check_circle:")
            st.rerun()
        st.error(result["message"], icon=":material/error:")


def render_knowledge_document(doc: dict, documents: list) -> None:
    """Aperçu d'un document : en-tête, résumé, corps ; ouverture, téléchargement et changement de statut."""
    try:
        path = knowledge_tools.absolute_path(doc["chemin"])
        meta, body = knowledge_tools.read_document(path)
    except (ValueError, OSError, UnicodeDecodeError) as e:
        st.error(f"Document illisible : {e}", icon=":material/error:")
        return
    with st.container(border=True):
        st.markdown(f"**{meta.get('sujet') or doc['chemin']}**")
        status = meta.get("statut", knowledge_tools.STATUS_ACTIVE)
        if meta.get("remplace_par"):
            status += f" par {meta['remplace_par']}"
        st.caption(f"`{doc['chemin']}` · {meta.get('type', '?')} · {str(meta.get('date', '?')).replace('T', ' ')} · "
                   f"{meta.get('auteur', '?')} · statut : {status} · tickets : "
                   f"{', '.join(meta.get('tickets') or []) or 'aucun'} · agent {meta.get('version_agent', '?')}")
        if meta.get("resume"):
            st.markdown(meta["resume"])
        with st.container(horizontal=True):
            if st.button("Ouvrir", icon=":material/open_in_new:", key="knowledge_open"):
                try:
                    open_document(path)
                except OSError as e:
                    st.error(f"Ouverture impossible : {e}", icon=":material/error:")
            st.download_button("Télécharger", data=knowledge_tools.render_document(meta, body),
                               file_name=os.path.basename(path), mime="text/markdown", icon=":material/download:",
                               key="knowledge_download")
        with st.container(height=420):
            st.markdown(body)
    render_knowledge_status(doc, meta, documents)


def render_knowledge_tab() -> None:
    """Onglet 📚 : état du répertoire partagé, recherche, aperçu, statut et import."""
    st.subheader("Connaissances partagées")
    st.caption("Analyses déposées par l'agent dans le répertoire partagé (AGENT_KNOWLEDGE_DIR), consultées avant "
               "chaque nouvelle analyse comme des données à vérifier. Tout utilisateur du partage lit tous les "
               "documents.")
    if not knowledge_tools.enabled():
        st.info("AGENT_KNOWLEDGE_DIR est vide : renseignez le répertoire partagé dans l'onglet ⚙️ Configuration.",
                icon=":material/info:")
        return
    state_box = st.container()
    render_knowledge_outbox()
    render_knowledge_import()

    with st.form("knowledge_search"):
        with st.container(horizontal=True):
            search_project = st.text_input("Projet ou client", key="knowledge_project")
            search_type = st.selectbox("Type", [KNOWLEDGE_ALL_TYPES, *knowledge_tools.KNOWLEDGE_TYPES],
                                       key="knowledge_type")
            search_ticket = st.text_input("Ticket", key="knowledge_ticket")
            search_keyword = st.text_input("Mot-clé", key="knowledge_keyword")
        include_obsolete = st.checkbox("Inclure les documents obsolètes et remplacés", key="knowledge_obsolete")
        search_clicked = st.form_submit_button("Rechercher", icon=":material/search:")
    # Le partage n'est lu qu'à la demande : tous les onglets s'exécutent à chaque interaction
    if search_clicked:
        with st.spinner("Lecture des en-têtes du répertoire partagé…"):
            st.session_state["knowledge_index"] = knowledge_tools.load_index()
    index = st.session_state.get("knowledge_index")

    with state_box, st.container(horizontal=True):
        st.metric("Joignable", "non vérifié" if index is None else ("oui" if index["joignable"] else
                                                                    "non (dernier index connu)"), border=True)
        st.metric("Documents indexés", "—" if index is None else len(index["documents"]), border=True)
        st.metric("En attente de dépôt", knowledge_tools.outbox_count(), border=True)
    state_box.caption(f"Répertoire : {knowledge_tools.knowledge_dir()}")

    if index is None:
        st.caption("Lancez une recherche (critères vides : tous les documents actifs).")
        return
    documents = knowledge_tools.search_documents(
        index["documents"], search_project, "" if search_type == KNOWLEDGE_ALL_TYPES else search_type,
        search_ticket, search_keyword, include_obsolete)
    if not documents:
        st.info("Aucun document ne correspond à ces critères.", icon=":material/search_off:")
        return
    rows = [{"Date": str(d["meta"].get("date", "")).replace("T", " "), "Projet": d["chemin"].split("/", 1)[0],
             "Type": d["meta"].get("type", ""), "Sujet": d["meta"].get("sujet", ""),
             "Tickets": ", ".join(d["meta"].get("tickets") or []), "Auteur": d["meta"].get("auteur", ""),
             "Statut": d["meta"].get("statut", "")} for d in documents]
    st.caption(f"{len(documents)} document(s). Sélectionnez une ligne pour l'aperçu.")
    table = st.dataframe(rows, hide_index=True, on_select="rerun", selection_mode="single-row",
                         key="knowledge_table")
    selected = table.selection.rows
    if selected and selected[0] < len(documents):
        render_knowledge_document(documents[selected[0]], index["documents"])


with tab_knowledge:
    render_knowledge_tab()


with tab_config:
    st.subheader("🛠️ Configuration globale du fichier .env")
    st.caption("Modifiez l'ensemble des variables d'environnement utilisées par l'application. "
               "Le fichier .env.example du projet donne les valeurs usuelles.")

    with st.form("env_form_global"):
        # Valeurs enregistrées dans le .env à la validation du formulaire
        env_values = {}

        st.markdown("#### 📌 Identifiants Jira")
        env_values["JIRA_DOMAIN"] = st.text_input("JIRA_DOMAIN", value=os.getenv("JIRA_DOMAIN", ""),
                                                  help="Ex: monentreprise.atlassian.net")
        env_values["JIRA_EMAIL"] = st.text_input("JIRA_EMAIL", value=os.getenv("JIRA_EMAIL", ""))
        env_values["JIRA_API_TOKEN"] = st.text_input("JIRA_API_TOKEN", value=os.getenv("JIRA_API_TOKEN", ""), type="password",
                                                     help="À créer sur https://id.atlassian.com/manage-profile/security/api-tokens")
        env_values["JIRA_PROJECT_KEY"] = st.text_input("JIRA_PROJECT_KEY (Par défaut)", value=os.getenv("JIRA_PROJECT_KEY", ""))

        st.markdown("---")
        st.markdown("#### 🔎 Recherche Jira (valeurs par défaut de l'onglet Lancement)")
        with st.container(horizontal=True):
            env_values["JIRA_SEARCH_TEAM"] = st.text_input(
                "JIRA_SEARCH_TEAM", value=os.getenv("JIRA_SEARCH_TEAM", ""),
                help="Équipe / composant pré-rempli dans la recherche (composant Jira ou équipe Atlassian).")
            env_values["JIRA_SEARCH_STATUSES"] = st.text_input(
                "JIRA_SEARCH_STATUSES", value=os.getenv("JIRA_SEARCH_STATUSES", ""),
                help="Statuts pré-remplis, séparés par une virgule. Ex: Analysée, Analysé")
            env_values["JIRA_SEARCH_LABELS"] = st.text_input(
                "JIRA_SEARCH_LABELS", value=os.getenv("JIRA_SEARCH_LABELS", ""),
                help="Labels pré-remplis, séparés par une virgule")
        with st.container(horizontal=True):
            env_values["JIRA_OPEN_EPICS_TEAM"] = st.text_input(
                "JIRA_OPEN_EPICS_TEAM", value=os.getenv("JIRA_OPEN_EPICS_TEAM", ""),
                help="Équipe Atlassian de la recherche rapide « Épopées ouvertes de mon équipe ». Ex: oda_Prenom_N")
            env_values["JIRA_OPEN_EPICS_STATUS"] = st.text_input(
                "JIRA_OPEN_EPICS_STATUS", value=os.getenv("JIRA_OPEN_EPICS_STATUS", ""),
                help="Statut des épopées de cette recherche rapide. Ex: Ouvert")
        with st.container(horizontal=True):
            env_values["JIRA_APPLICATION_FIELD"] = st.text_input(
                "JIRA_APPLICATION_FIELD", value=os.getenv("JIRA_APPLICATION_FIELD", ""),
                help="Champ Jira « Projet / Application » (ex. customfield_10268). Onglet Intégration : l'agent "
                     "consulte tous les tickets ayant la même valeur que le ticket (ex. APPLI). Vide = désactivé.")
            application_max = st.number_input(
                "JIRA_APPLICATION_MAX_TICKETS", min_value=0, step=50,
                value=env_int("JIRA_APPLICATION_MAX_TICKETS", 300),
                help="Nombre maximal de tickets de référence (les plus récemment mis à jour).")
        env_values["JIRA_APPLICATION_MAX_TICKETS"] = str(int(application_max))

        st.markdown("---")
        st.markdown("#### 🎫 Tickets Jira créés par l'agent (tâches d'épopée, post-merge Cucumber)")
        with st.container(horizontal=True):
            env_values["POSTMERGE_JIRA_PROJECT"] = st.text_input(
                "POSTMERGE_JIRA_PROJECT", value=os.getenv("POSTMERGE_JIRA_PROJECT", ""),
                help="Projet des tickets [CUCUMBER] post-merge. Vide = JIRA_PROJECT_KEY.")
            env_values["POSTMERGE_JIRA_COMPONENT"] = st.text_input(
                "POSTMERGE_JIRA_COMPONENT", value=os.getenv("POSTMERGE_JIRA_COMPONENT", ""),
                help="Composant Jira ou équipe Atlassian posé sur les tickets créés (post-merge et tâches d'épopée). "
                     "Vide = aucun.")
            env_values["POSTMERGE_JIRA_LABEL"] = st.text_input(
                "POSTMERGE_JIRA_LABEL", value=os.getenv("POSTMERGE_JIRA_LABEL", ""),
                help="Label des tickets post-merge. Vide = aucun.")
        with st.container(horizontal=True):
            env_values["JIRA_POSTMERGE_ISSUE_TYPE"] = st.text_input(
                "JIRA_POSTMERGE_ISSUE_TYPE", value=os.getenv("JIRA_POSTMERGE_ISSUE_TYPE", "Development"),
                help="Type des tickets post-merge.")
            env_values["JIRA_EPIC_TASK_ISSUE_TYPE"] = st.text_input(
                "JIRA_EPIC_TASK_ISSUE_TYPE", value=os.getenv("JIRA_EPIC_TASK_ISSUE_TYPE", ""),
                help="Type des tâches créées sous une épopée. Vide = premier type existant parmi Tâche, Task, "
                     "Development, Story.")
            env_values["JIRA_TEAM_FIELD"] = st.text_input(
                "JIRA_TEAM_FIELD", value=os.getenv("JIRA_TEAM_FIELD", "customfield_10001"),
                help="Champ Jira de l'équipe Atlassian (Team).")

        st.markdown("---")
        st.markdown("#### 🐙 Connexion Bitbucket / KISCloud")
        env_values["BITBUCKET_URL"] = st.text_input("BITBUCKET_URL", value=os.getenv("BITBUCKET_URL", ""),
                                                    help="Ex: https://git.kiscloud.io")
        env_values["BITBUCKET_HTTP_TOKEN"] = st.text_input("BITBUCKET_HTTP_TOKEN", value=os.getenv("BITBUCKET_HTTP_TOKEN", ""),
                                                           type="password",
                                                           help="Jeton d'accès HTTP personnel Bitbucket (lecture des dépôts).")
        env_values["BITBUCKET_WEBHOOK_SECRET"] = st.text_input(
            "BITBUCKET_WEBHOOK_SECRET",
            value=os.getenv("BITBUCKET_WEBHOOK_SECRET", ""),
            type="password",
            help="Secret partagé avec le webhook Bitbucket (champ Secret) pour signer les événements post-merge. "
                 "Générer : python -c \"import secrets; print(secrets.token_hex(32))\". "
                 "Redémarrer webhook_server.py après modification."
        )

        st.markdown("---")
        st.markdown("#### 🏗️ Connexion Jenkins (Cucumber / Tirs de tests)")
        env_values["JENKINS_URL"] = st.text_input("JENKINS_URL", value=os.getenv("JENKINS_URL", ""),
                                                  help="Ex: https://jenkins.kiscloud.io")
        env_values["JENKINS_USER"] = st.text_input("JENKINS_USER", value=os.getenv("JENKINS_USER", ""))
        env_values["JENKINS_API_TOKEN"] = st.text_input("JENKINS_API_TOKEN", value=os.getenv("JENKINS_API_TOKEN", ""),
                                                        type="password")
        with st.container(horizontal=True):
            env_values["MAVEN_REPOSITORY_URL"] = st.text_input(
                "MAVEN_REPOSITORY_URL", value=os.getenv("MAVEN_REPOSITORY_URL", ""),
                help="Dépôt Maven où est publié l'artefact monté par le job (maven-metadata.xml) : sa date de "
                     "publication, comparée aux commits des tâches, dit quel code a été testé.")
            env_values["MAVEN_REPOSITORY_USER"] = st.text_input(
                "MAVEN_REPOSITORY_USER", value=os.getenv("MAVEN_REPOSITORY_USER", ""))
            env_values["MAVEN_REPOSITORY_PASSWORD"] = st.text_input(
                "MAVEN_REPOSITORY_PASSWORD", value=os.getenv("MAVEN_REPOSITORY_PASSWORD", ""), type="password")
        env_values["JENKINS_CUCUMBER_JOB"] = st.text_input("JENKINS_CUCUMBER_JOB", value=os.getenv("JENKINS_CUCUMBER_JOB", ""),
                                                           help="Job pré-rempli après génération d'un patch. "
                                                                "Ex: ODATIO_CUCUMBER_NIGHTLY")

        st.markdown("---")
        st.markdown("#### 📁 Dépôts, Conventions & Prompts")
        env_values["BITBUCKET_STD_REPO_PATH"] = st.text_input(
            "BITBUCKET_STD_REPO_PATH (Lien ou chemin du Socle Standard)",
            value=os.getenv("BITBUCKET_STD_REPO_PATH", ""),
            help="URL du projet Bitbucket/KISCloud (ex: https://git.kiscloud.io/projects/COMPONENT) "
                 "ou chemin local d'un clone (ex: C:\\development\\branch_dev)."
        )
        env_values["BITBUCKET_CLIENT_REPO_PATH"] = st.text_input(
            "BITBUCKET_CLIENT_REPO_PATH (Dépôt client par défaut)",
            value=os.getenv("BITBUCKET_CLIENT_REPO_PATH", ""),
            help="Pré-remplit le dépôt client de l'onglet Lancement. "
                 "Ex: https://git.kiscloud.io/projects/CLIENT ou C:\\development\\CLIENT"
        )
        env_values["BITBUCKET_CUCUMBER_STD_REPO"] = st.text_input(
            "BITBUCKET_CUCUMBER_STD_REPO",
            value=os.getenv("BITBUCKET_CUCUMBER_STD_REPO", ""),
            help="URL du dépôt Bitbucket contenant les scénarios Cucumber du socle Standard."
        )
        env_values["DEV_CONVENTIONS_PATH"] = st.text_input(
            "DEV_CONVENTIONS_PATH",
            value=os.getenv("DEV_CONVENTIONS_PATH", ""),
            help="Fichier Markdown des conventions de développement. "
                 "Vide = odatio-specific-code-conventions-dev.md du projet."
        )
        env_values["PROMPTS_DIR"] = st.text_input(
            "PROMPTS_DIR",
            value=os.getenv("PROMPTS_DIR", ""),
            help="Dossier de prompts personnalisés : un fichier <nom>.md présent ici remplace le modèle du même nom "
                 "du dossier prompts/ du projet. Vide = modèles du projet."
        )

        st.markdown("---")
        st.markdown("#### 🥒 Tests Cucumber d'un client (onglet Cucumber)")
        with st.container(horizontal=True):
            cucumber_batch = st.number_input(
                "CUCUMBER_BATCH_SIZE", min_value=1, step=1, value=env_int("CUCUMBER_BATCH_SIZE", 3),
                help="Nombre de spécifiques par lot : un appel Claude neuf par lot.")
            cucumber_turns = st.number_input(
                "CUCUMBER_MAX_TURNS", min_value=5, step=5, value=env_int("CUCUMBER_MAX_TURNS", 40),
                help="Nombre maximal de tours de l'agent par lot (--max-turns). À 75 %, l'agent est invité à "
                     "finaliser ce qu'il a écrit et à rendre son JSON final.")
        env_values["CUCUMBER_BATCH_SIZE"] = str(int(cucumber_batch))
        env_values["CUCUMBER_MAX_TURNS"] = str(int(cucumber_turns))
        with st.container(horizontal=True):
            env_values["CUCUMBER_MAX_BUDGET_USD"] = st.text_input(
                "CUCUMBER_MAX_BUDGET_USD", value=os.getenv("CUCUMBER_MAX_BUDGET_USD", ""), placeholder="Vide = aucun",
                help="Budget maximal par appel Claude d'un lot, d'une correction ou d'une revue (--max-budget-usd). "
                     "Dépassement : lot en échec « budget atteint », fichiers annulés, repris par « Reprendre ».")
            inline_max = st.number_input(
                "CUCUMBER_INLINE_MAX_CHARS", min_value=0, step=2000,
                value=env_int("CUCUMBER_INLINE_MAX_CHARS", cucumber_tools.DEFAULT_INLINE_MAX_CHARS),
                help="Taille maximale (caractères) du contenu injecté dans le prompt d'un lot au lieu d'être lu par "
                     "l'agent : partie fixe (sommaire du catalogue, Step Definitions exemplaire, exemples des "
                     "recettes, runner à créer) et, séparément, extraits Standard et Jira du lot. Au-delà, le "
                     "chemin est donné pour lecture. 0 = rien d'injecté.")
        env_values["CUCUMBER_INLINE_MAX_CHARS"] = str(int(inline_max))
        env_values["CUCUMBER_INLINE_STUDIO_LINES"] = str(int(st.number_input(
            "CUCUMBER_INLINE_STUDIO_LINES", min_value=0, step=10,
            value=env_int("CUCUMBER_INLINE_STUDIO_LINES", cucumber_tools.DEFAULT_INLINE_STUDIO_LINES),
            help="Lignes au plus, par customized.xml, des parties modifiées (nœuds modifiés et leur contexte) "
                 "injectées dans le prompt du lot. Le reste du fichier est lu à la demande. 0 = rien d'injecté.")))
        env_values["CUCUMBER_INLINE_STEP_CODE_CHARS"] = str(int(st.number_input(
            "CUCUMBER_INLINE_STEP_CODE_CHARS", min_value=0, step=2000,
            value=env_int("CUCUMBER_INLINE_STEP_CODE_CHARS", cucumber_tools.DEFAULT_INLINE_STEP_CODE_CHARS),
            help="Taille maximale (caractères) du code des steps Standard candidats injecté dans le prompt d'un lot "
                 "(signature et corps : colonnes des tables, clés de contexte, helpers appelés), 8 steps au plus "
                 "par spécifique. 0 = rien d'injecté (l'agent lit le code dans la copie des sources du Standard).")))
        lean = st.checkbox(
            "CLAUDE_LEAN_SESSION", value=os.getenv("CLAUDE_LEAN_SESSION", "1").strip() != "0",
            help="Sessions Cucumber allégées : seules les définitions des outils autorisés sont envoyées (--tools), "
                 "sans serveur MCP (--strict-mcp-config), commandes ni skills (--disable-slash-commands), sections "
                 "propres au poste hors du prompt système. Décoché = comportement antérieur.")
        env_values["CLAUDE_LEAN_SESSION"] = "1" if lean else "0"
        env_values["CUCUMBER_EXCLUDE_GLOBS"] = st.text_input(
            "CUCUMBER_EXCLUDE_GLOBS", value=os.getenv("CUCUMBER_EXCLUDE_GLOBS", ""),
            placeholder="Vide = exclusions par défaut (pom.xml, tests unitaires, ressources statiques…)",
            help="Motifs (séparés par des virgules) des fichiers techniques exclus de l'inventaire des spécifiques. "
                 "Une valeur remplace entièrement la liste par défaut : tools/cucumber_tools.py, DEFAULT_EXCLUDE_GLOBS.")
        env_values["CUCUMBER_EXCLUDE_REPOS"] = st.text_input(
            "CUCUMBER_EXCLUDE_REPOS", value=os.getenv("CUCUMBER_EXCLUDE_REPOS", ""),
            placeholder="Vide = *-frontend, *-front, *-ui",
            help="Motifs des dépôts clients exclus de l'inventaire : les dépôts front (écrans) ne sont pas testables "
                 "avec les steps du Cucumber Standard.")
        env_values["CUCUMBER_PARAM_GLOBS"] = st.text_input(
            "CUCUMBER_PARAM_GLOBS", value=os.getenv("CUCUMBER_PARAM_GLOBS", ""),
            placeholder="Vide = *.properties, *i18n*, *messages*, *.sql, *param*.xml, *parameter*, *.csv",
            help="Motifs des fichiers classés « parametrage » (paramètres et messages).")
        try:
            match_min = float(os.getenv("CUCUMBER_STD_MATCH_MIN", "").strip() or cucumber_tools.DEFAULT_STD_MATCH_MIN)
        except ValueError:
            match_min = cucumber_tools.DEFAULT_STD_MATCH_MIN
        env_values["CUCUMBER_STD_MATCH_MIN"] = str(st.number_input(
            "CUCUMBER_STD_MATCH_MIN", min_value=0.0, max_value=1.0, step=0.05, value=match_min,
            help="Score minimal d'une feature du Cucumber Standard pour être rapprochée d'un spécifique (au plus 3, "
                 "dont l'agent part pour écrire ses tests). Plus haut = moins de rapprochements, plus sûrs. Pris en "
                 "compte au prochain inventaire."))
        try:
            dup_threshold = float(os.getenv("CUCUMBER_DUP_THRESHOLD", "").strip()
                                  or cucumber_verify.DEFAULT_DUP_THRESHOLD)
        except ValueError:
            dup_threshold = cucumber_verify.DEFAULT_DUP_THRESHOLD
        with st.container(horizontal=True):
            dup_value = st.number_input(
                "CUCUMBER_DUP_THRESHOLD", min_value=0.5, max_value=1.0, step=0.05, value=dup_threshold,
                help="Similarité (titre ou steps) au-delà de laquelle un scénario généré est signalé comme doublon "
                     "d'un scénario Standard ou client, puis envoyé à la revue.")
            review_turns = st.number_input(
                "CUCUMBER_REVIEW_MAX_TURNS", min_value=3, step=1, value=env_int("CUCUMBER_REVIEW_MAX_TURNS", 8),
                help="Nombre maximal de tours de l'appel de revue des scénarios signalés d'un lot (--max-turns).")
        env_values["CUCUMBER_DUP_THRESHOLD"] = str(dup_value)
        env_values["CUCUMBER_REVIEW_MAX_TURNS"] = str(int(review_turns))
        env_values["CUCUMBER_COMPILE_CMD"] = st.text_input(
            "CUCUMBER_COMPILE_CMD", value=os.getenv("CUCUMBER_COMPILE_CMD", cucumber_tools.DEFAULT_COMPILE_CMD),
            placeholder="Vide = pas de compilation",
            help="Commande de compilation lancée après chaque lot (en Python, sans token). Dépôt client sans pom.xml : "
                 "compilé par-dessus une copie du Cucumber Standard. Hors ligne (-o) en échec faute de dépendances : "
                 "relancée une fois en ligne.")
        with st.container(horizontal=True):
            fix_attempts = st.number_input(
                "CUCUMBER_FIX_ATTEMPTS", min_value=0, step=1, value=env_int("CUCUMBER_FIX_ATTEMPTS", 2),
                help="Appels de correction au plus par lot si la compilation échoue (0 = aucun).")
            fix_turns = st.number_input(
                "CUCUMBER_FIX_MAX_TURNS", min_value=3, step=1, value=env_int("CUCUMBER_FIX_MAX_TURNS", 10),
                help="Nombre maximal de tours de l'agent par appel de correction (--max-turns).")
        env_values["CUCUMBER_FIX_ATTEMPTS"] = str(int(fix_attempts))
        env_values["CUCUMBER_FIX_MAX_TURNS"] = str(int(fix_turns))
        dry_run = st.checkbox(
            "CUCUMBER_DRY_RUN", value=os.getenv("CUCUMBER_DRY_RUN", "1").strip() != "0",
            help="Après une compilation réussie, dry-run Cucumber des features du lot : vérifie que chaque step a "
                 "une définition (mvn test -Dcucumber.execution.dry-run=true), sans rien exécuter.")
        env_values["CUCUMBER_DRY_RUN"] = "1" if dry_run else "0"
        agent_compile = st.checkbox(
            "CUCUMBER_AGENT_COMPILE", value=os.getenv("CUCUMBER_AGENT_COMPILE", "1").strip() != "0",
            help="L'agent d'un lot (et de la revue / des corrections) compile lui-même le worktree pendant qu'il "
                 "écrit (CUCUMBER_COMPILE_CMD, test-compile : rien n'est exécuté) et corrige ses erreurs avec tout le "
                 "contexte du lot. Décoché = compilation après le lot seulement, corrigée par CLAUDE_MODEL_CUCUMBER_FIX.")
        env_values["CUCUMBER_AGENT_COMPILE"] = "1" if agent_compile else "0"
        agent_jenkins = st.checkbox(
            "CUCUMBER_AGENT_JENKINS", value=os.getenv("CUCUMBER_AGENT_JENKINS", "0").strip() == "1",
            help="Décoché (défaut) : l'agent livre un patch, ne pousse rien et ne lance aucun test ; vous appliquez le "
                 "patch, lancez votre job complet et importez son résultat. Coché : l'agent vérifie en autonomie "
                 "pendant la génération — il pousse ses tests sur sa branche de travail (profil client), lance le "
                 "job du client (profil ou CUCUMBER_VERIFY_JOB) avec les paramètres imposés du profil, un build à la "
                 "fois, corrige et revérifie, puis livre un patch vérifié à appliquer sur votre branche.")
        env_values["CUCUMBER_AGENT_JENKINS"] = "1" if agent_jenkins else "0"
        st.markdown("##### Vérification de la pertinence par exécution")
        st.caption("Sans job ni commande locale, la génération s'arrête au contrôle statique et affiche « pertinence "
                   "non vérifiée par exécution ».")
        env_values["CUCUMBER_VERIFY_JOB"] = st.text_input(
            "CUCUMBER_VERIFY_JOB", value=os.getenv("CUCUMBER_VERIFY_JOB", ""),
            placeholder="Vide = pas de job (ex. CUCUMBER-VERIFY-PATCH)",
            help="Job Jenkins de vérification. Avec un paramètre fichier PATCH (.patch du lot), TAGS et ENV : aucun "
                 "push. Sans PATCH : le job lit la branche de travail du profil client, où l'agent pousse ses tests "
                 "(jamais la branche d'intégration) ; les rôles de ses paramètres (branche, dépôt, arguments, "
                 "attente) sont détectés et corrigeables dans le profil client.")
        with st.container(horizontal=True):
            env_values["CUCUMBER_VERIFY_ENV"] = st.text_input(
                "CUCUMBER_VERIFY_ENV", value=os.getenv("CUCUMBER_VERIFY_ENV", ""),
                help="Environnement client (paramètre ENV du job, {env} de la commande locale).")
            env_values["CUCUMBER_VERIFY_ENV_STD"] = st.text_input(
                "CUCUMBER_VERIFY_ENV_STD", value=os.getenv("CUCUMBER_VERIFY_ENV_STD", ""),
                help="Environnement Standard de même version, pour la contre-épreuve des overrides (vide = pas de "
                     "contre-épreuve Standard).")
        env_values["CUCUMBER_INIT_TAGS"] = st.text_input(
            "CUCUMBER_INIT_TAGS", value=os.getenv("CUCUMBER_INIT_TAGS", ""), placeholder="Ex. @INIT_CLIENT_WMS",
            help="Initialisation du client, exécutée une fois par génération avant de juger les lots. En échec : "
                 "la vérification s'arrête, les scénarios sont « non vérifiés — environnement non initialisé ». "
                 "Désigne aussi les features d'init dont les codes créés sont imposés aux lots (défaut : tag "
                 "@INIT_*, dossier 0x-Contexte). Vide = pas d'initialisation préalable.")
        env_values["CUCUMBER_CLEAR_NAME"] = st.text_input(
            "CUCUMBER_CLEAR_NAME", value=os.getenv("CUCUMBER_CLEAR_NAME", ""),
            help="Repli si le profil client n'a pas de clear : seul clear autorisé pour purger l'environnement, "
                 "propre à chaque environnement (aucune valeur par défaut). Le profil client (onglet Cucumber) prime.")
        env_values["CUCUMBER_DETECTION_INTERVAL"] = st.text_input(
            "CUCUMBER_DETECTION_INTERVAL", value=os.getenv("CUCUMBER_DETECTION_INTERVAL", ""), placeholder="300",
            help="Onglet Cucumber : délai en secondes entre deux contrôles des commits des dépôts du projet ; la "
                 "détection automatique (inventaire, profil, état de l'init) n'est refaite que s'ils ont changé.")
        env_values["CUCUMBER_RUN_JOB"] = st.text_input(
            "CUCUMBER_RUN_JOB", value=os.getenv("CUCUMBER_RUN_JOB", ""), placeholder="DOSSIER/JOB",
            help="Run Jenkins automatique (onglet Cucumber, mode expert, « Résultats ») : job qui exécute <client>-cucumber, si "
                 "le profil client n'en donne pas. Il doit lire la branche du profil et accepter TAGS.")
        env_values["CUCUMBER_VERIFY_LOCAL_CMD"] = st.text_input(
            "CUCUMBER_VERIFY_LOCAL_CMD", value=os.getenv("CUCUMBER_VERIFY_LOCAL_CMD", ""),
            placeholder='mvn -q test "-Dcucumber.filter.tags={tags}" -Denv={env}',
            help="Repli local (si pas de job) : commande lancée dans le dossier de compilation ; {tags} et {env} "
                 "sont remplacés ; le rapport target/**/cucumber*.json est lu.")
        with st.container(horizontal=True):
            verify_timeout = st.number_input(
                "CUCUMBER_VERIFY_TIMEOUT", min_value=60, step=60, value=env_int("CUCUMBER_VERIFY_TIMEOUT", 3600),
                help="Durée maximale d'une exécution (build Jenkins ou commande locale), en secondes.")
            verify_fixes = st.number_input(
                "CUCUMBER_VERIFY_FIX_MAX", min_value=0, step=1, value=env_int("CUCUMBER_VERIFY_FIX_MAX", 4),
                help="Corrections au plus par lot après exécution. L'agent continue tant que l'échec change et "
                     "s'arrête pour un scénario dès que son échec revient à l'identique. 0 : aucune correction.")
            review_sample = st.number_input(
                "CUCUMBER_REVIEW_SAMPLE", min_value=0, step=1, value=env_int("CUCUMBER_REVIEW_SAMPLE", 0),
                help="Scénarios proposés à la revue humaine de l'onglet Cucumber : d'abord les « non vérifié », "
                     "puis des « pertinent » tirés au hasard.")
        env_values["CUCUMBER_VERIFY_TIMEOUT"] = str(int(verify_timeout))
        env_values["CUCUMBER_VERIFY_FIX_MAX"] = str(int(verify_fixes))
        # Ancien réglage (plafond fixe) : remplacé par le plafond adaptatif, 0 conservé comme « aucune correction »
        env_values["CUCUMBER_VERIFY_FIX_ATTEMPTS"] = "0" if int(verify_fixes) == 0 else ""
        env_values["CUCUMBER_REVIEW_SAMPLE"] = str(int(review_sample))
        with st.container(horizontal=True):
            slow_seconds = st.number_input(
                "CUCUMBER_SLOW_SCENARIO_SECONDS", min_value=10, step=30,
                value=env_int("CUCUMBER_SLOW_SCENARIO_SECONDS", 300),
                help="Import d'un run : un scénario est anormalement long au-delà de cette durée (ou de 3 fois la "
                     "durée médiane du run).")
            export_cap = st.number_input(
                "CUCUMBER_LOG_EXPORT_MAX_LINES", min_value=0, step=1000,
                value=env_int("CUCUMBER_LOG_EXPORT_MAX_LINES", 0),
                help="Plafond de lignes de l'outil d'export des logs serveur (0 : inconnu) : rappelé dans les "
                     "fenêtres à exporter ; un export qui l'atteint est signalé.")
            max_cause = st.number_input(
                "CUCUMBER_MAX_CAUSE_ATTEMPTS", min_value=1, step=1, value=env_int("CUCUMBER_MAX_CAUSE_ATTEMPTS", 3),
                help="Corrections infructueuses sur une même cause (même message, valeurs variables masquées) avant "
                     "que l'agent arrête de deviner et propose une méthode de preuve.")
        env_values["CUCUMBER_MAX_CAUSE_ATTEMPTS"] = str(int(max_cause))
        env_values["CUCUMBER_SERVER_LOG_DIR"] = st.text_input(
            "CUCUMBER_SERVER_LOG_DIR", value=os.getenv("CUCUMBER_SERVER_LOG_DIR", ""),
            help="Répertoire où sont déposés les logs applicatifs du serveur de l'environnement de test (*.log, "
                 "rotations, .gz) : à chaque import d'un run, la fenêtre de chaque échec y est extraite (secrets "
                 "masqués) et donnée au correcteur, sans export ni réimport manuel. Vide : non utilisé.").strip()
        st.markdown("**Logs de l'environnement de test dans groundcover** (lecture seule)")
        with st.container(horizontal=True):
            env_values["GROUNDCOVER_API_KEY"] = st.text_input(
                "GROUNDCOVER_API_KEY", value=os.getenv("GROUNDCOVER_API_KEY", ""), type="password",
                help="Clé d'API groundcover (Settings → Access → API Keys), de préférence d'un compte en lecture "
                     "seule.").strip()
            env_values["GROUNDCOVER_BACKEND_ID"] = st.text_input(
                "GROUNDCOVER_BACKEND_ID", value=os.getenv("GROUNDCOVER_BACKEND_ID", ""),
                help="Identifiant du backend (comptes multi-backend seulement, même écran que la clé).").strip()
            env_values["GROUNDCOVER_API_URL"] = st.text_input(
                "GROUNDCOVER_API_URL", value=os.getenv("GROUNDCOVER_API_URL", ""),
                placeholder="https://api.groundcover.com").strip()
        env_values["CUCUMBER_GROUNDCOVER_FILTER"] = st.text_input(
            "CUCUMBER_GROUNDCOVER_FILTER", value=os.getenv("CUCUMBER_GROUNDCOVER_FILTER", ""),
            placeholder="namespace:apparel-cucumber",
            help="Filtre gcQL de l'environnement où Jenkins joue les tests (namespace, workload, cluster…), ajouté à "
                 "toute requête : obligatoire, sans lui les logs groundcover ne sont pas lus.").strip()
        env_values["CUCUMBER_SLOW_SCENARIO_SECONDS"] = str(int(slow_seconds))
        env_values["CUCUMBER_LOG_EXPORT_MAX_LINES"] = str(int(export_cap)) if export_cap else ""
        jacoco = st.checkbox(
            "CUCUMBER_JACOCO", value=os.getenv("CUCUMBER_JACOCO", "").strip() == "1",
            help="Environnement client lancé avec l'agent JaCoCo : part des classes de chaque spécifique exécutées "
                 "(jacoco.exec archivé par le job, ou CUCUMBER_JACOCO_EXEC en local).")
        env_values["CUCUMBER_JACOCO"] = "1" if jacoco else "0"
        keep_tags = st.checkbox(
            "CUCUMBER_KEEP_AGENT_TAGS", value=os.getenv("CUCUMBER_KEEP_AGENT_TAGS", "").strip() == "1",
            help="Conserve les tags de vérification @agent_<client>_<lot>[_<n>] dans le patch livré (par défaut, "
                 "ils sont retirés du patch et gardés seulement dans le worktree).")
        env_values["CUCUMBER_KEEP_AGENT_TAGS"] = "1" if keep_tags else "0"
        env_values["CUCUMBER_JACOCO_EXEC"] = st.text_input(
            "CUCUMBER_JACOCO_EXEC", value=os.getenv("CUCUMBER_JACOCO_EXEC", ""),
            help="Mode local : chemin du jacoco.exec produit par l'environnement client.")

        st.markdown("---")
        st.markdown("#### 🔔 Post-merge Cucumber & écoute des merges")
        with st.container(horizontal=True):
            env_values["POSTMERGE_CUCUMBER_REPOS"] = st.text_input(
                "POSTMERGE_CUCUMBER_REPOS", value=os.getenv("POSTMERGE_CUCUMBER_REPOS", ""),
                help="Dépôt Cucumber de chaque projet client. Ex: CLIENT=client-cucumber-2,AUTRE=autre-cucumber. "
                     "Vide = détection du dépôt *cucumber* du projet.")
            env_values["POSTMERGE_CUCUMBER_BASE_BRANCH"] = st.text_input(
                "POSTMERGE_CUCUMBER_BASE_BRANCH", value=os.getenv("POSTMERGE_CUCUMBER_BASE_BRANCH", "integration"),
                help="Branche de base des patchs Cucumber post-merge.")
        with st.container(horizontal=True):
            env_values["WEBHOOK_TARGET_BRANCHES"] = st.text_input(
                "WEBHOOK_TARGET_BRANCHES", value=os.getenv("WEBHOOK_TARGET_BRANCHES", "integration,dev"),
                help="Branches dont les merges déclenchent l'agent, séparées par une virgule.")
            env_values["WEBHOOK_IGNORED_REPOS"] = st.text_input(
                "WEBHOOK_IGNORED_REPOS", value=os.getenv("WEBHOOK_IGNORED_REPOS", "cucumber"),
                help="Motifs de dépôts ignorés, séparés par une virgule.")
            env_values["WEBHOOK_STD_PROJECT"] = st.text_input(
                "WEBHOOK_STD_PROJECT", value=os.getenv("WEBHOOK_STD_PROJECT", ""),
                help="Projet Bitbucket du socle Standard. Vide = déduit de BITBUCKET_STD_REPO_PATH.")
        with st.container(horizontal=True):
            env_values["WEBHOOK_HOST"] = st.text_input("WEBHOOK_HOST", value=os.getenv("WEBHOOK_HOST", "0.0.0.0"),
                                                       help="Mode webhook uniquement (python webhook_server.py).")
            env_values["WEBHOOK_PORT"] = st.text_input("WEBHOOK_PORT", value=os.getenv("WEBHOOK_PORT", "8085"),
                                                       help="Mode webhook uniquement (python webhook_server.py).")

        st.markdown("---")
        st.markdown("#### 🔑 Compte Claude")
        # Valeur du .env seule : celle héritée du shell n'est pas enregistrée par défaut
        saved_config_dir = (dotenv_values(ENV_PATH).get("CLAUDE_CONFIG_DIR") or "").strip()
        config_dir_options = [""] + claude_config_dirs()
        if saved_config_dir and saved_config_dir not in config_dir_options:
            config_dir_options.append(saved_config_dir)
        shell_config_dir = os.environ.get("AGENT_SHELL_CLAUDE_CONFIG_DIR", "")

        def config_dir_label(config_dir: str) -> str:
            label = config_dir or ("Défaut du poste" + (f" ({shell_config_dir})" if shell_config_dir else " (~/.claude)"))
            account = claude_account(config_dir or shell_config_dir)
            return f"{label} — {account}" if account else label

        env_values["CLAUDE_CONFIG_DIR"] = st.selectbox(
            "CLAUDE_CONFIG_DIR", config_dir_options, index=config_dir_options.index(saved_config_dir),
            format_func=config_dir_label, accept_new_options=True,
            help="Dossier de configuration de Claude Code du compte à utiliser (abonnement Pro / Max / Team). "
                 "Proposés : ~/.claude et ~/.claude-* du poste ; saisissez un autre chemin si besoin. "
                 "Pour connecter un nouveau compte : CLAUDE_CONFIG_DIR=<dossier> puis `claude auth login`.")
        env_values["ANTHROPIC_API_KEY"] = st.text_input(
            "ANTHROPIC_API_KEY (Laissez vide pour utiliser le compte Claude ci-dessus)",
            value=os.getenv("ANTHROPIC_API_KEY", ""), type="password",
            help="Renseignée, la clé API est utilisée à la place du compte Claude.")

        st.markdown("---")
        st.markdown("#### 💸 Optimisation des tokens")
        jira_context_max = st.number_input(
            "JIRA_CONTEXT_MAX_CHARS", min_value=0, max_value=200000, step=1000,
            value=env_int("JIRA_CONTEXT_MAX_CHARS", 12000),
            help="Taille maximale du contexte Jira (description, Épopée, COST) injecté dans le prompt de cadrage. "
                 "0 = pas d'injection : l'agent interroge Jira lui-même."
        )
        memory_max = st.number_input(
            "MEMORY_MAX_CHARS", min_value=0, max_value=100000, step=1000,
            value=env_int("MEMORY_MAX_CHARS", 6000),
            help="Taille maximale des retours d'expérience injectés dans les prompts. 0 = désactivé."
        )
        env_values["JIRA_CONTEXT_MAX_CHARS"] = str(int(jira_context_max))
        env_values["MEMORY_MAX_CHARS"] = str(int(memory_max))
        model_help = ("Alias (sonnet, opus, haiku) ou identifiant complet du modèle, transmis avec --model. "
                      "Vide = modèle par défaut de Claude Code.")
        with st.container(horizontal=True):
            env_values["CLAUDE_MODEL_CADRAGE"] = st.text_input("CLAUDE_MODEL_CADRAGE", value=os.getenv("CLAUDE_MODEL_CADRAGE", ""), help=model_help)
            env_values["CLAUDE_MODEL_REFINE"] = st.text_input("CLAUDE_MODEL_REFINE", value=os.getenv("CLAUDE_MODEL_REFINE", ""), help=model_help)
        with st.container(horizontal=True):
            env_values["CLAUDE_MODEL_PATCH"] = st.text_input("CLAUDE_MODEL_PATCH", value=os.getenv("CLAUDE_MODEL_PATCH", ""), help=model_help)
            env_values["CLAUDE_MODEL_POSTMERGE"] = st.text_input("CLAUDE_MODEL_POSTMERGE", value=os.getenv("CLAUDE_MODEL_POSTMERGE", ""), help=model_help)
        with st.container(horizontal=True):
            env_values["CLAUDE_MODEL_BESOIN"] = st.text_input("CLAUDE_MODEL_BESOIN", value=os.getenv("CLAUDE_MODEL_BESOIN", ""),
                                                              help=model_help + " Utilisé par la rédaction du besoin (onglet Intégration).")
            env_values["CLAUDE_MODEL_CUCUMBER"] = st.text_input("CLAUDE_MODEL_CUCUMBER", value=os.getenv("CLAUDE_MODEL_CUCUMBER", ""),
                                                                help=model_help.replace("Vide = modèle par défaut de Claude Code.", "Vide = sonnet.")
                                                                + " Utilisé par chaque lot de la génération des tests Cucumber d'un client (onglet Cucumber).")
        env_values["CLAUDE_MODEL_CUCUMBER_FIX"] = st.text_input("CLAUDE_MODEL_CUCUMBER_FIX", value=os.getenv("CLAUDE_MODEL_CUCUMBER_FIX", ""),
                                                                placeholder="Vide = haiku",
                                                                help=model_help.replace("Vide = modèle par défaut de Claude Code.", "Vide = haiku.")
                                                                + " Utilisé par les corrections de compilation et de dry-run des lots Cucumber ; si elles échouent, "
                                                                  "une dernière tentative est faite avec CLAUDE_MODEL_CUCUMBER.")
        effort_help = ("Niveau d'effort transmis avec --effort : low, medium, high, xhigh ou max. "
                       "Vide = réglage de Claude Code sur le poste.")
        with st.container(horizontal=True):
            env_values["CLAUDE_EFFORT_CADRAGE"] = st.text_input("CLAUDE_EFFORT_CADRAGE", value=os.getenv("CLAUDE_EFFORT_CADRAGE", ""), help=effort_help)
            env_values["CLAUDE_EFFORT_REFINE"] = st.text_input("CLAUDE_EFFORT_REFINE", value=os.getenv("CLAUDE_EFFORT_REFINE", ""), help=effort_help)
        with st.container(horizontal=True):
            env_values["CLAUDE_EFFORT_PATCH"] = st.text_input("CLAUDE_EFFORT_PATCH", value=os.getenv("CLAUDE_EFFORT_PATCH", ""), help=effort_help)
            env_values["CLAUDE_EFFORT_POSTMERGE"] = st.text_input("CLAUDE_EFFORT_POSTMERGE", value=os.getenv("CLAUDE_EFFORT_POSTMERGE", ""), help=effort_help)
        with st.container(horizontal=True):
            env_values["CLAUDE_EFFORT_BESOIN"] = st.text_input("CLAUDE_EFFORT_BESOIN", value=os.getenv("CLAUDE_EFFORT_BESOIN", ""),
                                                               help=effort_help + " Utilisé par la rédaction du besoin (onglet Intégration).")
            env_values["CLAUDE_EFFORT_CUCUMBER"] = st.text_input("CLAUDE_EFFORT_CUCUMBER", value=os.getenv("CLAUDE_EFFORT_CUCUMBER", "medium"),
                                                                 help=effort_help + " Utilisé par chaque lot de la génération des tests Cucumber d'un client.")
        env_values["CLAUDE_EFFORT_CUCUMBER_FIX"] = st.text_input("CLAUDE_EFFORT_CUCUMBER_FIX", value=os.getenv("CLAUDE_EFFORT_CUCUMBER_FIX", "low"),
                                                                 help=effort_help.replace("Vide = réglage de Claude Code sur le poste.", "Vide = low.")
                                                                 + " Utilisé par les appels de correction et de revue des lots Cucumber.")
        st.caption("La consommation de chaque exécution est affichée (ligne [TOKENS]) et ajoutée à .tmp/token_usage.csv, "
                   "avec le modèle et l'effort utilisés.")

        st.markdown("---")
        st.markdown("#### 🗄️ Connexion Base de données (lecture seule)")
        current_db_type = os.getenv("DB_TYPE", "postgresql")
        env_values["DB_TYPE"] = st.selectbox("DB_TYPE", DB_TYPES,
                                             index=DB_TYPES.index(current_db_type) if current_db_type in DB_TYPES else 0)
        with st.container(horizontal=True):
            env_values["DB_HOST"] = st.text_input("DB_HOST", value=os.getenv("DB_HOST", ""))
            env_values["DB_PORT"] = st.text_input("DB_PORT", value=os.getenv("DB_PORT", ""), help="Vide = port par défaut du SGBD.")
        env_values["DB_NAME"] = st.text_input("DB_NAME (base, ou service name Oracle)", value=os.getenv("DB_NAME", ""))
        with st.container(horizontal=True):
            env_values["DB_USER"] = st.text_input("DB_USER", value=os.getenv("DB_USER", ""))
            env_values["DB_PASSWORD"] = st.text_input("DB_PASSWORD", value=os.getenv("DB_PASSWORD", ""), type="password")
        env_values["DB_SCHEMA"] = st.text_input(
            "DB_SCHEMA (valeur par défaut)", value=os.getenv("DB_SCHEMA", ""),
            help="Pré-remplit le champ « Schéma BDD cible » de l'onglet Lancement."
        )
        env_values["DB_ODBC_DRIVER"] = st.text_input(
            "DB_ODBC_DRIVER", value=os.getenv("DB_ODBC_DRIVER", "ODBC Driver 18 for SQL Server"),
            help="SQL Server via pyodbc uniquement : nom du pilote ODBC installé sur le poste."
        )
        st.caption("Utilisez de préférence un compte base en lecture seule : le mot de passe reste dans le .env "
                   "et n'est jamais transmis à l'agent.")

        st.markdown("#### 🥒 Base de l'environnement de test Cucumber (lecture seule)")
        st.caption("Base de l'environnement où Jenkins joue les tests (CUCUMBER_VERIFY_ENV) : l'agent des lots Cucumber "
                   "y prend ses données de référence réelles. Vide = base ci-dessus (DB_*). Ne mettez pas ces "
                   "informations dans CUCUMBER_VERIFY_ENV, transmis tel quel à Jenkins.")
        current_cuc_type = os.getenv("CUCUMBER_DB_TYPE", "") or current_db_type
        env_values["CUCUMBER_DB_TYPE"] = st.selectbox(
            "CUCUMBER_DB_TYPE", DB_TYPES, index=DB_TYPES.index(current_cuc_type) if current_cuc_type in DB_TYPES else 0)
        with st.container(horizontal=True):
            env_values["CUCUMBER_DB_HOST"] = st.text_input("CUCUMBER_DB_HOST", value=os.getenv("CUCUMBER_DB_HOST", ""))
            env_values["CUCUMBER_DB_PORT"] = st.text_input("CUCUMBER_DB_PORT", value=os.getenv("CUCUMBER_DB_PORT", ""),
                                                           help="Vide = port par défaut du SGBD.")
        env_values["CUCUMBER_DB_NAME"] = st.text_input("CUCUMBER_DB_NAME (base, ou service name Oracle)",
                                                       value=os.getenv("CUCUMBER_DB_NAME", ""))
        with st.container(horizontal=True):
            env_values["CUCUMBER_DB_USER"] = st.text_input("CUCUMBER_DB_USER", value=os.getenv("CUCUMBER_DB_USER", ""))
            env_values["CUCUMBER_DB_PASSWORD"] = st.text_input("CUCUMBER_DB_PASSWORD", type="password",
                                                               value=os.getenv("CUCUMBER_DB_PASSWORD", ""))
        env_values["CUCUMBER_DB_SCHEMA"] = st.text_input(
            "CUCUMBER_DB_SCHEMA (valeur par défaut)", value=os.getenv("CUCUMBER_DB_SCHEMA", ""),
            help="Pré-remplit le champ « Schéma BDD » de l'onglet Cucumber.")

        st.markdown("---")
        st.markdown("#### :material/folder_shared: Connaissances partagées")
        env_values["AGENT_KNOWLEDGE_DIR"] = st.text_input(
            "AGENT_KNOWLEDGE_DIR", value=os.getenv("AGENT_KNOWLEDGE_DIR", ""),
            placeholder=r"\\serveur\partage\agent-savoye, X:\agent-savoye ou /mnt/partage/agent-savoye",
            help="Répertoire commun où chaque analyse est déposée en Markdown et consultée avant les suivantes. "
                 "Chemin UNC, lecteur mappé ou chemin POSIX. Vide = fonction désactivée. Tout utilisateur du partage "
                 "lit tous les documents.")
        with st.container(horizontal=True):
            env_values["AGENT_KNOWLEDGE_AUTHOR"] = st.text_input(
                "AGENT_KNOWLEDGE_AUTHOR", value=os.getenv("AGENT_KNOWLEDGE_AUTHOR", ""),
                placeholder=f"Vide = {knowledge_tools.author()}",
                help="Auteur inscrit dans les documents déposés. Vide = utilisateur système.")
            knowledge_max = st.number_input(
                "AGENT_KNOWLEDGE_MAX_CONTEXT", min_value=0, max_value=100000, step=1000,
                value=env_int("AGENT_KNOWLEDGE_MAX_CONTEXT", knowledge_tools.DEFAULT_MAX_CONTEXT),
                help="Taille maximale (caractères) des connaissances partagées injectées dans un prompt. 0 = aucune.")
        env_values["AGENT_KNOWLEDGE_MAX_CONTEXT"] = str(int(knowledge_max))

        st.markdown("---")
        submit_save = st.form_submit_button("💾 Enregistrer la configuration complète", type="primary", use_container_width=True)

        if submit_save:
            # Les secrets sont enregistrés tels quels ; les autres valeurs sans espaces superflus. Écriture unique
            # (une ligne par valeur, échappements, relecture contrôlée)
            try:
                env_tools.write_env(ENV_PATH, {
                    name: value if name.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY")) else value.strip()
                    for name, value in env_values.items()})
            except ValueError as e:
                st.error(f"Configuration non enregistrée correctement : {e}.")
            else:
                st.success("Toute la configuration a été enregistrée avec succès dans le fichier .env !")
                st.rerun()

    # Hors formulaire : teste la configuration base déjà enregistrée dans le .env
    with st.container(horizontal=True, vertical_alignment="center"):
        test_db_clicked = st.button("Tester la connexion", icon=":material/lan:")
        st.caption("Teste la connexion base enregistrée (et le schéma DB_SCHEMA s'il est renseigné).")
    if test_db_clicked:
        with st.spinner("Connexion à la base…"):
            db_ok, db_message = test_connection(os.getenv("DB_SCHEMA", "").strip(), profile="")
        if db_ok:
            st.success(db_message, icon=":material/check_circle:")
        else:
            st.error(db_message, icon=":material/error:")
    if os.getenv("CUCUMBER_DB_HOST", "").strip() or os.getenv("CUCUMBER_DB_NAME", "").strip():
        if st.button("Tester la base Cucumber", icon=":material/lan:"):
            with st.spinner("Connexion à la base Cucumber…"):
                db_ok, db_message = test_connection(os.getenv("CUCUMBER_DB_SCHEMA", "").strip(), profile="cucumber")
            if db_ok:
                st.success(db_message, icon=":material/check_circle:")
            else:
                st.error(db_message, icon=":material/error:")

    # Hors formulaire : teste les logs groundcover enregistrés (clé, filtre de l'environnement de test)
    if os.getenv("GROUNDCOVER_API_KEY", "").strip():
        if st.button("Tester groundcover", icon=":material/lan:", key="test_groundcover"):
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            from tools import groundcover_tools
            try:
                now = _dt.now(_tz.utc)
                with st.spinner("Lecture des logs groundcover…"):
                    rows = groundcover_tools.search(now - _td(minutes=15), now, limit=3)
                st.success(f"groundcover joignable : {len(rows)} ligne(s) sur les 15 dernières minutes avec le filtre "
                           f"« {groundcover_tools.config()['filter']} »."
                           + (f" Dernière : {groundcover_tools.format_row(rows[-1])[:200]}" if rows else
                              " Aucune ligne : filtre trop strict, ou environnement inactif."),
                           icon=":material/check_circle:")
            except groundcover_tools.GroundcoverError as e:
                st.error(str(e), icon=":material/error:")

    # Hors formulaire : teste le répertoire partagé enregistré (existence, écriture, temps d'accès)
    with st.container(horizontal=True, vertical_alignment="center"):
        test_knowledge_clicked = st.button("Tester le répertoire partagé", icon=":material/folder_shared:",
                                           disabled=not knowledge_tools.enabled())
        st.caption(f"Teste AGENT_KNOWLEDGE_DIR enregistré : {knowledge_tools.knowledge_dir()}"
                   if knowledge_tools.enabled() else "AGENT_KNOWLEDGE_DIR vide : connaissances partagées désactivées.")
    if test_knowledge_clicked:
        with st.spinner("Test du répertoire partagé…"):
            knowledge_test = knowledge_tools.test_dir()
        (st.success if knowledge_test["ok"] else st.error)(
            knowledge_test["message"], icon=":material/check_circle:" if knowledge_test["ok"] else ":material/error:")
        for step, step_ok, detail in knowledge_test["etapes"]:
            st.caption(f"{'✅' if step_ok else '❌'} {step} : {detail}")
    st.caption("Recherche, aperçu, statut des documents, import de .md existants et documents en attente : onglet "
               "📚 Connaissances partagées.")

    st.divider()
    st.subheader("Mémoire de l'agent")
    memories = get_all_memories()

    with st.container(horizontal=True):
        if st.button("Importer l'historique local", icon=":material/download:",
                     help="Ajoute les *-cadrage.md et *.patch présents dans le workspace"):
            count = import_existing_deliverables(os.path.dirname(os.path.abspath(__file__)))
            st.toast(f"{count} ticket(s) importé(s) en mémoire.")
            st.rerun()

    if not memories:
        st.info("La mémoire est vide.")
    else:
        st.dataframe(memories, hide_index=True, width="stretch")

        selected_mem = st.selectbox("Consulter un ticket mémorisé", [m["issue_key"] for m in memories])
        mem = load_ticket_memory(selected_mem)
        with st.expander(f"Détail de {selected_mem}"):
            for fb in mem.get("feedbacks", []):
                st.markdown(f"- `{fb.get('source')}` {fb.get('feedback')}")
                if fb.get("diff"):
                    st.code(fb["diff"], language="diff")
            st.markdown(mem.get("final_cadrage") or "_Pas de cadrage._")
        if st.button(f"Supprimer {selected_mem} de la mémoire", icon=":material/delete:"):
            delete_ticket_memory(selected_mem)
            st.rerun()

        with st.container(border=True):
            st.markdown("**Purger la mémoire**")
            scope = st.selectbox("Périmètre", ["Tous les projets"] + sorted({m["project_key"] for m in memories}))
            confirm = st.checkbox("Je confirme la suppression définitive")
            if st.button("Purger", type="primary", disabled=not confirm, icon=":material/delete_sweep:"):
                count = clear_memory("" if scope == "Tous les projets" else scope)
                st.toast(f"{count} ticket(s) supprimé(s) de la mémoire.")
                st.rerun()