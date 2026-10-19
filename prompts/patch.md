LE CADRAGE A ÉTÉ VALIDÉ. APPLIQUE LES MODIFICATIONS ET GÉNÈRE LE PATCH DE MANIÈRE 100% AUTONOME.
${token_saving_rules}${capitalization_block}${db_block}

CONSIGNE STRICTE D'AUTONOMIE :
Tu ne dois JAMAIS poser de questions, demander de confirmations ou lister des "Suites possibles". Tu es exécuté en tâche de fond non-interactive.
Si le ticket concerne du code métier, du Studio, ou des tests Cucumber (scénarios .feature, step definitions, fichiers de données), TU DOIS CRÉER LA BRANCHE, APPLIQUER TOUTES LES MODIFICATIONS REQUISES ET GÉNÉRER LE FICHIER PATCH (.patch).
N'ajoute AUCUN test Cucumber d'accompagnement à un correctif de code : ils seront produits après le merge par le ticket SPEC "[CUCUMBER] Adaptation des tests post-merge". Ne modifie des fichiers Cucumber que si le ticket a lui-même pour objet des tests Cucumber.

Étapes d'exécution :
1. Pars du cadrage validé '${target_issue}-cadrage.md' : c'est ta source de référence. Ne relis pas Jira et ne refais pas l'analyse ;
   ouvre uniquement les portions utiles des fichiers listés dans sa partie technique.
   Si des patchs validés similaires figurent dans la capitalisation, réutilise leurs conventions (emplacement des fichiers, nommage, structure des .feature) lorsque c'est pertinent.
2. Crée la branche Git de travail '${target_issue}_dev_...' dans le/les dépôts concernés (y compris les dépôts de tests Cucumber client comme '${local_client_repo}').
3. Effectue toutes les modifications requises :
   - Si le ticket demande des tests Cucumber (ex: ajout de features, modification de runners, ajout du tag @DISABLED_...): rédige et modifie directement les fichiers BDD (.feature, .java, .xml, .json).
   - Si un fichier XML Studio est modifié, recalcule impérativement le xmlHash.
4. Génère le fichier patch Git final (ex: '${target_issue}-client.patch' ou '${target_issue}-cucumber.patch').
5. ${release_note_step}
6. Joins le/les fichiers .patch en pièce jointe sur le ticket Jira ${target_issue} via l'outil d'attachement.
7. Termine l'exécution avec un récapitulatif des fichiers modifiés dans le patch.
