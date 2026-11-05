
=== PREUVE EN BASE OBLIGATOIRE (contrôle automatique) ===
Pour ${ids}, tu as conclu « non_couvert » en invoquant une donnée absente ou « non vérifiable » (${donnees}), sans requête en base. Ce n'est pas accepté : tu as l'accès en lecture seule à la base de l'environnement (bloc d'accès base ci-dessus) et le code de l'application du client et du Standard.
Pour CHAQUE donnée en cause (document de rendu, valeur d'énumération, besoin, paramétrage, champ personnalisé, référentiel…) :
1. trouve la table qui la stocke : Grep dans le code de l'application (client puis Standard) l'entité (@Entity, @Table), le changelog Liquibase ou script SQL, ou le repository de ce type d'objet ; au besoin, liste les tables du schéma et cherche par nom ;
2. interroge-la : requête SELECT ciblée sur le code exact, puis LIKE si rien ;
3. présente : elle existe, écris le test en l'utilisant (sans la recréer) ; absente : crée-la dans le Background si une API du Standard le permet, sinon « non_couvert » avec le blocage « donnee_impossible » et la requête avec son résultat comme preuve.
Cite dans « recherches » une entrée par donnée : {"source": "base", "cherche": "<table> : <requête SQL>", "trouve": "<résultat de la requête>"}. Une requête en erreur se cite avec l'erreur exacte ; « non vérifiable » n'est jamais une conclusion.
Réponds avec le même JSON final, pour ${ids} uniquement.
