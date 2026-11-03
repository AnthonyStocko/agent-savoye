"""
Un résultat explicite par scénario en échec d'un run importé (0 token).

Chaque échec reçoit, à l'analyse de l'import, UN résultat parmi :
- correction : correction proposée (diff lisible, patch téléchargeable, cause en une phrase) — écrite par
  « Corriger » (lot « correction » de l'import) ;
- diagnostic : ce qui manque pour trancher ; le diagnostic temporaire (step TEMPORAIRE-) ajouté au patch pour le
  prochain run ;
- environnement : la cause prouvée et l'action à faire, en une phrase ;
- produit : défaut du produit, avec sa preuve et la désactivation proposée (patch téléchargeable) ;
- hors_depot : le scénario n'appartient pas au dépôt de l'agent, avec la façon de l'y intégrer ;
- hors_perimetre : la feature est d'un module exclu du périmètre testé (profil client), rien à corriger.
« Rien à faire » n'existe que pour un run entièrement vert : un import avec au moins un échec et zéro résultat est
une erreur de l'agent (missing). Tous les scénarios du dépôt sont ceux de l'agent, tagués par lui ou non : la
correction d'un scénario écrit à la main ou par un autre outil lui ajoute le tag de son lot de correction.
Le classement (classify) est contrôlé par le jeu de référence tests/reference/.
"""
import difflib
import os
import re

from tools import cucumber_wait_steps
from tools.cucumber_tools import _git, _read_json, _read_text_file, _write_json, _write_text
from tools.cucumber_verify import failure_kind, parse_scenarios

CATEGORIES = {"correction": "Correction proposée", "diagnostic": "Diagnostic nécessaire",
              "environnement": "Environnement", "produit": "Défaut du produit",
              "hors_depot": "Hors du dépôt de l'agent", "hors_perimetre": "Hors périmètre (module exclu)"}
ICONS = {"correction": "🛠", "diagnostic": "🔎", "environnement": "🌐", "produit": "🐞", "hors_depot": "📦",
         "hors_perimetre": "🚫"}
# Catégories traitées par « Corriger » (lot « correction » de l'import)
TO_WRITE = ("correction", "diagnostic")

# Nature de la correction : libellé lu par l'utilisateur
NATURES = {
    "api_bibliotheque": "remplacer l'appel Java absent par l'API existante de la bibliothèque de la version du pom "
                        "du client (méthode ou step), vérifiée par mvn test-compile",
    "step_equivalent": "remplacer le step non défini par le step existant équivalent",
    "champ_non_renvoye": "champ contrôlé non renvoyé par l'endpoint ou non mappé par le step : corriger la colonne "
                         "ou le step",
    "code_raccourci": "raccourcir le code créé, partout où il est utilisé, à la longueur du modèle",
    "brouillon_complet": "mettre à jour par le brouillon complet lu, en ne modifiant que les champs visés",
    "mecanisme_ecriture": "diagnostiquer le mécanisme d'écriture du champ (écrit puis relu vide)",
    "chemin_fichier": "corriger le chemin du fichier de données ou de règles du test",
    "regle_standard": "copier dans le dépôt client le fichier de règle du Standard que le test charge",
    "clear_client": "nom de clear du client à vérifier : celui de SON application (jamais celui d'un autre client), "
                    "à renseigner dans son profil, puis régénérer l'initialisation",
    "deploiement": "déployer le code testé, puis relancer (le test n'est pas modifié)",
    "configuration_partagee": "rendre le scénario autonome : poser dans son Background la configuration qu'il lit",
    "dependance_tests": "rendre les données du scénario uniques à sa feature (vert seul, rouge en run complet)",
    "donnee_propre": "corriger la création, dans le Background, de la donnée que la feature crée elle-même",
    "donnee_non_idempotente": "rendre la création rejouable : suppression préalable, init du client ou code unique "
                              "par exécution",
    "donnee_non_creee": "créer dans la feature (Background) l'objet qu'elle utilise sans jamais le créer",
    "assertion": "corriger le scénario d'après l'écart constaté",
    "technique": "corriger le step, la colonne ou le fichier du test",
    "version": "écart de version entre la bibliothèque de steps et le serveur",
    "environnement": "dépendance absente de l'environnement",
    "donnee_reference": "donnée de référence absente de l'environnement",
    "init": "initialisation du run en échec",
    "liste_choix_vide": "liste de choix vide sur un champ obligatoire d'un écran",
    "delai_bibliotheque": "remplacer le step d'attente de la bibliothèque (délai fixe dépassé) par un step du projet "
                          "de nom unique, délai paramétrable, qui suit l'état réel des éléments attendus et échoue "
                          "immédiatement sur une erreur",
    "bug_applicatif": "erreur du serveur",
    "cascade": "conséquence d'un autre échec",
    "hors_depot": "scénario absent du dépôt de l'agent",
}

