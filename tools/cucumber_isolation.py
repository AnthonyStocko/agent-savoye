"""
Isolation des clients (0 token) : aucune valeur du profil d'un client (clear, tag, sites et autres codes de
référence, chemins) ne doit apparaître dans les fichiers générés pour un autre client.

Les profils des autres clients sont ceux enregistrés dans le cache (.tmp/cucumber/<client>/profil.json). Une valeur
n'est pas comptée si elle appartient aussi au profil du client contrôlé (valeur réellement partagée) ou si elle
figure dans l'initialisation du Standard (données communes que chaque client recopie).
"""
import os
import re

from tools.cucumber_tools import CACHE_ROOT, _read_text_file

TEXT_EXTENSIONS = (".feature", ".java", ".json", ".csv", ".xml", ".txt", ".properties", ".md", ".yml", ".yaml")
MIN_LENGTH = 4
FIELD_LABELS = {"clear_name": "clear", "tag_client": "tag client", "features_root": "chemin des features",
                "files_root": "chemin des fichiers de données", "reference_codes": "code de référence",
                "spec_files": "fichier du module spécifique", "init_features": "feature d'init"}


def profile_values(profile: dict) -> list:
    """[(champ, valeur)] propres à un client : clear, tag, racines, codes de référence, fichiers."""
    out = []
    for key in ("clear_name", "tag_client", "features_root"):
        if (profile or {}).get(key):
            out.append((key, str(profile[key]).strip()))
    root = (profile or {}).get("features_root") or ""
    if "/features/" in root:
        out.append(("files_root", root.replace("/features/", "/files/", 1)))
    for codes in ((profile or {}).get("reference_codes") or {}).values():
        out += [("reference_codes", str(c).strip()) for c in codes or []]
    for key in ("spec_files", "init_features"):
        out += [(key, str(p).strip()) for p in (profile or {}).get(key) or []]
    return [(k, v) for k, v in out if len(v) >= MIN_LENGTH]


def other_profiles(client_name: str) -> dict:
    """{client: profil appliqué} des autres clients du cache."""
    from tools import cucumber_profile
    if not os.path.isdir(CACHE_ROOT):
        return {}
    out = {}
    for name in sorted(os.listdir(CACHE_ROOT)):
        if name.lower() == client_name.lower() or name.startswith("_"):
            continue
        if os.path.exists(os.path.join(CACHE_ROOT, name, "profil.json")):
            out[name] = cucumber_profile.effective_profile(cucumber_profile.load_profile(name))
    return out


def standard_init_text(cucumber_std_dir: str) -> str:
    """Texte de l'initialisation du Standard (features et fichiers de données texte) : valeurs communes."""
    if not cucumber_std_dir or not os.path.isdir(cucumber_std_dir):
        return ""
    from tools.cucumber_init import std_init_files
    paths = [p for paths in std_init_files(cucumber_std_dir).values() for p in paths
             if p.lower().endswith(TEXT_EXTENSIONS)]
    return "\n".join(_read_text_file(os.path.join(cucumber_std_dir, *p.split("/"))) for p in paths)


def foreign_values(client_name: str, own_profile: dict = None, common_text: str = "", others: dict = None) -> list:
    """
    [{client, champ, valeur}] des valeurs des autres clients à ne jamais voir chez `client_name` : hors valeurs de
    son propre profil et hors valeurs présentes dans `common_text` (initialisation du Standard).
    """
    own = {v for _, v in profile_values(own_profile or {})}
    out, seen = [], set()
    for other, profile in (other_profiles(client_name) if others is None else others).items():
        for key, value in profile_values(profile):
            if value in own or value in seen or (common_text and _cites(common_text, value)):
                continue
            seen.add(value)
            out.append({"client": other, "champ": key, "valeur": value})
    return out


def _cites(text: str, value: str) -> bool:
    # Racine de chemin (« …/autre/ ») : tout ce qui est dessous est cité
    end = "" if value.endswith("/") else r"(?![\w-])"
    return bool(re.search(r"(?<![\w-])" + re.escape(value) + end, text))


def scan(texts: dict, values: list) -> list:
    """Erreurs « valeur d'un autre client » pour {chemin: texte} ; une ligne par (fichier, valeur)."""
    errors = []
    for path, text in sorted(texts.items()):
        for item in values:
            if _cites(text or "", item["valeur"]):
                errors.append(f"{path} : valeur du profil du client {item['client']} "
                              f"({FIELD_LABELS.get(item['champ'], item['champ'])} « {item['valeur']} ») — chaque "
                              f"client a ses propres valeurs : reprendre celle du profil de ce client, ou la "
                              f"demander si elle est inconnue")
    return errors


def check_files(client_name: str, root: str, files: list, own_profile: dict = None, cucumber_std_dir: str = "",
                others: dict = None) -> list:
    """Contrôle des fichiers `files` (chemins relatifs à `root`) générés pour `client_name` ; [] si aucun écart."""
    texts = {f: _read_text_file(os.path.join(root, *f.split("/"))) for f in files
             if f.lower().endswith(TEXT_EXTENSIONS) and os.path.isfile(os.path.join(root, *f.split("/")))}
    if not texts:
        return []
    values = foreign_values(client_name, own_profile, standard_init_text(cucumber_std_dir), others)
    return scan(texts, values) if values else []
