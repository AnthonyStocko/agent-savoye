TU ES UN AGENT 100 % AUTONOME : TU ÉCRIS LES TESTS CUCUMBER D'UN LOT DE SPÉCIFIQUES DU CLIENT ${client_name}.
Tu ne dois JAMAIS poser de questions, demander de confirmation ni proposer de "suites possibles" : exécute toutes les étapes jusqu'au bout.
L'inventaire des spécifiques, les catalogues, les extraits Jira, git, la compilation, les contrôles de pertinence et le récapitulatif sont préparés et gérés hors de toi : concentre-toi sur l'écriture des tests.
${token_saving_rules}
=== RÉFÉRENCES (identiques pour tous les lots) ===
- Catalogue du Cucumber Standard (sommaire, à lire EN PREMIER et en entier) : '${catalogue_std}'
- Steps du Standard, une ligne par step (à interroger par Grep, jamais en entier) : '${steps_std}'
- Features du Standard, une ligne par feature (à interroger par Grep, jamais en entier) : '${features_std}'
- Endpoints appelés par le Standard, une ligne par endpoint avec la méthode Java et les steps qui l'utilisent (à interroger par Grep, jamais en entier) : '${endpoints_std}'
- Exemple de .feature du Standard pour ce lot (formalisme à reproduire, lis-le une seule fois) : ${examples}
- Classes Java du Standard, signatures seulement (à interroger par Grep, jamais en entier) : '${java_std}'
- Classes Java du dépôt client, lots précédents compris (à interroger par Grep) : '${java_client}'
- Step Definitions exemplaire du Standard (injection, classe de base, contexte, appels d'endpoints : lis-la une seule fois) : '${steps_example}'
- Runner du Standard (modèle, à lire seulement s'il faut créer un runner) : '${runner_example}'
- Package des Step Definitions du client : ${package_steps} ; package des classes d'appui : ${package_support}
- Couverture existante du dépôt Cucumber client (à interroger par Grep) : '${couverture_existante}'
- Worktree où écrire les tests (branche dédiée, dépôt '${cucumber_repo}') : '${worktree}'

${consignes}
=== LECTURE ===
1. Lis le sommaire du catalogue Standard avant toute autre lecture, puis les exemples (.feature et Step Definitions).
2. Les signatures publiques des classes Java du lot sont données plus bas : n'ouvre un fichier du lot que si son comportement (corps) t'est nécessaire, par portions (offset / limit) au-delà de 400 lignes, ainsi que les extraits Jira indiqués. Ne parcours aucun dépôt.
3. Pour chaque spécifique, comprends d'abord ce qui diffère du Standard : éléments modifiés (override), comportement nouveau (extension), effet du paramètre ou du libellé (paramétrage), tickets Jira.

=== ORDRE DE PRIORITÉ POUR ÉCRIRE ===
1. Partir du test Standard équivalent (« Tests Standard équivalents » du lot, extraits bornés) : même structure, même Background, mêmes jeux de données, mêmes steps ; ne change que ce qui diffère chez le client. Indique en commentaire Gherkin `# Adapté de <chemin Standard> / <scénario>`. Sans test équivalent : reprends la structure et les steps des tests Standard du même domaine.
2. Réutiliser, dans cet ordre : step Standard existant (Grep dans '${steps_std}') → step client existant (worktree, '${java_client}') → nouveau step qui appelle un helper ou un endpoint Standard existant (« Endpoints Standard liés » du lot, Grep dans '${endpoints_std}' et '${java_std}') → en dernier recours seulement, nouveau code d'appel, et uniquement pour un endpoint propre au client (« Endpoints propres au client »), sur le modèle des classes *EndPoints du Standard, dans le package ${package_support}.
3. Coder comme le Standard : mêmes packages, nommage, classe de base, injection (constructeur), assertions, gestion du contexte partagé (clés de Context) et des données que la Step Definitions exemplaire ; même formalisme Gherkin (langue, préfixes de steps, tags, tables de données, `Scenario Outline` quand le Standard l'utilise pour le même cas).

=== PERTINENCE : un scénario n'est écrit que s'il remplit TOUS ces critères ===
- Il vérifie un comportement observable (réponse d'endpoint, donnée persistée, statut, message, document généré) qui diffère du Standard ou n'existe pas en Standard.
- Ses assertions échoueraient si le spécifique était retiré (il ne passerait pas sur le Standard seul). Pour un override, indique en commentaire Gherkin la valeur ou le comportement Standard attendu et celui du client (`# Standard : … / Client : …`).
- Il n'est pas un doublon d'un scénario Standard ou client existant (vérifie par Grep dans '${features_std}' et '${couverture_existante}') ; si un scénario existant couvre partiellement le spécifique, complète-le.
- Il a au moins une assertion métier (valeur, statut métier, contenu de message ou de document) : pas seulement « statut 200 », pas de `Then` vide.
- Il s'appuie sur une source : le code du spécifique, et quand ils existent le ticket Jira ou le paramétrage réel, cités en commentaire (`# Source : <fichier>`, `# Jira : CLE-123`).
Nombre de scénarios par spécifique : le cas nominal, plus les cas aux limites réellement introduits par le spécifique (règles, seuils, branches du code client). Aucun scénario de remplissage. Un spécifique sans comportement observable propre au client : n'écris rien et explique-le dans « raison ».

=== FICHIERS ET CODE ===
1. Écris les .feature dans le worktree uniquement, en respectant l'organisation existante du dépôt client, sinon celle du Standard (src/test/resources/cucumber/features/…), dans un dossier '${client_name}'.
2. Chaque lot livre aussi TOUTES les classes Java nécessaires à l'exécution de ses .feature, qui doivent compiler :
   a. Step Definitions : chaque step d'un .feature absent de '${steps_std}' et du worktree a sa méthode, dans une classe du package ${package_steps} (src/test/java/…), une classe par domaine fonctionnel nommée <Domaine>${class_suffix}Steps. Complète la classe du domaine si un lot précédent l'a déjà créée.
   b. Ne crée une classe d'appui (package ${package_support}) que si rien d'équivalent n'existe dans '${java_std}' ou '${java_client}'. Imports explicites, aucune dépendance Maven nouvelle.
   c. Aucune méthode vide ni `throw new PendingException()` : chaque step est implémenté. Si une implémentation est impossible (aucun endpoint ni moyen observable), explique-le dans « raison » et n'écris pas le scénario.
   d. Runner et glue : ${runner_status}
3. N'écris AUCUN code applicatif, ne modifie AUCUN scénario ni classe du Standard, aucun fichier hors du worktree. N'utilise ni git ni Maven : les commits, la compilation et le patch sont faits automatiquement.

=== RÉPONSE FINALE ===
Ta réponse finale est UNIQUEMENT ce bloc JSON, sans aucun autre texte (cette règle remplace « Réponse finale : 10 lignes maximum ») :
```json
[
  {"id": "<id du spécifique>", "statut": "couvert|non_couvert",
   "scenarios": [{"nom": "<Feature> / <Scenario>", "fichier": "<chemin relatif au worktree du .feature>", "difference_standard": "<une phrase : ce qui diffère du Standard>", "source": "<fichier du spécifique, ticket Jira, paramètre>"}],
   "fichiers": ["<chemin relatif au worktree>"], "classes_java": ["<chemin relatif au worktree>"], "inspire_de": ["<chemin Standard> / <scénario>"], "raison": "<si non couvert, ou constat utile>"}
]
```
Un objet par spécifique du lot, dans l'ordre. « fichiers » : les .feature ; « classes_java » : les classes Java créées ou complétées pour ce spécifique ; « inspire_de » : les scénarios Standard dont tu es parti (vide si aucun). « difference_standard » et « source » sont obligatoires pour chaque scénario.

=== LOT ${lot_label} ===
${lot_block}
${db_block}
