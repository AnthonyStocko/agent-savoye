"""
Migration Java 21 du projet de tests <client>-cucumber et contrôle de non-régression (onglet Cucumber, bouton
« Migration Java 21 et contrôle de non-régression » ; main.run_java21_migration), 0 token.

But : confirmer qu'après le passage de l'application du client en Java 21, aucune fonctionnalité couverte par les
tests n'a changé de comportement. Les tests eux-mêmes ne sont JAMAIS modifiés.

1. Migration (migrate_pom) sur une branche dédiée (« migration-java21 » par défaut, profil « java21_branche »),
   créée depuis la branche lue par le job : seul pom.xml change — version Java (maven.compiler.release, ou
   source/target, java.version) portée à 21, et versions des plugins de build trop anciennes pour Java 21 (compiler,
   surefire, failsafe), chaque changement avec sa raison. Contrôle déterministe (branch_errors) : le diff de la
   branche ne touche que pom.xml, et ce pom est exactement le pom d'origine migré ; sinon arrêt.
   Compilation en Java 21 et dry-run strict : mêmes scénarios, même nombre de steps qu'avant (compare_dry_runs).
   Une erreur de compilation due à Java 21 n'est PAS corrigée : elle est rapportée (compile_findings : fichier,
   ligne, API, remplacement proposé) et attend une décision humaine.
2. Préalables : l'outillage du job compile en Java 21 (toolchain : version lue dans la console d'un essai), et les
   paramètres d'environnement AVANT (application en Java 11) et APRÈS (application en Java 21) sont confirmés par
   l'utilisateur (propose_environments les propose depuis le profil et l'historique Jenkins).
3. Runs de comparaison : même filtre de tags, même branche de tests, seuls l'environnement et le pom changent.
   AVANT réutilisé s'il existe un run complet vert récent (reference_import), sinon relancé ; APRÈS sur la branche
   migrée ; chaque scénario dont le résultat diffère est relancé une fois en APRÈS (instabilité écartée).
4. Rapport (compare_results, report_markdown) : verdict, tableau par scénario, nouveaux échecs classés
   (classify_failure), durées comparées, liens des builds et diff du pom.
5. Livraison : la branche n'est JAMAIS fusionnée par l'agent ; sans régression, une PR est proposée vers la branche
   lue par le job, pour validation humaine (pull_request_payload).
"""
import os
import re
from datetime import datetime, timedelta

TARGET_JAVA = 21
DEFAULT_BRANCH = "migration-java21"
DEFAULT_REFERENCE_DAYS = 7
ESSAI_TAG = "@java21_essai_compilation"
REPORT_FILE = "java21.md"
BEGIN = "<!-- MIGRATION-JAVA21:DEBUT -->"
END = "<!-- MIGRATION-JAVA21:FIN -->"

# Version minimale des plugins de build pour Java 21, et version posée si la version du pom est plus ancienne
PLUGIN_MINIMUMS = {
    "maven-compiler-plugin": ("3.11.0", "3.13.0", "prise en charge de release 21 (versions antérieures : "
                                                  "« invalid target release » ou analyse des classes 65 incomplète)"),
    "maven-surefire-plugin": ("3.0.0", "3.2.5", "exécution des tests sur une JVM 21 (fournisseur JUnit Platform "
                                                "et modules Java des versions 2.x)"),
    "maven-failsafe-plugin": ("3.0.0", "3.2.5", "même raison que surefire"),
}
JAVA_PROPERTIES = ("maven.compiler.release", "maven.compiler.source", "maven.compiler.target", "java.version",
                   "maven.compiler.testRelease", "maven.compiler.testSource", "maven.compiler.testTarget")


