"""
Correction connue « délai dépassé » (0 token).

Un step d'attente de la bibliothèque échoue par ConditionTimeoutException après un délai fixe (ex. « INIT-
Initialisation globale par fichiers via l'écran » : 360 s codées dans GS1EndPoints.sendPack), sans dire où en est
l'attente ni s'arrêter sur une erreur notifiée entre-temps. Correction : step du projet de nom unique (préfixe du
client), délai paramétrable dans la feature, qui suit l'état réel des éléments attendus (fichiers intégrés sur
fichiers envoyés, lus sur la notification) et échoue immédiatement sur une erreur notifiée, avec l'état lu.
Le step de la bibliothèque est remplacé dans les features d'init, sous une note TODO-VERSION.
"""
import os
import re

from tools.cucumber_tools import java_class_suffix, java_package_segment

MARKER = "TODO-VERSION:"
DELAY_ENV = "CUCUMBER_WAIT_DELAY_SECONDS"
_TIMEOUT = re.compile(r"ConditionTimeoutException")
_WITHIN = re.compile(r"within\s+(\d+)\s*(second|minute|s\b|min\b)", re.I)
# Attente de la bibliothèque de steps (classe du Standard dans le message), pas une attente écrite par le projet
_LIBRARY = re.compile(r"\bcucumber\.(?:endPoints|util)\.[\w.$]+")
_KEYWORD = re.compile(r"^(\s*)(Given|When|Then|And|But|\*|Soit|Quand|Alors|Et|Mais)\s+(.*?)\s*$")

# Steps d'attente de la bibliothèque dont la correction est connue : {step : step du projet (sans préfixe)}
KNOWN = {
    "INIT- Initialisation globale par fichiers via l'écran":
        "Initialisation globale par fichiers via l'écran avec un délai de {int} secondes",
}


def is_library_timeout(message: str) -> bool:
    """Délai dépassé dans une attente de la bibliothèque (ConditionTimeoutException sur une de ses classes)."""
    return bool(_TIMEOUT.search(message or "") and _LIBRARY.search(message or ""))


def timeout_seconds(message: str) -> int:
    """Délai écoulé lu dans le message (« within 360 seconds »), 0 si absent."""
    match = _WITHIN.search(message or "")
    if not match:
        return 0
    return int(match.group(1)) * (60 if match.group(2).lower().startswith("m") else 1)


def known_step(step: str) -> str:
    """Step de la bibliothèque à la correction connue ('' sinon)."""
    step = (step or "").strip()
    return step if step in KNOWN else ""


def prefix(client_name: str) -> str:
    return java_package_segment(client_name).upper()


def project_expression(client_name: str, step: str) -> str:
    return f"{prefix(client_name)}- {KNOWN[step]}"


def delay(observed: int) -> int:
    """Délai du step du projet : CUCUMBER_WAIT_DELAY_SECONDS, à défaut le double du délai dépassé (900 s au moins)."""
    try:
        value = int(os.getenv(DELAY_ENV, "").strip() or 0)
    except ValueError:
        value = 0
    return value if value > 0 else max(900, 2 * (observed or 0))


def java_path(client_name: str) -> str:
    return (f"src/test/java/cucumber/features/{java_package_segment(client_name)}/"
            f"Attente{java_class_suffix(client_name)}Steps.java")


