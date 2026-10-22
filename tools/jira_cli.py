"""
Point d'entrée en ligne de commande pour que l'agent Claude Code crée un ticket Jira
et y attache des fichiers, de façon idempotente (relançable sans créer de doublon).

  python tools/jira_cli.py create --project SPEC --summary "..." --description-file desc.md \
      --component oda_Prenom_N --label WAIT_TEAM_SPEC
  python tools/jira_cli.py attach --issue PROJ-123 --file PROJ-123-client-cucumber-2.patch

La dernière ligne de sortie est toujours lisible par machine : JIRA_KEY=<clé> ou JIRA_ERROR=<message>.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from tools.jira_tools import (
    add_jira_attachment,
    create_jira_issue,
    find_jira_issue_by_summary,
    get_jira_attachment_names,
)

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


def create_or_get_issue(project: str, summary: str, description: str, component: str, labels: list,
                        issue_type: str = "") -> tuple:
    """Renvoie (clé, créé?) : réutilise un ticket existant portant exactement le même résumé."""
    existing = find_jira_issue_by_summary(project, summary)
    if existing:
        return existing, False
    return create_jira_issue(project, summary, description, component, labels, issue_type), True


def attach_files(issue_key: str, file_paths: list) -> list:
    """Attache les fichiers absents du ticket. Renvoie la liste des messages."""
    already = get_jira_attachment_names(issue_key)
    messages = []
    for path in file_paths:
        if os.path.basename(path) in already:
            messages.append(f"Déjà attaché, ignoré : {os.path.basename(path)}")
        else:
            messages.append(add_jira_attachment(issue_key, os.path.abspath(path)))
    return messages


def main() -> int:
    parser = argparse.ArgumentParser(description="Création de tickets et pièces jointes Jira pour l'agent")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Crée un ticket (ou renvoie celui qui a déjà ce résumé)")
    create.add_argument("--project", required=True)
    create.add_argument("--summary", required=True)
    create.add_argument("--description-file", required=True, help="Fichier Markdown de la description")
    create.add_argument("--component", default="", help="Composant Jira ou équipe Atlassian")
    create.add_argument("--label", action="append", default=[], help="Répétable")
    create.add_argument("--issue-type", default="")

    attach = sub.add_parser("attach", help="Attache un ou plusieurs fichiers à un ticket")
    attach.add_argument("--issue", required=True)
    attach.add_argument("--file", action="append", required=True, help="Répétable")

    args = parser.parse_args()
    try:
        if args.command == "create":
            with open(args.description_file, "r", encoding="utf-8") as f:
                description = f.read()
            key, created = create_or_get_issue(args.project, args.summary, description,
                                               args.component, args.label, args.issue_type)
            print(f"Ticket {'créé' if created else 'déjà existant, réutilisé'} : {key}")
            print(f"JIRA_KEY={key}")
        else:
            for message in attach_files(args.issue, args.file):
                print(message)
            print(f"JIRA_KEY={args.issue}")
        return 0
    except Exception as e:
        print(f"JIRA_ERROR={e}")
        return 1


if __name__ == "__main__":
    # Uniquement en exécution directe : main.py importe ce module et gère déjà sa propre sortie
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
