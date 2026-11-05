"""
Étape 2 de l'onglet « Cucumber » simplifié : dépôt <client>-cucumber (0 token).

- Dépôt absent : création dans le projet Bitbucket du client, après contrôle des droits du compte (sans droit :
  message clair, arrêt), puis contenu minimal sur une branche nommée d'après la version du client (version_branch).
- Dépôt présent : branche de la version créée si elle manque (depuis la branche d'intégration), sinon contrôle.
- Contenu minimal (scaffold_files), sur le modèle des projets clients : pom.xml avec la bibliothèque Cucumber à la
  version publiée la plus proche ≤ version du Standard du client ; RunnerTest (glue, features, rapport JSON,
  exclusion du tag de désactivation) ; arborescence des features, fichiers et steps du client ; .gitignore.
- Dépôt existant : alignement de la bibliothèque et du runner (align_plan), diff affiché, appliqué après validation.
Aucune valeur propre à un client n'est écrite en dur : nom, tag et versions viennent du profil et de la version
du client choisie.
"""
import difflib
import re

import requests

from tools import pr_review

LIBRARY_GROUP, LIBRARY_ARTIFACT = "a-sis", "Cucumber"
# Dépendances communes aux projets clients (mêmes que le Cucumber Standard) ; versions par défaut
DEFAULT_CUCUMBER_VERSION = "7.13.0"
RUNNER_PATH = "src/test/java/cucumber/RunnerTest.java"


# Branche de version : numéro seul ou court préfixe de version (v1.3, release/1.3, version-1.3) ; jamais une branche
# de travail qui finit par un numéro (« cucumber_canson_agent-init-v1.1 » n'est pas la branche de la 1.1)
_VERSION_BRANCH = re.compile(r"^(?P<prefixe>(?:[vV]|version[-_/]?|releases?[-_/]|rel[-_/])?)(?P<numero>\d+\.\d+(?:\.\d+)*)$")


def branch_version_line(branch: str) -> str:
    """Ligne majeure.mineure d'une branche de version (« v1.3 » -> 1.3) ; '' si ce n'est pas une branche de version."""
    match = _VERSION_BRANCH.match(branch or "")
    return version_line(match.group("numero")) if match else ""


def version_line(version_client: str) -> str:
    """Ligne majeure.mineure d'une version du client (1.3.1 -> 1.3, 1.21-SNAPSHOT -> 1.21) ; '' si illisible."""
    numbers = re.findall(r"\d+", version_client or "")
    return ".".join(numbers[:2]) if len(numbers) >= 2 else (numbers[0] if numbers else "")


def version_branch(version_client: str, branches: list = ()) -> str:
    """
    Branche de la version du client dans le dépôt <client>-cucumber : la branche existante de la même version
    (même majeure.mineure, quel que soit son préfixe : « 1.3 », « v1.3 », « 1.3.1 »), sinon la convention des
    autres branches de version du dépôt (préfixe le plus fréquent), sinon « v<majeure.mineure> ». Jamais deux
    branches pour la même version.
    """
    line = version_line(version_client)
    if not line:
        return ""
    found = [(b, m) for b in branches or [] for m in [_VERSION_BRANCH.match(b)] if m]
    same = [b for b, m in found if version_line(m.group("numero")) == line]
    if same:
        return sorted(same, key=lambda b: (b != line and b != f"v{line}", len(b)))[0]
    if found:
        prefixes = [m.group("prefixe") for _, m in found]
        return max(set(prefixes), key=prefixes.count) + line
    return f"v{line}"


