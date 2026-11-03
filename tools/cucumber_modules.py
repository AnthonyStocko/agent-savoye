"""
Modules testés d'un client (onglet Cucumber, « Données du projet »), 0 token.

Un module (WMS, TMS, OMS…) n'est jamais pris dans une liste en dur : c'est un tag @<X> porté par une feature rangée
sous un dossier <X> (features/<X>/…, features/<client>/<X>/…, features/<client>/01-Contexte/<X>/…), dans les
features du client ou du Standard. Le client a le module si ses features le portent, ou si une tâche de son
inventaire le nomme (segment de dépôt, de dossier ou de premier dossier de fichier, ex. canson-tms-organization).

Le profil client garde les modules décochés (« modules_exclus », saisie) ; un module exclu l'est partout :
- init : ni générée, ni corrigée, ni vérifiée (features d'init et tags d'init du module retirés du profil appliqué,
  scope_profile) ; ses clears et sa configuration commentés dans les inits des autres modules ;
- génération et correction : ses tâches ne sont ni proposées, ni générées, ni corrigées ;
- couverture : ses tâches « hors périmètre (module exclu) », hors des taux et du nombre de tâches à tester ;
- runs lancés par l'agent : « and not @<TAG> » et « and not @INIT_…_<X> » ajoutés à l'expression (run_expression) ;
- runs Jenkins lancés à la main (filtre = tag client du job) : le tag client des features du module est remplacé
  par EXCLUDED_TAG (exclude_text), réversible à la réactivation (restore_text), livré en patch avec aperçu ;
- import d'un run : un échec dans une feature du module est « hors périmètre », pas à corriger.
"""
import os
import re
from datetime import datetime

from tools import cucumber_tools
from tools.cucumber_tools import (_git, _read_text_file, add_worktree, cache_dir, format_patch, java_package_segment,
                                  load_inventory)

FIELD = "modules_exclus"
EXCLUDED_TAG = "@MODULE_EXCLU"
FEATURES_DIR = "src/test/resources/cucumber/features"
OUT_OF_SCOPE = "hors_perimetre"
OUT_OF_SCOPE_LABEL = "hors périmètre (module exclu)"
_TOKEN = re.compile(r"[^a-z0-9]+")
_TAG = re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]*")


# ---------------------------------------------------------------------------
# Détection
# ---------------------------------------------------------------------------

def tag_lines(text: str) -> list:
    """(index, ligne) des lignes de tags (hors commentaires) d'une feature."""
    return [(i, line) for i, line in enumerate((text or "").splitlines(keepends=True))
            if line.lstrip().startswith("@")]


def feature_tags(text: str) -> list:
    """Tags de la feature et de ses scénarios, dans l'ordre, sans doublon."""
    return list(dict.fromkeys(t for _, line in tag_lines(text) for t in _TAG.findall(line.split("#", 1)[0])))


def _folders(path: str) -> set:
    return {s.upper() for s in (path or "").replace("\\", "/").split("/")[:-1] if s}


def codes_in_features(texts: dict, ignored=()) -> dict:
    """{code: tag écrit} : tag @X porté par une feature rangée sous un dossier X (`ignored` : tag client, etc.)."""
    skip = {t.lstrip("@").upper() for t in ignored if t}
    out = {}
    for path, text in (texts or {}).items():
        folders = _folders(path)
        for tag in feature_tags(text):
            code = tag[1:].upper()
            if code in folders and code not in skip:
                out.setdefault(code, tag)
    return out


def std_codes(cucumber_std_dir: str) -> dict:
    """{code: tag} du Standard : dossiers de premier niveau de features/ dont une feature porte @<dossier>."""
    root = os.path.join(cucumber_std_dir or "", *FEATURES_DIR.split("/"))
    out = {}
    for name in sorted(os.listdir(root)) if cucumber_std_dir and os.path.isdir(root) else []:
        folder = os.path.join(root, name)
        if not os.path.isdir(folder):
            continue
        found = ""
        for current, _, files in os.walk(folder):
            for f in (f for f in files if f.endswith(".feature")):
                tags = {t.upper(): t for t in feature_tags(_read_text_file(os.path.join(current, f)))}
                found = tags.get(f"@{name.upper()}") or ("@" + name if f"@INIT_{name.upper()}" in tags else "")
                if found:
                    break
            if found:
                break
        if found:
            out[name.upper()] = found
    return out


def _tokens(value: str) -> set:
    return {t.upper() for t in _TOKEN.split((value or "").lower()) if t}


