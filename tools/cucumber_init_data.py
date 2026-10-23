"""
Initialisation Cucumber du client et données qu'elle crée, en Python seul (0 token).

Les tests générés s'exécutent après l'initialisation du client (feature taguée @INIT_*, dossier 0x-Contexte), qui
purge l'environnement puis intègre ses fichiers de données (xlsm/xlsx, CSV, XML) et ses tables de steps. Ce module
la détecte, extrait les codes de référence qu'elle crée (sites, donneurs d'ordres, secteurs, zones, emplacements)
et les écrit dans .tmp/cucumber/<client>/init_client.json. Un fichier illisible est ignoré avec un avertissement.
"""
import csv
import io
import os
import re
import xml.etree.ElementTree as ET
import zipfile

from tools.cucumber_checks import FEATURES_ROOT, FILES_ROOT, RULES_ROOT, data_references, repo_files, resolved_path
from tools.cucumber_tools import _read_text_file, _write_json, cache_dir

# Catégorie d'un fichier de données d'après son nom (premier motif trouvé), colonne « code » de ses lignes
CATEGORIES = (("sites", re.compile(r"RefSite(?!Group)", re.I)),
              ("donneurs_ordres", re.compile(r"RefStockOwner", re.I)),
              ("secteurs", re.compile(r"WmsSector", re.I)),
              ("partenaires", re.compile(r"(Exc|Ref)Partner", re.I)),
              ("zones", re.compile(r"(Wms|Ref)LocationArea", re.I)),
              ("emplacements", re.compile(r"(Wms|Ref)Location(Creation)?Draft", re.I)))
LABELS = {"sites": "sites", "donneurs_ordres": "donneurs d'ordres", "secteurs": "secteurs",
          "partenaires": "partenaires", "zones": "zones", "emplacements": "emplacements"}
# Catégorie d'une table de step de création d'après le texte du step
STEP_CATEGORIES = (("sites", re.compile(r"\bsites?\b", re.I)),
                   ("donneurs_ordres", re.compile(r"donneurs? d'ordres?|stock ?owner", re.I)),
                   ("secteurs", re.compile(r"secteurs?|sectors?", re.I)),
                   ("partenaires", re.compile(r"partenaires?|partners?", re.I)),
                   ("zones", re.compile(r"\bzones?\b", re.I)),
                   ("emplacements", re.compile(r"emplacements?", re.I)))
_CREATION_STEP = re.compile(r"cr[ée]ation|cr[ée]er|int[ée]gration|initialisation", re.I)
_GLOBAL_INIT_STEP = "Initialisation globale par fichiers"
_CONTEXT_DIR = re.compile(r"(^|/)\d+-Contexte(/|$)", re.I)
_TAG_TOKEN = re.compile(r"@[\w-]+")
_SPREADSHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
MAX_PROMPT_CODES = 30


def init_path(client_name: str) -> str:
    return os.path.join(cache_dir(client_name), "init_client.json")


def feature_tags(text: str) -> set:
    """Tags d'une feature (feature et scénarios)."""
    return {t for line in text.splitlines() if line.strip().startswith("@") for t in line.split()
            if t.startswith("@")}


def is_init_feature(rel: str, text: str, wanted_tags: set) -> bool:
    """Feature d'initialisation : un tag de CUCUMBER_INIT_TAGS si donné, sinon @INIT_*, sinon dossier 0x-Contexte."""
    tags = feature_tags(text)
    if wanted_tags:
        return bool(tags & wanted_tags)
    if any(t.upper().startswith("@INIT_") for t in tags):
        return True
    inner = rel[len(FEATURES_ROOT):] if rel.startswith(FEATURES_ROOT) else rel
    return bool(_CONTEXT_DIR.search(inner.rsplit("/", 1)[0])) and "initialisation" in inner.lower()


def init_tags_wanted(expression: str) -> set:
    """Tags positifs d'une expression CUCUMBER_INIT_TAGS (ex. « @INIT_CLIENT_WMS or @INIT_CLIENT_TMS »)."""
    return set(_TAG_TOKEN.findall(re.sub(r"\bnot\s+@[\w-]+", "", expression or "")))


def step_lines(text: str) -> list:
    """Steps d'une feature (texte après le mot-clé), dans l'ordre."""
    steps = []
    for line in text.splitlines():
        match = re.match(r"^\s*(Given|When|Then|And|But|\*|Soit|Quand|Alors|Et|Mais)\s+(.+?)\s*$", line)
        if match:
            steps.append(match.group(2))
    return steps


