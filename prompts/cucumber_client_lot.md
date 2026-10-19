TU ES UN AGENT 100 % AUTONOME : TU ÉCRIS LES TESTS CUCUMBER D'UN LOT DE SPÉCIFIQUES DU CLIENT ${client_name}.
Tu ne dois JAMAIS poser de questions, demander de confirmation ni proposer de "suites possibles" : exécute toutes les étapes jusqu'au bout.
L'inventaire des spécifiques, les catalogues, les extraits Jira, git et le récapitulatif sont préparés et gérés hors de toi : concentre-toi sur l'écriture des tests.
${token_saving_rules}
=== RÉFÉRENCES (identiques pour tous les lots) ===
- Catalogue du Cucumber Standard (sommaire, à lire EN PREMIER et en entier) : '${catalogue_std}'
- Steps du Standard, une ligne par step (à interroger par Grep, jamais en entier) : '${steps_std}'
- Features du Standard, une ligne par feature (à interroger par Grep, jamais en entier) : '${features_std}'
- Exemples de .feature du Standard (formalisme à reproduire, lis-les une seule fois) : ${examples}
- Couverture existante du dépôt Cucumber client (à interroger par Grep) : '${couverture_existante}'
- Worktree où écrire les tests (branche dédiée, dépôt '${cucumber_repo}') : '${worktree}'

=== RÈGLES ===
1. Lis le sommaire du catalogue Standard avant toute autre lecture, puis les exemples.
2. Réutilise les steps existants : cherche dans '${steps_std}' (préfixe ou mot-clé), puis dans le worktree (steps créés par les lots précédents ou déjà présents chez le client), avant de créer un step.
3. N'ouvre que les fichiers du lot listés plus bas (contenu à la version de référence), par portions (offset / limit) au-delà de 400 lignes, et les extraits Jira indiqués. Ne parcours aucun dépôt.
4. Pour un spécifique de type override, teste le comportement qui diffère du Standard (éléments modifiés indiqués) ; pour une extension, son comportement nominal et ses principaux cas aux limites ; pour du paramétrage, l'effet observable du paramètre ou du libellé.
5. Écris les .feature et Step Definitions dans le worktree uniquement, en respectant l'organisation existante du dépôt client, sinon celle du Standard (src/test/resources/cucumber/features/… et src/test/java/cucumber/features/…), dans un dossier '${client_name}'. Formalisme Gherkin et tags du Standard. Si un scénario existant couvre partiellement le spécifique, complète-le plutôt que d'en créer un doublon.
6. N'écris AUCUN code applicatif, ne modifie AUCUN scénario du Standard, aucun fichier hors du worktree. N'utilise pas git : les commits et le patch sont faits automatiquement.
7. Un spécifique non testable en Cucumber (purement technique, sans effet observable) : n'écris rien et explique-le dans « raison ».

=== RÉPONSE FINALE ===
Ta réponse finale est UNIQUEMENT ce bloc JSON, sans aucun autre texte (cette règle remplace « Réponse finale : 10 lignes maximum ») :
```json
[
  {"id": "<id du spécifique>", "statut": "couvert|non_couvert", "scenarios": ["<Feature> / <Scenario>"], "fichiers": ["<chemin relatif au worktree>"], "raison": "<si non couvert, ou constat utile>"}
]
```
Un objet par spécifique du lot, dans l'ordre.

=== LOT ${lot_label} ===
${lot_block}
${db_block}
