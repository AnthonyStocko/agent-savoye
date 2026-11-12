DIAGNOSTIC D'ENVIRONNEMENT, LECTURE SEULE. Tu ne modifies rien, tu ne poses aucune question.

La purge de l'initialisation Cucumber du client ${client_name} (clear de l'init) échoue de façon reproductible sur le
serveur de l'application de test : « ${erreur} ».
Schéma de la base : ${schema}. Table citée : ${table}. Builds en échec : ${builds}.
${logs_block}

Lis UNIQUEMENT les logs du serveur de l'application dans la fenêtre de la purge ci-dessus. Cherche ce qui explique
qu'une ligne de ${table} ait déjà disparu au moment de sa suppression : suppression en cascade par la base,
déclencheur, autre transaction, contrainte, données incohérentes. Ne recopie aucune ligne de log ni aucune valeur
de donnée.

Réponds sur une seule ligne :
« LOGS_PURGE : <ce que montrent les logs, 300 caractères au plus> »
ou « LOGS_PURGE : aucun log lisible (<raison>) ».
