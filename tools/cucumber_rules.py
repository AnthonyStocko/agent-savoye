"""
Enseignements des campagnes (point 9), contrôles sans token.

Longueur des codes (9b) : chaque code créé par une feature (table d'un step de création, colonne « code ») ne
dépasse pas la longueur maximale du champ code de son entité, lue dans le modèle (classes @Entity des dépôts
serveur : @Column(length = …), @Size(max = …), @Length(max = …), constante résolue dans la classe ou une autre).
Sans modèle trouvé pour l'entité, DEFAULT_CODE_LENGTH s'applique. Un code trop long est une erreur de lot.
"""
import os
import re

from tools.cucumber_init_data import _CREATION_STEP, CATEGORIES, STEP_CATEGORIES
from tools.cucumber_tools import _git, _read_json, _read_text_file, _write_json, cache_dir

DEFAULT_CODE_LENGTH = 20
INDEX_FORMAT = 1
_MODEL_DIR = re.compile(r"[\\/](?:datamodel|model|models|entity|entities|domain)[\\/]", re.I)
_SKIPPED_DIRS = {".git", "target", "node_modules", "build", "dist"}
_CONSTANT = re.compile(r"\bstatic\s+final\s+(?:int|Integer)\s+(\w+)\s*=\s*(\d+)\s*;|"
                       r"\bfinal\s+static\s+(?:int|Integer)\s+(\w+)\s*=\s*(\d+)\s*;")
_CODE_FIELD = re.compile(r"^\s*(?:private|protected|public)?\s*String\s+code\s*;", re.M)
_LENGTH = re.compile(r"@Column\s*\([^)]*?\blength\s*=\s*([\w.]+)|@(?:Size|Length)\s*\([^)]*?\bmax\s*=\s*([\w.]+)")
_STEP = re.compile(r"^\s*(?:Given|When|Then|And|But|\*|Soit|Quand|Alors|Et|Mais)\s+(.+?)\s*$")
_ENTITY_PREFIX = re.compile(r"^(?:Ref|Wms|Tms|Oms|Exc|Spec)")


def _model_files(root: str):
    """Fichiers Java des dossiers de modèle d'un dépôt (sorties de build et front exclus)."""
    for directory, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIPPED_DIRS and not d.endswith(("-frontend", "-front", "-ui"))]
        if not _MODEL_DIR.search(directory + os.sep):
            continue
        for name in names:
            if name.endswith(".java"):
                yield os.path.join(directory, name)


def model_lengths(texts: dict) -> dict:
    """{Classe: longueur maximale du champ code} des classes @Entity ({Classe: source}), constantes résolues."""
    constants = {}
    for name, text in texts.items():
        for match in _CONSTANT.finditer(text):
            key, value = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), match.group(4))
            constants[f"{name}.{key}"] = int(value)
    lengths = {}
    for name, text in texts.items():
        if "@Entity" not in text:
            continue
        field = _CODE_FIELD.search(text)
        if not field:
            continue
        head = text[max(0, field.start() - 600):field.start()]
        found = [m.group(1) or m.group(2) for m in _LENGTH.finditer(head)]
        value = found[-1] if found else ""
        if value.isdigit():
            lengths[name] = int(value)
        elif value:
            qualified = value if "." in value else f"{name}.{value}"
            if qualified in constants:
                lengths[name] = constants[qualified]
    return lengths


def code_length_index(client_name: str, roots: list) -> dict:
    """
    Longueurs maximales des codes par entité, lues dans les dépôts `roots` (serveur du Standard et du client) ;
    cache .tmp/cucumber/<client>/longueurs_codes.json, recalculé quand un dépôt change de commit.
    """
    roots = [r for r in roots if r and os.path.isdir(r)]
    key = {"format": INDEX_FORMAT, "depots": {r: _git(r, "rev-parse", "HEAD").stdout.strip() for r in roots}}
    path = os.path.join(cache_dir(client_name), "longueurs_codes.json")
    cached = _read_json(path, {}) or {}
    if cached.get("cle") == key:
        return cached.get("longueurs", {})
    texts = {}
    for root in roots:
        for file in _model_files(root):
            text = _read_text_file(file)
            if "String code" in text or "static final" in text or "final static" in text:
                texts.setdefault(os.path.basename(file)[:-5], text)
    lengths = model_lengths(texts)
    _write_json(path, {"cle": key, "longueurs": lengths})
    return lengths


def entity_of(step: str, lengths: dict) -> str:
    """Entité créée par un step : catégorie de référence connue, sinon classe du modèle nommée dans le step."""
    category = next((c for c, p in STEP_CATEGORIES if p.search(step)), "")
    pattern = dict(CATEGORIES).get(category)
    if pattern:
        found = sorted(n for n in lengths if pattern.search(n))
        if found:
            return found[0]
    words = set(re.findall(r"[a-zà-ÿ]{4,}", step.lower()))
    return next((n for n in sorted(lengths) if n.lower() in words or _ENTITY_PREFIX.sub("", n).lower() in words), "")


def created_codes(text: str) -> list:
    """(step, code) des tables des steps de création d'une feature (colonne « code », valeurs littérales)."""
    out, step, header = [], "", None
    for line in text.splitlines():
        stripped = line.strip()
        match = _STEP.match(line)
        if match:
            step, header = match.group(1), None
            continue
        if not stripped.startswith("|"):
            step = "" if stripped and not stripped.startswith("#") else step
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if step and _CREATION_STEP.search(step) and "code" in header and header.index("code") < len(cells):
            value = cells[header.index("code")]
            if value and not re.search(r"[$<{]", value):
                out.append((step, value))
    return out


def check_code_lengths(worktree: str, features: list, lengths: dict) -> list:
    """Erreurs de lot : codes créés plus longs que la longueur maximale de leur entité (défaut si modèle absent)."""
    errors = []
    for rel in features:
        path = os.path.join(worktree, *rel.split("/"))
        if not os.path.exists(path):
            continue
        for step, code in created_codes(_read_text_file(path)):
            entity = entity_of(step, lengths)
            limit = lengths.get(entity, DEFAULT_CODE_LENGTH)
            if len(code) > limit:
                source = (f"longueur du code de {entity} dans son modèle" if entity else
                          f"longueur par défaut, modèle de l'entité introuvable")
                errors.append(f"{rel} : code « {code} » ({len(code)} caractères) au-delà de {limit} ({source}) dans "
                              f"« {step} » — raccourcis-le")
    return errors
