=== ACCÈS BASE DE DONNÉES CLIENT (LECTURE SEULE) ===
Base ${db_type} du client, schéma cible : ${db_schema}. Les commandes ci-dessous sont le SEUL accès autorisé à la base
(aucun autre client SQL, aucune connexion directe, ne recherche pas les identifiants de connexion) :
  ${base_cmd} tables --like "MOTIF%"
  ${base_cmd} describe NOM_TABLE
  ${base_cmd} query "SELECT col1, col2 FROM nom_table WHERE ..." --limit 20
Utilité : vérifier la structure réelle des tables (colonnes, types, clés), le paramétrage, les valeurs de référence,
la volumétrie (COUNT) et ${db_usage}.
Règles :
- 8 commandes au maximum sur toute l'exécution ; requêtes avec colonnes nommées (jamais SELECT *) et clause WHERE.
- ${schema_hint}
- Restitue tes constats (tables et colonnes vérifiées, paramétrage, volumétrie) dans ${db_findings_target}.
- Ne recopie JAMAIS de données nominatives (noms, adresses, e-mails, téléphones, identifiants de personnes…) ${db_private_targets} : décris-les de façon anonyme (« un client », « la commande concernée »).
- Si une commande renvoie DB_ERROR=…, n'insiste pas : poursuis sans la base et signale-le en une ligne.
=== FIN ACCÈS BASE DE DONNÉES ===
