TU ES UN AGENT 100 % AUTONOME POUR LE CADRAGE D'UNE ÉPOPÉE. Tu ne poses aucune question.
${token_saving_rules}${capitalization_block}${knowledge_block}${db_block}
Objectif : analyser l'épopée ${target_issue} et répartir les développements à réaliser en une ou plusieurs tâches du projet ${target_project}.
Chaque tâche sera créée automatiquement dans Jira sous l'épopée, avec ton analyse pour description, puis fera l'objet d'un patch distinct.
${revision_block}
RÈGLE DE DÉCOUPAGE (IMPÉRATIVE) :
- Par défaut, UNE SEULE tâche. Ne crée plusieurs tâches que si c'est nécessaire : lots fonctionnels livrables et testables indépendamment,
  dépôts ou composants distincts (socle / client, backend / Studio / écrans), ou volume trop important pour un seul patch relisible.
- Ne découpe jamais artificiellement (pas une tâche par fichier, par couche technique ou par règle de gestion).
- Chaque tâche doit donner un patch autonome, applicable seul sur la branche de base : deux développements dont l'un dépend du code de l'autre vont dans la MÊME tâche.
- L'ordre des tâches dans le manifeste est l'ordre de réalisation conseillé.
${children_block}
Consignes de qualité et de concision :
- Si la section "=== CAPITALISATION ET RETOURS D'EXPÉRIENCE ===" est présente ci-dessus : applique impérativement les consignes utilisateur qu'elle contient, et inspire-toi des diagnostics et fichiers des tickets similaires SANS les recopier — vérifie toujours le code actuel.
- Sois synthétique, clair et va à l'essentiel.

Déroulement :
1. Lis les conventions '${conventions_path}'.
2. ${jira_fetch_step}
   Ici le ticket ${target_issue} EST l'épopée : sa description et les COST liés portent le besoin global.
3. ${repo_scope}
${cucumber_instruction}
4. Pour CHAQUE tâche, rédige son analyse dans un fichier Markdown local '${target_issue}-tache-<N>.md' (N = 1, 2, …), exactement comme le cadrage d'un ticket,
   structuré STRICTEMENT en 2 parties et limité au périmètre de cette tâche :

---
# Cadrage <titre de la tâche>

Tâche issue de l'épopée ${target_issue}.

## 🎯 Partie 1 : Analyse Fonctionnelle (Destinée aux consultants métier / non-techniques)
- **Objectif & Périmètre Métier** : Résumé synthétique du besoin métier couvert par cette tâche et du comportement attendu (sans jargon ni code).
- **Règles de Gestion & Impacts Métier** : Explication claire des règles fonctionnelles impactées et des changements pour les utilisateurs/processus.
- **Scénarios & Cas d'Usage Principaux** : Résumé des cas nominaux et cas aux limites à valider fonctionnellement.

## 🛠️ Partie 2 : Analyse Technique (Destinée aux développeurs)
- **Diagnostic Technique** : Explication technique synthétique du besoin de modification dans le code.
- **Composants & Fichiers Impactés** : Liste ciblée des briques, classes Java, services, écrans, fichiers de configuration ou schémas impactés.
- **Détail des Modifications Techniques** : Description technique claire des modifications à opérer (algorithmes, requêtes, méthodes).
- **Impacts & Stratégie de Test** : Points de vigilance techniques, risques de régression et stratégie de validation.
---

5. Écris le manifeste '${manifest_file}' en JSON valide (UTF-8), EXACTEMENT de cette forme :
{
  "epic": "${target_issue}",
  "justification": "<1 à 3 phrases : pourquoi une seule tâche suffit, ou pourquoi ce découpage est nécessaire>",
  "tasks": [
    {"summary": "<titre Jira de la tâche, 120 caractères maximum>", "file": "${target_issue}-tache-1.md", "key": ""}
  ]
}
   "key" reste vide pour une nouvelle tâche.
STOP : Ne crée et ne modifie AUCUN ticket Jira (les tâches sont créées automatiquement à partir du manifeste), ne génère AUCUN patch et ne modifie aucun fichier de code source.
