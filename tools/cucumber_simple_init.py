"""
Étape 4 de l'onglet « Cucumber » simplifié : initialisation du client (0 token).

Clear : choix DÉTERMINISTE d'après les routes du code de l'application du client à la version choisie, jamais
repris d'un autre client ni deviné :
1. ressources de clear déployées : lignes « runtime: …/init-data-rest-clear-main-<m>@ » actives des bundle.list de
   l'application du client (à la référence de sa version), puis de chaque contrib qu'ils importent (« import: »),
   lue dans le Standard à la version du client, récursivement (deployed_clears) ;
2. routes de chaque ressource : annotations @DELETE("/clear/<m>/<x>") de sa classe REST dans le Standard à la même
   version (clear_routes) ;
3. décision (decide_clear) : « full/all » déployée → purge du Standard (« INIT- Purge de l'environnement ») ; sinon
   « <m>/all » de chaque module testé, dans l'ordre WMS, TMS, OMS, puis les autres. Un module testé sans clear
   déployé, ou dont la route « all » n'est pas prouvée, arrête l'étape avec la cause (aucune valeur par défaut).

Init : celle du Standard à la version du client, avec les seuls écarts du client (tags, titre, clear décidé,
modules exclus, steps absents de sa bibliothèque). Une init déjà présente sur la branche de la version est comparée,
jamais recopiée ni réécrite sans validation : clear différent de la décision (correction proposée en diff),
contournements écrits pour une bibliothèque plus ancienne (signalés), écarts de steps avec le Standard (listés).
"""
import re

from tools import cucumber_init

CLEAR_PREFIX = "init-data-rest-clear-main-"
STD_PURGE_STEP = cucumber_init.STD_PURGE_STEP
CLEAR_STEP = cucumber_init.CLEAR_STEP
MODULE_ORDER = ("WMS", "TMS", "OMS")
STANDARD_ROUTE = "full/all"
INIT_MARKER = "TODO-INIT:"

_BUNDLE = re.compile(r"^[ \t]*(runtime|import)[ \t]*:[ \t]*[\w.-]+/([\w.-]+)@", re.M)
_DELETE = re.compile(r'@DELETE\(\s*"/?clear/([A-Za-z0-9_-]+/[A-Za-z0-9_-]+)"\s*\)')
_STEP = re.compile(r"^(\s*)(Given|When|Then|And|But|\*)\s+(.*?)\s*$")
_PURGE_ARG = re.compile(r'^' + re.escape(CLEAR_STEP) + r'\s+"([^"]*)"\s*$')
_ROUTE_LITERAL = re.compile(r'"([a-z0-9]+/[A-Za-z0-9_-]+)"')
_LIBRARY_CITED = re.compile(r"biblioth[eè]que\s+(\d+\.\d+(?:\.\d+)?)", re.I)
_TODO_VERSION = "TODO-VERSION"


# ---------------------------------------------------------------------------
# Clear : ressources déployées, routes, décision
# ---------------------------------------------------------------------------

def deployed_clears(client_bundles: list, contrib_bundle) -> dict:
    """
    Ressources de clear déployées chez le client : {module en minuscules: preuve}. `client_bundles` : [(source,
    texte)] des bundle.list de l'application à sa version ; `contrib_bundle(artifact)` : bundle.list d'une contrib
    importée dans le Standard à la version du client ('' si introuvable). Les lignes commentées ne comptent pas.
    """
    found, seen, pending = {}, set(), list(client_bundles or [])
    while pending:
        source, text = pending.pop(0)
        for kind, artifact in _BUNDLE.findall(text or ""):
            if kind == "runtime" and artifact.startswith(CLEAR_PREFIX):
                found.setdefault(artifact[len(CLEAR_PREFIX):].lower(), f"{source} : runtime {artifact}")
            elif kind == "import" and artifact not in seen:
                seen.add(artifact)
                sub = contrib_bundle(artifact)
                if sub:
                    pending.append((f"{artifact} (bundle.list, Standard)", sub))
    return found


