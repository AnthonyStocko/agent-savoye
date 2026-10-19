=== RÈGLES D'ÉCONOMIE DE TOKENS (OBLIGATOIRES) ===
- Localise avant de lire : Grep / Glob avec un motif précis (filtre de type ou de chemin) pour trouver fichiers et numéros de ligne.
- Lis seulement les portions utiles (paramètres offset / limit autour des lignes trouvées).
- Ne lis JAMAIS en entier un fichier de plus de 400 lignes (XML Studio, gros services Java…) : cible la zone avec Grep puis offset / limit.
- Ne relis pas un fichier ou une portion déjà lus : réutilise ce que tu as déjà vu.
- Ne liste jamais une arborescence complète (pas de `ls -R`, `find` ou `tree` sans filtre) : utilise Glob avec un motif ciblé.
- Borne toutes les sorties Bash (`| head -n 50`, `--stat`, `--oneline`, `-l`…).
- N'affiche jamais un diff ou un patch complet (`git diff`, `git show`, lecture d'un .patch) : utilise `git diff --stat` ou un fichier précis, borné. Rediriger un diff vers un fichier .patch reste autorisé.
- Regroupe dans un même tour les appels d'outils indépendants.
- Réponse finale : 10 lignes maximum (le détail est dans les fichiers produits).
=== FIN RÈGLES D'ÉCONOMIE ===
