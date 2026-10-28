"""
Un résultat explicite par scénario en échec d'un run importé (0 token).

Chaque échec reçoit, à l'analyse de l'import, UN résultat parmi :
- correction : correction proposée (diff lisible, patch téléchargeable, cause en une phrase) — écrite par
  « Corriger » (lot « correction » de l'import) ;
- diagnostic : ce qui manque pour trancher ; le diagnostic temporaire (step TEMPORAIRE-) ajouté au patch pour le
  prochain run ;
- environnement : la cause prouvée et l'action à faire, en une phrase ;
- produit : défaut du produit, avec sa preuve et la désactivation proposée (patch téléchargeable) ;
- hors_depot : le scénario n'appartient pas au dépôt de l'agent, avec la façon de l'y intégrer.
« Rien à faire » n'existe que pour un run entièrement vert : un import avec au moins un échec et zéro résultat est
une erreur de l'agent (missing). Tous les scénarios du dépôt sont ceux de l'agent, tagués par lui ou non : la
correction d'un scénario écrit à la main ou par un autre outil lui ajoute le tag de son lot de correction.
Le classement (classify) est contrôlé par le jeu de référence tests/reference/.
"""
import difflib
import os
import re

from tools.cucumber_tools import _git, _read_json, _read_text_file, _write_json, _write_text
from tools.cucumber_verify import failure_kind, parse_scenarios

CATEGORIES = {"correction": "Correction proposée", "diagnostic": "Diagnostic nécessaire",
              "environnement": "Environnement", "produit": "Défaut du produit",
              "hors_depot": "Hors du dépôt de l'agent"}
ICONS = {"correction": "🛠", "diagnostic": "🔎", "environnement": "🌐", "produit": "🐞", "hors_depot": "📦"}
# Catégories traitées par « Corriger » (lot « correction » de l'import)
TO_WRITE = ("correction", "diagnostic")

# Nature de la correction : libellé lu par l'utilisateur
NATURES = {
    "step_equivalent": "remplacer le step non défini par le step existant équivalent",
    "champ_non_renvoye": "champ contrôlé non renvoyé par l'endpoint ou non mappé par le step : corriger la colonne "
                         "ou le step",
    "code_raccourci": "raccourcir le code créé, partout où il est utilisé, à la longueur du modèle",
    "brouillon_complet": "mettre à jour par le brouillon complet lu, en ne modifiant que les champs visés",
    "mecanisme_ecriture": "diagnostiquer le mécanisme d'écriture du champ (écrit puis relu vide)",
    "chemin_fichier": "corriger le chemin du fichier de données ou de règles du test",
    "assertion": "corriger le scénario d'après l'écart constaté",
    "technique": "corriger le step, la colonne ou le fichier du test",
    "version": "écart de version entre la bibliothèque de steps et le serveur",
    "environnement": "dépendance absente de l'environnement",
    "donnee_reference": "donnée de référence absente de l'environnement",
    "init": "initialisation du run en échec",
    "liste_choix_vide": "liste de choix vide sur un champ obligatoire d'un écran",
    "bug_applicatif": "erreur du serveur",
    "cascade": "conséquence d'un autre échec",
    "hors_depot": "scénario absent du dépôt de l'agent",
}

_UNDEFINED = re.compile(r"UndefinedStep|\bis undefined\b|\bare undefined\b|step(?:s)? (?:is|are) undefined|"
                        r"Undefined step", re.I)
_UNDEFINED_STEP = re.compile(r"[Tt]he step [\"'](.+?)[\"']")
_EMPTY_READ = re.compile(r"expected:?\s*<[^<>]+>\s*but was:?\s*<\s*>|relue?s?\s+vide|lue?\s+vide apr[eè]s [ée]criture|"
                         r"[ée]crit(?:e|s)?\b.{0,80}\brelu(?:e|s)?\b.{0,40}\bvide", re.I)
_EMPTY_CHOICES = re.compile(r"liste de choix vide|aucun choix (?:n'est )?disponible|no (?:available )?(?:choices|"
                            r"options) (?:available|for)|empty (?:choice|option) list", re.I)
