TU ES UN AGENT 100 % AUTONOME : TU ÉCRIS UN STEP DU PROJET QUI CHARGE LES FICHIERS DE DONNÉES DE L'INITIALISATION DU CUCUMBER STANDARD, SANS LES COPIER, POUR LE CLIENT ${client_name}.
Tu ne dois JAMAIS poser de questions, demander de confirmation ni proposer de "suites possibles".

=== CONTEXTE ===
L'init du client (worktree '${worktree}') est celle du Standard, avec un seul écart : la purge (clear propre au client). Dans le Standard, le step « ${std_step} » charge les fichiers de données (classeurs .xlsm, CSV…) du dossier qui correspond à celui de la feature qui l'appelle : pour l'init WMS du Standard, `files/WMS/01-Contexte/`. Appelé depuis la feature du client (`features/<client>/01-Contexte/<MOD>/`), il chercherait `files/<client>/01-Contexte/<MOD>/` : il faudrait recopier tous les fichiers du Standard dans le dépôt du client. On veut l'éviter : ces fichiers restent ceux du Standard (bibliothèque `test-jar` du client, ou Cucumber Standard exécuté par le job), et suivent ses évolutions.

=== CE QUE TU ÉCRIS ===
UNE classe '${java_path}' (package ${package}), qui définit le step « ${expression} » : paramètre = code du module (WMS, TMS, OMS). Il fait EXACTEMENT ce que fait « ${std_step} » (mêmes appels, même ordre, mêmes attentes, même contrôle du résultat de chaque fichier), mais sur le dossier de données du Standard pour ce module (`cucumber/files/<MOD>/01-Contexte/`).

=== ATTENTE DE L'INTÉGRATION (constat sur Jenkins) ===
`GS1EndPoints.sendPack(fichiers, true)` attend la fin de l'intégration 360 s au plus (délai FIXE de la bibliothèque) : sur Jenkins, 42 fichiers sur 47 étaient intégrés quand il a abandonné (ConditionTimeoutException, sans état). N'utilise donc PAS l'attente de la bibliothèque : envoie le pack SANS attente (`sendPack(fichiers, false)`), puis suis l'intégration toi-même comme le fait ce step du projet déjà éprouvé (KisWebSocket : getNbIntegration, getFinish, getException, getMessageException ; journal de l'avancement « n/total fichier(s) intégré(s) »), avec un délai de ${delai} s (constante de la classe, surchargeable par la propriété système « cucumber.init.delai »), échec IMMÉDIAT sur une erreur notifiée et échec explicite au délai avec l'état lu :
```java
${exemple_attente}
```

=== ET LES RÈGLES ===
La même classe définit un second step, « ${rules_expression} » : il fait EXACTEMENT ce que fait « ${std_rules_step} » (QARuleStep : lecture de TOUTES les règles .json du dossier, même traitement), mais sur le dossier de règles du Standard pour ce module (`cucumber/rules/<MOD>/01-Contexte/`), au lieu de `rules/<dossier de la feature>/` (constat sur Jenkins : NullPointerException dans QARuleStep, le dossier `rules/<client>/01-Contexte/<MOD>/` n'existant pas). Même résolution que pour les fichiers ci-dessous (disque, sinon classpath extrait sous `target/`, dossier distinct de celui des fichiers).

=== OÙ SONT LES FICHIERS PENDANT LE RUN (constat sur Jenkins) ===
Le job n'a dans son espace QUE le dépôt du client : le dossier `./src/test/resources/cucumber/files/<MOD>/01-Contexte` du Standard N'EXISTE PAS sur le disque (échec constaté : « Dossier de données du Standard introuvable : …/spec/./src/test/resources/cucumber/files/WMS/01-Contexte »). Les fichiers du Standard sont en revanche dans le CLASSPATH : la bibliothèque `test-jar` du client (dépendance a-sis:Cucumber, type test-jar) embarque les ressources de test du Standard sous `cucumber/files/<MOD>/01-Contexte/`.
Donc :
1. cherche d'abord le dossier sur le disque comme le fait le Standard (s'il existe, utilise-le) ;
2. sinon, lis-le dans le classpath (ClassLoader.getResource("cucumber/files/<MOD>/01-Contexte/")) : ressource dans un jar → extrais les fichiers de ce dossier (sous-dossiers compris, noms et ordre conservés) dans un dossier TEMPORAIRE sous `target/` (ex. target/fichiers-standard/<MOD>/01-Contexte), recréé à chaque exécution ; ressource sur le disque → utilise-la directement ;
3. puis appelle l'intégration du Standard sur ce dossier ;
4. introuvable partout : échec explicite qui cite les deux emplacements cherchés.

=== MÉTHODE ===
1. Grep dans '${java_std}' les annotations des steps « ${std_step} » et « ${std_rules_step} » (signatures, jamais de fichier en entier), puis lis sa méthode et les helpers qu'elle appelle pour comprendre comment le dossier de données est déterminé (chemin de la feature courante, ressource du classpath, propriété, paramètre…).
2. Réutilise les classes et méthodes PUBLIQUES du Standard (même classe de base et même injection du contexte que '${clear_step_java}' s'il existe). Une méthode protected ou privée est inutilisable : n'en recopie pas la logique si elle dépend d'un état interne.
3. Si le dossier ne peut être déterminé QUE par l'emplacement de la feature courante, sans aucun moyen public de désigner un autre dossier, n'écris AUCUN fichier et explique pourquoi dans « restantes » (les fichiers seront alors copiés, comme avant).

=== RÈGLES ===
- Jamais d'écriture dans src/test/resources ni ailleurs dans le dépôt : la seule écriture permise est l'extraction temporaire sous `target/`. Aucune dépendance Maven nouvelle, aucun fichier du Standard modifié, aucun autre fichier du worktree modifié.
- Aucune méthode vide, aucun `throw new PendingException()`. Module inconnu ou dossier introuvable à l'exécution : échec explicite avec le chemin cherché.
- N'utilise ni git ni Maven : compilation et contrôles sont relancés automatiquement.

=== ÉCHEC DE LA VERSION PRÉCÉDENTE DU STEP SUR JENKINS ===
${echec_precedent}
Tu écris la classe EN ENTIER, à neuf (l'ancienne version a été retirée du worktree) : ne reprends pas une logique qui a échoué.

=== ERREURS DE LA TENTATIVE PRÉCÉDENTE ===
${erreurs}

=== RÉPONSE FINALE ===
Aucun texte hors du JSON final. Ta réponse finale est UNIQUEMENT ce bloc JSON (cette règle remplace « Réponse finale : 10 lignes maximum ») :
```json
{"fichiers": ["${java_path}"], "resolution": "<comment le step du Standard trouve son dossier, et comment le tien désigne celui du Standard>", "restantes": ["<raison, si aucun fichier n'est écrit>"]}
```