def init_data_files(rel: str, text: str, present: set) -> list:
    """Fichiers intégrés par une feature d'init : références des steps (règles comprises), et dossier entier si chargé."""
    files = [resolved_path(rel, ref) for ref in data_references(text)]
    if _GLOBAL_INIT_STEP in text:
        folder = resolved_path(rel, {"nom": "", "regle": False})
        files += sorted(p for p in present if p.startswith(folder))
    return [p for p in dict.fromkeys(files) if p in present and p.startswith((FILES_ROOT, RULES_ROOT))]


def protected_files(init: dict) -> list:
    """
    Fichiers de l'initialisation globale, partagés par tous les tests : features d'init et fichiers qu'elles intègrent.
    Une modification les casse tous (fichier rejeté : intégration arrêtée, échecs en cascade).
    """
    return list(dict.fromkeys(p for f in (init or {}).get("features", [])
                              for p in [f["chemin"], *(f.get("fichiers") or [])]))


# ---------------------------------------------------------------------------
# Lecture des fichiers de données : lignes [cellules]
# ---------------------------------------------------------------------------

def _xlsx_rows(data: bytes) -> list:
    """Lignes de la feuille « data » (à défaut la première) d'un classeur xlsx / xlsm, sans dépendance externe."""
    archive = zipfile.ZipFile(io.BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in archive.namelist():
        for item in ET.fromstring(archive.read("xl/sharedStrings.xml")).iter(f"{_SPREADSHEET_NS}si"):
            shared.append("".join(t.text or "" for t in item.iter(f"{_SPREADSHEET_NS}t")))
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    sheets = [(s.get("name"), s.get(f"{_REL_NS}id")) for s in workbook.iter(f"{_SPREADSHEET_NS}sheet")]
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
    name, rid = next((s for s in sheets if (s[0] or "").lower() == "data"), sheets[0] if sheets else ("", ""))
    target = rels.get(rid, "worksheets/sheet1.xml").lstrip("/")
    sheet = ET.fromstring(archive.read(target if target.startswith("xl/") else f"xl/{target}"))
    rows = []
    for row in sheet.iter(f"{_SPREADSHEET_NS}row"):
        cells = {}
        for position, cell in enumerate(row.iter(f"{_SPREADSHEET_NS}c")):
            column = _column_index(cell.get("r", ""), position)
            value = cell.find(f"{_SPREADSHEET_NS}v")
            if cell.get("t") == "s" and value is not None:
                text = shared[int(value.text)]
            elif cell.get("t") == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(f"{_SPREADSHEET_NS}t"))
            else:
                text = value.text if value is not None else ""
            cells[column] = (text or "").strip()
        # Cellules vides absentes du XML : la position vient de la lettre de colonne
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return rows


def _column_index(reference: str, default: int) -> int:
    """Index (0-based) de la colonne d'une référence de cellule (« AB12 » -> 27)."""
    letters = re.match(r"[A-Z]+", reference or "")
    if not letters:
        return default
    index = 0
    for letter in letters.group(0):
        index = index * 26 + ord(letter) - ord("A") + 1
    return index - 1


def _csv_rows(data: bytes) -> list:
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    delimiter = ";" if text.count(";") >= text.count(",") else ","
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _xml_rows(data: bytes) -> list:
    """Éléments XML à feuilles : un en-tête (noms locaux) et une ligne de valeurs par élément parent."""
    rows = []
    for element in ET.fromstring(data).iter():
        leaves = [c for c in element if len(c) == 0 and (c.text or "").strip()]
        if leaves:
            rows += [[c.tag.rsplit("}", 1)[-1] for c in leaves], [c.text.strip() for c in leaves]]
    return rows


def read_rows(path: str) -> list:
    """Lignes d'un fichier de données (xlsx / xlsm, CSV, XML) ; lève ValueError s'il est illisible."""
    with open(path, "rb") as f:
        data = f.read()
    extension = path.rsplit(".", 1)[-1].lower()
    try:
        if extension in ("xlsx", "xlsm"):
            return _xlsx_rows(data)
        if extension == "csv":
            return _csv_rows(data)
        if extension == "xml":
            return _xml_rows(data)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, IndexError, csv.Error) as e:
        raise ValueError(str(e)) from e
    return []


def table_codes(rows: list) -> list:
    """Valeurs de la colonne « code » sous la première ligne d'en-tête qui en a une (actions DELETE exclues)."""
    header = next((i for i, row in enumerate(rows) if "code" in row), None)
    if header is None:
        return []
    names = rows[header]
    code, action = names.index("code"), names.index("action") if "action" in names else None
    out = []
    for row in rows[header + 1:]:
        value = row[code] if code < len(row) else ""
        if value and not (action is not None and action < len(row) and row[action].upper() == "DELETE"):
            out.append(value)
    return list(dict.fromkeys(out))