def clear_routes(sources: dict) -> dict:
    """Routes DELETE /clear/<m>/<x> lues dans les classes REST : {module: [« m/x »]} ; `sources` {module: [texte]}."""
    out = {}
    for module, texts in (sources or {}).items():
        routes = [r for text in texts for r in _DELETE.findall(text or "") if r.split("/", 1)[0].lower() == module]
        out[module] = list(dict.fromkeys(routes))
    return out


def ordered_modules(modules) -> list:
    """Modules testés dans l'ordre WMS, TMS, OMS, puis les autres par ordre alphabétique."""
    upper = {m.upper() for m in modules or ()}
    return [m for m in MODULE_ORDER if m in upper] + sorted(upper - set(MODULE_ORDER))


def decide_clear(deployed: dict, routes: dict, tested) -> dict:
    """
    Clear de l'init : {"mode": "standard" | "client" | "", "routes", "motif", "preuves", "erreur"}. « standard » :
    la ressource full est déployée et sa route full/all prouvée, la purge du Standard reste ; « client » : route
    « <m>/all » de chaque module testé ; '' : `erreur` donne la cause (rien n'est deviné).
    """
    proofs = [f"{m} : {p}" for m, p in sorted((deployed or {}).items())]
    if "full" in (deployed or {}) and STANDARD_ROUTE in (routes or {}).get("full", []):
        return {"mode": "standard", "routes": [STANDARD_ROUTE], "preuves": proofs, "erreur": "",
                "motif": f"ressource {CLEAR_PREFIX}full déployée (route DELETE /clear/{STANDARD_ROUTE}) : purge du "
                         f"Standard « {STD_PURGE_STEP} » conservée"}
    modules = ordered_modules(tested)
    if not modules:
        return {"mode": "", "routes": [], "preuves": proofs, "motif": "",
                "erreur": "aucun module testé (étape 1, « Modules testés »)"}
    chosen, missing, unproven = [], [], []
    for module in modules:
        key = module.lower()
        if key not in (deployed or {}):
            missing.append(module)
        elif f"{key}/all" not in (routes or {}).get(key, []):
            unproven.append(f"{module} ({CLEAR_PREFIX}{key} déployée, route DELETE /clear/{key}/all introuvable "
                            f"dans le Standard à cette version)")
        else:
            chosen.append(f"{key}/all")
    if missing or unproven:
        reasons = ([f"aucune ressource de clear déployée pour {', '.join(missing)} (ni {CLEAR_PREFIX}full) dans les "
                    f"bundle.list de l'application et des contribs qu'elle importe"] if missing else []) + unproven
        return {"mode": "", "routes": [], "preuves": proofs, "motif": "", "erreur": " ; ".join(reasons)}
    return {"mode": "client", "routes": chosen, "preuves": proofs, "erreur": "",
            "motif": f"{CLEAR_PREFIX}full non déployée : clear de chaque module testé, "
                     + ", ".join(f"DELETE /clear/{r}" for r in chosen)}


def route_of(argument: str) -> str:
    """Argument du step de purge ramené à sa route : « init-data-rest-clear-main-wms » → « wms/all »."""
    argument = (argument or "").strip()
    return f"{argument[len(CLEAR_PREFIX):]}/all" if argument.startswith(CLEAR_PREFIX) else argument


def expected_steps(decision: dict) -> list:
    """Steps de purge attendus (sans mot-clé), dans l'ordre."""
    if decision.get("mode") == "standard":
        return [STD_PURGE_STEP]
    return [f'{CLEAR_STEP} "{r}"' for r in decision.get("routes") or []]


def purges(text: str) -> list:
    """Purges actives d'une feature : [(index de ligne, route)] ; purge du Standard → « full/all »."""
    out = []
    for index, line in enumerate((text or "").splitlines()):
        match = _STEP.match(line)
        if not match or line.lstrip().startswith("#"):
            continue
        step = match.group(3)
        if step == STD_PURGE_STEP:
            out.append((index, STANDARD_ROUTE))
        else:
            argument = _PURGE_ARG.match(step)
            if argument:
                out.append((index, route_of(argument.group(1))))
    return out