_UNDEFINED = re.compile(r"UndefinedStep|\bis undefined\b|\bare undefined\b|step(?:s)? (?:is|are) undefined|"
                        r"Undefined step", re.I)
_UNDEFINED_STEP = re.compile(r"[Tt]he step [\"'](.+?)[\"']")
_EMPTY_READ = re.compile(r"expected:?\s*<[^<>]+>\s*but was:?\s*<\s*(?:\[\s*\])?\s*>|relue?s?\s+vide|lue?\s+vide apr[eè]s [ée]criture|"
                         r"[ée]crit(?:e|s)?\b.{0,80}\brelu(?:e|s)?\b.{0,40}\bvide", re.I)
_EMPTY_CHOICES = re.compile(r"liste de choix vide|aucun choix (?:n'est )?disponible|no (?:available )?(?:choices|"
                            r"options) (?:available|for)|empty (?:choice|option) list", re.I)
_CODE_LENGTH = re.compile(r"taille de la valeur.{0,200}trop grande|value too long|exceeds? (?:the )?max(?:imum)? length",
                          re.I)
_DRAFT_REQUIRED = re.compile(r"La propri[ée]t[ée]\b.{0,120}\best requise", re.I)
# Règle du Standard : rules/<domaine du Standard>/… ; une règle sous rules/<client>/ est propre au client
_STANDARD_RULE = re.compile(r"/cucumber/rules/(?:WMS|TMS|OMS|WES)/[\w./'-]+\.json")
_FILE_MISSING = re.compile(r"FileNotFoundException|NoSuchFileException|No such file or directory", re.I)
_COMPILE = re.compile(r"cannot find symbol|COMPILATION ERROR|Compilation failure|incompatible types|"
                      r"cannot be applied to given types", re.I)
_DUPLICATE = re.compile(r"existe d[ée]j[àa]|already exists?|duplicate key", re.I)
# Purge par un clear absent de l'environnement du client (souvent le clear d'un autre client)
_CLEAR_ROUTE = re.compile(r"No route found|\b404\b", re.I)
_CLEAR_STEP = re.compile(r"Purge de l'environnement par le clear|\bclear/[\w-]+/", re.I)
_CLEAR_NAME = re.compile(r"par le clear\s+\"([^\"]+)\"")
_VERSION = re.compile(r"No route found|has no field|Invalid path|UnrecognizedPropertyException|Unrecognized field",
                      re.I)


def _cites(text: str, code: str) -> bool:
    return bool(re.search(r"(?<![\w-])" + re.escape(code) + r"(?![\w-])", text))


