TU CAPITALISES UNE RÉUSSITE DE L'AGENT EN COMPÉTENCE RÉUTILISABLE (domaine : ${domaine_libelle}, client ${client_name}).
Tu ne poses aucune question. Tu n'utilises aucun outil : tout est ci-dessous.

=== CE QUI A ÉTÉ RÉSOLU (réussite PROUVÉE : ${preuve}) ===
${reussite}

=== CE QUE L'AGENT A CHANGÉ (extrait du diff) ===
${diff}

=== COMPÉTENCES DÉJÀ ENREGISTRÉES DANS CE DOMAINE ===
${existantes}

=== CONSIGNE ===
Rédige la procédure qui permettrait de résoudre DU PREMIER COUP un cas semblable la prochaine fois, chez ce client ou chez un autre :
- générale : décris la démarche (quoi vérifier, dans quel ordre, quel type de correction, quel step ou quelle donnée), pas seulement les valeurs de ce cas ; les valeurs propres au client ne vont que dans « exemple » ;
- déclencheurs : les messages d'échec ou situations reconnaissables qui doivent la faire appliquer (mots exacts des messages) ;
- si une compétence existante couvre déjà ce cas, action « mettre_a_jour » avec son nom exact, en fusionnant ce que celle-ci apporte ;
- si la réussite est triviale ou propre à ce seul cas (rien de réutilisable), action « aucune » ;
- jamais de mot de passe, jeton, identifiant de connexion ni donnée personnelle.

=== RÉPONSE FINALE ===
UNIQUEMENT ce bloc JSON :
```json
{"action": "creer|mettre_a_jour|aucune", "nom": "<nom court et parlant>", "description": "<ce que résout la compétence, une phrase>", "declencheurs": ["<message ou situation>"], "etapes": ["<étape 1>", "<étape 2>"], "pieges": ["<erreur à éviter>"], "exemple": "<le cas d'origine en deux lignes>"}
```
