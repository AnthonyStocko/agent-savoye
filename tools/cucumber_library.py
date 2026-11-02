"""
API de la bibliothèque de steps (a-sis:Cucumber, test-jar) dans la version du pom du dépôt client : tout appel Java
vers la bibliothèque est vérifié contre les classes de CETTE version (méthode, nombre d'arguments), jamais contre les
sources du Standard (souvent plus récentes) ni la version d'un autre client.

- pom_library_version(pom_text) : version déclarée par le pom du client (propriétés ${…} résolues).
- load_api(version) : classes et méthodes lues dans le jar du dépôt Maven local (fichiers .class, sans JDK), en cache
  .tmp/cucumber/_standard/api_bibliotheque_<version>.json.
- check_java(text, api, rel) : appels absents ou d'arité inconnue dans la version → erreurs « fichier:ligne … ».
- describe(api, classe) : méthodes publiques d'une classe (consultées avant d'écrire un appel).
"""
import difflib
import json
import os
import re
import struct
import zipfile

LIBRARY_GROUP, LIBRARY_ARTIFACT = "a-sis", "Cucumber"
_ACC_PUBLIC, _ACC_PROTECTED, _ACC_STATIC, _ACC_VARARGS = 0x0001, 0x0004, 0x0008, 0x0080
# Méthodes de java.lang.Object, présentes sur toute classe
_OBJECT_METHODS = {"toString": [0], "equals": [1], "hashCode": [0], "getClass": [0], "notify": [0],
                   "notifyAll": [0], "wait": [0, 1, 2]}


# ---------------------------------------------------------------------------
# Version et jar
# ---------------------------------------------------------------------------

def _xml_text(block: str, tag: str) -> str:
    match = re.search(rf"<{tag}>\s*([^<]*?)\s*</{tag}>", block)
    return match.group(1) if match else ""


def pom_library_version(pom_text: str) -> str:
    """Version de a-sis:Cucumber déclarée dans les dépendances du pom ('' si absente)."""
    text = re.sub(r"<!--.*?-->", "", pom_text or "", flags=re.S)
    properties = dict(re.findall(r"<([\w.-]+)>\s*([^<]*?)\s*</\1>",
                                 (re.search(r"<properties>(.*?)</properties>", text, re.S) or [None, ""])[1]))
    for block in re.findall(r"<dependency>(.*?)</dependency>", text, re.S):
        if _xml_text(block, "groupId") == LIBRARY_GROUP and _xml_text(block, "artifactId") == LIBRARY_ARTIFACT:
            version = _xml_text(block, "version")
            return re.sub(r"\$\{([^}]+)\}", lambda m: properties.get(m.group(1), m.group(0)), version)
    return ""


def repo_library_version(repo_dir: str) -> str:
    """Version de la bibliothèque du pom.xml du dépôt client ('' sans pom ou sans dépendance)."""
    try:
        with open(os.path.join(repo_dir, "pom.xml"), encoding="utf-8", errors="replace") as f:
            return pom_library_version(f.read())
    except OSError:
        return ""


def maven_repository() -> str:
    return os.getenv("MAVEN_LOCAL_REPO", "").strip() or os.path.join(os.path.expanduser("~"), ".m2", "repository")


def library_jar(version: str) -> str:
    """Chemin du test-jar de la version dans le dépôt Maven local ('' s'il n'y est pas)."""
    path = os.path.join(maven_repository(), *LIBRARY_GROUP.split("."), LIBRARY_ARTIFACT, version,
                        f"{LIBRARY_ARTIFACT}-{version}-tests.jar")
    return path if version and os.path.exists(path) else ""


# ---------------------------------------------------------------------------
# Lecture des .class
# ---------------------------------------------------------------------------

def _arg_count(descriptor: str) -> int:
    params, count, i = descriptor[1:descriptor.index(")")], 0, 0
    while i < len(params):
        while params[i] == "[":
            i += 1
        i = params.index(";", i) + 1 if params[i] == "L" else i + 1
        count += 1
    return count


