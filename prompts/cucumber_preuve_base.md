
=== PREUVE EN BASE OBLIGATOIRE (contrôle automatique) ===
Tu as l'accès en lecture seule à la base de l'environnement (bloc d'accès base ci-dessus) et le code de l'application du client et du Standard.
${references}${non_couverts}
Méthode, pour CHAQUE donnée ou code en cause :
1. trouve la table qui la stocke : Grep dans le code de l'application (client puis Standard) l'entité (@Entity, @Table), le changelog Liquibase ou script SQL, ou le repository de ce type d'objet ; au besoin, liste les tables du schéma et cherche par nom ;
2. interroge-la : requête SELECT ciblée sur le code exact, puis LIKE si rien ;
3. présente : utilise-la (sans la recréer) ; absente : remplace un code de fichier par un code existant et cohérent de la base, ou crée la donnée dans le Background si une API du Standard le permet, sinon « non_couvert » avec le blocage « donnee_impossible » et la requête avec son résultat comme preuve.
Cite dans « recherches » une entrée par requête : {"source": "base", "cherche": "<table> : <requête SQL>", "trouve": "<résultat de la requête>"}. Une requête en erreur se cite avec l'erreur exacte ; « non vérifiable » n'est jamais une conclusion.
Réponds avec le même JSON final, pour ${ids} uniquement.
