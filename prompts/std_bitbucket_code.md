Le code du Standard se lit UNIQUEMENT dans Bitbucket, à jour (dernier commit de la branche de base de chaque dépôt), avec cet outil — jamais dans une copie locale, qui pourrait dater de la veille :
  ${cli} repos --like TEXTE                       dépôts du projet
  ${cli} search "TEXTE"                           recherche de code (classe, méthode, libellé…)
  ${cli} files DEPOT --prefix DOSSIER/ --like TEXTE   fichiers d'un dépôt
  ${cli} show DEPOT CHEMIN --from N --to M        contenu d'un fichier (numéros de ligne)
  ${cli} grep DEPOT "REGEX" --prefix DOSSIER/ --ext .java   occurrences dans un dépôt
Lance chaque commande seule, telle quelle (sans cd, pipe, && ni redirection).
Étape patch uniquement : `${cli} checkout DEPOT` crée (ou met à jour) la copie locale du SEUL dépôt à modifier, sous '${work_root}', sur sa branche de base à jour ; crée ta branche de travail et fais tes modifications dans cette copie, jamais ailleurs. Aucun push.