def parse_class(data: bytes) -> dict:
    """{nom, super, methodes: {nom: [[arguments, statique, varargs]]}} (méthodes publiques et protégées)."""
    pos, pool = 10, [None]
    count = struct.unpack_from(">H", data, 8)[0]
    while len(pool) < count:
        tag = data[pos]
        if tag == 1:
            length = struct.unpack_from(">H", data, pos + 1)[0]
            pool.append(data[pos + 3:pos + 3 + length].decode("utf-8", errors="replace"))
            pos += 3 + length
            continue
        size = {3: 4, 4: 4, 5: 8, 6: 8, 7: 2, 8: 2, 9: 4, 10: 4, 11: 4, 12: 4, 15: 3, 16: 2, 17: 4, 18: 4,
                19: 2, 20: 2}[tag]
        pool.append(struct.unpack_from(">H", data, pos + 1)[0] if tag == 7 else None)
        pos += 1 + size
        if tag in (5, 6):
            pool.append(None)
    class_name = lambda index: pool[pool[index]].replace("/", ".") if index else ""
    _, this, parent, interfaces = struct.unpack_from(">HHHH", data, pos)
    pos += 8 + 2 * interfaces

    def members(pos):
        items, total = [], struct.unpack_from(">H", data, pos)[0]
        pos += 2
        for _ in range(total):
            flags, name, descriptor, attributes = struct.unpack_from(">HHHH", data, pos)
            pos += 8
            for _ in range(attributes):
                pos += 6 + struct.unpack_from(">I", data, pos + 2)[0]
            items.append((flags, pool[name], pool[descriptor]))
        return items, pos
    _, pos = members(pos)
    methods = {}
    for flags, name, descriptor in members(pos)[0]:
        if flags & (_ACC_PUBLIC | _ACC_PROTECTED) and not name.startswith("<"):
            entry = [_arg_count(descriptor), bool(flags & _ACC_STATIC), bool(flags & _ACC_VARARGS)]
            if entry not in methods.setdefault(name, []):
                methods[name].append(entry)
    return {"nom": class_name(this), "super": class_name(parent), "methodes": methods}


def read_jar(jar: str) -> dict:
    """{classe: {super, methodes}} de toutes les classes du jar."""
    classes = {}
    with zipfile.ZipFile(jar) as archive:
        for entry in archive.namelist():
            if entry.endswith(".class") and not entry.startswith("META-INF/"):
                try:
                    parsed = parse_class(archive.read(entry))
                except (struct.error, IndexError, KeyError, ValueError, TypeError):
                    continue
                classes[parsed["nom"]] = {"super": parsed["super"], "methodes": parsed["methodes"]}
    return classes


def _cache_path(version: str) -> str:
    from tools import cucumber_tools
    safe = re.sub(r"[^\w.-]", "_", version)
    return os.path.join(cucumber_tools.STD_CACHE_DIR, f"api_bibliotheque_{safe}.json")


def load_api(version: str, jar: str = "") -> dict:
    """
    API de la version : {"version", "jar", "classes"} ; {} si le jar n'est pas dans le dépôt Maven local (il y est
    après la première compilation du dépôt client). Les versions -SNAPSHOT ne sont jamais prises en cache.
    """
    jar = jar or library_jar(version)
    if not jar:
        return {}
    cache = _cache_path(version)
    stamp = os.path.getmtime(jar)
    if not version.endswith("-SNAPSHOT") and os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("date_jar") == stamp:
                return cached
        except (OSError, ValueError):
            pass
    api = {"version": version, "jar": jar, "date_jar": stamp, "classes": read_jar(jar)}
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(api, f)
    except OSError:
        pass
    return api


# ---------------------------------------------------------------------------
# Vérification des appels
# ---------------------------------------------------------------------------

def _blank_comments_and_strings(text: str) -> str:
    """Commentaires et contenu des chaînes remplacés par des espaces (lignes et guillemets conservés)."""
    def blank(match):
        text = match.group(0)
        if text[0] in "\"'":
            return text[0] + " " * (len(text) - 2) + text[-1]
        return re.sub(r"[^\n]", " ", text)
    return re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', blank, text, flags=re.S)


def _arguments(code: str, start: int) -> tuple:
    """(nombre d'arguments ou None si indécidable, fin) de l'appel dont la parenthèse ouvrante est en `start`."""
    depth, commas, empty = 0, 0, True
    for i in range(start, len(code)):
        char = code[i]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                inner = code[start + 1:i]
                if "<" in inner or "->" in inner:
                    return None, i
                return (0 if empty else commas + 1), i
        elif depth == 1 and char == ",":
            commas += 1
        if depth >= 1 and i > start and not char.isspace():
            empty = False
    return None, len(code)


def _lookup(api: dict, fqn: str, method: str) -> tuple:
    """(arités connues, classe connue jusqu'au bout de la hiérarchie) de `method` sur `fqn` et ses parents."""
    classes, arities, seen = api.get("classes") or {}, [], set()
    while fqn and fqn not in seen:
        seen.add(fqn)
        if fqn == "java.lang.Object":
            return arities + _OBJECT_METHODS.get(method, []), True
        info = classes.get(fqn)
        if info is None:
            return arities, False
        for entry in info["methodes"].get(method, []):
            arities.append(-entry[0] if entry[2] else entry[0])
        fqn = info["super"]
    return arities, True