def _sentence(text: str, limit: int = 220) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def classify(result: dict) -> dict:
    """
    Résultat d'un échec importé d'après son message et son step (0 token) : {categorie, nature, cause, action}.
    La cause tient en une phrase ; l'action dit quoi faire. Le propriétaire du scénario et les cascades sont jugés
    par failure_results.
    """
    message, step = str(result.get("message") or ""), str(result.get("step") or "")
    full = " ".join([message, str(result.get("erreur") or "")])

    def out(categorie, nature, cause, action):
        return {"categorie": categorie, "nature": nature, "cause": _sentence(cause), "action": _sentence(action)}
    # Le dépôt client ne compile pas : aucun scénario n'a tourné, seul le code Java est en cause
    if _COMPILE.search(full) or result.get("nature") == "compilation":
        symbols = list(dict.fromkeys(re.findall(r"symbol:\s*(?:method|variable|class)\s+([\w$]+(?:\([^)]*\))?)", full)))
        return out("correction", "api_bibliotheque",
                   "Le code Java du dépôt ne compile pas : " + (f"{', '.join(symbols[:4])} absent(s) de la bibliothèque "
                                                                f"de steps de la version du pom du client"
                                                                if symbols else _sentence(message, 160)) + ".",
                   "Remplacer chaque appel absent par une méthode ou un step existant de la bibliothèque de la version "
                   "du pom du client (python tools/cucumber_compile.py <client> --api <Classe>), puis vérifier par "
                   "mvn test-compile ; aucune livraison sans compilation réussie.")
    # Contexte du run établi hors du message (code testé absent du déploiement, configuration partagée écrite par
    # une autre feature) : prime sur le message, qui n'en montre que l'effet
    context = result.get("contexte") or {}
    if context.get("code_non_deploye"):
        return out("environnement", "deploiement", f"Le test vérifie un code pas encore déployé sur l'environnement "
                                                   f"({context['code_non_deploye']}).",
                   "Déployer la version qui contient ce code, puis relancer ; le test n'est pas modifié.")
    # Contrôles statiques de la feature (cucumber_data_rules) : cause prouvée par le texte du dépôt
    checks = context.get("controles") or {}
    if _DUPLICATE.search(full):
        return out("correction", "donnee_non_idempotente", "Donnée créée à chaque exécution et refusée en doublon : "
                                                          + _sentence(message, 140)
                   + (" — création dans le Background, rejouée avant chaque scénario."
                      if any("Background" in e for e in checks.get("idempotence") or []) else ""),
                   "Rendre la création rejouable : supprimer l'objet juste avant de le créer, le créer une seule fois "
                   "dans l'init du client, ou un code unique par exécution (longueur maximale du modèle respectée).")
    if checks.get("non_crees"):
        codes = checks["non_crees"]
        cited = [c for c in codes if _cites(full, c)] or codes
        return out("correction", "donnee_non_creee", f"La feature utilise {', '.join(cited[:4])} sans jamais le "
                                                     f"créer (ni Background, ni fichier de données, ni init du "
                                                     f"client).",
                   "Créer ces objets dans le Background de la feature (fichier de données ou step de création), "
                   "avec des codes propres à la feature.")
    if checks.get("fichiers") and _FILE_MISSING.search(full):
        return out("correction", "chemin_fichier", f"Fichier lu absent du dépôt : {checks['fichiers'][0].split(' ', 1)[1]}.",
                   "Créer le fichier au chemin attendu (dossier de la feature) ou corriger le nom dans le scénario.")
    if context.get("dependance"):
        shared = ", ".join(f"{d['code']} ({', '.join(d['features'][:2])})" for d in context.get("donnees_partagees")
                           or [])
        return out("correction", "dependance_tests", f"En échec (dépendance entre tests) : {context['dependance']}"
                                                     + (f" ; données partagées avec d'autres features : {shared}"
                                                        if shared else "") + ".",
                   "Rendre uniques à la feature les données qu'elle crée (codes d'OP, d'UM, fichiers de données), "
                   "sans dépendre de l'ordre des features ; le test reste en échec jusqu'au prochain run complet vert.")
    shared = [d for d in context.get("donnees_partagees") or [] if _cites(full, d["code"])]
    if shared:
        return out("correction", "dependance_tests", "En échec (dépendance entre tests) : donnée partagée avec "
                                                     "d'autres features : " + ", ".join(
                                                         f"{d['code']} ({', '.join(d['features'][:2])})"
                                                         for d in shared) + ".",
                   "Rendre uniques à la feature les données qu'elle crée (codes d'OP, d'UM, fichiers de données), "
                   "sans dépendre de l'ordre des features.")
    if context.get("configuration_partagee"):
        return out("correction", "configuration_partagee", f"Le scénario dépend d'une configuration partagée "
                                                           f"modifiée par une autre feature "
                                                           f"({context['configuration_partagee']}).",
                   "Poser dans le Background du scénario la configuration qu'il lit, sans dépendre de l'ordre des "
                   "features.")
    if result.get("statut") == "indefini" or _UNDEFINED.search(full):
        name = (_UNDEFINED_STEP.search(full) or [None, step])[1] or step
        return out("correction", "step_equivalent", f"Le step « {name} » n'est défini nulle part.",
                   "Remplacer ce step par le step existant équivalent (Standard ou projet).")
    # Attente de la bibliothèque à délai fixe dépassé : correction connue (cucumber_wait_steps), step du projet
    if cucumber_wait_steps.is_library_timeout(full):
        seconds = cucumber_wait_steps.timeout_seconds(full)
        known = cucumber_wait_steps.known_step(step)
        return out("correction", "delai_bibliotheque",
                   f"Délai dépassé dans l'attente de la bibliothèque (step « {step} »"
                   + (f", {seconds} s" if seconds else "") + ") : ConditionTimeoutException sans état des éléments "
                   "attendus ni arrêt sur erreur.",
                   ("Remplacer ce step par le step du projet à délai paramétrable qui suit les fichiers intégrés et "
                    "échoue immédiatement sur une erreur (correction connue, 0 token)." if known else
                    "Écrire un step du projet de nom unique, délai paramétrable, qui suit l'état réel des éléments "
                    "attendus et échoue immédiatement sur une erreur."))
    if _EMPTY_CHOICES.search(full):
        return out("produit", "liste_choix_vide", f"Liste de choix vide sur un champ obligatoire de l'écran "
                                                  f"(step « {step} ») : l'écran ne peut pas être validé.",
                   "Désactiver le scénario (modifications proposées, à télécharger) et signaler le défaut de l'écran Studio, preuve jointe.")
    if _EMPTY_READ.search(full):
        return out("diagnostic", "mecanisme_ecriture", f"Le champ écrit est relu vide (step « {step} ») : le "
                                                       f"mécanisme d'écriture n'est pas prouvé.",
                   "Ajouter un diagnostic temporaire de l'écriture (valeur envoyée, réponse, relecture) au prochain "
                   "run.")
    if _CODE_LENGTH.search(full):
        return out("correction", "code_raccourci", "Un code créé par le test dépasse la longueur autorisée par le "
                                                   "modèle.",
                   "Raccourcir ce code partout où il est utilisé (features, fichiers de données, assertions).")
    if _DRAFT_REQUIRED.search(full):
        return out("correction", "brouillon_complet", "La mise à jour par brouillon omet une propriété requise "
                                                      "(brouillon partiel).",
                   "Repartir du brouillon complet lu et ne modifier que les champs visés.")
    if _CLEAR_ROUTE.search(full) and (_CLEAR_STEP.search(step) or _CLEAR_STEP.search(full)):
        name = (_CLEAR_NAME.search(step) or _CLEAR_NAME.search(full) or [None, ""])[1]
        return out("environnement", "clear_client",
                   f"Purge refusée par l'environnement (404 - No route found) : le clear{f' « {name} »' if name else ''} "
                   f"n'existe pas sur l'environnement de ce client — nom de clear du client à vérifier (il peut "
                   f"venir d'un autre client).",
                   "Renseigner dans le profil du client le clear déclaré par SON application (bundle de clear), "
                   "puis régénérer l'initialisation ; ne jamais reprendre le clear d'un autre client.")
    if _VERSION.search(full):
        return out("environnement", "version", f"Écart de version entre la bibliothèque de steps et le serveur : "
                                               f"{_sentence(message, 140)}",
                   "Aligner la version de la bibliothèque de steps sur celle du serveur de l'environnement.")
    nature, _ = failure_kind(full)
    own = [code for code in context.get("codes_feature") or [] if _cites(full, code)]
    if own and nature in ("donnee_reference", "environnement"):
        return out("correction", "donnee_propre", f"Donnée absente que la feature crée elle-même : "
                                                  f"{', '.join(own[:3])} — sa création a échoué ou vient trop tard.",
                   "Corriger la création de cette donnée dans le Background de la feature (fichier de données, "
                   "ordre des steps), avec un code propre à la feature.")
    if nature == "champ_absent":
        return out("correction", "champ_non_renvoye", f"Le champ contrôlé est renvoyé vide (null) par le step "
                                                      f"« {step} ».",
                   "Corriger la colonne contrôlée ou le step : champ non renvoyé par l'endpoint ou non mappé.")
    if _FILE_MISSING.search(full) and _STANDARD_RULE.search(full):
        path = _STANDARD_RULE.search(full).group(0).lstrip("/")
        return out("correction", "regle_standard", f"Fichier de règle du Standard non copié dans le dépôt client : "
                                                   f"{path}.",
                   "Copier ce fichier de règle du Standard dans le dépôt client, au même chemin (le chemin est juste).")
    if _FILE_MISSING.search(full):
        path = (re.search(r"([\w./'-]+\.(?:json|csv|xml|xlsm|xlsx|txt))", full) or [None, ""])[1]
        return out("correction", "chemin_fichier", f"Fichier introuvable : {path or _sentence(message, 120)}.",
                   "Corriger le chemin du fichier dans le scénario (fichier du dépôt client).")
    if nature == "donnee_reference":
        return out("environnement", "donnee_reference", f"Donnée de référence absente : {_sentence(message, 140)}",
                   "Créer cette donnée dans le Background du scénario ou dans l'init du projet.")
    if nature == "environnement":
        return out("environnement", "environnement", f"Dépendance absente de l'environnement : "
                                                     f"{_sentence(message, 140)}",
                   "La recréer par l'init du projet (contrôle d'environnement), puis relancer.")
    if nature == "bug_applicatif":
        return out("produit", "bug_applicatif", f"Erreur du serveur sur le step « {step} » : "
                                                f"{_sentence(message, 140)}",
                   "Désactiver le scénario (modifications proposées, à télécharger) et signaler le défaut, preuve jointe.")
    if nature == "assertion":
        return out("correction", "assertion", f"Écart constaté au step « {step} » : {_sentence(message, 150)}",
                   "Corriger le scénario d'après l'écart (données, étapes manquantes, valeurs attendues).")
    return out("correction", "technique", f"Erreur du test au step « {step} » : {_sentence(message, 150)}",
               "Corriger le step, la colonne ou le fichier du test.")


