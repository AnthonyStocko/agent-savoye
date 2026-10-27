"""
Fichier .env de l'agent : contrôle au démarrage et écriture unique par l'IHM.

- check_env : chaque ligne doit être « CLÉ=valeur », un commentaire ou une ligne vide ; une ligne en défaut (texte
  collé, valeur répartie sur plusieurs lignes) est signalée avec son numéro, au lieu des avertissements bruts de
  python-dotenv (« could not parse statement starting at line … »).
- write_env : seule écriture du .env par l'IHM. Chaque valeur tient sur une ligne, entre guillemets doubles, avec
  les antislashs et guillemets échappés (chemins Windows et UNC compris) ; le fichier est relu et chaque valeur
  comparée après l'écriture.
- neutralize_invalid_lines : met en commentaire les lignes en défaut (copie .env.bak gardée), sans rien supprimer.
"""
import io
import os
import re
import shutil

from dotenv import dotenv_values
from dotenv.parser import parse_stream

_KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=")


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read()
    except OSError:
        return ""


def check_env(path: str) -> list:
    """Lignes en défaut du .env : [{"ligne", "message"}] ; [] si le fichier est valide ou absent."""
    text = _read(path)
    lines = text.splitlines()
    problems = []
    for binding in parse_stream(io.StringIO(text)):
        if not binding.error:
            continue
        number = binding.original.line
        content = lines[number - 1] if 0 < number <= len(lines) else ""
        what = ("ligne faite uniquement d'espaces" if content and not content.strip() else
                "texte sans « CLÉ= » (texte collé, ou suite d'une valeur répartie sur plusieurs lignes)"
                if not _KEY_LINE.match(content) else "valeur mal fermée (guillemet ou échappement)")
        problems.append({"ligne": number, "message": f"ligne {number} : {what}"})
    return problems


def check_line(problems: list) -> str:
    """Message de démarrage : « .env : lignes 18 à 47 invalides (…) »."""
    if not problems:
        return ""
    numbers = [p["ligne"] for p in problems]
    ranges, start = [], numbers[0]
    for previous, current in zip(numbers, numbers[1:] + [None]):
        if current != previous + 1:
            ranges.append(str(start) if start == previous else f"{start} à {previous}")
            start = current
    return (f"Fichier .env : {len(problems)} ligne(s) invalide(s) (ligne(s) {', '.join(ranges)}) — "
            f"{problems[0]['message'].split(' : ', 1)[1]}. Elles sont ignorées ; corrigez-les dans le fichier ou "
            f"neutralisez-les (mise en commentaire, copie .env.bak gardée).")


def format_value(value: str) -> str:
    """Valeur sur une ligne, entre guillemets doubles, antislashs et guillemets échappés."""
    one_line = " ".join(str(value).splitlines()) if "\n" in str(value) or "\r" in str(value) else str(value)
    return '"' + one_line.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_env(path: str, values: dict) -> None:
    """
    Écrit `values` ({clé: valeur}) dans le .env : ligne existante remplacée (première, hors commentaire), sinon
    ajoutée en fin. Relit le fichier et lève ValueError si une valeur relue diffère de la valeur écrite.
    """
    lines = _read(path).splitlines()
    pending = dict(values)
    for index, line in enumerate(lines):
        match = _KEY_LINE.match(line)
        if match and match.group(1) in pending:
            key = match.group(1)
            lines[index] = f"{key}={format_value(pending.pop(key))}"
    lines += [f"{key}={format_value(value)}" for key, value in pending.items()]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    read_back = dotenv_values(path)
    wrong = [k for k, v in values.items() if read_back.get(k) != " ".join(str(v).splitlines())
             and read_back.get(k) != str(v)]
    if wrong:
        raise ValueError(f"valeur(s) relue(s) différente(s) après l'écriture du .env : {', '.join(wrong)}")


def neutralize_invalid_lines(path: str) -> int:
    """Met en commentaire les lignes en défaut (copie <path>.bak) ; renvoie leur nombre."""
    problems = {p["ligne"] for p in check_env(path)}
    if not problems:
        return 0
    shutil.copyfile(path, path + ".bak")
    lines = _read(path).splitlines()
    lines = [("# " + line.strip()) if n in problems and line.strip() else ("" if n in problems else line)
             for n, line in enumerate(lines, 1)]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return len(problems)