_CODE_LENGTH = re.compile(r"taille de la valeur.{0,200}trop grande|value too long|exceeds? (?:the )?max(?:imum)? length",
                          re.I)
_DRAFT_REQUIRED = re.compile(r"La propri[ée]t[ée]\b.{0,120}\best requise", re.I)
_FILE_MISSING = re.compile(r"FileNotFoundException|NoSuchFileException|No such file or directory", re.I)
_VERSION = re.compile(r"No route found|has no field|Invalid path|UnrecognizedPropertyException|Unrecognized field",
                      re.I)


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
    if result.get("statut") == "indefini" or _UNDEFINED.search(full):
        name = (_UNDEFINED_STEP.search(full) or [None, step])[1] or step
        return out("correction", "step_equivalent", f"Le step « {name} » n'est défini nulle part.",
                   "Remplacer ce step par le step existant équivalent (Standard ou projet).")
    if _EMPTY_CHOICES.search(full):
        return out("produit", "liste_choix_vide", f"Liste de choix vide sur un champ obligatoire de l'écran "
                                                  f"(step « {step} ») : l'écran ne peut pas être validé.",
                   "Désactiver le scénario (patch proposé) et signaler le défaut de l'écran Studio, preuve jointe.")
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
    if _VERSION.search(full):
        return out("environnement", "version", f"Écart de version entre la bibliothèque de steps et le serveur : "
                                               f"{_sentence(message, 140)}",
                   "Aligner la version de la bibliothèque de steps sur celle du serveur de l'environnement.")
    nature, _ = failure_kind(full)
    if nature == "champ_absent":
        return out("correction", "champ_non_renvoye", f"Le champ contrôlé est renvoyé vide (null) par le step "
                                                      f"« {step} ».",
                   "Corriger la colonne contrôlée ou le step : champ non renvoyé par l'endpoint ou non mappé.")
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
                   "Désactiver le scénario (patch proposé) et signaler le défaut, preuve jointe.")
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


def failure_results(results: dict, init_failure: dict = None, is_owned=None, repo_label: str = "") -> list:
    """
    Résultats de chaque scénario en échec ({id: résultat} de l'analyse), dans l'ordre du run : classement,
    initialisation en échec (tout relève de l'environnement), cascades (catégorie de l'échec qui les cause),
    scénarios hors du dépôt de l'agent.
    """
    failures = [(i, r) for i, r in results.items() if r.get("statut") != "passe"]
    out, by_file = [], {}
    for identifier, result in failures:
        fichier, titre = identifier.split("::", 1) if "::" in identifier else ("", identifier)
        item = {"id": identifier, "fichier": fichier, "titre": titre, "step": result.get("step", ""),
                "message": _sentence(result.get("message", ""), 300), "statut": "a_traiter", "patch": "",
                "lot": None, "preuve": _sentence(result.get("message", ""), 300)}
        if is_owned is not None and fichier and not is_owned(fichier):
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
            item.update(classify(result))
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
    merged = _git(worktree, "merge", "--no-edit", "-X", "theirs", "-m",
                  f"Cucumber: synchronisation avec {base} (la branche l'emporte sur un conflit)", base)
    if merged.returncode != 0:
        _git(worktree, "merge", "--abort")
        return f"fusion de {base} impossible : {(merged.stderr or merged.stdout).strip()[:200]}"
    kept = sorted(p for p in both
                  if _git(worktree, "rev-parse", f"HEAD:{p}").stdout.strip() == _git(
                      worktree, "rev-parse", f"{base}:{p}").stdout.strip())
    if kept and log:
        log(kept)
    return ""


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
                        action="Diagnostic temporaire ajouté au patch : appliquer le patch, relancer le run, puis "
                               "importer sa console.")
        elif touched or done:
            item.update(categorie="correction", cause=_sentence((done or {}).get("cause") or item["cause"]),
                        action=_sentence((done or {}).get("correction") or "Correction écrite dans le patch.", 300))
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
