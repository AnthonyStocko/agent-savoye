# Recettes de test Cucumber (generate-cucumber-client)

Connaissance réutilisable, injectée dans le prompt des lots dont un spécifique correspond (calcul Python, 0 token).
Personnalisable : une copie dans PROMPTS_DIR remplace ce fichier.

Une section « ## <titre> » par cas. Champs (tous facultatifs sauf la consigne ou les steps) :
- Natures : natures d'inventaire concernées (processus, objet métier, libellés, scripts SQL…), séparées par des virgules.
- Mots : mots du libellé, du dossier ou des chemins du spécifique (synonymes FR/EN compris, sans accents).
- Chemins : expression régulière sur le dossier et les chemins des fichiers du spécifique.
- Contenu : expression régulière cherchée dans le contenu des fichiers du spécifique.
- Steps Standard : steps du Cucumber Standard à utiliser, entre `backquotes`.
- Exemple : feature du Standard à prendre pour modèle (nom ou fin de chemin), exportée bornée pour l'agent.
- Statut : statut attendu dans le JSON final quand la recette s'applique.
- Consigne : ce que l'agent doit faire (peut continuer sur les lignes suivantes).
La recette s'applique si la nature du spécifique fait partie de « Natures » (quand renseigné) ET si l'un des critères
Mots, Chemins ou Contenu correspond (quand au moins un est renseigné).

## Process interactif Studio
- Natures : processus
- Mots : interactive, interactif, screen, ecran
- Steps Standard : `KIS- Ouvrir une interaction {string}` ; `KIS- Entrer sur cette interaction` (table `value | submit`, une ligne par saisie) ; `KIS- Fermer cette interaction` ; `KIS- Le message d'erreur sur process interactif contient {string}`
- Exemple : TOW-14.feature
- Consigne : un process interactif se teste en le déroulant écran par écran, comme l'exemple. Prépare les données par les steps Standard du domaine, ouvre l'interaction par son libellé (i18n du process), saisis chaque écran avec la table `value | submit` (valeurs, `$VARIABLE.champ` du contexte, boutons OK / MORE / END), ferme l'interaction, puis vérifie le résultat final : message d'erreur attendu (step dédié) ou état de l'objet métier relu par un step Standard. L'absence de step « process interactif » n'est jamais une raison de non-couverture.

## Champ ou entité modifié sans step dédié
- Contenu : customString\d|customField|customDate\d|customNumber\d|setCustom\w+\(
- Consigne : quand l'effet du spécifique est un champ (customString1…) ou une entité modifiée sans step Standard qui l'expose, crée un step qui relit l'objet par l'endpoint REST Standard de l'entité (Grep dans le fichier des endpoints Standard, puis dans les classes *EndPoints), sur le modèle des classes *EndPoints et de leurs steps, et vérifie la valeur du champ. Pas d'accès base dans les tests, sauf si le Standard en contient déjà un exemple (cite-le).

## Listener / événement sans logique propre
- Natures : processus
- Mots : listener, event
- Statut : couvert_indirectement
- Consigne : un listener ou une définition d'événement qui ne fait que transmettre au process qu'il déclenche n'a pas de test dédié. Réponds « couvert_indirectement » avec « couvert_par » : l'id du spécifique du process déclencheur (dans ce lot ou dans l'inventaire). Si le listener porte une règle propre (filtre, condition, transformation de variables), teste cette règle à travers le process et réponds « couvert ».