def _version_key(version: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", version or "")[:4])


def _java_level(value: str) -> int:
    """« 1.8 » -> 8, « 11 » -> 11, « ${x} » -> 0."""
    value = (value or "").strip()
    if value.startswith("1."):
        value = value[2:]
    return int(value) if value.isdigit() else 0


# ---------------------------------------------------------------------------
# 1. Migration du pom.xml
# ---------------------------------------------------------------------------

def _properties_block(text: str):
    return re.search(r"<properties>(.*?)</properties>", text, re.S)


def java_version(pom: str) -> int:
    """Niveau Java du pom (release, sinon target/source, sinon java.version) ; 0 si inconnu."""
    block = _properties_block(pom or "")
    props = dict(re.findall(r"<([\w.-]+)>\s*([^<\s]+)\s*</\1>", block.group(1))) if block else {}
    for name in ("maven.compiler.release", "maven.compiler.target", "maven.compiler.source", "java.version"):
        if _java_level(props.get(name, "")):
            return _java_level(props[name])
    plugin = re.search(r"<artifactId>\s*maven-compiler-plugin\s*</artifactId>(.*?)</plugin>", pom or "", re.S)
    for tag in ("release", "target", "source"):
        found = re.search(rf"<{tag}>\s*([^<\s]+)\s*</{tag}>", plugin.group(1)) if plugin else None
        if found:
            value = found.group(1)
            prop = re.fullmatch(r"\$\{([^}]+)\}", value)
            level = _java_level(props.get(prop.group(1), "")) if prop else _java_level(value)
            if level:
                return level
    return 0


def migrate_pom(pom: str, target: int = TARGET_JAVA) -> dict:
    """
    pom.xml porté en Java `target` : {"pom", "changements": [{"element", "avant", "apres", "raison"}], "erreur"}.
    Seuls changent : les propriétés Java (JAVA_PROPERTIES) et les balises release/source/target littérales du
    maven-compiler-plugin, puis la version des plugins de PLUGIN_MINIMUMS si elle est plus ancienne (littérale ou
    propriété). Sans aucun réglage Java, maven.compiler.release est ajoutée aux <properties>.
    """
    text, changes = pom or "", []
    if "<project" not in text:
        return {"pom": text, "changements": [], "erreur": "pom.xml illisible (pas d'élément <project>)"}
    block = _properties_block(text)
    if block:
        inner = block.group(1)
        for name in JAVA_PROPERTIES:
            match = re.search(rf"(<{re.escape(name)}>\s*)([^<\s]+)(\s*</{re.escape(name)}>)", inner)
            if match and _java_level(match.group(2)) and _java_level(match.group(2)) != target:
                changes.append({"element": f"<properties>/{name}", "avant": match.group(2), "apres": str(target),
                                "raison": f"compilation des tests en Java {target}"})
                inner = inner[:match.start(2)] + str(target) + inner[match.end(2):]
        text = text[:block.start(1)] + inner + text[block.end(1):]
    plugin = re.search(r"(<artifactId>\s*maven-compiler-plugin\s*</artifactId>)(.*?)(</plugin>)", text, re.S)
    if plugin:
        body = plugin.group(2)
        for tag in ("release", "source", "target", "testRelease", "testSource", "testTarget"):
            match = re.search(rf"(<{tag}>\s*)([^<\s]+)(\s*</{tag}>)", body)
            if match and _java_level(match.group(2)) and _java_level(match.group(2)) != target:
                changes.append({"element": f"maven-compiler-plugin/<{tag}>", "avant": match.group(2),
                                "apres": str(target), "raison": f"compilation des tests en Java {target}"})
                body = body[:match.start(2)] + str(target) + body[match.end(2):]
        text = text[:plugin.start(2)] + body + text[plugin.end(2):]
    if not java_version(text):
        block = _properties_block(text)
        line = f"\n        <maven.compiler.release>{target}</maven.compiler.release>"
        if block:
            text = text[:block.end(1)].rstrip() + line + "\n    " + text[block.end(1):]
        else:
            anchor = re.search(r"</modelVersion>|</version>", text)
            if not anchor:
                return {"pom": pom, "changements": [], "erreur": "aucun emplacement pour les <properties> du pom"}
            text = (text[:anchor.end()] + f"\n\n    <properties>{line}\n    </properties>" + text[anchor.end():])
        changes.append({"element": "<properties>/maven.compiler.release", "avant": "(absent)", "apres": str(target),
                        "raison": f"aucun réglage de version Java : compilation des tests en Java {target}"})
    for artifact, (minimum, chosen, reason) in PLUGIN_MINIMUMS.items():
        found = re.search(rf"<artifactId>\s*{re.escape(artifact)}\s*</artifactId>\s*(<version>\s*)([^<\s]+)"
                          rf"(\s*</version>)", text)
        if not found:
            continue
        value = found.group(2)
        prop = re.fullmatch(r"\$\{([^}]+)\}", value)
        if prop:
            prop_match = re.search(rf"(<{re.escape(prop.group(1))}>\s*)([^<\s]+)(\s*</{re.escape(prop.group(1))}>)",
                                   text)
            if prop_match and _version_key(prop_match.group(2)) < _version_key(minimum):
                changes.append({"element": f"<properties>/{prop.group(1)} ({artifact})", "avant": prop_match.group(2),
                                "apres": chosen, "raison": reason})
                text = text[:prop_match.start(2)] + chosen + text[prop_match.end(2):]
        elif _version_key(value) < _version_key(minimum):
            changes.append({"element": f"{artifact}/<version>", "avant": value, "apres": chosen, "raison": reason})
            text = text[:found.start(2)] + chosen + text[found.end(2):]
    return {"pom": text, "changements": changes, "erreur": ""}


def branch_errors(changed_files: list, pom_before: str, pom_after: str, target: int = TARGET_JAVA) -> list:
    """
    Contrôle déterministe de la branche de migration : '' de diff hors pom.xml, et le pom de la branche est
    exactement le pom d'origine migré (aucun autre changement). Liste des erreurs (vide : conforme).
    """
    errors = [f"{path} : modifié par la migration (seul pom.xml est autorisé : features, steps, données et "
              f"scénarios restent intacts)" for path in changed_files if path != "pom.xml"]
    if "pom.xml" in changed_files and migrate_pom(pom_before, target)["pom"] != pom_after:
        errors.append("pom.xml : changements autres que la version Java et les versions des plugins de build")
    return errors


# ---------------------------------------------------------------------------
# Compilation et dry-run en Java 21
# ---------------------------------------------------------------------------

def jdk_version(output: str) -> int:
    """Version majeure de la JVM de Maven (« mvn -v » : « Java version: 21.0.2 ») ; 0 si illisible."""
    found = re.search(r"Java version:\s*(?:1\.)?(\d+)", output or "")
    return int(found.group(1)) if found else 0


def dry_run_signature(report: list) -> dict:
    """Scénarios d'un rapport de dry-run et leur nombre de steps (Background compris) : {id: steps}."""
    from tools.cucumber_import import _rel
    from tools.cucumber_verify import report_rel, scenario_id
    out = {}
    for feature in report if isinstance(report, list) else []:
        rel, background = _rel(report_rel(feature.get("uri", ""))), 0
        for element in feature.get("elements") or []:
            steps = len(element.get("steps") or [])
            if element.get("type") == "background":
                background = steps
                continue
            identifier = scenario_id(rel, str(element.get("name", "")))
            out[identifier] = out.get(identifier, 0) + steps + background
            background = 0
    return out


def compare_dry_runs(before: dict, after: dict) -> dict:
    """Mêmes scénarios et même nombre de steps avant et après la migration : {ok, manquants, ajoutes, steps}."""
    missing = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    steps = sorted(f"{i} : {before[i]} step(s) avant, {after[i]} après" for i in set(before) & set(after)
                   if before[i] != after[i])
    return {"ok": not (missing or added or steps) and bool(before), "manquants": missing, "ajoutes": added,
            "steps": steps, "scenarios": len(before), "nombre_steps": sum(before.values())}


# API retirées ou rendues inaccessibles entre Java 11 et 21 : (motif du message ou du code, API, remplacement)
REMOVED_APIS = (
    (r"javax\.xml\.bind|JAXBContext|DatatypeConverter", "javax.xml.bind (JAXB)",
     "jakarta.xml.bind:jakarta.xml.bind-api + implémentation (dépendance de test), imports jakarta.xml.bind"),
    (r"javax\.annotation\.(PostConstruct|PreDestroy|Generated|Resource)", "javax.annotation (Common Annotations)",
     "jakarta.annotation:jakarta.annotation-api, imports jakarta.annotation"),
    (r"javax\.activation", "javax.activation", "jakarta.activation:jakarta.activation-api"),
    (r"javax\.xml\.ws|javax\.jws", "javax.xml.ws (JAX-WS)", "jakarta.xml.ws:jakarta.xml.ws-api"),
    (r"sun\.misc\.BASE64(En|De)coder", "sun.misc.BASE64Encoder/Decoder", "java.util.Base64"),
    (r"sun\.misc\.Unsafe|sun\.reflect", "API internes sun.*", "API publiques (VarHandle, java.lang.invoke)"),
    (r"Nashorn|nashorn|ScriptEngineManager", "moteur JavaScript Nashorn", "org.openjdk.nashorn:nashorn-core ou GraalJS"),
    (r"Thread\.(stop|suspend|resume)\b|\.stop\(\)", "Thread.stop/suspend/resume",
     "interruption coopérative (Thread.interrupt)"),
    (r"SecurityManager|System\.setSecurityManager", "SecurityManager", "à retirer (déprécié pour suppression)"),
    (r"com\.sun\.tools\.javac|lombok", "Lombok (accès aux classes internes de javac)",
     "lombok 1.18.30 ou plus récent (dépendance de test, décision humaine : hors version Java et plugins)"),
    (r"new (Integer|Long|Double|Boolean|Character|Short|Byte|Float)\(", "constructeurs des types enveloppes",
     "Integer.valueOf(…) et équivalents"),
    (r"Runtime\.runFinalization|finalize\(\)", "finalisation", "Cleaner ou try-with-resources"),
    (r"InaccessibleObjectException|illegal reflective access|--illegal-access", "réflexion sur les modules du JDK",
     "--add-opens dans l'argLine de surefire (décision humaine)"),
)
_LOCATION = re.compile(r"^(?P<fichier>[^:\s][^:]*?):(?P<ligne>\d+)(?::\d+)?\s+(?P<message>.*)$")


def compile_findings(errors: list) -> list:
    """
    Erreurs de compilation Java 21, NON corrigées : [{fichier, ligne, api, remplacement, message}] pour décision
    humaine. Une erreur sans API connue garde « api » vide et propose une analyse manuelle.
    """
    out = []
    for error in errors or []:
        located = _LOCATION.match(error.strip())
        message = located.group("message") if located else error.strip()
        api, replacement = "", "à analyser : erreur sans API retirée connue"
        for pattern, name, proposal in REMOVED_APIS:
            if re.search(pattern, error):
                api, replacement = name, proposal
                break
        out.append({"fichier": located.group("fichier") if located else "", "ligne": int(located.group("ligne"))
                    if located else 0, "api": api, "remplacement": replacement, "message": message[:300]})
    return out


# ---------------------------------------------------------------------------
# 2. Préalables : outillage du job, paramètres d'environnement
# ---------------------------------------------------------------------------

_TOOLCHAIN_KO = (re.compile(r"invalid (?:target )?release:?\s*(\d+)", re.I),
                 re.compile(r"release version (\d+) not supported", re.I),
                 re.compile(r"Source option (\d+) is no longer supported|target release (\d+) conflicts", re.I))
_TOOLCHAIN_VERSION = (re.compile(r"Java version:\s*(?:1\.)?(\d+)"),
                      re.compile(r"(?:openjdk|java) version \"(?:1\.)?(\d+)"),
                      re.compile(r"\bjavac (?:1\.)?(\d+)\b"))
_JAVAC_RELEASE = re.compile(r"with javac \[[^\]]*\b(?:release|target) (\d+)\]")


def toolchain(console: str, target: int = TARGET_JAVA) -> dict:
    """
    Outillage du job d'après la console d'un essai : {"ok": True/False/None, "version", "preuve"}. None : version
    illisible (le job n'affiche ni « Java version » ni la compilation javac).
    """
    for line in (console or "").splitlines():
        for pattern in _TOOLCHAIN_KO:
            if pattern.search(line):
                return {"ok": False, "version": 0, "preuve": line.strip()[:200]}
    for pattern in _TOOLCHAIN_VERSION:
        found = pattern.search(console or "")
        if found:
            version = int(found.group(1))
            line = next(l for l in console.splitlines() if pattern.search(l))
            return {"ok": version >= target, "version": version, "preuve": line.strip()[:200]}
    found = _JAVAC_RELEASE.search(console or "")
    if found:
        version = int(found.group(1))
        line = next(l for l in console.splitlines() if _JAVAC_RELEASE.search(l))
        return {"ok": version >= target, "version": version, "preuve": line.strip()[:200]}
    return {"ok": None, "version": 0, "preuve": ""}


ENV_ROLES = ("composant_version", "kis_version")


def environment_errors(avant: dict, apres: dict, roles: dict) -> list:
    """Paramètres d'environnement confirmés : les deux rôles détectés sur le job, renseignés, AVANT ≠ APRÈS."""
    errors = [f"paramètre du rôle « {role} » non détecté sur le job (rôles des paramètres, onglet Cucumber)"
              for role in ENV_ROLES if not (roles or {}).get(role)]
    for label, values in (("AVANT", avant), ("APRÈS", apres)):
        missing = [role for role in ENV_ROLES if not str((values or {}).get(role) or "").strip()]
        if missing:
            errors.append(f"environnement {label} non confirmé : {', '.join(missing)} à renseigner")
    if not errors and all(str(avant[r]).strip() == str(apres[r]).strip() for r in ENV_ROLES):
        errors.append("environnements AVANT et APRÈS identiques : le contrôle ne comparerait rien")
    return errors


def overrides(values: dict, roles: dict) -> dict:
    """{nom du paramètre du job: valeur} pour les rôles d'environnement."""
    return {roles[r]: str(values[r]).strip() for r in ENV_ROLES if (roles or {}).get(r) and values.get(r)}


def propose_environments(profile: dict, builds: list, roles: dict, repository: str = "") -> dict:
    """
    Valeurs proposées (à confirmer) : AVANT = composant et KIS du dernier build vert du client ; APRÈS = version
    client choisie dans le profil (déduction Jenkins) si elle diffère, sinon le build du client le plus récent dont
    le composant diffère. {"avant", "apres", "sources"}.
    """
    repo = (repository or "").strip().lower()
    names = {r: (roles or {}).get(r) for r in ENV_ROLES}

    def values(build):
        params = build.get("parametres") or {}
        return {r: str(params.get(n) or "") for r, n in names.items() if n}

    mine = [b for b in builds or [] if not repo or repo in {str(v).strip().lower()
                                                            for v in (b.get("parametres") or {}).values()}]
    green = next((b for b in mine if str(b.get("resultat") or "").upper() == "SUCCESS"), None)
    avant = values(green) if green else {r: "" for r in ENV_ROLES}
    sources = {"avant": f"dernier build vert n° {green.get('numero')}" if green else "aucun build vert du client"}
    deduced = (((profile or {}).get("version_client") or {}).get("deduction") or {}).get("jenkins") or {}
    apres = {r: str(deduced.get(r) or "") for r in ENV_ROLES}
    if all(apres.values()) and apres != avant:
        sources["apres"] = "version client choisie (Données du projet)"
    else:
        other = next((b for b in mine if values(b).get("composant_version")
                      and values(b).get("composant_version") != avant.get("composant_version")), None)
        apres = values(other) if other else {r: "" for r in ENV_ROLES}
        sources["apres"] = (f"build n° {other.get('numero')} (autre version du composant)" if other
                            else "aucune valeur connue : à renseigner")
    return {"avant": avant, "apres": apres, "sources": sources}


def reference_import(imports: list, branch: str, avant: dict, roles: dict, params_of, max_days: int,
                     now: datetime = None) -> dict:
    """
    Run AVANT réutilisable : import d'un run complet vert (aucun échec) de moins de `max_days` jours, sur la même
    branche de tests et avec les paramètres d'environnement AVANT. `imports` : [{entry, analyse}] (plus récent
    d'abord) ; `params_of(entry)` : paramètres du build. Renvoie l'analyse retenue, {} si aucune.
    """
    now = now or datetime.now()
    expected = overrides(avant, roles)
    for item in imports or []:
        entry, analysis = item.get("entry") or {}, item.get("analyse") or {}
        try:
            when = datetime.fromisoformat(entry.get("date", ""))
        except ValueError:
            continue
        results = analysis.get("resultats") or {}
        if now - when > timedelta(days=max_days) or not analysis.get("run_complet") or not results \
                or any(r.get("statut") != "passe" for r in results.values()):
            continue
        params = params_of(entry) or {}
        if roles.get("branche") and str(params.get(roles["branche"], "")).strip() != branch:
            continue
        if all(str(params.get(name, "")).strip() == value for name, value in expected.items()):
            return analysis
    return {}


# ---------------------------------------------------------------------------
# 3-4. Comparaison des runs et rapport
# ---------------------------------------------------------------------------

CATEGORIES = {"identique": "identique", "nouvel_echec": "nouvel échec", "nouveau_succes": "nouveau succès",
              "instable": "instable", "absent_apres": "absent après", "nouveau_scenario": "absent avant"}
_ENV_FAILURE = re.compile(
    r"UnsupportedClassVersionError|class file version|Unsupported class file major version|"
    r"InaccessibleObjectException|module java\.\w+ does not \"opens\"|illegal reflective|"
    r"Connection refused|ConnectException|UnknownHostException|503 Service Unavailable|502 Bad Gateway|"
    r"application (?:not|non) (?:started|démarrée)|failed to start|n'a pas démarré|"
    r"NoSuchMethodError|NoClassDefFoundError|ClassNotFoundException|socle|version du Standard", re.I)


def _status(result) -> str:
    return (result or {}).get("statut", "") if result else ""


def compare_results(before: dict, after: dict, rerun: dict = None) -> list:
    """
    Lignes par scénario ({id: résultat} AVANT, APRÈS, relance APRÈS des écarts) :
    {id, avant, apres, relance, categorie, confirme, message}. Un écart (statut différent) est confirmé si la
    relance donne le même statut que le premier APRÈS ; relance revenue au statut d'AVANT : « instable ».
    """
    rerun = rerun or {}
    rows = []
    for identifier in sorted(set(before) | set(after)):
        avant, apres = _status(before.get(identifier)), _status(after.get(identifier))
        again = _status(rerun.get(identifier))
        message = (after.get(identifier) or {}).get("message", "") or (before.get(identifier) or {}).get("message", "")
        if not apres:
            category, confirmed = "absent_apres", True
        elif not avant:
            category, confirmed = "nouveau_scenario", True
        elif avant == apres:
            category, confirmed = "identique", False
        elif again and again == avant:
            category, confirmed = "instable", False
        else:
            category = "nouvel_echec" if apres != "passe" else "nouveau_succes"
            confirmed = bool(again) and again == apres
        rows.append({"id": identifier, "avant": avant, "apres": apres, "relance": again, "categorie": category,
                     "confirme": confirmed, "message": message[:300]})
    return rows


def to_rerun(rows: list) -> list:
    """Scénarios dont le résultat diffère entre AVANT et APRÈS, à relancer une fois en APRÈS."""
    return [r["id"] for r in rows if r["categorie"] in ("nouvel_echec", "nouveau_succes")]


def classify_failure(row: dict) -> str:
    """Nouvel échec : « instabilite » (non confirmé), « environnement_java21 », sinon « regression_application »."""
    if row["categorie"] == "instable" or (row["categorie"] == "nouvel_echec" and not row["confirme"]):
        return "instabilite"
    if _ENV_FAILURE.search(row.get("message") or ""):
        return "environnement_java21"
    return "regression_application"


CLASS_LABELS = {"regression_application": "régression probable de l'application",
                "environnement_java21": "problème d'environnement Java 21 (démarrage, version du socle)",
                "instabilite": "instabilité"}


def verdict(rows: list) -> dict:
    """{"regression": bool, "ecarts": N, "libelle"} : écarts = différences confirmées et scénarios absents."""
    gaps = [r for r in rows if r["categorie"] in ("absent_apres", "nouveau_scenario")
            or (r["categorie"] in ("nouvel_echec", "nouveau_succes") and r["confirme"])]
    return {"regression": bool(gaps), "ecarts": len(gaps),
            "libelle": "Aucune régression détectée" if not gaps else f"{len(gaps)} écart(s)"}


def compare_durations(before: dict, after: dict, ratio: float = 1.5, min_seconds: float = 30.0) -> dict:
    """
    Durées par scénario ({id: secondes}) : {"avant", "apres", "ecart_pct", "notables": [(id, avant, après)]}.
    Notable : durée multipliée par `ratio` ou plus et allongée d'au moins `min_seconds`.
    """
    total_before, total_after = sum(before.values()), sum(after.values())
    notable = [(i, before[i], after[i]) for i in sorted(set(before) & set(after))
               if after[i] >= before[i] * ratio and after[i] - before[i] >= min_seconds]
    pct = round(100 * (total_after - total_before) / total_before, 1) if total_before else 0.0
    return {"avant": round(total_before, 1), "apres": round(total_after, 1), "ecart_pct": pct, "notables": notable}


_FEATURE_TITLE = re.compile(r"^\s*(?:Feature|Fonctionnalité)\s*:\s*(.+?)\s*$", re.M)
_JIRA = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")


def scenario_context(feature_text: str, title: str) -> dict:
    """Fonctionnalité (titre de la Feature) et tickets cités (tags, commentaires) autour du scénario."""
    feature = _FEATURE_TITLE.search(feature_text or "")
    lines = (feature_text or "").splitlines()
    index = next((n for n, line in enumerate(lines) if re.search(rf":\s*{re.escape(title)}\s*$", line)), None)
    around = "\n".join(lines[max(0, index - 6):index + 1]) if index is not None else ""
    header = "\n".join(lines[:next((n for n, l in enumerate(lines) if _FEATURE_TITLE.match(l)), 0) + 1])
    tickets = list(dict.fromkeys(_JIRA.findall(around) + _JIRA.findall(header)))
    return {"fonctionnalite": feature.group(1) if feature else "", "tickets": tickets}


def scenario_lines(feature_text: str, titles: list) -> dict:
    """Ligne de chaque scénario `titles` dans la feature (relance ciblée « fichier:ligne »)."""
    from tools.cucumber_verify import parse_scenarios
    found = {s["titre"]: s["ligne"] for s in parse_scenarios(feature_text or "")}
    return {t: found[t] for t in titles if t in found}


def rerun_selection(ids: list, feature_text) -> str:
    """Option Maven de relance ciblée : -Dcucumber.features=<fichier>:<ligne>,… (fichier entier si ligne inconnue)."""
    by_file = {}
    for identifier in ids:
        path, title = identifier.split("::", 1)
        by_file.setdefault(path, []).append(title)
    parts = []
    for path, titles in sorted(by_file.items()):
        lines = scenario_lines(feature_text(path), titles)
        parts += [f"{path}:{lines[t]}" for t in titles if t in lines] if len(lines) == len(titles) else [path]
    return f"-Dcucumber.features={','.join(parts)}" if parts else ""


def report_markdown(client: str, data: dict) -> str:
    """Rapport de non-régression (IHM, A_REVOIR.md, connaissances partagées)."""
    rows, verdict_ = data.get("lignes") or [], data.get("verdict") or {}
    lines = [f"# Migration Java 21 de {client}-cucumber — contrôle de non-régression", "",
             f"**Verdict : {verdict_.get('libelle') or data.get('motif') or 'non établi'}**", "",
             f"- Date : {data.get('date', '')[:16]} ; branche de migration `{data.get('branche', '')}` depuis "
             f"`{data.get('cible', '')}` (jamais fusionnée par l'agent)",
             f"- AVANT (Java 11) : {data.get('avant_libelle', '—')} — build {data.get('build_avant') or '—'}"
             + (" (run de référence réutilisé)" if data.get("avant_reutilise") else ""),
             f"- APRÈS (Java 21) : {data.get('apres_libelle', '—')} — build {data.get('build_apres') or '—'}"
             + (f" ; relance des écarts : {data['build_relance']}" if data.get("build_relance") else ""),
             f"- Outillage du job : {data.get('outillage', '—')}"]
    if data.get("motif") and not verdict_:
        lines += ["", f"⛔ Arrêt : {data['motif']}"]
    if data.get("pr"):
        lines.append(f"- PR proposée pour validation humaine : {data['pr']}")
    dry = data.get("dry_run") or {}
    if dry:
        lines.append(f"- Dry-run strict : {dry.get('scenarios', 0)} scénario(s), {dry.get('nombre_steps', 0)} step(s) "
                     + ("identiques avant / après" if dry.get("ok") else "**différents avant / après**"))
    findings = data.get("compilation") or []
    if findings:
        lines += ["", f"## Erreurs de compilation en Java 21 : {len(findings)} (non corrigées, décision humaine)", "",
                  "| Fichier | Ligne | API | Remplacement proposé | Message |", "|---|---|---|---|---|"]
        lines += [f"| `{f['fichier']}` | {f['ligne'] or ''} | {f['api'] or '—'} | {f['remplacement']} | "
                  f"{f['message'].replace('|', '/')} |" for f in findings]
    if rows:
        gaps = [r for r in rows if r["categorie"] != "identique"]
        lines += ["", f"## Résultats par scénario : {len(rows)} comparé(s), {len(gaps)} différent(s)", "",
                  "| Scénario | AVANT | APRÈS | Relance | Écart | Classement | Message d'échec APRÈS | Fonctionnalité "
                  "| Tickets |", "|---|---|---|---|---|---|---|---|---|"]
        for r in gaps or []:
            ctx = (data.get("contextes") or {}).get(r["id"], {})
            klass = CLASS_LABELS.get(r.get("classement", ""), "") if r["categorie"] in ("nouvel_echec", "instable") \
                else ""
            lines.append(f"| {r['id'].split('::', 1)[1]} (`{r['id'].split('::', 1)[0]}`) | {r['avant'] or '—'} | "
                         f"{r['apres'] or '—'} | {r['relance'] or '—'} | {CATEGORIES[r['categorie']]}"
                         f"{' (confirmé)' if r['confirme'] else ''} | {klass} | "
                         f"{(r['message'] if r['apres'] != 'passe' else '').replace('|', '/')[:200]} | "
                         f"{ctx.get('fonctionnalite', '')} | {', '.join(ctx.get('tickets') or [])} |")
        if not gaps:
            lines.append("| _tous identiques_ | | | | | | | | |")
        classes = {}
        for r in rows:
            if r["categorie"] in ("nouvel_echec", "instable"):
                classes.setdefault(r.get("classement", ""), []).append(r["id"])
        if classes:
            lines += ["", "## Nouveaux échecs classés", ""]
            lines += [f"- {CLASS_LABELS.get(k, k)} : {len(v)} — " + ", ".join(i.split("::", 1)[1] for i in v[:10])
                      for k, v in classes.items()]
    durations = data.get("durees") or {}
    if durations:
        lines += ["", "## Durées", "",
                  f"- Total : {durations['avant']} s AVANT, {durations['apres']} s APRÈS ({durations['ecart_pct']:+} %)"]
        lines += [f"- ⚠️ {i.split('::', 1)[1]} : {a:.0f} s → {b:.0f} s" for i, a, b in durations["notables"][:15]]
    if data.get("changements"):
        lines += ["", "## Changements du pom.xml", ""]
        lines += [f"- {c['element']} : {c['avant']} → {c['apres']} ({c['raison']})" for c in data["changements"]]
    if data.get("diff_pom"):
        lines += ["", "```diff", data["diff_pom"].strip(), "```"]
    return "\n".join(lines) + "\n"


def a_revoir_section(current: str, report: str) -> str:
    """A_REVOIR.md avec la section de la migration Java 21 remplacée (le reste conservé)."""
    section = f"{BEGIN}\n{report.strip()}\n{END}"
    if BEGIN in current and END in current:
        return current[:current.index(BEGIN)] + section + current[current.index(END) + len(END):]
    return (current.rstrip() + "\n\n" if current.strip() else "") + section + "\n"


# ---------------------------------------------------------------------------
# 5. Livraison : PR proposée, jamais de fusion
# ---------------------------------------------------------------------------

_SCM_URL = re.compile(r"/scm/([^/]+)/([^/]+?)(?:\.git)?/?$", re.I)


def repo_coordinates(url: str) -> tuple:
    """(projet, dépôt) Bitbucket d'une adresse de clone …/scm/<projet>/<dépôt>.git ; ('', '') si illisible."""
    found = _SCM_URL.search(re.sub(r"//[^@/]+@", "//", url or ""))
    return (found.group(1), found.group(2)) if found else ("", "")


def pull_request_payload(project: str, repo: str, source: str, target: str, client: str, report: str) -> dict:
    """Corps de création d'une PR Bitbucket (validation humaine ; l'agent ne fusionne jamais)."""
    ref = lambda branch: {"id": f"refs/heads/{branch}",  # noqa: E731
                          "repository": {"slug": repo, "project": {"key": project}}}
    return {"title": f"Migration Java 21 de {client}-cucumber (aucune régression détectée)",
            "description": (report[:30000] + "\n\n_PR proposée par l'agent : à valider et fusionner par une "
                                             "personne. L'agent ne fusionne jamais cette branche._"),
            "fromRef": ref(source), "toRef": ref(target)}


def reference_days() -> int:
    try:
        return max(0, int(os.getenv("CUCUMBER_JAVA21_REFERENCE_DAYS", "") or DEFAULT_REFERENCE_DAYS))
    except ValueError:
        return DEFAULT_REFERENCE_DAYS