def task_modules(spec: dict, codes) -> set:
    """Modules nommés par une tâche : segments de son dépôt, de son dossier et du premier dossier de ses fichiers."""
    names = [spec.get("depot") or "", spec.get("dossier") or ""]
    for item in spec.get("fichiers") or []:
        names += [item.get("repo") or "", (item.get("path") or "").split("/", 1)[0]]
    found = set().union(*map(_tokens, names)) if names else set()
    return found & {c.upper() for c in codes}


def feature_modules(path: str, text: str, codes) -> set:
    """Modules d'une feature : ses tags @<X> et ses dossiers <X>, parmi `codes`."""
    codes = {c.upper() for c in codes}
    tags = {t[1:].upper() for t in feature_tags(text)}
    return (tags | _folders(path)) & codes


def detect(client_texts: dict, std: dict, inventory: dict, client_tag: str = "", client_name: str = "") -> list:
    """
    Modules du client : [{"code", "tag", "sources"}] triés. `client_texts` {chemin: texte} (features du client),
    `std` {code: tag} (std_codes), `inventory` (tâches). Aucun module en dur : vocabulaire = features du client et
    du Standard ; présence = features du client ou tâches de l'inventaire.
    """
    ignored = [client_tag, f"@{java_package_segment(client_name)}" if client_name else ""]
    own = codes_in_features(client_texts, ignored)
    vocabulary = {**{c: t for c, t in (std or {}).items() if c not in {i.lstrip("@").upper() for i in ignored if i}},
                  **own}
    out = {}
    for code, tag in own.items():
        count = sum(1 for p, t in client_texts.items() if code in feature_modules(p, t, [code]))
        out[code] = {"code": code, "tag": tag, "sources": [f"{count} feature(s) du client"]}
    tasks = {}
    for spec in (inventory or {}).get("specifiques") or []:
        for code in task_modules(spec, vocabulary):
            tasks[code] = tasks.get(code, 0) + 1
    for code, count in tasks.items():
        out.setdefault(code, {"code": code, "tag": vocabulary[code], "sources": []})["sources"].append(
            f"{count} tâche(s) de l'inventaire")
    return [out[c] for c in sorted(out)]


def detect_in_worktree(worktree: str, client_name: str, cucumber_std_dir: str, client_tag: str = "") -> list:
    """detect sur les .feature du worktree du dépôt client (hors target/), le Standard et l'inventaire du client."""
    root = os.path.join(worktree, *FEATURES_DIR.split("/"))
    texts = {}
    for current, dirs, files in os.walk(root) if os.path.isdir(root) else []:
        dirs[:] = [d for d in dirs if d != "target"]
        for f in (f for f in files if f.endswith(".feature")):
            full = os.path.join(current, f)
            texts[os.path.relpath(full, worktree).replace(os.sep, "/")] = _read_text_file(full)
    return detect(texts, std_codes(cucumber_std_dir), load_inventory(client_name), client_tag, client_name)


# ---------------------------------------------------------------------------
# Profil : modules testés, modules exclus
# ---------------------------------------------------------------------------

def modules(profile: dict) -> list:
    return list((profile or {}).get("modules") or [])


def excluded(profile: dict) -> set:
    return {str(c).strip().upper() for c in (profile or {}).get(FIELD) or [] if str(c).strip()}


def codes(profile: dict) -> set:
    return {m["code"] for m in modules(profile)} | excluded(profile)


def module_tag(profile: dict, code: str) -> str:
    return next((m["tag"] for m in modules(profile) if m["code"] == code.upper()), f"@{code.upper()}")


def is_out(found: set, out: set) -> bool:
    """Exclu : nommé par au moins un module, et seulement par des modules exclus."""
    return bool(found) and set(found) <= set(out)


def path_excluded(path: str, profile: dict, text: str = None) -> str:
    """Code du module exclu d'un chemin (feature ou fichier de données, dossiers et tags), '' sinon."""
    out = excluded(profile)
    if not out:
        return ""
    found = feature_modules(path, text or "", codes(profile))
    return sorted(found)[0] if is_out(found, out) else ""


def task_excluded(spec: dict, profile: dict) -> str:
    out = excluded(profile)
    found = task_modules(spec, codes(profile)) if out else set()
    return sorted(found)[0] if is_out(found, out) else ""


def _init_tag_of(tag: str, out: set) -> bool:
    upper = tag.upper()
    return upper.startswith("@INIT_") and any(upper == f"@INIT_{c}" or upper.endswith(f"_{c}") for c in out)