def apply_clear(text: str, decision: dict) -> tuple:
    """
    Purges de la feature remplacées par celles de la décision si elles diffèrent : la première purge laisse place aux
    steps attendus (même mot-clé, puis « And »), les autres sont commentées sous TODO-INIT. Renvoie (texte, notes).
    """
    found = purges(text)
    expected = expected_steps(decision)
    if not found or not expected or [r for _, r in found] == list(decision.get("routes") or []):
        return text, []
    lines = text.splitlines(keepends=True)
    first = found[0][0]
    indent, keyword = _STEP.match(lines[first].rstrip("\r\n")).group(1, 2)
    eol = "\r\n" if lines[first].endswith("\r\n") else "\n"
    before = ", ".join(r for _, r in found)
    replaced = {first: [f"{indent}# {INIT_MARKER} clear déduit des routes de l'application du client à sa version "
                        f"({decision.get('motif', '')}) ; avant : {before}{eol}"]
                + [f"{indent}{keyword if i == 0 else 'And'} {step}{eol}" for i, step in enumerate(expected)]}
    for index, _ in found[1:]:
        replaced[index] = [f"{indent}# {lines[index].lstrip()}"]
    out = [part for i, line in enumerate(lines) for part in replaced.get(i, [line])]
    return "".join(out), [f"clear {before} → {', '.join(decision.get('routes') or [])}"]


# ---------------------------------------------------------------------------
# Step Java de purge par route (mode « client »)
# ---------------------------------------------------------------------------

def defines_clear_step(java_text: str) -> bool:
    return f'"{CLEAR_STEP} {{string}}"' in (java_text or "")


def accepted_routes(java_text: str):
    """
    Routes acceptées par une classe qui définit le step de purge : ensemble des routes littérales, ou None si elle
    suit la convention « init-data-rest-clear-main-<m> » (toute route <m>/all acceptée).
    """
    if "CLEAR_RESOURCE_PREFIX" in (java_text or "") or CLEAR_PREFIX in (java_text or ""):
        return None
    return set(_ROUTE_LITERAL.findall(java_text or ""))


def missing_routes(java_text: str, routes: list) -> list:
    accepted = accepted_routes(java_text)
    if accepted is None:
        return [r for r in routes if not r.endswith("/all")]
    return [r for r in routes if r not in accepted]


def clear_java(client_name: str, routes: list, proofs: list = ()) -> str:
    """Step du projet « INIT- Purge de l'environnement par le clear {string} » limité aux routes prouvées."""
    from tools.cucumber_tools import java_class_suffix, java_package_segment
    segment, suffix = java_package_segment(client_name), java_class_suffix(client_name)
    listed = ", ".join(f'"{r}"' for r in routes)
    origin = "".join(f"     * - {p}\n" for p in proofs)
    return f"""package cucumber.features.{segment};

import cucumber.endPoints.EndPoints;
import cucumber.features.BaseStep;
import cucumber.features.kis.QAFileIntegrationStep;
import cucumber.util.context.TestContext;
import io.cucumber.java.en.Given;

import java.util.Arrays;
import java.util.List;

public class Init{suffix}Steps extends BaseStep {{

    /**
     * Routes de clear déployées chez le client à sa version (bundle.list de l'application et contribs importées) :
{origin}     */
    private static final List<String> CLEAR_ROUTES = Arrays.asList({listed});

    private static final String URL_CLEAR = "clear/";

    private final QAFileIntegrationStep fileIntegration;

    public Init{suffix}Steps(TestContext testContext) {{
        super(testContext);
        fileIntegration = new QAFileIntegrationStep(testContext);
    }}

    /** Purge par une route de clear du client, comme « {STD_PURGE_STEP} » le fait avec clear/full/all. */
    @Given("{CLEAR_STEP} {{string}}")
    public void purgeParClear(String clearRoute) throws Exception {{
        if (!CLEAR_ROUTES.contains(clearRoute)) {{
            throw new IllegalArgumentException("Clear inconnu : \\"" + clearRoute + "\\" (attendu : "
                    + String.join(", ", CLEAR_ROUTES) + ")");
        }}
        fileIntegration.enableInitData();
        EndPoints.sendDeleteRest(URL_CLEAR + clearRoute, null, null, null, true);
    }}
}}
"""