def file_category(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return next((category for category, pattern in CATEGORIES if pattern.search(name)), "")


def file_codes(worktree: str, paths: list, warnings: list) -> dict:
    """{catégorie: [codes]} créés par des fichiers de données ; un fichier illisible ajoute un avertissement."""
    codes = {}
    for rel in paths:
        category = file_category(rel)
        if not category:
            continue
        try:
            rows = read_rows(os.path.join(worktree, *rel.split("/")))
        except (OSError, ValueError) as e:
            warnings.append(f"{rel} illisible, ignoré ({str(e)[:120]})")
            continue
        codes.setdefault(category, []).extend(table_codes(rows))
    return codes


def step_codes(text: str) -> dict:
    """{catégorie: [codes]} des tables de steps de création (colonne code) d'une feature."""
    codes, step, header = {}, "", None
    for line in text.splitlines():
        stripped = line.strip()
        match = re.match(r"^\s*(Given|When|Then|And|But|\*|Soit|Quand|Alors|Et|Mais)\s+(.+?)\s*$", line)
        if match:
            step, header = match.group(2), None
            continue
        if not stripped.startswith("|"):
            step = "" if stripped and not stripped.startswith("#") else step
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if header is None:
            header = cells
            continue
        category = next((c for c, p in STEP_CATEGORIES if p.search(step)), "") if _CREATION_STEP.search(step) else ""
        if category and "code" in header and header.index("code") < len(cells):
            value = cells[header.index("code")]
            if value and not re.search(r"[$<{]", value):
                codes.setdefault(category, []).append(value)
    return codes


def merge_codes(*sources: dict) -> dict:
    merged = {}
    for source in sources:
        for category, values in source.items():
            merged[category] = list(dict.fromkeys(merged.get(category, []) + values))
    return merged


def client_init(worktree: str, client_name: str, init_tags: str = "") -> dict:
    """
    Initialisation du client dans le worktree : {"features": [{chemin, tags, steps, fichiers}], "codes":
    {catégorie: [codes]}, "avertissements": [...]}, écrite dans init_client.json. `init_tags` (CUCUMBER_INIT_TAGS)
    désigne les features d'init quand il est donné.
    """
    present = repo_files(worktree)
    wanted = init_tags_wanted(init_tags)
    features, warnings, codes = [], [], {}
    for rel in sorted(p for p in present if p.startswith(FEATURES_ROOT) and p.endswith(".feature")):
        text = _read_text_file(os.path.join(worktree, *rel.split("/")))
        if not is_init_feature(rel, text, wanted):
            continue
        files = init_data_files(rel, text, present)
        features.append({"chemin": rel, "tags": sorted(feature_tags(text)), "steps": step_lines(text)[:40],
                         "fichiers": files})
        codes = merge_codes(codes, file_codes(worktree, files, warnings), step_codes(text))
    result = {"features": features, "codes": {k: sorted(v) for k, v in codes.items()}, "avertissements": warnings}
    _write_json(init_path(client_name), result)
    return result


def init_paths(init: dict) -> list:
    return [f["chemin"] for f in (init or {}).get("features", [])]


def init_block(init: dict, clear_name: str, max_codes: int = MAX_PROMPT_CODES) -> str:
    """Bloc du prompt de lot (borné) : codes créés par l'init du client et interdiction de réécrire purge et init."""
    paths = init_paths(init)
    if not paths:
        return ("- Initialisation du client : aucune feature d'initialisation détectée dans le dépôt client ; crée "
                "toute donnée de référence utilisée dans le Background de la feature.")
    parts = []
    for category, label in LABELS.items():
        values = (init.get("codes") or {}).get(category) or []
        if values:
            shown = ", ".join(values[:max_codes]) + (f", … ({len(values) - max_codes} autres)"
                                                     if len(values) > max_codes else "")
            parts.append(f"{label} [{shown}]")
    created = "; ".join(parts) or "aucun code extrait de ses fichiers"
    return (f"- Données créées par l'init du client : {created}. Utilise UNIQUEMENT ces codes, ou crée la donnée dans "
            f"le Background de la feature. Ne réécris jamais de purge ni d'init : elles sont faites par "
            f"{', '.join(paths)} (seul clear autorisé : « {clear_name} »).")


# ---------------------------------------------------------------------------
# Contrôles d'un lot par rapport à l'initialisation du client
# ---------------------------------------------------------------------------

# Colonnes de tables qui utilisent un code de site ou de donneur d'ordres
USAGE_COLUMNS = (("sites", re.compile(r"(^|\.)(refSite\.code|refSiteCode|siteCode)$", re.I)),
                 ("donneurs_ordres", re.compile(r"(^|\.)(refStockOwner\.code|refStockOwnerCode|stockOwnerCode)$",
                                                re.I)),
                 ("secteurs", re.compile(r"(^|\.)((wms|ref)Sector\.code|(wms|ref)?SectorCode)$", re.I)),
                 ("partenaires", re.compile(r"(^|\.)((exc|ref)Partner\.code|(exc|ref)?PartnerCode)$", re.I)))
USAGE_LABELS = {"sites": "site", "donneurs_ordres": "donneur d'ordres", "secteurs": "secteur",
                "partenaires": "partenaire"}
_PURGE_OR_INIT = re.compile(r"^INIT- (Purge\b|Initialisation globale)")


def used_codes(text: str) -> list:
    """[(ligne, catégorie, code)] des codes de site et de donneur d'ordres des tables d'une feature."""
    out, header, in_table = [], None, False
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            header, in_table = (None, False) if stripped and not stripped.startswith("#") else (header, in_table)
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not in_table:
            header, in_table = cells, True
            continue
        for column, value in zip(header or [], cells):
            category = next((c for c, p in USAGE_COLUMNS if p.search(column)), "")
            if category and value and not re.search(r"[$<{]", value):
                out.append((number, category, value))
    return out


def feature_created_codes(worktree: str, rel: str, text: str, present: set) -> dict:
    """Codes créés par la feature elle-même : tables de steps de création et fichiers de données qu'elle intègre."""
    files = [p for p in (resolved_path(rel, r) for r in data_references(text)) if p in present]
    return merge_codes(step_codes(text), file_codes(worktree, files, []))


def check_init_codes(worktree: str, features: list, init: dict) -> list:
    """
    Codes de site, de donneur d'ordres, de secteur ou de partenaire utilisés par les features, absents de l'init du
    client (codes de référence du profil) et non créés dans la feature : une erreur par code. Une catégorie sans code extrait de l'init n'est pas contrôlée.
    """
    known, paths, present, errors = (init or {}).get("codes") or {}, init_paths(init), repo_files(worktree), []
    for rel in (f for f in features if f not in paths):
        text = _read_text_file(os.path.join(worktree, *rel.split("/")))
        created = feature_created_codes(worktree, rel, text, present)
        reported = set()
        for number, category, code in used_codes(text):
            if not known.get(category) or code in known[category] or code in created.get(category, []) \
                    or (category, code) in reported:
                continue
            reported.add((category, code))
            errors.append(f"{rel}:{number} {USAGE_LABELS[category]} « {code} » absent de l'initialisation du client "
                          f"({', '.join(paths)}) et non créé dans la feature : utilise l'un de "
                          f"{', '.join(known[category][:10])}, ou crée-le dans le Background")
    return errors


def check_execution_order(features: list, init: dict) -> list:
    """Features qui s'exécuteraient avant l'init du client (Cucumber trie les features par chemin)."""
    paths = init_paths(init)
    if not paths:
        return []
    last = max(paths)
    return [f"{rel} : s'exécute avant l'initialisation du client {last} (Cucumber trie les features par chemin, "
            f"majuscules avant minuscules) : place-la sous la racine du client, après l'initialisation"
            for rel in features if rel not in paths and rel.endswith(".feature") and rel < last]


def check_no_init_rewrite(worktree: str, new: set, init: dict) -> list:
    """Nouvelles features qui réécrivent une purge ou une initialisation : faites par la feature d'init du client."""
    paths = init_paths(init)
    errors = []
    for rel in sorted(f for f in new if f not in paths):
        for number, step in enumerate(_read_text_file(os.path.join(worktree, *rel.split("/"))).splitlines(), 1):
            match = re.match(r"^\s*\S+\s+(INIT- .*)$", step)
            if match and _PURGE_OR_INIT.match(match.group(1)):
                errors.append(f"{rel}:{number} « {match.group(1).strip()[:80]} » : ne réécris jamais de purge ni "
                              f"d'init, elles sont faites par {', '.join(paths) or 'la feature d init du client'} ; "
                              f"supprime ce step")
    return errors


def check_lot_init(worktree: str, features: list, new: set, init: dict) -> list:
    """Contrôles d'un lot par rapport à l'init du client : ordre d'exécution, purge / init réécrites, codes inconnus."""
    features = [f for f in features if f.endswith(".feature")
                and os.path.exists(os.path.join(worktree, *f.split("/")))]
    return (check_execution_order(features, init) + check_no_init_rewrite(worktree, new, init)
            + check_init_codes(worktree, features, init))