def owned(worktree: str, base: str, fichier: str) -> bool:
    """Le scénario appartient au dépôt de l'agent : sa feature est dans le worktree ou dans la branche de base."""
    if worktree and os.path.exists(os.path.join(worktree, *fichier.split("/"))):
        return True
    return bool(worktree and base and os.path.isdir(worktree)
                and _git(worktree, "cat-file", "-e", f"{base}:{fichier}").returncode == 0)


def failure_results(results: dict, init_failure: dict = None, is_owned=None, repo_label: str = "",
                    contexts: dict = None, out_of_scope=None) -> list:
    """
    Résultats de chaque scénario en échec ({id: résultat} de l'analyse), dans l'ordre du run : classement,
    initialisation en échec (tout relève de l'environnement), cascades (catégorie de l'échec qui les cause),
    scénarios hors du dépôt de l'agent. `contexts` : {id: contexte du run} lu par classify (dépendance entre
    tests, données partagées). `out_of_scope(fichier)` : module exclu du périmètre testé de la feature ('' sinon).
    """
    failures = [(i, r) for i, r in results.items() if r.get("statut") != "passe"]
    out, by_file = [], {}
    for identifier, result in failures:
        fichier, titre = identifier.split("::", 1) if "::" in identifier else ("", identifier)
        item = {"id": identifier, "fichier": fichier, "titre": titre, "step": result.get("step", ""),
                "message": _sentence(result.get("message", ""), 300), "statut": "a_traiter", "patch": "",
                "lot": None, "preuve": _sentence(result.get("message", ""), 300)}
        module = out_of_scope(fichier) if out_of_scope and fichier else ""
        if module:
            item.update(categorie="hors_perimetre", nature="hors_perimetre", statut="hors_perimetre",
                        cause=f"La feature {fichier} est du module {module}, exclu du périmètre testé du client.",
                        action=f"Rien à corriger. Pour ne plus l'exécuter dans les runs lancés à la main, retirez le "
                               f"tag client des features {module} (« Données du projet », Modules testés).")
        elif is_owned is not None and fichier and not is_owned(fichier):
            item.update(categorie="hors_depot", nature="hors_depot",
                        cause=f"La feature {fichier} n'est pas dans " + (f"le dépôt {repo_label}." if repo_label
                                                                         else "le dépôt de l'agent."),
                        action="L'ajouter à la branche du dépôt lue par Jenkins (ou importer comme projet le dépôt "
                               "qui la contient) pour que l'agent la prenne en charge.")
        elif init_failure:
            item.update(categorie="environnement", nature="init",
                        cause=_sentence(f"L'initialisation du run a échoué : "
                                        f"{init_failure.get('message') or init_failure.get('step') or 'cause inconnue'}"),
                        action="Corriger l'initialisation (bandeau « À faire »), puis relancer le run.")
        elif result.get("statut") == "cascade":
            root = next((o for o in by_file.get(fichier, []) if o["categorie"] != "cascade"), None)
            item.update(categorie=root["categorie"] if root else "diagnostic", nature="cascade",
                        cause=f"Conséquence de l'échec de « {root['titre']} »." if root else
                        "Scénario non exécuté après l'échec d'un scénario précédent.",
                        action="Traité avec l'échec qui le cause." if root else
                        "Relancer le run après correction du scénario précédent.")
            if root:
                item["cause_de"] = root["id"]
        else:
            item.update(classify({**result, "contexte": {**(result.get("contexte") or {}),
                                                        **((contexts or {}).get(identifier) or {})}}))
        by_file.setdefault(fichier, []).append(item)
        out.append(item)
    return out