# ---------------------------------------------------------------------------
# Init du Standard adaptée au client ; comparaison avec une init existante
# ---------------------------------------------------------------------------

_PLACEHOLDER = "\0clear\0"


def client_feature(text: str, module: str, client_name: str, decision: dict, client_tag: str = "") -> str:
    """Feature d'init du Standard au nom du client (tags, titre, intégration vérifiée) avec le clear décidé."""
    adapted = cucumber_init.adapt_feature(text, module, client_name, _PLACEHOLDER, client_tag)
    out = []
    for line in adapted.splitlines(keepends=True):
        if f'"{_PLACEHOLDER}"' not in line:
            out.append(line)
            continue
        indent, keyword = _STEP.match(line.rstrip("\r\n")).group(1, 2)
        eol = "\r\n" if line.endswith("\r\n") else "\n"
        out += [f"{indent}{keyword if i == 0 else 'And'} {step}{eol}" for i, step in enumerate(expected_steps(decision))]
    return "".join(out)


def active_steps(text: str) -> list:
    """Steps actifs (sans mot-clé, hors purges), dans l'ordre."""
    out = []
    for line in (text or "").splitlines():
        match = _STEP.match(line)
        if match and not line.lstrip().startswith("#"):
            step = match.group(3)
            if step != STD_PURGE_STEP and not _PURGE_ARG.match(step):
                out.append(step)
    return out


def step_gaps(client_text: str, std_text: str) -> dict:
    """Écarts de steps actifs entre l'init du client et celle du Standard adaptée : {standard_seulement, client_seulement}."""
    client, std = active_steps(client_text), active_steps(std_text)
    return {"standard_seulement": [s for s in dict.fromkeys(std) if s not in client],
            "client_seulement": [s for s in dict.fromkeys(client) if s not in std]}


def _version_key(version: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", version or ""))


def outdated_workarounds(text: str, library_version: str) -> list:
    """
    Contournements TODO-VERSION écrits pour une bibliothèque plus ancienne que celle du dépôt : à revoir (jamais
    retirés automatiquement). [« l. N : bibliothèque X (dépôt en Y) — texte »].
    """
    out = []
    current = _version_key(library_version)
    for number, line in enumerate((text or "").splitlines(), 1):
        if _TODO_VERSION not in line:
            continue
        cited = _LIBRARY_CITED.search(line)
        if cited and current and _version_key(cited.group(1)) < current:
            out.append(f"l. {number} : écrit pour la bibliothèque {cited.group(1)} (dépôt en {library_version}) — "
                       + line.strip().lstrip("#").strip()[:160])
    return out


def init_tags(texts: dict, excluded=()) -> str:
    """Tags @INIT_… des features d'init (modules exclus écartés), joints par « or »."""
    excluded = {m.upper() for m in excluded or ()}
    tags = set()
    for text in (texts or {}).values():
        for line in (text or "").splitlines():
            if line.lstrip().startswith("@"):
                tags |= {t for t in line.split() if t.upper().startswith("@INIT_")}
    kept = [t for t in sorted(tags) if t.upper().rsplit("_", 1)[-1] not in excluded]
    return " or ".join(kept)


def summary(gaps: dict) -> str:
    """« 3 step(s) du Standard absents, 2 step(s) propres au client » ('' sans écart)."""
    parts = []
    if gaps.get("standard_seulement"):
        parts.append(f"{len(gaps['standard_seulement'])} step(s) du Standard absent(s) ou remplacé(s)")
    if gaps.get("client_seulement"):
        parts.append(f"{len(gaps['client_seulement'])} step(s) propre(s) au client")
    return ", ".join(parts)
