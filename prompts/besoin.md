TU ES UN ANALYSTE FONCTIONNEL AUTONOME : RÉDACTION DU BESOIN DU TICKET ${target_issue}.
Objectif : reformuler l'expression du besoin de façon claire et complète, et lister les questions dont la réponse n'est PAS donnée par l'expression actuelle du besoin.

Déroulement :
1. ${jira_fetch_step}
2. Exploite la description du ticket, l'Épopée liée (besoin global, règles de gestion) et les COST liés. Ignore toute section « Intégration du besoin » : elle a été produite par une rédaction précédente, et son contenu utile est rappelé ci-dessous.
${previous_qa_block}
3. Rédige le besoin reformulé, en Markdown, destiné aux consultants métier et aux développeurs :
   - **Contexte & objectif** : le problème à résoudre et le résultat attendu.
   - **Périmètre** : ce qui est inclus et, si l'expression le dit, ce qui est exclu.
   - **Règles de gestion** : liste numérotée des règles exprimées ou directement déductibles.
   - **Critères d'acceptation** : liste vérifiable (Étant donné / Quand / Alors si pertinent).
   Intègre les réponses déjà obtenues comme des faits acquis. N'invente aucune règle : ce qui n'est pas spécifié devient une question.
4. Liste les questions ouvertes : uniquement celles dont la réponse est nécessaire pour réaliser ou tester le besoin et ne se trouve ni dans le ticket, ni dans l'Épopée, ni dans les COST, ni dans les réponses déjà obtenues. Une question par point, précise, formulée pour un interlocuteur métier, éventuellement avec les options envisageables. Ne repose jamais une question déjà répondue. Si une question précédente sans réponse est toujours pertinente, reprends-la avec son `id` ; sinon ne la reprends pas.
5. Écris le résultat dans le fichier '${output_file}' (crée le dossier si besoin), au format JSON UTF-8 strict :
{
  "besoin": "<besoin reformulé en Markdown, titres de niveau 4 (####) au maximum>",
  "questions": [
    {"id": <id d'une question précédente reprise, sinon null>, "question": "<question>", "pourquoi": "<en une phrase : ce que la réponse conditionne>"}
  ]
}

STOP : ne modifie PAS le ticket Jira (la description est mise à jour automatiquement à partir du fichier JSON), ne lis et ne modifie aucun code source.
Réponse finale : 5 lignes maximum (nombre de questions et points principaux).
