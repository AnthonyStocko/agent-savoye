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
   - **Inventaire des spécifiques** (aucun token) : Python compare chaque dépôt client au Standard. Les dépôts front (`CUCUMBER_EXCLUDE_REPOS`, par défaut `*-frontend`, `*-front`, `*-ui`) sont exclus : le Cucumber Standard ne pilote pas les écrans. Un fichier dont le contenu a déjà existé dans le Standard est un simple écart de version et est ignoré. Les fichiers restants sont classés `override`, `extension` ou `parametrage`, puis regroupés en spécifiques fonctionnels (processus Studio, plugin d'interface, objet métier). Python rapproche ensuite chaque spécifique de la couverture Cucumber existante et de ses tickets Jira. Le tableau permet de choisir les spécifiques à traiter ; les non couverts et partiels sont cochés par défaut. Une estimation du coût s'affiche à partir des lots déjà exécutés.
   - **Génération des tests Cucumber** : les spécifiques cochés sont traités par lots de `CUCUMBER_BATCH_SIZE`, avec un appel Claude neuf par lot (Sonnet par défaut, `--max-turns` `CUCUMBER_MAX_TURNS`, outils Read/Write/Edit/Grep/Glob seulement). L'agent ne reçoit que le sommaire du catalogue Cucumber Standard, les fichiers et diffs résumés du lot, et les extraits Jira utiles. Python crée le worktree, commite après chaque lot, puis produit `<client>-cucumber.patch` et le récapitulatif `<client>-cucumber-couverture.md`. Rien n'est poussé, et le dépôt `<client>-cucumber` doit déjà exister.
   - **Reprendre** : après une interruption (quota, erreur), reprend au premier lot non terminé (`.tmp/cucumber/<client>/etat.json`).
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
| `cucumber_client_lot.md` | Un lot de tests Cucumber des spécifiques d'un client (onglet Cucumber) : partie fixe en tête, lot en fin |
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