def scope_profile(profile: dict) -> dict:
    """Profil appliqué sans les features ni les tags d'init des modules exclus (« init_exclues » : chemins retirés)."""
    out = excluded(profile)
    if not out:
        return profile
    kept = [p for p in profile.get("init_features") or [] if not path_excluded(p, profile)]
    removed = [p for p in profile.get("init_features") or [] if p not in kept]
    terms = [t.strip() for t in re.split(r"\s+or\s+", profile.get("init_tags") or "") if t.strip()]
    return {**profile, "init_features": kept, "init_exclues": removed,
            "init_tags": " or ".join(t for t in terms if not _init_tag_of(t, out)),
            "init_features_tous": list(profile.get("init_features") or []),
            "init_tags_tous": profile.get("init_tags") or ""}


def init_tags_of(profile: dict, client_name: str = "") -> list:
    """Tags d'init des modules exclus (détectés dans le profil, sinon construits @INIT_<CLIENT>_<X>)."""
    out = excluded(profile)
    found = [t for t in re.findall(r"@\w+", " ".join(str((profile or {}).get(k) or "") for k in
                                                     ("init_tags_detectes", "init_tags"))) if _init_tag_of(t, out)]
    if client_name:
        found += [f"@INIT_{java_package_segment(client_name).upper()}_{c}" for c in sorted(out)]
    return list(dict.fromkeys(found))


def run_expression(tags: str, profile: dict, client_name: str = "") -> str:
    """
    Expression de tags d'un run lancé par l'agent : termes « or » réduits aux tags d'init d'un module exclu retirés,
    puis « and not @<TAG> » et « and not @INIT_…_<X> » pour chaque module exclu. Sans module exclu : inchangée.
    Expression réduite à rien (seulement des inits de modules exclus) : '' (rien à lancer).
    """
    tags = (tags or "").strip()
    out = excluded(profile)
    if not out:
        return tags
    terms = [t.strip() for t in re.split(r"\s+or\s+", tags) if t.strip()] if "(" not in tags else [tags]
    kept = [t for t in terms if not _init_tag_of(t, out)]
    if terms and not kept:
        return ""
    expression = " or ".join(kept)
    negations = [module_tag(profile, c) for c in sorted(out)] + init_tags_of(profile, client_name)
    negations = [n for n in dict.fromkeys(negations)
                 if not re.search(rf"not\s+{re.escape(n)}(?![\w])", expression, re.I)]
    if not negations:
        return expression
    head = f"({expression})" if " or " in expression else expression
    return " and ".join(([head] if head else []) + [f"not {n}" for n in negations])


def summary_line(profile: dict) -> str:
    """« Modules testés : WMS — TMS exclu » ; '' sans module détecté ni exclu."""
    out = excluded(profile)
    tested = [m["code"] for m in modules(profile) if m["code"] not in out]
    if not tested and not out:
        return ""
    line = "Modules testés : " + (", ".join(tested) or "aucun")
    if out:
        line += " — " + ", ".join(sorted(out)) + (" exclus" if len(out) > 1 else " exclu")
    return line


# ---------------------------------------------------------------------------
# Tag client des features d'un module exclu (runs lancés à la main)
# ---------------------------------------------------------------------------

def _swap(text: str, old: str, new: str) -> tuple:
    """Tag `old` remplacé par `new` sur les lignes de tags (même place, fins de ligne gardées) : (texte, n)."""
    lines = (text or "").splitlines(keepends=True)
    count = 0
    pattern = re.compile(rf"(?<![\w@]){re.escape(old)}(?![\w])")
    for i, line in tag_lines(text):
        body, sep, comment = line.partition("#")
        new_body, n = pattern.subn(new, body)
        if n:
            lines[i] = new_body + sep + comment
            count += n
    return "".join(lines), count


def exclude_text(text: str, client_tag: str) -> tuple:
    """Tag client remplacé par EXCLUDED_TAG à la même place (réversible par restore_text) : (texte, n)."""
    return _swap(text, client_tag, EXCLUDED_TAG) if client_tag else (text, 0)


def restore_text(text: str, client_tag: str) -> tuple:
    return _swap(text, EXCLUDED_TAG, client_tag) if client_tag else (text, 0)


def module_features(worktree: str, profile: dict, code: str) -> dict:
    """{chemin: texte} des features du worktree dont le seul module est `code` (ou des modules tous exclus)."""
    root = os.path.join(worktree, *FEATURES_DIR.split("/"))
    out, known = {}, codes(profile) | {code.upper()}
    for current, dirs, files in os.walk(root) if os.path.isdir(root) else []:
        dirs[:] = [d for d in dirs if d != "target"]
        for f in sorted(f for f in files if f.endswith(".feature")):
            full = os.path.join(current, f)
            rel = os.path.relpath(full, worktree).replace(os.sep, "/")
            text = _read_text_file(full)
            found = feature_modules(rel, text, known)
            if code.upper() in found and is_out(found, excluded(profile) | {code.upper()}):
                out[rel] = text
    return out


