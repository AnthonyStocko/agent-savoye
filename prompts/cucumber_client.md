TU ES UN AGENT 100 % AUTONOME : COUVERTURE CUCUMBER DE TOUS LES SPÉCIFIQUES DU CLIENT ${client_name}.
Tu ne dois JAMAIS poser de questions, demander de confirmation ni proposer de "suites possibles" : exécute toutes les étapes jusqu'au bout.
${token_saving_rules}${db_block}
=== CONTEXTE ===
- Dépôt(s) client            : '${local_client_repo}'
- Socle Standard             : '${local_std_repo}' (version Odatio ${odatio_version})
- Dépôt Cucumber Standard    : '${local_cucumber_std_repo}' (référence de formalisme)
- Dépôt Cucumber client      : '${cucumber_repo_dir}' (dépôt '${cucumber_repo}', branche de base 'origin/${cucumber_base_branch}')
- Projet / Application Jira  : ${application}
- Tickets Jira de l'application : ${jira_tickets_file}
- Conventions de développement : '${conventions_path}'

=== ÉTAPES OBLIGATOIRES ===
1. MISE À JOUR : exécute `git -C "${cucumber_repo_dir}" fetch origin --prune`.
2. INVENTAIRE DES SPÉCIFIQUES : identifie dans le dépôt client tout ce qui étend, surcharge ou contourne le Standard (processus Studio XML, services Java, paramètres, écrans, messages). Compare avec le socle Standard pour distinguer le spécifique du standard. Localise avec Grep / Glob, lis par portions (offset / limit) et ignore le purement technique (build, configuration d'environnement, refactoring sans effet fonctionnel).
3. CONTEXTE FONCTIONNEL : pour comprendre le comportement attendu de chaque spécifique, cherche dans les tickets Jira de l'application (Grep sur le fichier ci-dessus : mots-clés métier, noms de processus ou de classes), puis lis uniquement les passages utiles. Si l'accès base est indiqué plus haut, vérifie le paramétrage réel en respectant strictement ses règles.
4. RÉFÉRENCE STANDARD : étudie les .feature et Step Definitions de '${local_cucumber_std_repo}' (formalisme Gherkin, organisation des dossiers, tags, steps réutilisables). Réutilise les steps existants avant d'en créer de nouveaux.
5. ÉCART DE COUVERTURE : repère dans '${cucumber_repo_dir}' les spécifiques déjà couverts par des scénarios, pour ne créer aucun doublon. Un scénario existant incomplet est complété plutôt que dupliqué.
6. BRANCHE DE TRAVAIL ISOLÉE : ne touche pas à la copie de travail actuelle de '${cucumber_repo_dir}'. Crée un worktree :
   git -C "${cucumber_repo_dir}" worktree add "${worktree_root}/CUCUMBER-${client_name}" -b "cucumber_${client_name}_coverage" origin/${cucumber_base_branch}
   (si la branche existe déjà : git -C "${cucumber_repo_dir}" worktree add "${worktree_root}/CUCUMBER-${client_name}" "cucumber_${client_name}_coverage" ; si le worktree existe déjà, réutilise-le tel quel)
7. ÉCRITURE DES TESTS dans ce worktree uniquement : .feature et Step Definitions couvrant chaque spécifique, au formalisme Gherkin du Standard et selon les conventions. N'écris AUCUN code applicatif et ne modifie AUCUN scénario du Standard.
8. PATCH : commit local puis génération du patch (PAS de git push) :
   git -C "${worktree_root}/CUCUMBER-${client_name}" add -A
   git -C "${worktree_root}/CUCUMBER-${client_name}" commit -m "Cucumber coverage of ${client_name} specifics"
   git -C "${worktree_root}/CUCUMBER-${client_name}" format-patch origin/${cucumber_base_branch}..HEAD --stdout > "${workspace_dir}/${client_name}-cucumber.patch"
   (le patch contient tous les commits de la branche : ceux d'une exécution précédente sont inclus si la branche a été réutilisée)
9. RÉCAPITULATIF : écris '${workspace_dir}/${client_name}-cucumber-couverture.md' en Markdown :
   # Couverture Cucumber des spécifiques ${client_name}
   ## Spécifiques couverts
   (tableau | Spécifique | Fichier(s) source | Scénario(s) | Nouveau/Adapté |)
   ## Spécifiques non couverts
   (liste : spécifique, raison — ex. purement technique, non testable en Cucumber, comportement indéterminé)
   Rédaction synthétique, sans verbiage.
10. RÉPONSE FINALE : 10 lignes maximum (nombre de spécifiques identifiés, couverts, non couverts, chemin du patch).

INTERDIT : git push ; toute modification hors du worktree créé à l'étape 6 (dépôts client, Standard, Cucumber Standard compris) ; toute donnée nominative issue de la base dans les scénarios, le patch ou le récapitulatif.