def missing(analysis: dict) -> list:
    """Échecs du run sans résultat (erreur de l'agent) : identifiants."""
    failed = [i for i, r in (analysis.get("resultats") or {}).items() if r.get("statut") != "passe"]
    done = {e["id"] for e in analysis.get("echecs") or [] if e.get("categorie") in CATEGORIES}
    return [i for i in failed if i not in done]


def disable_patch(worktree: str, fichier: str, titre: str, tag: str) -> str:
    """Patch (diff git) qui désactive le scénario `titre` par le tag `tag`, sans toucher au worktree ; '' sinon."""
    path = os.path.join(worktree, *fichier.split("/"))
    if not os.path.exists(path):
        return ""
    text = _read_text_file(path)
    lines = text.splitlines(keepends=True)
    target = next((s for s in parse_scenarios(text) if s["titre"] == titre), None)
    if not target or tag in target["tags"]:
        return ""
    index = target["ligne"] - 1
    indent = re.match(r"\s*", lines[index]).group(0)
    previous = index - 1
    if previous >= 0 and lines[previous].strip().startswith("@"):
        lines[previous] = lines[previous].rstrip("\r\n") + f" {tag}" + ("\r\n" if lines[previous].endswith(
            "\r\n") else "\n")
    else:
        lines.insert(index, f"{indent}{tag}\n")
    diff = "".join(difflib.unified_diff(text.splitlines(keepends=True), lines, f"a/{fichier}", f"b/{fichier}"))
    return f"diff --git a/{fichier} b/{fichier}\n{diff}" if diff else ""


