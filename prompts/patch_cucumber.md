LE CADRAGE A ÉTÉ VALIDÉ PAR L'UTILISATEUR. GÉNÈRE MAINTENANT LE PATCH ET LES TESTS CUCUMBER POUR ${target_issue}.
${token_saving_rules}${capitalization_block}${db_block}
Consignes :
1. Pars du cadrage validé '${target_issue}-cadrage.md' : c'est ta source de référence. Ne relis pas Jira et ne refais pas l'analyse ;
   ouvre uniquement les portions utiles des fichiers listés dans sa partie technique.
   Si des patchs validés similaires figurent dans la capitalisation, réutilise leurs conventions (emplacement des fichiers, nommage, structure des .feature) lorsque c'est pertinent.
2. Crée la branche de travail '${target_issue}_dev_...' (depuis 'integration' ou 'dev' selon le projet).
3. Applique les modifications requises dans le code source (SOCLE ou CLIENT).
4. INSPECTION & ADAPTATION CUCUMBER :
   - Consulte le dépôt Cucumber Standard ('${local_cucumber_std_repo}') pour référence.
   - Ajoute ou mets à jour les scénarios Cucumber (.feature) et Step Definitions nécessaires dans le dépôt client/std pour couvrir la correction.
5. Si un fichier XML Studio est modifié, recalculer le xmlHash.
6. Génère le/les fichiers patch Git incluant le code ET les tests Cucumber ('${target_issue}-client.patch' ou '${target_issue}-std.patch').
7. ${release_note_step}
8. Joins le ou les fichiers patch générés en pièce jointe sur le ticket Jira ${target_issue} via l'outil d'attachement.
