"""
Bibliothèque Cucumber du dépôt <client>-cucumber alignée sur la version du Standard du client (0 token, sauf
correction des erreurs de compilation par un appel Claude borné, voir main.align_library).

- Version cible (target_version) : celle qu'exécute Jenkins pour ce client (CUCUMBER_STANDARD_BRANCH du dernier
  build du client, source « jenkins » de la version du Standard), à défaut celle de l'application du client (pom de
  l'application, source « code »). Écart entre les deux : avertissement, Jenkins l'emporte.
- Version disponible (choose_version) : versions publiées de l'artefact dans le dépôt Maven (maven-metadata.xml) ;
  la version exacte de la cible, sinon la plus haute ≤ cible, jamais supérieure ; aucune : rien n'est changé.
- Mise à jour (set_pom_version) : SEULE la version de la bibliothèque change dans le pom.xml (sa ligne <version>
  ou la propriété qu'elle référence) — exception explicite à l'interdiction de toucher au pom, contrôlée par
  only_version_changed avant tout push (cucumber_delivery).
- Contournements TODO-VERSION devenus inutiles (workaround_proposals) : proposés au retrait, jamais retirés seuls.
- Déclenchement (gap) : écart entre la version du pom et la cible, coche « mise à jour automatique de la
  bibliothèque » du profil (« maj_bibliotheque » : vide ou auto = activée, « non » = désactivée).
"""
import re
from datetime import datetime

from tools import standard_version
from tools.cucumber_checks import version_key
from tools.cucumber_library import LIBRARY_ARTIFACT, LIBRARY_GROUP, _xml_text

AUTO_FIELD = "maj_bibliotheque"


def enabled(profile: dict) -> bool:
    """Mise à jour automatique de la bibliothèque : activée par défaut, « non » la désactive."""
    return str((profile or {}).get(AUTO_FIELD) or "auto").strip().lower() not in ("non", "false", "0", "off")


def target_version(document: dict) -> dict:
    """
    Version cible du client d'après sa version du Standard détectée (profil « version_standard ») :
    {"version", "source", "detail", "avertissement"} ; version '' si aucune source.
    """
    sources = {s.get("source"): s for s in ((document or {}).get("version_standard") or {}).get("sources") or []}
    jenkins, code = sources.get("jenkins") or {}, sources.get("code") or {}
    chosen = jenkins if jenkins.get("version") else code
    warning = ""
    if jenkins.get("version") and code.get("version") and jenkins["version"] != code["version"]:
        warning = (f"Jenkins exécute {jenkins['version']} ({jenkins.get('detail', '')}) mais l'application du client "
                   f"est en {code['version']} ({code.get('detail', '')}) : la version exécutée par Jenkins est retenue")
    return {"version": chosen.get("version", ""), "source": chosen.get("source", ""),
            "detail": chosen.get("detail", ""), "avertissement": warning}


def gap(current: str, target: str) -> bool:
    """La bibliothèque du pom n'est pas sur la ligne de la cible (majeure.mineure)."""
    return bool(current and target) and standard_version.normalize(current)[0] != standard_version.normalize(target)[0]


def metadata_versions(xml_text: str) -> list:
    """Versions publiées d'un maven-metadata.xml."""
    return re.findall(r"<version>\s*([^<\s]+)\s*</version>", re.sub(r"<!--.*?-->", "", xml_text or "", flags=re.S))


def available_versions(fetch=None) -> dict:
    """{"versions", "source", "erreur"} : maven-metadata.xml de a-sis:Cucumber dans MAVEN_REPOSITORY_URL."""
    if fetch is None:
        import requests
        from tools.cucumber_job import maven_repository
        base, auth = maven_repository()
        if not base:
            return {"versions": [], "source": "", "erreur": "MAVEN_REPOSITORY_URL non configuré"}
        url = f"{base}/{LIBRARY_GROUP.replace('.', '/')}/{LIBRARY_ARTIFACT}/maven-metadata.xml"

        def fetch():
            response = requests.get(url, auth=auth, timeout=20)
            response.raise_for_status()
            return url, response.text
    try:
        source, text = fetch()
    except Exception as e:  # noqa: BLE001 - dépôt injoignable : aucune version, rien n'est changé
        return {"versions": [], "source": "", "erreur": f"maven-metadata.xml illisible : {e}"}
    return {"versions": metadata_versions(text), "source": source, "erreur": ""}


def _line(version: str) -> tuple:
    return tuple(int(p) for p in standard_version.normalize(version)[0].split(".")) \
        if standard_version.normalize(version)[0] else ()