def _pom(client: str, library_version: str, cucumber_version: str) -> str:
    dependencies = "".join(f"""
        <dependency>
            <groupId>{group}</groupId>
            <artifactId>{artifact}</artifactId>
            <version>{version}</version>
            <scope>test</scope>
        </dependency>""" for group, artifact, version in (
        ("io.cucumber", "cucumber-junit", cucumber_version), ("io.cucumber", "cucumber-java", cucumber_version),
        ("io.cucumber", "cucumber-java8", cucumber_version), ("io.cucumber", "cucumber-picocontainer", cucumber_version),
        ("org.awaitility", "awaitility", "3.0.0"), ("io.rest-assured", "rest-assured", "4.1.2")))
    return f"""<?xml version="1.0" encoding="UTF-8"?>

<project xmlns="http://maven.apache.org/POM/4.0.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>

    <groupId>savoye</groupId>
    <artifactId>{client}-cucumber</artifactId>
    <version>1.0</version>
    <name>cucumber-{client}</name>

    <properties>
        <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
        <maven.compiler.source>11</maven.compiler.source>
        <maven.compiler.target>11</maven.compiler.target>
    </properties>

    <dependencies>{dependencies}

        <dependency>
            <groupId>{LIBRARY_GROUP}</groupId>
            <artifactId>{LIBRARY_ARTIFACT}</artifactId>
            <version>{library_version}</version>
            <type>test-jar</type>
            <scope>test</scope>
        </dependency>
    </dependencies>
    <build>
        <plugins>
            <plugin>
                <groupId>org.apache.maven.plugins</groupId>
                <artifactId>maven-surefire-plugin</artifactId>
                <version>3.2.2</version>
                <configuration>
                    <includes>
                        <include>**/*Runner.java</include>
                        <include>**/*Test.java</include>
                        <include>**/Test*.java</include>
                    </includes>
                    <systemPropertyVariables>
                        <https>${{https}}</https>
                        <ws>${{ws}}</ws>
                        <url>${{url}}</url>
                    </systemPropertyVariables>
                </configuration>
            </plugin>
        </plugins>
    </build>
</project>
"""


def _runner(disabled_tag: str) -> str:
    return f"""package cucumber;

import io.cucumber.junit.Cucumber;
import io.cucumber.junit.CucumberOptions;
import org.junit.BeforeClass;
import org.junit.runner.RunWith;

import java.io.File;

@RunWith(Cucumber.class)
@CucumberOptions(
        features = "src/test/resources",
        glue = "cucumber",
        // Scénarios désactivés : voir A_REVOIR.md
        tags = "not {disabled_tag}",
        plugin = {{
                "pretty",
                "json:target/cucumber-reports/Cucumber.json"
        }}
)
public class RunnerTest {{

    @BeforeClass
    public static void setUp() {{
        // Garantit la création du dossier même si aucun scénario n'est exécuté
        File dir = new File("target/cucumber-reports");
        if (!dir.exists()) {{
            dir.mkdirs();
        }}
    }}
}}
"""


def scaffold_files(client: str, library_version: str, disabled_tag: str,
                   cucumber_version: str = DEFAULT_CUCUMBER_VERSION) -> dict:
    """Contenu minimal d'un nouveau dépôt <client>-cucumber : {chemin: texte}."""
    return {
        "pom.xml": _pom(client, library_version, cucumber_version or DEFAULT_CUCUMBER_VERSION),
        RUNNER_PATH: _runner(disabled_tag),
        f"src/test/java/cucumber/features/{client}/.gitkeep": "",
        f"src/test/resources/cucumber/features/{client}/.gitkeep": "",
        f"src/test/resources/cucumber/files/{client}/.gitkeep": "",
        ".gitignore": "target/\n.idea/\n*.iml\n.vscode/\n",
    }


def _diff(path: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff((before or "").splitlines(True), after.splitlines(True),
                                        f"a/{path}", f"b/{path}"))


def align_runner(text: str, disabled_tag: str) -> str:
    """Runner existant aligné : rapport JSON ajouté s'il manque, tag de désactivation exclu ; reste inchangé."""
    out = text
    if "json:" not in out:
        out = re.sub(r'(plugin\s*=\s*\{)', r'\1"json:target/cucumber-reports/Cucumber.json", ', out, count=1) \
            if re.search(r'plugin\s*=\s*\{', out) else \
            re.sub(r'(@CucumberOptions\s*\()', r'\1\n        plugin = {"json:target/cucumber-reports/Cucumber.json"},',
                   out, count=1)
    if disabled_tag and disabled_tag not in out:
        found = re.search(r'tags\s*=\s*"([^"]*)"', out)
        out = (out[:found.start(1)] + f"({found.group(1)}) and not {disabled_tag}" + out[found.end(1):]) if found \
            else re.sub(r'(@CucumberOptions\s*\()', rf'\1\n        tags = "not {disabled_tag}",', out, count=1)
    return out


