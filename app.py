import os
import re
import subprocess
import sys
import time
import streamlit as st
from dotenv import load_dotenv, set_key

from tools import cucumber_tools, cucumber_verify, integration_tools
from tools.epic_tools import load_epic_manifest
from tools.jira_tools import (
    JiraSearchError, fetch_tickets_data, fetch_tickets_with_epic_context, get_jira_issue_summary,
    set_jira_description_markdown,
)
from tools.db_tools import DB_TYPES, test_connection
from tools.jenkins_tools import trigger_cucumber_job
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


def run_main_step(cmd: list, on_line=None) -> int:
    """Lance main.py en affichant ses logs au fil de l'eau (`on_line` : appelé à chaque ligne). Renvoie le code retour."""
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace"
    )
    output_container = st.empty()
    logs = ""
    for line in process.stdout:
        logs += line
        output_container.code(logs, language="text")
        if on_line:
            on_line(line)
    process.wait()
    return process.returncode


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
            help="Ex: SPEC, OXY, PHE..."
        )
        jira_team_input = st.text_input(
            "Équipe / Composant (Optionnel) :",
            value=os.getenv("JIRA_SEARCH_TEAM", ""),
            key=f"{key_prefix}search_team",
            help="Composant Jira ou équipe Atlassian. Ex: TEAM_SPEC, WMS, Socle...",
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
            help="Ex: WAIT_TEAM_SPEC, BUG (laissez vide si aucun label requis)",
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
load_dotenv(ENV_PATH, override=True)

st.title("🤖 Tableau de bord - Agent Scrum Master")

tab_integration, tab_execution, tab_cucumber, tab_merges, tab_config = st.tabs(
    ["🧩 Intégration", "🚀 Lancement", "🥒 Cucumber", "🔔 Écoute des merges", "⚙️ Configuration (.env)"])

# Initialisation de l'état
if "workflow_step" not in st.session_state:
    st.session_state["workflow_step"] = "INIT"

# ---------------------------------------------------------
# ONGLET 1 : LANCEMENT & RECHERCHE PARAMÉTRABLE
# ---------------------------------------------------------
with tab_execution:
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
        
        is_oxy = (current_project == "OXY")
        if is_oxy:
            st.info("ℹ️ Projet OXY détecté : Analyse basée uniquement sur le socle Standard (branche dev).")
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
            if not is_oxy and not client_repo_path.strip():
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
                    st.error("Une erreur est survenue lors de l'analyse de l'épopée : consultez les logs ci-dessus "
                             "(les tâches déjà créées sont conservées et réutilisées à la relance).")
            run_cadrage_btn = run_direct_patch_btn = False

        # Option A : Lancer ou revoir l'analyse
        if run_cadrage_btn:
            if not is_oxy and not client_repo_path.strip():
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

                process = subprocess.Popen(
                    cmd_cadrage,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace"
                )
                output_container = st.empty()
                logs = ""
                for line in process.stdout:
                    logs += line
                    output_container.code(logs, language="text")

                process.wait()

                if process.returncode == 0:
                    st.success("Analyse terminée et sauvegardée.")
                    st.session_state["workflow_step"] = "REVIEW_ANALYSIS"
                    st.session_state["current_ticket"] = selected_ticket
                    st.session_state["current_client_repo"] = client_repo_path.strip()
                    st.rerun()
                else:
                    st.error("Une erreur est survenue lors du cadrage.")

        # Option B : Génération directe du Patch (sans passer par la relecture)
        if run_direct_patch_btn:
            if not is_oxy and not client_repo_path.strip():
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

                process = subprocess.Popen(
                    cmd_patch,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace"
                )
                output_container = st.empty()
                logs = ""
                for line in process.stdout:
                    logs += line
                    output_container.code(logs, language="text")

                process.wait()

                if process.returncode == 0:
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

                process = subprocess.Popen(
                    cmd_refine,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace"
                )

                output_container = st.empty()
                logs = ""
                for line in process.stdout:
                    logs += line
                    output_container.code(logs, language="text")

                process.wait()

                if process.returncode == 0:
                    st.toast("Analyse révisée et mise à jour !", icon=":material/check_circle:")
                    st.rerun()
                else:
                    # Pas de rerun : les logs restent affichés pour diagnostiquer l'échec
                    st.error(f"La révision a échoué (code {process.returncode}). Rien n'a été enregistré en mémoire : consultez les logs ci-dessus.")

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

                process = subprocess.Popen(
                    cmd_val_patch,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace"
                )

                output_container = st.empty()
                logs = ""
                for line in process.stdout:
                    logs += line
                    output_container.code(logs, language="text")

                process.wait()

                if process.returncode == 0:
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
                    st.error(f"La révision a échoué (code {returncode}) : consultez les logs ci-dessus.")

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


with tab_integration:
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
                 "besoin. Ex: CANSON. Plusieurs valeurs séparées par des virgules. "
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
                                         "complété avec les réponses : voir le journal ci-dessus.")
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


