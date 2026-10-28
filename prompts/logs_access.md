=== LOGS APPLICATIFS DE L'ENVIRONNEMENT DE TEST (groundcover, LECTURE SEULE) ===
Ce que l'application a réellement fait pendant le run (exceptions, refus de règles, messages envoyés ou rejetés). Seul accès autorisé (filtre de l'environnement de test imposé, ne cherche pas la clé d'API) :
  ${cli} search --start 2026-10-28T17:27:30Z --end 2026-10-28T17:29:30Z --query "TOW-730" --limit 50
  ${cli} search --start … --end … --query "level:error" --limit 50
Heures en UTC (celles des steps du rapport) ; fenêtre de ${max_minutes} minutes au plus, ${max_lines} lignes au plus ; --query : texte libre (code, référence, nom d'exception) ou filtres gcQL (level:error, workload:<nom>), sans pipe.
Règles :
- Cherche d'abord dans l'extrait déjà fourni (logs-serveur.txt, s'il est cité) ; n'interroge groundcover que pour élargir (autre valeur, autre minute).
- 6 recherches au maximum sur toute l'exécution, chacune ciblée sur la valeur ou l'exception du step en échec, autour de son heure.
- Une cause prouvée par les logs cite la ligne mot pour mot ; ne recopie aucune donnée nominative.
- Si une commande renvoie GROUNDCOVER_ERROR=…, n'insiste pas : poursuis sans les logs et signale-le en une ligne.
=== FIN LOGS APPLICATIFS ===