def sync_with_branch(worktree: str, base: str, log=None) -> str:
    """
    Worktree de l'agent remis sur la branche réelle (`base`, ex. origin/integration) : fusion où la version de la
    BRANCHE l'emporte sur un conflit — une correction faite à la main depuis le lot de l'agent n'est jamais écrasée
    par sa version périmée (les runs de l'agent testent alors l'état réel). `log` : reçoit la liste des fichiers où
    la version de la branche a été retenue sur un conflit. Renvoie '' si le worktree contient la branche, sinon la
    raison (fusion impossible, annulée).
    """
    if not (worktree and base and os.path.isdir(worktree)):
        return ""
    if _git(worktree, "merge-base", "--is-ancestor", base, "HEAD").returncode == 0:
        return ""
    fork = _git(worktree, "merge-base", "HEAD", base).stdout.strip()
    both = set()
    if fork:
        ours = set(_git(worktree, "diff", "--name-only", fork, "HEAD").stdout.splitlines())
        theirs = set(_git(worktree, "diff", "--name-only", fork, base).stdout.splitlines())
        both = ours & theirs
    message = f"Cucumber: synchronisation avec {base} (la branche l'emporte sur un conflit)"
    merged = _git(worktree, "merge", "--no-edit", "-X", "theirs", "-m", message, base)
    if merged.returncode != 0 and not _resolve_with_branch(worktree, base, message):
        _git(worktree, "merge", "--abort")
        return f"fusion de {base} impossible : {(merged.stderr or merged.stdout).strip()[:200]}"
    kept = sorted(p for p in both
                  if _git(worktree, "rev-parse", f"HEAD:{p}").stdout.strip() == _git(
                      worktree, "rev-parse", f"{base}:{p}").stdout.strip())
    if kept and log:
        log(kept)
    return ""


