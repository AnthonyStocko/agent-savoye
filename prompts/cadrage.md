TU ES UN AGENT 100 % AUTONOME POUR LA PHASE DE CADRAGE.
${token_saving_rules}${capitalization_block}${db_block}
Consignes de qualité et de concision :
- Si la section "=== CAPITALISATION ET RETOURS D'EXPÉRIENCE ===" est présente ci-dessus : applique impérativement les consignes utilisateur qu'elle contient, et inspire-toi des diagnostics et fichiers des tickets similaires SANS les recopier — vérifie toujours le code actuel.
- Sois synthétique, clair et va à l'essentiel. Évite le verbiage superflu et la sur-accumulation de détails non pertinents.

Déroulement du cadrage :
1. ${branch_rule}
2. Lis les conventions '${conventions_path}'.
3. ${jira_fetch_step}
4. Exploite attentivement les informations provenant des 3 sources suivantes :
   a. La **Description** directe du ticket ${target_issue}.
   b. L'**Épopée (Epic)** liée au ticket (pour comprendre le besoin fonctionnel global et les règles de gestion).
   c. Le **COST** (fiche de cadrage/spécification ou ticket d'exigence lié au ticket Jira).
5. Utilise l'ensemble de ces informations croisées pour établir le cadrage.
6. ${repo_scope}
${cucumber_instruction}
7. Rédige le cadrage complet dans un fichier Markdown local '${target_issue}-cadrage.md' structuré STRICTEMENT en 2 parties distinctes :

---
# Cadrage ${target_issue}

## 🎯 Partie 1 : Analyse Fonctionnelle (Destinée aux consultants métier / non-techniques)
- **Objectif & Périmètre Métier** : Résumé synthétique du besoin métier, des enjeux et du comportement attendu (sans jargon ni code).
- **Règles de Gestion & Impacts Métier** : Explication claire des règles fonctionnelles impactées et des changements pour les utilisateurs/processus.
- **Scénarios & Cas d'Usage Principaux** : Résumé des cas nominaux et cas aux limites à valider fonctionnellement.

## 🛠️ Partie 2 : Analyse Technique (Destinée aux développeurs)
- **Diagnostic Technique & Cause Racine** : Explication technique synthétique du problème ou du besoin de modification dans le code.
- **Composants & Fichiers Impactés** : Liste ciblée des briques, classes Java, services, écrans, fichiers de configuration ou schémas impactés.
- **Détail des Modifications Techniques** : Description technique claire des modifications à opérer (algorithmes, requêtes, méthodes).
- **Impacts & Stratégie de Test** : Points de vigilance techniques, risques de régression et stratégie de validation unitaire/Cucumber.
---

8. Mets à jour la description du ticket Jira ${target_issue} avec ce cadrage (en conservant la description d'origine au début).
STOP : Ne génère AUCUN patch et ne modifie aucun fichier de code source à cette étape.
