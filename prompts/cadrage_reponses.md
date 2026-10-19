LE MÉTIER A RÉPONDU AUX QUESTIONS OUVERTES DU TICKET ${target_issue} : COMPLÈTE LE CADRAGE AVEC CES RÉPONSES.

${integration_block}

1. Lis le cadrage existant dans '${cadrage_file}'.
2. Complète-le directement dans ce fichier avec les réponses ci-dessus :
   - Partie 1 (fonctionnelle) : reporte chaque réponse dans le périmètre, la règle de gestion ou le scénario concerné, avec la référence de sa question (ex. « (Q2) »). Supprime les hypothèses ou points en suspens que les réponses tranchent.
   - Partie 2 (technique) : adapte ce que les réponses changent (composants, modifications, stratégie de test). Si une réponse impose une vérification dans le code, ajoute-la dans « Impacts & Stratégie de Test » sous la forme « À vérifier : … » plutôt que de supposer.
   - Ne modifie pas ce que les réponses ne concernent pas. N'ajoute aucune information absente du cadrage, du besoin reformulé ou des réponses.
3. Conserve STRICTEMENT la structure : titre « # Cadrage ${target_issue} », Partie 1 : Analyse Fonctionnelle, Partie 2 : Analyse Technique. Rédaction synthétique.

STOP : modifie UNIQUEMENT '${cadrage_file}'. Ne mets PAS à jour Jira (la description est mise à jour automatiquement à partir du fichier), ne lis ni ne modifie aucun code source, ne génère aucun patch.
Réponse finale : 5 lignes maximum (ce qui a changé dans le cadrage).