def _accepts(arities: list, count: int) -> bool:
    return any(a == count or (a < 0 and count >= -a - 1) for a in arities)


def _all_methods(api: dict, fqn: str) -> dict:
    methods, seen = {}, set()
    while fqn and fqn not in seen and fqn in (api.get("classes") or {}):
        seen.add(fqn)
        for name, entries in api["classes"][fqn]["methodes"].items():
            methods.setdefault(name, entries)
        fqn = api["classes"][fqn]["super"]
    return methods


def _signature(name: str, entries: list) -> str:
    return " / ".join(f"{name}({'…' if e[2] else e[0]})" for e in entries)


def check_java(text: str, api: dict, rel: str = "") -> list:
    """
    Appels vers la bibliothèque absents de la version de `api` : méthode statique (Classe.methode(…)) ou méthode d'un
    champ / d'une variable typé(e) par une classe de la bibliothèque. Renvoie « rel:ligne message » ; [] sans API.
    """
    classes = api.get("classes") or {}
    if not classes:
        return []
    code = _blank_comments_and_strings(text)
    imported = {}
    for fqn, star in re.findall(r"^\s*import\s+(?!static\b)([\w.]+?)(\.\*)?\s*;", code, re.M):
        if star:
            imported.update({c.rsplit(".", 1)[1]: c for c in classes if c.rsplit(".", 1)[0] == fqn})
        elif fqn in classes:
            imported[fqn.rsplit(".", 1)[1]] = fqn
    if not imported:
        return []
    typed = {}
    for type_name, var in re.findall(r"\b([A-Z]\w*)(?:\s*<[^;(){}]*?>)?\s+([a-z_]\w*)\s*(?=[=;,)])", code):
        if type_name in imported:
            typed[var] = imported[type_name]
    errors = []
    for match in re.finditer(r"(?<![\w.])([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*(?=\()", code):
        receiver, method = match.group(1), match.group(2)
        fqn = imported.get(receiver) or typed.get(receiver)
        if not fqn:
            continue
        arities, complete = _lookup(api, fqn, method)
        line = code.count("\n", 0, match.start()) + 1
        where = f"{rel}:{line}" if rel else f"ligne {line}"
        simple = fqn.rsplit(".", 1)[1]
        if not arities:
            if not complete:
                continue
            available = _all_methods(api, fqn)
            close = difflib.get_close_matches(method, list(available), n=3, cutoff=0.5)
            errors.append(f"{where} {simple}.{method}() n'existe pas dans la bibliothèque {api.get('version')} "
                          f"(pom du client) — méthodes existantes proches : "
                          + (", ".join(_signature(c, available[c]) for c in close) or "aucune")
                          + " ; utilise une méthode ou un step existant de cette version")
            continue
        count, _ = _arguments(code, code.index("(", match.end()))
        if count is not None and complete and not _accepts(arities, count):
            errors.append(f"{where} {simple}.{method} : {count} argument(s) donné(s), la bibliothèque "
                          f"{api.get('version')} attend "
                          + " ou ".join(str(a) if a >= 0 else f"au moins {-a - 1}" for a in sorted(set(arities))))
    return list(dict.fromkeys(errors))


def has_method(api: dict, fqn: str, method: str, count: int) -> bool:
    arities, _ = _lookup(api, fqn, method)
    return _accepts(arities, count)


def describe(api: dict, name: str) -> str:
    """Méthodes publiques de la classe `name` (nom simple ou complet) dans la version, héritées comprises."""
    classes = api.get("classes") or {}
    matches = [c for c in classes if c == name or c.rsplit(".", 1)[-1] == name]
    if not matches:
        close = difflib.get_close_matches(name, sorted({c.rsplit(".", 1)[-1] for c in classes}), n=5, cutoff=0.5)
        return f"Classe {name} absente de la bibliothèque {api.get('version')}" + (
            f" ; classes proches : {', '.join(close)}" if close else "")
    lines = []
    for fqn in matches:
        lines.append(f"{fqn} (bibliothèque {api.get('version')}) :")
        for method, entries in sorted(_all_methods(api, fqn).items()):
            lines.append("  " + " / ".join(f"{'static ' if e[1] else ''}{method}({'…' if e[2] else e[0]} arg.)"
                                           for e in entries))
    return "\n".join(lines)