# ---------------------------------------------------------
# ONGLET CUCUMBER : COUVERTURE DES SPÉCIFIQUES D'UN CLIENT
# ---------------------------------------------------------
with tab_cucumber:
    st.markdown("### Tests Cucumber des spécifiques d'un client")
    st.caption("L'agent inventorie les spécifiques du dépôt client (écarts avec le Standard), puis écrit les .feature et "
               "Step Definitions qui les couvrent dans le dépôt <client>-cucumber, au formalisme du Cucumber Standard. "
               "Le résultat est un patch local : rien n'est poussé.")

    # Valeurs par défaut du .env, puis dernières valeurs saisies (conservées dans la session)
    st.session_state.setdefault("cucumber_repo", os.getenv("BITBUCKET_CLIENT_REPO_PATH", ""))
    st.session_state.setdefault("cucumber_db_schema", os.getenv("DB_SCHEMA", ""))
    st.session_state.setdefault("cucumber_application", "")
    st.session_state.setdefault("cucumber_std_repo", os.getenv("BITBUCKET_CUCUMBER_STD_REPO", ""))

    cucumber_repo_input = st.text_input(
        "Dépôt Bitbucket du projet client :", key="cucumber_repo",
        help="URL du projet Bitbucket (ex. https://git.kiscloud.io/projects/NOVALIA) ou chemin d'un clone local.",
    ).strip()
    # Nom du client recalculé quand le dépôt change, sauf s'il a été modifié à la main depuis
    derived_client = cucumber_tools.client_name_from_repo(cucumber_repo_input)
    if st.session_state.get("cucumber_client_derived") != derived_client:
        st.session_state["cucumber_client_derived"] = derived_client
        st.session_state["cucumber_client_name"] = derived_client

    col_client, col_schema, col_app = st.columns(3)
    with col_client:
        cucumber_client = st.text_input(
            "Nom du client :", key="cucumber_client_name",
            help="Déduit du dépôt (clé du projet Bitbucket). Le dépôt cible est <client>-cucumber.",
        ).strip().lower()
    with col_schema:
        cucumber_schema = st.text_input(
            "Schéma BDD (optionnel) :", key="cucumber_db_schema",
            help="Schéma de la base client consultable en lecture seule. Vide = l'agent n'utilise pas la base.",
        ).strip()
    with col_app:
        cucumber_application = st.text_input(
            "Projet / Application Jira (optionnel) :", key="cucumber_application",
            disabled=not os.getenv("JIRA_APPLICATION_FIELD", "").strip(),
            help="Ex: CANSON. Les tickets Jira ayant cette valeur (champ JIRA_APPLICATION_FIELD) servent de "
                 "référence fonctionnelle sur les spécifiques.",
        ).strip()
    if cucumber_client:
        st.caption(f"Dépôt cible : **{cucumber_client}-cucumber** · identifiant de travail "
                   f"**{cucumber_tools.work_id(cucumber_client)}**")

    cucumber_std_input = st.text_input(
        "Dépôt Bitbucket du Cucumber Standard :", key="cucumber_std_repo",
        help="URL de navigation (.../projects/X/repos/Y/browse), URL clonable (.../scm/x/y.git) ou chemin d'un "
             "clone local, utilisé tel quel. Défaut : BITBUCKET_CUCUMBER_STD_REPO (onglet Configuration).",
    ).strip()
    # Référence Git du Standard lue dans le journal de la dernière exécution (pour ce dépôt seulement)
    std_ref_slot = st.empty()

    def show_std_ref() -> None:
        last = st.session_state.get("cucumber_std_ref") or {}
        if last.get("repo") == cucumber_std_input and last.get("ref"):
            std_ref_slot.caption(f"Référence Git utilisée lors de la dernière exécution : **{last['ref']}**")

    def capture_std_ref(line: str) -> None:
        if line.startswith(cucumber_tools.STD_REF_LOG):
            ref = line[len(cucumber_tools.STD_REF_LOG):].strip()
            st.session_state["cucumber_std_ref"] = {"repo": cucumber_std_input, "ref": ref}
            show_std_ref()

    show_std_ref()

    cucumber_cmd = [
        sys.executable, "main.py",
        "--step", "generate-cucumber-client",
        "--client-repo", cucumber_repo_input,
        "--client-name", cucumber_client,
        "--application", cucumber_application,
        "--db-schema", cucumber_schema,
        "--cucumber-std-repo", cucumber_std_input,
    ]
    ready = bool(cucumber_repo_input and cucumber_client)

    # 1. Inventaire des spécifiques (calculs Python, aucun token)
    if st.button("Inventaire des spécifiques", use_container_width=True, icon=":material/inventory_2:",
                 disabled=not ready, help="Prépare les dépôts, puis inventorie les spécifiques du client, les "
                                          "catalogues Cucumber et les extraits Jira. Aucun appel à Claude."):
        st.info(f"Inventaire des spécifiques {cucumber_client} en cours...")
        if run_main_step(cucumber_cmd + ["--inventory-only"], on_line=capture_std_ref) == 0:
            st.success("Inventaire terminé.")
        else:
            st.error("L'inventaire a échoué : voir le journal ci-dessus.")

    inventory = cucumber_tools.load_inventory(cucumber_client) if cucumber_client else {}
    cucumber_state = cucumber_tools.load_state(cucumber_client) if cucumber_client else {}
    selected_ids = []
    if inventory.get("specifiques"):
        specs = inventory["specifiques"]
        counts = {k: sum(1 for s in specs if s["couverture"] == k) for k in ("non_couvert", "partiel", "deja_couvert")}
        st.markdown(f"**{len(specs)} spécifique(s)** :gray[(inventaire du "
                    f"{inventory['genere_le'].replace('T', ' ')[:16]})] · {counts['non_couvert']} non couvert(s) · "
                    f"{counts['partiel']} partiel(s) · {counts['deja_couvert']} déjà couvert(s)")
        table = [{"Traiter": s["couverture"] in ("non_couvert", "partiel"), "Spécifique": s["libelle"],
                  "Type": s["type"], "Fichiers": s["nb_fichiers"], "Couverture": s["couverture"],
                  "Tests Standard liés": len(s.get("tests_std") or []), "Jira": bool(s.get("jira")), "id": s["id"]}
                 for s in specs]
        edited = st.data_editor(
            table, key=f"cucumber_selection_{cucumber_client}_{inventory['genere_le']}", hide_index=True,
            width="stretch", height=320,
            disabled=["Spécifique", "Type", "Fichiers", "Couverture", "Tests Standard liés", "Jira", "id"],
            column_config={
                "Traiter": st.column_config.CheckboxColumn("Traiter", width="small"),
                "Fichiers": st.column_config.NumberColumn("Fichiers", width="small"),
                "Tests Standard liés": st.column_config.NumberColumn(
                    "Tests Standard liés", width="small",
                    help="Features du Cucumber Standard rapprochées du spécifique (au plus 3, score ≥ "
                         "CUCUMBER_STD_MATCH_MIN), dont l'agent part pour écrire ses tests. 0 = aucun équivalent."),
                "Jira": st.column_config.CheckboxColumn("Jira", width="small", help="Tickets Jira associés"),
                "id": st.column_config.TextColumn("id", width="small"),
            },
        )
        selected_ids = [row["id"] for row in edited if row["Traiter"]]
        linked = sum(1 for s in specs if s.get("tests_std"))
        unresolved_steps = (inventory.get("catalogue_standard") or {}).get("nb_steps_non_resolus")
        if unresolved_steps:
            st.caption(f":orange[{unresolved_steps} step(s) du Cucumber Standard déclaré(s) par une constante non "
                       f"résolue : absents du catalogue (voir le journal de l'inventaire).]")
        with st.expander(f"Tests Standard liés : {linked}/{len(specs)} spécifique(s) avec au moins un équivalent"):
            for s in specs:
                tests = s.get("tests_std") or []
                if not tests:
                    st.markdown(f"- **{s['libelle']}** : :gray[aucun test Standard équivalent]")
                    continue
                st.markdown(f"- **{s['libelle']}**\n" + "\n".join(
                    f"    - `{t['path'].split('features/', 1)[-1]}` — {t['feature']} :gray[(score {t['score']}, "
                    f"{t['raison']})]" for t in tests))
        batch_size = max(1, env_int("CUCUMBER_BATCH_SIZE", 3))
        lot_count = -(-len(selected_ids) // batch_size)
        average = cucumber_tools.lot_cost_average(os.path.join(".tmp", "token_usage.csv"))
        if average["lots"]:
            estimate = (f"≈ {lot_count * average['tokens'] / 1e6:.1f} M tokens, "
                        f"≈ {lot_count * average['cout_usd']:.2f} $ (moyenne de {average['lots']} lot(s) précédent(s))")
        else:
            estimate = "coût inconnu (aucun lot exécuté jusqu'ici)"
        st.caption(f"{len(selected_ids)} spécifique(s) coché(s) → {lot_count} lot(s) de {batch_size} · {estimate}")

    # 2. Génération par lots (un appel Claude neuf par lot), reprise et complément
    pending = cucumber_tools.pending_lots(cucumber_state)
    completable = cucumber_tools.completable_specifics(cucumber_state) if cucumber_state.get("lots") else []
    # Une nouvelle génération repart de la base : les lots d'une génération existante sont écrasés
    existing = bool(cucumber_state.get("lots")) and os.path.isdir(cucumber_tools.worktree_dir(cucumber_client))
    overwrite = True
    if existing:
        st.warning(f"Une génération existe déjà ({len(cucumber_state['lots'])} lot(s)) : une nouvelle génération "
                   f"repart de la base et **les lots précédents seront écrasés** (commits, patch, récapitulatif). "
                   f"Pour ajouter des tests sans rien perdre, utilisez « Compléter la génération ».",
                   icon=":material/warning:")
        overwrite = st.checkbox("Je confirme : les lots précédents seront écrasés",
                                key=f"cucumber_overwrite_{cucumber_client}")
    col_generate, col_resume, col_complete = st.columns([2, 1, 1])
    with col_generate:
        generate = st.button("Génération des tests Cucumber", type="primary", use_container_width=True,
                             icon=":material/science:", disabled=not (ready and selected_ids and overwrite),
                             help="Traite uniquement les spécifiques cochés. Lancez d'abord l'inventaire.")
    with col_resume:
        resume = st.button("Reprendre", use_container_width=True, icon=":material/play_arrow:",
                           disabled=not (ready and pending),
                           help=f"{len(pending)} lot(s) non terminé(s) lors de la dernière génération."
                           if pending else "Aucun lot à reprendre.")
    with col_complete:
        complete = st.button("Compléter la génération", use_container_width=True, icon=":material/playlist_add:",
                             disabled=not (ready and completable),
                             help=f"{len(completable)} spécifique(s) non couvert(s) faute de step, d'endpoint, "
                                  f"d'interaction ou de vérification : repris dans de nouveaux lots (recettes de "
                                  f"test, synonymes), sans effacer les lots ni les commits précédents."
                             if completable else "Aucun spécifique à compléter.")
    if generate or resume or complete:
        progress = st.progress(0.0, text="Préparation (inventaire, catalogues, worktree)...")

        def show_lot_progress(line: str) -> None:
            capture_std_ref(line)
            match = re.match(r"\[LOT (\d+)/(\d+)\]", line)
            if match:
                current, total_lots = int(match.group(1)), int(match.group(2))
                progress.progress(min((current - 1) / total_lots, 1.0), text=f"Lot {current}/{total_lots} en cours")

        extra = ["--resume"] if resume else ["--complete"] if complete else ["--specifics", ",".join(selected_ids)]
        returncode = run_main_step(cucumber_cmd + extra, on_line=show_lot_progress)
        progress.progress(1.0, text="Terminé" if returncode == 0 else "Interrompu")
        if returncode == 0:
            st.success(f"Tests Cucumber des spécifiques {cucumber_client} générés.")
        else:
            st.error(f"Génération interrompue (code {returncode}) : utilisez « Reprendre » pour continuer.")
        cucumber_state = cucumber_tools.load_state(cucumber_client)

    if cucumber_state.get("lots"):
        lot_rows = []
        for lot in cucumber_state["lots"]:
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
                "Tokens": total_tokens or None,
                "Coût ($)": round(tokens["cout_usd"], 3) if isinstance(tokens.get("cout_usd"), (int, float)) else None,
                "Erreur": lot.get("erreur", ""),
            })
        with st.expander(f"Lots de la dernière génération ({sum(1 for r in lot_rows if r['Statut'] == 'termine')}"
                         f"/{len(lot_rows)} terminés)", expanded=bool(pending)):
            controlled = [cucumber_tools.lot_indicators(lot) for lot in cucumber_state["lots"]]
            controlled = [c for c in controlled if c]
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

    # 3. Vérification par exécution et revue humaine des scénarios
    verified_rows = cucumber_verify.scenario_rows(cucumber_state) if cucumber_state.get("lots") else []
    if verified_rows:
        indicators = cucumber_verify.verification_indicators(cucumber_state)
        st.markdown("#### :material/fact_check: Vérification des scénarios")
        if not indicators["verifies"]:
            message = next((lot["execution"]["message"] for lot in cucumber_state["lots"] if lot.get("execution")), "")
            st.warning(f"**Pertinence non vérifiée par exécution** : {message or 'aucun moyen d’exécution configuré'}. "
                       "Configurez CUCUMBER_VERIFY_JOB ou CUCUMBER_VERIFY_LOCAL_CMD (onglet Configuration).",
                       icon=":material/help:")
        else:
            def pct(value):
                return "—" if value is None else f"{value} %"
            st.caption(f"Pertinents : {pct(indicators['taux_pertinent'])} des {indicators['verifies']} vérifié(s) · "
                       f"mutations détectées : {pct(indicators['taux_mutation'])} · overrides en échec sur le "
                       f"Standard : {pct(indicators['taux_echec_standard'])} · {indicators['retires']} retiré(s) vers "
                       f"le patch à revoir · validation humaine : {pct(indicators['taux_validation_humaine'])}")
        st.dataframe([{k: v for k, v in row.items() if k != "tag"} for row in verified_rows], hide_index=True,
                     width="stretch", column_config={
                         "Build": st.column_config.LinkColumn("Build", display_text="Jenkins"),
                         "Statut": st.column_config.TextColumn(
                             "Statut", help="pertinent : passe sur le client, échoue sur le Standard (override) et "
                                            "mutation détectée ; faible : mutation non détectée ou passe sur le "
                                            "Standard ; invalide : échoue sur le client ; non_verifie : pas "
                                            "d'exécution (jamais compté pertinent)"),
                         "Client": st.column_config.TextColumn("Client", width="small"),
                         "Standard": st.column_config.TextColumn("Standard", width="small"),
                         "Mutation": st.column_config.TextColumn("Mutation", width="small"),
                         "Statique": st.column_config.TextColumn("Statique", width="small"),
                     })
        sample = cucumber_verify.review_sample(cucumber_state)
        if sample:
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
                        verdict = (cucumber_state.get("verdicts") or {}).get(item["tag"])
                        if verdict:
                            st.markdown(f":gray[Verdict : **{verdict['verdict'].replace('_', ' ')}**"
                                        + (f" — {verdict['motif']}" if verdict.get("motif") else "") + "]")
                        with st.container(horizontal=True, vertical_alignment="bottom"):
                            reason = st.text_input("Motif", key=f"verdict_motif_{item['tag']}",
                                                   placeholder="Obligatoire si non pertinent")
                            if st.button("Pertinent", key=f"verdict_ok_{item['tag']}", icon=":material/thumb_up:"):
                                cucumber_verify.record_verdict(cucumber_client, cucumber_state, item, "pertinent",
                                                               reason)
                                cucumber_tools.save_state(cucumber_client, cucumber_state)
                                st.rerun()
                            if st.button("Non pertinent", key=f"verdict_ko_{item['tag']}",
                                         icon=":material/thumb_down:", disabled=not reason.strip()):
                                cucumber_verify.record_verdict(cucumber_client, cucumber_state, item,
                                                               "non_pertinent", reason)
                                cucumber_tools.save_state(cucumber_client, cucumber_state)
                                st.rerun()
                st.caption("Un verdict « Non pertinent » et son motif deviennent une consigne du projet, injectée "
                           "dans les lots des prochaines générations.")

    # Derniers livrables du client (affichés aussi après un rechargement de la page)
    if cucumber_client and os.path.exists(cucumber_tools.patch_path(cucumber_client)):
        patch_file = cucumber_tools.patch_path(cucumber_client)
        coverage_file = cucumber_tools.coverage_path(cucumber_client)
        generated_at = time.strftime("%d/%m/%Y %H:%M", time.localtime(os.path.getmtime(patch_file)))
        with st.container(border=True):
            st.markdown(f"**:material/description: Patch** `{patch_file}` :gray[(généré le {generated_at})]")
            with open(patch_file, "rb") as f:
                st.download_button("Télécharger le patch", f.read(), file_name=os.path.basename(patch_file),
                                   mime="text/x-diff", icon=":material/download:")
            review_patch = cucumber_verify.a_revoir_path(cucumber_client)
            if os.path.exists(review_patch):
                with open(review_patch, "rb") as f:
                    st.download_button("Scénarios retirés à revoir (patch séparé, avec leur motif)", f.read(),
                                       file_name=os.path.basename(review_patch), mime="text/x-diff",
                                       icon=":material/rule:")
            if os.path.exists(coverage_file):
                with st.expander("Récapitulatif de couverture"):
                    with open(coverage_file, "r", encoding="utf-8", errors="replace") as f:
                        st.markdown(f.read())


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
                 "Ex : NOVALIA, OSCARO/odatio-oscaro. Les dépôts *cucumber* sont toujours ignorés."
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
            set_key(ENV_PATH, "POLL_REPOSITORIES", projects_value)
            set_key(ENV_PATH, "POLL_INTERVAL_SECONDS", str(int(listen_interval) * 60))
            os.environ["POLL_REPOSITORIES"] = projects_value
            os.environ["POLL_INTERVAL_SECONDS"] = str(int(listen_interval) * 60)
            with st.spinner("Démarrage de l'écoute…"):
                ok, message = start_poller(projects_value, int(listen_interval) * 60, listen_catch_up)
            if ok:
                st.toast(message, icon=":material/sensors:")
                st.rerun()
            else:
                st.error(message)


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
                help="Labels pré-remplis, séparés par une virgule. Ex: WAIT_TEAM_SPEC")
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
                     "consulte tous les tickets ayant la même valeur que le ticket (ex. CANSON). Vide = désactivé.")
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
                 "Ex: https://git.kiscloud.io/projects/NOVALIA ou C:\\development\\NOVALIA"
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
                "CUCUMBER_MAX_TURNS", min_value=5, step=5, value=env_int("CUCUMBER_MAX_TURNS", 25),
                help="Nombre maximal de tours de l'agent par lot (--max-turns).")
        env_values["CUCUMBER_BATCH_SIZE"] = str(int(cucumber_batch))
        env_values["CUCUMBER_MAX_TURNS"] = str(int(cucumber_turns))
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
            "CUCUMBER_DRY_RUN", value=os.getenv("CUCUMBER_DRY_RUN", "").strip() == "1",
            help="Après une compilation réussie, dry-run Cucumber des features du lot : vérifie que chaque step a "
                 "une définition (mvn test -Dcucumber.execution.dry-run=true), sans rien exécuter.")
        env_values["CUCUMBER_DRY_RUN"] = "1" if dry_run else "0"
        st.markdown("##### Vérification de la pertinence par exécution")
        st.caption("Sans job ni commande locale, la génération s'arrête au contrôle statique et affiche « pertinence "
                   "non vérifiée par exécution ».")
        env_values["CUCUMBER_VERIFY_JOB"] = st.text_input(
            "CUCUMBER_VERIFY_JOB", value=os.getenv("CUCUMBER_VERIFY_JOB", ""),
            placeholder="Vide = pas de job (ex. CUCUMBER-VERIFY-PATCH)",
            help="Job Jenkins dédié acceptant PATCH (fichier .patch du lot), TAGS et ENV, et archivant le rapport "
                 "Cucumber JSON. Ses paramètres sont vérifiés avant chaque génération : aucun push n'est fait.")
        with st.container(horizontal=True):
            env_values["CUCUMBER_VERIFY_ENV"] = st.text_input(
                "CUCUMBER_VERIFY_ENV", value=os.getenv("CUCUMBER_VERIFY_ENV", ""),
                help="Environnement client (paramètre ENV du job, {env} de la commande locale).")
            env_values["CUCUMBER_VERIFY_ENV_STD"] = st.text_input(
                "CUCUMBER_VERIFY_ENV_STD", value=os.getenv("CUCUMBER_VERIFY_ENV_STD", ""),
                help="Environnement Standard de même version, pour la contre-épreuve des overrides (vide = pas de "
                     "contre-épreuve Standard).")
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
                "CUCUMBER_VERIFY_FIX_ATTEMPTS", min_value=0, step=1, value=env_int("CUCUMBER_VERIFY_FIX_ATTEMPTS", 1),
                help="Appels de correction par lot pour les scénarios invalides ou faibles, avant leur retrait.")
            review_sample = st.number_input(
                "CUCUMBER_REVIEW_SAMPLE", min_value=0, step=1, value=env_int("CUCUMBER_REVIEW_SAMPLE", 5),
                help="Scénarios proposés à la revue humaine de l'onglet Cucumber : d'abord les « non vérifié », "
                     "puis des « pertinent » tirés au hasard.")
        env_values["CUCUMBER_VERIFY_TIMEOUT"] = str(int(verify_timeout))
        env_values["CUCUMBER_VERIFY_FIX_ATTEMPTS"] = str(int(verify_fixes))
        env_values["CUCUMBER_REVIEW_SAMPLE"] = str(int(review_sample))
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
                help="Dépôt Cucumber de chaque projet client. Ex: NOVALIA=novalia-cucumber-2,OSCARO=oscaro-cucumber. "
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
        st.markdown("#### 🔑 Clé API Anthropic")
        env_values["ANTHROPIC_API_KEY"] = st.text_input(
            "ANTHROPIC_API_KEY (Laissez vide pour utiliser Claude Pro CLI)",
            value=os.getenv("ANTHROPIC_API_KEY", ""), type="password")

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
                                                                 help=effort_help.replace("Vide = réglage de Claude Code sur le poste.", "Vide = CLAUDE_EFFORT_CUCUMBER.")
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

        st.markdown("---")
        submit_save = st.form_submit_button("💾 Enregistrer la configuration complète", type="primary", use_container_width=True)

        if submit_save:
            # Les secrets sont enregistrés tels quels ; les autres valeurs sans espaces superflus
            for env_name, env_value in env_values.items():
                is_secret = env_name.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_API_KEY"))
                set_key(ENV_PATH, env_name, env_value if is_secret else env_value.strip())

            st.success("Toute la configuration a été enregistrée avec succès dans le fichier .env !")
            st.rerun()

    # Hors formulaire : teste la configuration base déjà enregistrée dans le .env
    with st.container(horizontal=True, vertical_alignment="center"):
        test_db_clicked = st.button("Tester la connexion", icon=":material/lan:")
        st.caption("Teste la connexion base enregistrée (et le schéma DB_SCHEMA s'il est renseigné).")
    if test_db_clicked:
        with st.spinner("Connexion à la base…"):
            db_ok, db_message = test_connection(os.getenv("DB_SCHEMA", "").strip())
        if db_ok:
            st.success(db_message, icon=":material/check_circle:")
        else:
            st.error(db_message, icon=":material/error:")

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