# Agent Scrum Master

Agent d'assistance aux développements Odatio. À partir d'un ticket Jira, il produit l'analyse (cadrage fonctionnel et technique), puis le patch Git et la release note, et les publie sur le ticket. Il s'appuie sur la CLI [Claude Code](https://docs.claude.com/fr/docs/claude-code/overview).

- **Intégration du besoin** : reformulation du besoin et questions ouvertes ajoutées à la description Jira. Les réponses saisies dans l'IHM y sont ajoutées à leur tour.
- **Analyse / cadrage** d'un ticket : la description Jira est mise à jour avec l'analyse.
- **Révision** de l'analyse selon vos consignes. Vos corrections sont mémorisées et réutilisées sur les tickets suivants.
- **Patch** : création de la branche, modifications, fichier `.patch` attaché au ticket et release note ajoutée à la description.
- **Épopées** : découpage en tâches enfants créées dans Jira, puis un patch par tâche.
- **Post-merge Cucumber** : à chaque merge Bitbucket, création d'un ticket `[CUCUMBER]` avec les scénarios BDD à adapter et le patch correspondant.

## Prérequis

| Outil | Usage |
|---|---|
| **Python 3.12** | Application (Streamlit) et outils |
| **Git** | Clones, branches et patchs |
| **Claude Code CLI** (`claude`), connectée | Exécution de l'agent |
| **JDK 21 + Maven** (recommandé) | Permet à l'agent de compiler et tester les patchs Java |
| **Pilote ODBC Microsoft** (facultatif) | Uniquement pour une base SQL Server via `pyodbc` |

Comptes et accès nécessaires : Jira Cloud (jeton API), Bitbucket KISCloud (jeton HTTP, droits lecture) et, en option, Jenkins et un compte base de données en lecture seule.

### Installer Claude Code

```powershell
irm https://claude.ai/install.ps1 | iex   # ou : npm install -g @anthropic-ai/claude-code
claude                                    # première exécution : connexion (/login)
```

L'agent utilise l'abonnement Claude de la CLI connectée. Pour passer par une clé API, renseignez `ANTHROPIC_API_KEY`.

## Installation

```powershell
git clone https://github.com/AnthonyStocko/agent-savoye.git
cd agent-savoye
py -3.12 -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

Complétez ensuite le `.env`, ou lancez l'application et utilisez l'onglet **⚙️ Configuration**. Les variables indispensables sont `JIRA_DOMAIN`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY`, `BITBUCKET_URL`, `BITBUCKET_HTTP_TOKEN` et `BITBUCKET_STD_REPO_PATH`. L'onglet Lancement signale celles qui manquent.

À personnaliser pour chaque utilisateur :

- `JIRA_OPEN_EPICS_TEAM` et `POSTMERGE_JIRA_COMPONENT` : votre équipe Atlassian (ex. `oda_Prenom_N`).
- `JIRA_SEARCH_TEAM`, `JIRA_SEARCH_STATUSES` et `JIRA_SEARCH_LABELS` : vos critères de recherche habituels.
- `BITBUCKET_STD_REPO_PATH` : URL du projet Bitbucket (clonée automatiquement dans `.tmp/`) ou chemin d'un clone local.

## Utilisation

```powershell
venv\Scripts\python.exe -m streamlit run app.py
```

1. **🧩 Intégration** : recherchez les tickets (projet, équipe, statuts, labels) ; choisissez un ticket puis **Rédaction du besoin**. L'agent reformule le besoin et liste les questions dont la réponse ne figure pas dans l'expression initiale. Pour cela, il consulte aussi tous les tickets Jira (tous projets) du même « Projet / Application » que le ticket, par exemple CANSON : champ `JIRA_APPLICATION_FIELD`, valeur modifiable dans l'onglet. Le tout est publié dans la section « Intégration du besoin » de la description Jira, le reste de la description restant intact. Répondez aux questions dans l'IHM puis cliquez sur **Envoyer les réponses et compléter le besoin**. Les réponses sont d'abord ajoutées sous leur question dans la description Jira. L'agent complète ensuite le besoin reformulé avec ces réponses et ne garde que les questions restantes. Si le ticket a déjà un cadrage (`<TICKET>-cadrage.md`), l'agent le complète aussi avec les réponses, puis le cadrage est remplacé dans la description Jira (le reste de la description est conservé). Questions et réponses sont conservées dans `.tmp/integration/`.
2. **🚀 Lancement** : recherchez les tickets (projet, équipe, statuts, labels), choisissez le ticket et le dépôt client, puis lancez l'analyse ou le patch. Pour le projet `OXY`, seul le socle Standard (branche `dev`) est utilisé.
3. **🥒 Cucumber** : saisissez le dépôt Bitbucket du projet client (le nom du client, donc le dépôt cible `<client>-cucumber`, en est déduit et reste modifiable), puis éventuellement le schéma BDD et le Projet / Application Jira (ex. `CANSON`). Le champ **Dépôt Bitbucket du Cucumber Standard** (défaut : `BITBUCKET_CUCUMBER_STD_REPO`) accepte une URL de navigation, une URL clonable ou le chemin d'un clone local, utilisé tel quel (fetch seulement). Chaque dépôt distant a son clone, `.tmp/cucumber_std/<projet>_<dépôt>/`, recloné si son `origin` change. Après `fetch`, il est détaché sur la première référence distante existante (`origin/release/<version>`, `origin/v<version>`, `origin/<version>`, tags de la version, puis `origin/dev`, `origin/main`, `origin/master`) ; la référence et le commit retenus s'affichent sous le champ.
   - **Inventaire des spécifiques** (aucun token) : Python compare chaque dépôt client au Standard. Les dépôts front (`CUCUMBER_EXCLUDE_REPOS`, par défaut `*-frontend`, `*-front`, `*-ui`) sont exclus : le Cucumber Standard ne pilote pas les écrans. Un fichier dont le contenu a déjà existé dans le Standard est un simple écart de version et est ignoré. Les fichiers restants sont classés `override`, `extension` ou `parametrage`, puis regroupés en spécifiques fonctionnels (processus Studio, plugin d'interface, objet métier). Python rapproche ensuite chaque spécifique de la couverture Cucumber existante, de ses tickets Jira et des tests du Cucumber Standard qui testent le même processus : au plus 3 features par spécifique, score ≥ `CUCUMBER_STD_MATCH_MIN` (0,4 par défaut). Le score combine les mots-clés du spécifique retrouvés dans la feature et dans les Step Definitions qu'elle utilise (noms de classes, endpoints, chemins REST), l'objet métier ou l'élément overridé, et le domaine. Ces features sont extraites dans `.tmp/cucumber/<client>/std/`, bornées à 150 lignes. La colonne « Tests Standard liés » du tableau et son détail repliable montrent, avant de lancer, les spécifiques sans équivalent. Le tableau permet de choisir les spécifiques à traiter ; les non couverts et partiels sont cochés par défaut. Une estimation du coût s'affiche à partir des lots déjà exécutés.
   - **Génération des tests Cucumber** : les spécifiques cochés sont traités par lots de `CUCUMBER_BATCH_SIZE`, avec un appel Claude neuf par lot (Sonnet par défaut, `--max-turns` `CUCUMBER_MAX_TURNS`, outils Read/Write/Edit/Grep/Glob seulement). L'agent ne reçoit que le sommaire du catalogue Cucumber Standard, les signatures Java du Standard et du dépôt client (jamais les corps), les fichiers, diffs résumés et signatures publiques du lot, les extraits Jira utiles, les tests Standard équivalents et les endpoints liés. Le catalogue des endpoints du Cucumber Standard (`endpoints_std.md`, une ligne par endpoint : méthode, chemin, classe Java qui l'encapsule, steps qui l'utilisent) est extrait sans token des classes `*EndPoints` ; chaque spécifique reçoit les endpoints Standard qu'il expose, surcharge ou dont le chemin nomme son objet métier, et les endpoints propres à ses contrôleurs. L'ordre de priorité est imposé : partir du test Standard équivalent (override : adapter le scénario Standard, commentaire `# Adapté de …`), puis réutiliser step Standard → step client → nouveau step sur un helper / endpoint Standard → nouveau code d'appel pour un endpoint propre au client seulement, et coder comme le Standard. Un scénario n'est écrit que s'il vérifie un comportement observable propre au client, échouerait sur le Standard seul, n'est pas un doublon, porte une assertion métier et cite sa source ; le JSON du lot donne pour chaque scénario `difference_standard` et `source`. L'exemple de `.feature` du lot est la feature Standard la mieux classée du lot. Chaque lot livre aussi les Step Definitions et classes d'appui nécessaires. Après chaque lot, Python contrôle les nouveaux scénarios : doublon d'un scénario Standard ou client (similarité du titre ou des steps ≥ `CUCUMBER_DUP_THRESHOLD`, 0,85 par défaut ; les scénarios de structure comme « Description de la fiche de test » sont ignorés), absence de `Then` ou `Then` génériques (statut HTTP), `difference_standard` ou `source` vides, taux de steps Standard réutilisés. Les scénarios signalés passent dans un seul appel de revue (`cucumber_client_review.md`, `--max-turns` `CUCUMBER_REVIEW_MAX_TURNS`, 8 par défaut) qui les corrige, les supprime ou les conserve en le justifiant. Après chaque lot, Python compile (`CUCUMBER_COMPILE_CMD`, par défaut `mvn -q -o test-compile` ; dépôt client sans `pom.xml` : compilé par-dessus une copie du Standard dans `.tmp/cucumber/<client>/build`). En cas d'erreurs, jusqu'à `CUCUMBER_FIX_ATTEMPTS` appels de correction limités ont lieu ; si les erreurs persistent, le lot est commité quand même et passe en `erreur_compilation`. Avec `CUCUMBER_DRY_RUN=1`, un dry-run Cucumber vérifie en plus que chaque step du lot a une définition. Python crée le worktree, commite après chaque lot, puis produit `<client>-cucumber.patch` et le récapitulatif `<client>-cucumber-couverture.md` (en tête, taux de réutilisation des steps Standard et scénarios supprimés à la revue avec leur motif ; par spécifique, scénarios, différence avec le Standard, source, test Standard d'inspiration, steps réutilisés / créés, endpoints et compilation). Le tableau des lots de l'onglet affiche ces indicateurs par lot. Rien n'est poussé, et le dépôt `<client>-cucumber` doit déjà exister.
   - **Vérification par exécution** (`tools/cucumber_verify.py`, sans token) : un scénario n'est gardé dans le patch que s'il est valide (il passe sur le client) et discriminant. Chaque scénario reçoit un identifiant stable `<feature>::<scénario>` et les tags `@agent_<client>_<lot>` et `@agent_<client>_<lot>_<n>`. Au plus 3 exécutions par lot : sur le client (`CUCUMBER_VERIFY_ENV`) ; sur le Standard pour les overrides (`CUCUMBER_VERIFY_ENV_STD`, échec attendu sur un `Then`) ; et sur le client avec les attendus des `Then` faussés par Python (chaînes `_MUT`, nombres +1, booléens inversés, une ligne de données sur deux), échec attendu. Statuts : `pertinent`, `faible` (mutation non détectée, ou passe sur le Standard), `invalide` (échoue sur le client), `non_verifie` (jamais compté pertinent). Les scénarios invalides ou faibles passent dans au plus `CUCUMBER_VERIFY_FIX_ATTEMPTS` appels de correction (`cucumber_client_fix.md`), sont recompilés et revérifiés seuls ; ceux qui le restent sont retirés du patch vers `<client>-cucumber-a-revoir.patch`, avec leur motif. Exécution par un job Jenkins dédié `CUCUMBER_VERIFY_JOB` (paramètres `PATCH` en fichier, `TAGS`, `ENV`, vérifiés avant usage ; aucun push), proposé dans `jenkins/CUCUMBER-VERIFY-PATCH.Jenkinsfile` car aucun job existant ne prend un patch. Sinon, repli local `CUCUMBER_VERIFY_LOCAL_CMD` (`{tags}`, `{env}`), durée bornée par `CUCUMBER_VERIFY_TIMEOUT`. Sans l'un ni l'autre, la génération s'arrête au contrôle statique et affiche « pertinence non vérifiée par exécution ». Avec `CUCUMBER_JACOCO=1`, la part des classes de chaque spécifique exécutées est calculée à partir du `jacoco.exec` (classes seulement ; « non exercé » à 0 %).
   - **Revue humaine** : l'onglet affiche chaque scénario vérifié (statut, niveaux statique / client / Standard / mutation, différence avec le Standard, source, test Standard d'inspiration, lien vers le build), et un échantillon de `CUCUMBER_REVIEW_SAMPLE` scénarios pertinents à juger (**Pertinent** / **Non pertinent** avec motif). Un verdict « Non pertinent » devient une consigne du projet (mémoire de l'agent), injectée dans les lots suivants. Le récapitulatif donne en tête les taux de scénarios pertinents, de mutations détectées, d'échec des overrides sur le Standard, de validation humaine, et les scénarios retirés avec leur motif.
   - **Reprendre** : après une interruption (quota, erreur), reprend au premier lot non terminé (`.tmp/cucumber/<client>/etat.json`). Un lot en `erreur_compilation` ne relance que la correction.
4. **🔔 Écoute des merges** : démarre et arrête l'écoute périodique des merges Bitbucket (post-merge Cucumber).
5. **⚙️ Configuration** : toutes les variables du `.env`, le test de connexion à la base et la gestion de la mémoire de l'agent.

Sans IHM :

```powershell
venv\Scripts\python.exe main.py --issue SPEC-1234 --project SPEC --client-repo https://git.kiscloud.io/projects/NOVALIA --step cadrage
venv\Scripts\python.exe main.py --step generate-cucumber-client --client-repo https://git.kiscloud.io/projects/NOVALIA --application CANSON --inventory-only   # inventaire seul, sans token
venv\Scripts\python.exe main.py --step generate-cucumber-client --client-repo https://git.kiscloud.io/projects/NOVALIA --application CANSON --db-schema NOVALIA   # tests des spécifiques non couverts (client déduit : novalia)
venv\Scripts\python.exe main.py --step generate-cucumber-client --client-repo https://git.kiscloud.io/projects/NOVALIA --resume   # reprise des lots non terminés
venv\Scripts\python.exe webhook_server.py --poll --projects NOVALIA   # écoute des merges
```

Les livrables (`<TICKET>-cadrage.md`, `<TICKET>-*.patch`, `<TICKET>-release-note.md`…) sont écrits à la racine du projet. Les clones, la mémoire et les journaux sont écrits dans `.tmp/`. Aucun de ces fichiers n'est versionné.

## Personnaliser les prompts

Les consignes envoyées à l'agent sont dans `prompts/` :

| Fichier | Étape |
|---|---|
| `besoin.md`, `cadrage_reponses.md` | Rédaction du besoin et questions ouvertes, cadrage complété avec les réponses (onglet Intégration) |
| `cucumber_client_lot.md`, `cucumber_client_review.md`, `cucumber_client_fix.md` | Un lot de tests Cucumber des spécifiques d'un client (onglet Cucumber) : partie fixe en tête, lot en fin ; revue des scénarios signalés par les contrôles ; correction des erreurs de compilation et des scénarios invalides ou faibles après exécution |
| `cadrage.md`, `refine.md` | Analyse d'un ticket, révision |
| `epic_cadrage.md`, `epic_revision.md` | Découpage d'une épopée |
| `patch.md`, `patch_cucumber.md` | Génération du patch (sans ou avec tests Cucumber) |
| `postmerge.md` | Ticket et patch Cucumber post-merge |
| `token_saving_rules.md`, `db_access.md`, `cucumber_enabled.md`, `cucumber_disabled.md`, `release_note.md` | Blocs communs |

Les variables s'écrivent `${nom}`, par exemple `${target_issue}`. Pour écrire un `$` littéral, utilisez `$$`. Si un modèle contient une variable inconnue, l'exécution s'arrête et le message liste les variables disponibles.

Pour adapter les prompts sans toucher au dépôt, renseignez `PROMPTS_DIR` avec un dossier personnel. Tout `<nom>.md` présent dans ce dossier remplace le modèle du même nom, et les autres modèles restent ceux du projet.

Les conventions de développement lues par l'agent sont dans `odatio-specific-code-conventions-dev.md`. Pour en utiliser d'autres, renseignez `DEV_CONVENTIONS_PATH`.

## Mise à jour

```powershell
git pull
venv\Scripts\python.exe -m pip install -r requirements.txt
```

Comparez ensuite votre `.env` avec `.env.example` pour repérer les nouvelles variables.
