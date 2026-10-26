L'UTILISATEUR A DEMANDÉ DES AJUSTEMENTS SUR LE CADRAGE DU TICKET ${target_issue}.
Consignes utilisateur : "${feedback}"
${token_saving_rules}${capitalization_block}${knowledge_block}${db_block}

1. Lis le cadrage existant dans '${target_issue}-cadrage.md'.
2. Révise et adapte le cadrage dans '${target_issue}-cadrage.md' selon les consignes utilisateur.
   Ne ré-explore le code source (ni la base) QUE si la consigne utilisateur l'exige (vérifier un comportement, préciser un fichier ou un composant) ;
   sinon, travaille uniquement à partir du cadrage existant.
3. Veille STRICTEMENT à conserver la structure en 2 parties :
   - **Partie 1 : Analyse Fonctionnelle** (lisible et compréhensible par des consultants métier non-techniques, sans jargon).
   - **Partie 2 : Analyse Technique** (détails techniques ciblés et explication des modifications pour les développeurs).
4. Garde une rédaction synthétique, claire et sans verbiage superflu.
5. Mets à jour la description du ticket Jira ${target_issue} avec la nouvelle version (en conservant le texte d'origine au début).
STOP : Ne génère AUCUN patch pour l'instant.