def align_plan(files: dict, library_version: str, disabled_tag: str, client: str = "") -> dict:
    """
    Alignement d'un dépôt existant ({chemin: texte} de pom.xml et du runner) : {"files": {chemin: nouveau texte},
    "diff", "motifs"} ; vide si tout est aligné. Seules la version de la bibliothèque et les options du runner
    (rapport JSON, exclusion du tag de désactivation) changent ; runner absent : RunnerTest créé.
    """
    from tools.cucumber_library_upgrade import set_pom_version
    changed, reasons = {}, []
    pom = files.get("pom.xml") or ""
    if pom:
        try:
            new_pom, old = set_pom_version(pom, library_version)
        except ValueError as e:
            reasons.append(f"pom.xml : {e} (à corriger à la main)")
            new_pom, old = pom, ""
        if new_pom != pom:
            changed["pom.xml"] = new_pom
            reasons.append(f"bibliothèque Cucumber {old} → {library_version}")
    runner_path = next((p for p in files if p != "pom.xml"), "")
    if not runner_path:
        changed[RUNNER_PATH] = _runner(disabled_tag)
        reasons.append("runner absent : RunnerTest créé")
    else:
        aligned = align_runner(files[runner_path], disabled_tag)
        if aligned != files[runner_path]:
            changed[runner_path] = aligned
            reasons.append("runner : rapport JSON et exclusion du tag de désactivation")
    diff = "".join(_diff(p, files.get(p, ""), t) for p, t in changed.items())
    return {"files": changed, "diff": diff, "motifs": reasons}


# ---------------------------------------------------------------------------
# Bitbucket : existence, droits, création (jamais de suppression)
# ---------------------------------------------------------------------------

class RepoError(RuntimeError):
    pass


WRITE_TOKEN = "BITBUCKET_WRITE_TOKEN"


def write_token() -> str:
    """Jeton réservé aux écritures sur <client>-cucumber (création, branche, pushes) ; BITBUCKET_HTTP_TOKEN : lecture."""
    import os
    return os.getenv(WRITE_TOKEN, "").strip()


def write_token_missing(slug: str = "<client>-cucumber") -> str:
    """Message clair si le jeton d'écriture manque ('' s'il est renseigné)."""
    return "" if write_token() else (f"{WRITE_TOKEN} non renseigné : ce jeton, distinct de BITBUCKET_HTTP_TOKEN (lecture), "
                                     f"est réservé aux écritures sur {slug} (onglet Configuration)")


def _call(method: str, path: str, write: bool = False, **kwargs):
    """Appel REST Bitbucket : jeton de lecture, ou jeton d'écriture (`write`) pour la création et ses contrôles."""
    if not pr_review.configured():
        raise RepoError("BITBUCKET_URL ou BITBUCKET_HTTP_TOKEN non renseigné (onglet Configuration)")
    headers = {**pr_review._headers(), "Content-Type": "application/json"}
    if write:
        if not write_token():
            raise RepoError(write_token_missing())
        headers["Authorization"] = f"Bearer {write_token()}"
    try:
        return requests.request(method, f"{pr_review.base_url()}{path}", headers=headers, timeout=30, **kwargs)
    except requests.RequestException as e:
        raise RepoError(f"Bitbucket injoignable : {type(e).__name__}") from e


def repo_exists(project: str, slug: str, call=None) -> bool:
    response = (call or _call)("GET", f"/rest/api/1.0/projects/{project}/repos/{slug}")
    if response.status_code == 404:
        return False
    if response.status_code != 200:
        raise RepoError(f"lecture du dépôt {project}/{slug} : HTTP {response.status_code}")
    return True


def can_create(project: str, call=None) -> bool:
    """Le compte du jeton d'écriture peut créer un dépôt dans `project` (droit d'administration du projet)."""
    response = (call or _call)("GET", "/rest/api/1.0/projects", params={"permission": "PROJECT_ADMIN", "limit": 1000},
                               **({} if call else {"write": True}))
    if response.status_code != 200:
        raise RepoError(f"lecture des droits du compte : HTTP {response.status_code}")
    return project.upper() in {p.get("key", "").upper() for p in response.json().get("values") or []}


def create_repo(project: str, slug: str, call=None) -> str:
    """Crée le dépôt et renvoie son adresse de clone HTTP. Lève RepoError."""
    response = (call or _call)("POST", f"/rest/api/1.0/projects/{project}/repos", json={"name": slug, "scmId": "git"},
                               **({} if call else {"write": True}))
    if response.status_code not in (200, 201):
        raise RepoError(f"création de {project}/{slug} refusée : HTTP {response.status_code} {response.text[:200]}")
    links = (response.json().get("links") or {}).get("clone") or []
    return next((l["href"] for l in links if l.get("name") == "http"), "")