def java(client_name: str) -> str:
    """Classe de steps du projet (API de la bibliothèque 26.16 : GS1EndPoints, KisWebSocket, BaseStep)."""
    name = f"Attente{java_class_suffix(client_name)}Steps"
    step = KNOWN["INIT- Initialisation globale par fichiers via l'écran"]
    return f"""package cucumber.features.{java_package_segment(client_name)};

import cucumber.endPoints.excGs1.GS1EndPoints;
import cucumber.features.BaseStep;
import cucumber.pojo.request.KIS.filesPackRequest.FilePack;
import cucumber.util.KisWebSocket;
import cucumber.util.constant.config.ConstantConfig;
import cucumber.util.context.Context;
import cucumber.util.context.TestContext;
import io.cucumber.java.Before;
import io.cucumber.java.Scenario;
import io.cucumber.java.en.Given;

import java.util.List;

/**
 * TODO-VERSION : « INIT- Initialisation globale par fichiers via l'écran » de la bibliothèque attend la fin de
 * l'intégration 360 s (délai fixe, ConditionTimeoutException sans état) et ne s'arrête pas sur une erreur notifiée.
 * Step du projet : délai paramétrable, suivi des fichiers intégrés sur les fichiers envoyés, échec immédiat sur une
 * erreur notifiée, avec l'état lu.
 */
public class {name} extends BaseStep {{

    private Scenario scenario;

    public {name}(TestContext testContext) {{
        super(testContext);
    }}

    @Before
    public void retenirLeScenario(Scenario scenario) {{
        this.scenario = scenario;
    }}

    private void journal(String message) {{
        if (scenario != null) {{
            scenario.log(message);
        }}
        System.out.println("[{prefix(client_name)} INIT] " + message);
    }}

    @Given("{prefix(client_name)}- {step}")
    public void initialisationGlobaleParFichiers(int delai) throws Exception {{
        String dossier = ConstantConfig.FILE_PATH + getScenarioContext().getContext(Context.SCENARIO_DIRECTORY);
        List<FilePack> fichiers = GS1EndPoints.uploadFilesFromFolder(dossier);
        // Envoi sans l'attente fixe de la bibliothèque : l'état est suivi ici
        GS1EndPoints.sendPack(fichiers, false);
        KisWebSocket suivi = new GS1EndPoints().getKisWebSocket();
        long debut = System.currentTimeMillis();
        long fin = debut + delai * 1000L;
        int vus = -1;
        while (true) {{
            int integres = suivi.getNbIntegration();
            if (Boolean.TRUE.equals(suivi.getException())) {{
                throw new AssertionError("Intégration en erreur après " + integres + "/" + fichiers.size()
                        + " fichier(s) intégré(s) : " + suivi.getMessageException());
            }}
            if (Boolean.TRUE.equals(suivi.getFinish())) {{
                journal(integres + "/" + fichiers.size() + " fichier(s) intégré(s) en "
                        + (System.currentTimeMillis() - debut) / 1000 + " s");
                return;
            }}
            if (integres != vus) {{
                vus = integres;
                journal(integres + "/" + fichiers.size() + " fichier(s) intégré(s) après "
                        + (System.currentTimeMillis() - debut) / 1000 + " s");
            }}
            if (System.currentTimeMillis() > fin) {{
                throw new AssertionError("Délai de " + delai + " s dépassé : " + integres + "/" + fichiers.size()
                        + " fichier(s) intégré(s), aucune erreur notifiée (fichier suivant bloqué ou trop lent)");
            }}
            Thread.sleep(500);
        }}
    }}
}}
"""


def apply_text(text: str, client_name: str, seconds: int) -> tuple:
    """Steps d'attente connus remplacés par le step du projet (note TODO-VERSION au-dessus) : (texte, notes)."""
    lines = (text or "").splitlines(keepends=True)
    out, notes = [], []
    for number, line in enumerate(lines, 1):
        match = _KEYWORD.match(line.rstrip("\r\n")) if not line.lstrip().startswith("#") else None
        step = known_step(match.group(3)) if match else ""
        if not step:
            out.append(line)
            continue
        indent, keyword = match.group(1), match.group(2)
        eol = "\r\n" if line.endswith("\r\n") else "\n"
        replacement = project_expression(client_name, step).replace("{int}", str(seconds))
        out += [f"{indent}# {MARKER} « {step} » : attente fixe de la bibliothèque dépassée (ConditionTimeoutException) "
                f"— step du projet, délai {seconds} s paramétrable, suivi des fichiers intégrés, échec immédiat sur "
                f"erreur{eol}", f"{indent}{keyword} {replacement}{eol}"]
        notes.append(f"l. {number} : « {step} » → « {replacement} »")
    return "".join(out), notes


def apply(worktree: str, paths: list, client_name: str, message: str) -> dict:
    """
    Correction connue dans les features `paths` du worktree : step remplacé, classe de steps du projet écrite si
    absente. Renvoie {"fichiers", "notes", "delai"} (fichiers vides : rien à corriger).
    """
    seconds = delay(timeout_seconds(message))
    changed, notes = [], []
    for rel in paths:
        full = os.path.join(worktree, *rel.split("/"))
        if not os.path.exists(full):
            continue
        with open(full, encoding="utf-8", newline="") as f:
            text = f.read()
        new, found = apply_text(text, client_name, seconds)
        if new != text:
            with open(full, "w", encoding="utf-8", newline="") as f:
                f.write(new)
            changed.append(rel)
            notes += [f"{rel.rsplit('/', 2)[-2]}/{rel.rsplit('/', 1)[-1]} {n}" for n in found]
    if changed:
        path = java_path(client_name)
        full = os.path.join(worktree, *path.split("/"))
        if not os.path.exists(full):
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="\n") as f:
                f.write(java(client_name))
            changed.append(path)
    return {"fichiers": changed, "notes": notes, "delai": seconds}
