=== LOGS SERVEUR DE L'ENVIRONNEMENT DE TEST (Groundcover, serveur MCP, LECTURE SEULE) ===
Ce que l'application a réellement fait pendant le run (exceptions, refus de règles, messages envoyés ou rejetés). Seul serveur MCP chargé : Groundcover ; outils autorisés (lecture) : ${outils}.
Application de test : ${application}.
Fenêtre de chaque échec (heure UTC du step en échec −${marge} s / +${marge} s) :
${fenetres}
Règles :
- Cherche d'abord dans l'extrait déjà fourni (logs-serveur.txt, s'il est cité) ; interroge Groundcover pour ce qu'il ne couvre pas.
- Une requête par fenêtre, filtrée sur ce namespace, ce conteneur (et ce pod s'il est cité), triée par heure, 100 lignes au plus ; n'élargis (±2 minutes au plus) que si la fenêtre est vide. 6 requêtes au maximum sur tout l'appel.
- Une cause prouvée par les logs cite la ou les lignes mot pour mot, horodatage compris ; remplace tout mot de passe, jeton ou clé par *** et ne recopie aucune donnée nominative.
- Si un outil Groundcover échoue (authentification, session expirée, serveur injoignable) : n'insiste pas, écris sur une ligne seule « ${marqueur} : <raison> », poursuis sans ces logs et indique dans « restantes » l'export manuel à faire (fenêtre ci-dessus).
=== FIN LOGS SERVEUR ===