def retag_plan(worktree: str, profile: dict, code: str, restore: bool = False) -> list:
    """
    Aperçu : [{"fichier", "tags": n, "avant", "apres"}] des features du module `code` dont le tag client serait
    remplacé par EXCLUDED_TAG (`restore` : remis). Lignes de tags seules montrées (avant / après).
    """
    tag = (profile or {}).get("tag_client") or ""
    plan = []
    for rel, text in module_features(worktree, profile, code).items():
        new, count = (restore_text if restore else exclude_text)(text, tag)
        if count:
            before = [line.rstrip("\r\n") for _, line in tag_lines(text)]
            after = [line.rstrip("\r\n") for _, line in tag_lines(new)]
            plan.append({"fichier": rel, "tags": count, "avant": [b for b, a in zip(before, after) if b != a],
                         "apres": [a for b, a in zip(before, after) if b != a], "texte": new})
    return plan


def retag_worktree_dir(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "worktree_modules")


def retag_patch_path(client_name: str, code: str, restore: bool = False) -> str:
    return os.path.join(cucumber_tools.WORKSPACE_DIR, f"{client_name}-cucumber-module-{code.lower()}-"
                                       f"{'reactive' if restore else 'exclu'}.patch")


def apply_retag(cucumber_repo_dir: str, client_name: str, base: str, profile: dict, code: str,
                restore: bool = False) -> dict:
    """
    Applique le plan (retag_plan) dans un espace dédié qui repart de `base`, un commit, un patch à appliquer au
    dépôt client. Renvoie {"fichiers", "patch", "message"} ; rien à changer : patch ''. Lève RuntimeError.
    """
    branch = f"cucumber_{client_name}_modules"
    worktree = add_worktree(cucumber_repo_dir, retag_worktree_dir(client_name), branch, base, reset=True)
    plan = retag_plan(worktree, profile, code, restore)
    output = retag_patch_path(client_name, code, restore)
    if os.path.exists(output):
        os.remove(output)
    if not plan:
        return {"fichiers": [], "patch": "", "message": (
            f"aucune feature du module {code} ne porte {EXCLUDED_TAG} sur {base} : rien à remettre" if restore else
            f"aucune feature du module {code} ne porte le tag client sur {base} : rien à retirer")}
    for item in plan:
        with open(os.path.join(worktree, *item["fichier"].split("/")), "w", encoding="utf-8", newline="") as f:
            f.write(item["texte"])
    files = [p["fichier"] for p in plan]
    _git(worktree, "add", "--", *files)
    tag = profile.get("tag_client") or ""
    message = (f"Cucumber : module {code} réactivé chez {client_name} ({EXCLUDED_TAG} -> {tag})" if restore else
               f"Cucumber : module {code} exclu chez {client_name} ({tag} -> {EXCLUDED_TAG})")
    commit = _git(worktree, "commit", "-m", message)
    if commit.returncode != 0:
        raise RuntimeError(f"commit impossible : {commit.stderr.strip()[:300]}")
    format_patch(worktree, f"{base}..HEAD", output)
    return {"fichiers": files, "patch": output if os.path.exists(output) else "", "message": message}


# ---------------------------------------------------------------------------
# Saisie (IHM) et état du retrait du tag client
# ---------------------------------------------------------------------------

def save_excluded(client_name: str, codes_out) -> dict:
    """Modules décochés enregistrés dans la saisie du profil (les autres champs saisis sont gardés)."""
    from tools import cucumber_profile
    document = cucumber_profile.load_profile(client_name)
    saisi = document.setdefault("saisi", {})
    values = sorted({str(c).strip().upper() for c in codes_out or [] if str(c).strip()})
    if values:
        saisi[FIELD] = values
    else:
        saisi.pop(FIELD, None)
    cucumber_profile._write_json(cucumber_profile.profile_path(client_name), document)
    return document


def record_retag(client_name: str, code: str, restore: bool, result: dict) -> dict:
    """État du tag client par module dans le profil (« retrait_tag ») : patch produit, date, fichiers."""
    from tools import cucumber_profile
    document = cucumber_profile.load_profile(client_name)
    states = document.setdefault("retrait_tag", {})
    states[code.upper()] = {"etat": "remis" if restore else "retire", "patch": result.get("patch", ""),
                            "fichiers": result.get("fichiers") or [],
                            "date": datetime.now().isoformat(timespec="seconds")}
    cucumber_profile._write_json(cucumber_profile.profile_path(client_name), document)
    return document


def retag_state(document: dict, code: str) -> dict:
    return ((document or {}).get("retrait_tag") or {}).get(code.upper()) or {}
