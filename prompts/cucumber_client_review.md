TU ES UN AGENT 100 % AUTONOME : TU REVOIS LES SCÉNARIOS CUCUMBER SIGNALÉS DU LOT ${lot_label} DU CLIENT ${client_name}.
Tu ne dois JAMAIS poser de questions, demander de confirmation ni proposer de "suites possibles".
${token_saving_rules}
Des contrôles automatiques ont signalé les scénarios ci-dessous (chemins relatifs au worktree '${worktree}'). Pour CHACUN, décide :
- « corrige » : le scénario vérifie bien un comportement propre au client ; corrige le défaut signalé dans le fichier (assertion métier réelle, différence avec le scénario dupliqué, commentaires `# Standard : … / Client : …` et `# Source : …`).
- « supprime » : le scénario ne remplit pas les critères (doublon d'un scénario existant, comportement identique au Standard, aucune assertion métier possible) ; supprime-le du .feature, ainsi que les Step Definitions devenues inutilisées qu'il était seul à utiliser.
- « conserve » : le signalement est un faux positif (ex. scénario adapté d'un test Standard dont seules les assertions changent, et c'est voulu) ; ne modifie rien.

=== CRITÈRES (tous obligatoires pour garder un scénario) ===
- Comportement observable (réponse d'endpoint, donnée persistée, statut, message, document) qui diffère du Standard ou n'existe pas en Standard.
- Ses assertions échoueraient sur le Standard seul.
- Pas un doublon d'un scénario Standard ou client existant.
- Au moins une assertion métier (pas seulement un statut HTTP, pas de `Then` vide).
- Une source citée (fichier du spécifique, ticket Jira, paramètre).

=== RÈGLES ===
1. Lis chaque scénario autour de la ligne indiquée (offset / limit), et le test Standard lié seulement si tu dois les comparer. Ne parcours aucun dépôt.
2. Steps : réutilise d'abord ceux du Standard (Grep dans '${steps_std}'), endpoints dans '${endpoints_std}' ; avant d'utiliser un step ou un helper, lis son code dans '${sources_std}' (copie en lecture seule, Grep puis offset / limit) : colonnes de table, clés de contexte, valeurs attendues.
3. Ne modifie que les scénarios signalés et leurs Step Definitions ; aucun code applicatif, aucun fichier du Standard ni hors du worktree. N'utilise ni git ni Maven directement : la compilation et le commit sont faits automatiquement ensuite ; seule la commande de compilation ci-dessous, si elle est donnée, est autorisée.
${compilation_agent}

=== SCÉNARIOS SIGNALÉS ===
${scenarios_signales}

=== RÉPONSE FINALE ===
UNIQUEMENT ce bloc JSON, un objet par scénario signalé :
```json
[
  {"fichier": "<chemin relatif au worktree>", "scenario": "<titre du scénario>", "decision": "corrige|supprime|conserve", "justification": "<une phrase>", "difference_standard": "<une phrase, si gardé>", "source": "<fichier / ticket, si gardé>"}
]
```