def choose_version(versions: list, target: str) -> dict:
    """
    {"version", "motif"} : version publiée exacte de la cible (même ligne majeure.mineure, la plus haute de cette
    ligne), sinon la plus haute ≤ cible ; jamais supérieure ; instantanés écartés. Aucune : version ''.
    """
    goal = _line(target)
    releases = [v for v in versions or [] if "SNAPSHOT" not in v.upper() and _line(v)]
    if not goal:
        return {"version": "", "motif": f"version cible illisible : {target or 'aucune'}"}
    exact = sorted((v for v in releases if _line(v) == goal), key=version_key)
    if exact:
        return {"version": exact[-1], "motif": f"version exacte de la cible {target}"}
    lower = sorted((v for v in releases if _line(v) < goal), key=version_key)
    if lower:
        return {"version": lower[-1], "motif": f"pas de version {target} publiée : la plus haute inférieure"}
    return {"version": "", "motif": f"aucune version publiée ≤ {target} (versions : {', '.join(releases[:8]) or 'aucune'})"}


def set_pom_version(pom_text: str, new: str) -> tuple:
    """
    pom.xml avec la SEULE version de a-sis:Cucumber changée (ligne <version> de la dépendance, ou propriété
    ${…} qu'elle référence) : (texte, ancienne version). Lève ValueError si la dépendance est introuvable.
    """
    text = pom_text or ""
    for match in re.finditer(r"<dependency>(.*?)</dependency>", text, re.S):
        block = match.group(1)
        if _xml_text(block, "groupId") != LIBRARY_GROUP or _xml_text(block, "artifactId") != LIBRARY_ARTIFACT:
            continue
        version = re.search(r"(<version>\s*)([^<\s]+)(\s*</version>)", block)
        if not version:
            raise ValueError("dépendance a-sis:Cucumber sans <version>")
        prop = re.fullmatch(r"\$\{([^}]+)\}", version.group(2))
        if prop:
            name = re.escape(prop.group(1))
            found = re.search(rf"(<{name}>\s*)([^<\s]+)(\s*</{name}>)", text)
            if not found:
                raise ValueError(f"propriété {prop.group(1)} introuvable")
            return text[:found.start(2)] + new + text[found.end(2):], found.group(2)
        start = match.start(1) + version.start(2)
        return text[:start] + new + text[match.start(1) + version.end(2):], version.group(2)
    raise ValueError("dépendance a-sis:Cucumber introuvable dans le pom.xml")


def only_version_changed(before: str, after: str) -> bool:
    """Le pom après est exactement le pom avant dont seule la version de la bibliothèque a été remplacée."""
    from tools.cucumber_library import pom_library_version
    new = pom_library_version(after)
    try:
        return bool(new) and before != after and set_pom_version(before, new)[0] == after
    except ValueError:
        return False


_COMMENTED_STEP = re.compile(r"^\s*#\s*((?:Given|When|Then|And|But|\*)\s+.+?)\s*$")


def workaround_proposals(markers: list, texts: dict, known) -> list:
    """
    Contournements TODO-VERSION (`markers` [{fichier, ligne, texte}], `texts` {fichier: texte}) que la nouvelle
    version rend inutiles : step commenté sous le marqueur, désormais défini (`known(step) -> bool`). Proposés au
    retrait : [{fichier, ligne, step, raison}].
    """
    out = []
    for marker in markers or []:
        lines = (texts.get(marker["fichier"]) or "").splitlines()
        following = lines[marker["ligne"]] if marker["ligne"] < len(lines) else ""
        step = _COMMENTED_STEP.match(following)
        if step and known(re.sub(r"^\s*(Given|When|Then|And|But|\*)\s+", "", step.group(1))):
            out.append({"fichier": marker["fichier"], "ligne": marker["ligne"], "step": step.group(1),
                        "raison": f"step défini dans la nouvelle version (contournement : {marker['texte'][:120]})"})
    return out


def record(state: dict, result: dict) -> dict:
    """Montée de version ajoutée à l'état (« bibliotheque »), pour le bilan."""
    result = {"date": datetime.now().isoformat(timespec="seconds"), **result}
    state.setdefault("bibliotheque", []).append(result)
    return result


def bilan_lines(items: list) -> list:
    """Bilan : ancienne et nouvelle version, source de la cible, corrections, contournements, runs, décision."""
    lines = []
    for item in items or []:
        lines.append(f"- {item['date'][:16]} · bibliothèque {item.get('ancienne') or '?'} → "
                     f"{item.get('nouvelle') or '—'} (cible {item.get('cible') or '?'}, source {item.get('source')}) — "
                     f"{'✅ livrée' if item.get('livree') else '⏸ non livrée'} : {item.get('motif', '')}")
        if item.get("avertissement"):
            lines.append(f"  - ⚠️ {item['avertissement']}")
        lines += [f"  - correction : {c}" for c in item.get("corrections") or []]
        lines += [f"  - contournement retirable : {w['fichier']}:{w['ligne']} — {w['step']}"
                  for w in item.get("contournements") or []]
        lines += [f"  - run : {r.get('type', 'complet')}, import n° {r.get('import') or '?'}, {r.get('echecs')} "
                  f"échec(s)" + (f" — {r['build']}" if r.get("build") else "") for r in item.get("runs") or []]
        lines += [f"  - casse : {c}" for c in item.get("casse") or []]
    return lines