def _resolve_with_branch(worktree: str, base: str, message: str) -> bool:
    """
    Conflits que « -X theirs » ne tranche pas (fichier modifié par l'agent mais supprimé ou renommé sur la branche,
    ex. correction de casse d'un dossier) : la branche l'emporte — fichier supprimé s'il n'existe plus dans `base`,
    sinon version de `base`. Commit de la fusion. True si tout est résolu.
    """
    unmerged = sorted(set(_git(worktree, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()))
    if not unmerged:
        return False
    for path in unmerged:
        if _git(worktree, "cat-file", "-e", f"{base}:{path}").returncode == 0:
            _git(worktree, "checkout", base, "--", path)
            _git(worktree, "add", "--", path)
        else:
            _git(worktree, "rm", "-q", "--cached", "--ignore-unmatch", "--", path)
            full = os.path.join(worktree, *path.split("/"))
            if os.path.exists(full) and _git(worktree, "ls-files", "--", path).stdout.strip() == "":
                os.remove(full)
    if _git(worktree, "diff", "--name-only", "--diff-filter=U").stdout.strip():
        return False
    return _git(worktree, "commit", "--no-edit", "-q", "-m", message).returncode == 0


def analysis_path(imports_dir: str, numero) -> str:
    return os.path.join(imports_dir, str(numero), "analyse.json")


def record_correction(imports_dir: str, lot: dict, worktree: str) -> list:
    """
    Résultat de chaque scénario d'un lot « correction » après son exécution : correction proposée (diff et patch
    du lot), diagnostic nécessaire (step TEMPORAIRE- au patch, ou ce qui manque quand rien n'a été modifié). Écrit
    le patch du lot dans le dossier de l'import et met à jour son analyse. Renvoie les résultats mis à jour.
    """
    path = analysis_path(imports_dir, lot.get("import"))
    analysis = _read_json(path, {}) or {}
    echecs = {e["id"]: e for e in analysis.get("echecs") or []}
    before = lot.get("avant") or ""
    diff = _git(worktree, "diff", "--binary", f"{before}..HEAD").stdout if before and os.path.isdir(worktree) else ""
    patch = ""
    if diff.strip():
        patch = os.path.join(imports_dir, str(lot["import"]), f"correction-lot-{lot['numero']}.patch")
        _write_text(patch, diff)
    fixed = {c.get("tag"): c for c in (lot.get("correction") or {}).get("corrections") or [] if isinstance(c, dict)}
    remaining = [str(r) for r in (lot.get("correction") or {}).get("restantes") or []]
    updated = []
    for scenario in lot.get("corrections") or []:
        item = echecs.get(scenario["id"])
        if item is None:
            continue
        done = fixed.get(scenario.get("tag"))
        touched = bool(diff.strip()) and scenario["fichier"] in diff
        item.update(lot=lot["numero"], statut="traite", patch=patch if touched or done else "")
        if touched and "TEMPORAIRE-" in diff:
            item.update(categorie="diagnostic", cause=_sentence((done or {}).get("cause") or item["cause"]),
                        action="Diagnostic temporaire ajouté aux modifications : les appliquer, relancer le run, puis "
                               "importer sa console.")
        elif touched or done:
            item.update(categorie="correction", cause=_sentence((done or {}).get("cause") or item["cause"]),
                        action=_sentence((done or {}).get("correction") or "Correction écrite dans les modifications.", 300))
        else:
            item.update(categorie="diagnostic", patch="",
                        cause=_sentence(remaining[0] if remaining else
                                        "La correction n'a rien modifié et n'a rien expliqué (erreur de l'agent)."),
                        action="Ce qui manque pour trancher : " + _sentence("; ".join(remaining[1:3]) or
                                                                           "relancer « Corriger » avec la console du "
                                                                           "prochain run.", 300))
        updated.append(item)
    if updated:
        analysis["echecs"] = list(echecs.values())
        _write_json(path, analysis)
    return updated


def pending(analysis: dict) -> list:
    """Échecs à écrire par « Corriger » (correction ou diagnostic non encore traités)."""
    return [e for e in analysis.get("echecs") or [] if e.get("statut") == "a_traiter"
            and e.get("categorie") in TO_WRITE and e.get("nature") != "cascade"]


def failure_title(item: dict) -> str:
    return f"{ICONS.get(item['categorie'], '')} {CATEGORIES.get(item['categorie'], item['categorie'])}"
