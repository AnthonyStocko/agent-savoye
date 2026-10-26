TU ES UN AGENT 100 % AUTONOME : TU ÉCRIS LES STEPS DE RECRÉATION DES DONNÉES D'INITIALIZERS SERVEUR DU CLIENT ${client_name}, EFFACÉES PAR LE CLEAR DE SON INITIALISATION CUCUMBER.
Tu ne dois JAMAIS poser de questions, demander de confirmation ni proposer de "suites possibles".

=== CONTEXTE ===
Au démarrage, le serveur du client exécute des initializers Java qui créent des données. Le clear de l'initialisation Cucumber les efface et rien ne les recrée. Les features d'initialisation du worktree '${worktree}' appellent déjà, après l'initialisation globale par fichiers, les steps ci-dessous : il manque leurs définitions Java.

=== STEPS À DÉFINIR (expression exacte, donnée, initializer d'origine et sa source) ===
${donnees}

=== ERREURS DE LA TENTATIVE PRÉCÉDENTE ===
${erreurs}

=== RÈGLES ===
1. Écris UNE classe '${java_path}' (package ${package}), sur le modèle de '${clear_step_java}' (même classe de base, même injection du contexte) : une méthode par expression, annotation @Given avec l'expression EXACTE ci-dessus (paramètre {string} : le code).
2. Chaque méthode reproduit ce que fait l'initializer pour ce code : lis sa source (fichier indiqué, méthode initialize() et ce qu'elle appelle) et reprends les MÊMES valeurs (code, libellé, booléens, rattachements). Javadoc de la méthode : « Recrée <entité> comme l'initializer <classe> (<dépôt>) ».
3. Idempotente : vérifie d'abord si la donnée existe (lecture par l'endpoint REST du Standard) et ne la crée que si elle est absente ; une seconde exécution ne doit rien changer ni échouer.
4. Utilise uniquement les endpoints et helpers du Cucumber Standard : cherche-les par Grep dans '${java_std}' (signatures, jamais en entier) ; Step Definitions exemplaire : '${steps_example}'. Aucune dépendance Maven nouvelle, aucun code applicatif, aucun fichier du Standard modifié.
5. Aucune méthode vide, aucun `throw new PendingException()`, aucune assertion inventée. Si une donnée n'est pas recréable par l'API, n'écris pas sa méthode et explique-le dans « restantes ».
6. Ne modifie ni les features ni aucun autre fichier du worktree. N'utilise ni git ni Maven : compilation et contrôles sont relancés automatiquement.

=== RÉPONSE FINALE ===
Aucun texte hors du JSON final. Ta réponse finale est UNIQUEMENT ce bloc JSON (cette règle remplace « Réponse finale : 10 lignes maximum ») :
```json
{"ecrites": <nombre>, "restantes": ["<expression : raison>"]}
```
