"""
Version du Standard propre à chaque client (client fictif) : version trouvée dans un parent, un BOM, une
propriété ; normalisation des formats ; conflit entre sources signalé ; deux clients, deux versions.
"""
import os

import pytest

from tests.conftest import CLIENT, git, write
from tools import cucumber_profile, cucumber_tools, standard_version as sv

STD_GROUP = "com.example.std"


def pom(body: str, group: str = "com.example.client", artifact: str = "app", version: str = "1.0.0") -> str:
    return (f'<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
            f"<groupId>{group}</groupId><artifactId>{artifact}</artifactId><version>{version}</version>{body}"
            f"</project>")


PARENT = pom(f"<parent><groupId>{STD_GROUP}</groupId><artifactId>std-parent</artifactId>"
             f"<version>26.22.3</version></parent>")
BOM = pom(f"<dependencyManagement><dependencies><dependency><groupId>{STD_GROUP}</groupId>"
          f"<artifactId>std-bom</artifactId><version>26.21-SNAPSHOT</version><type>pom</type><scope>import</scope>"
          f"</dependency></dependencies></dependencyManagement>")
PROPERTY = pom(f"<properties><std.version>26.20.1</std.version></properties><dependencies><dependency>"
               f"<groupId>{STD_GROUP}.wms</groupId><artifactId>wms-core</artifactId><version>${{std.version}}</version>"
               f"</dependency><dependency><groupId>org.other</groupId><artifactId>lib</artifactId>"
               f"<version>27.1</version></dependency></dependencies>")


def repo_with(root: str, name: str, files: dict, branch: str = "integration") -> str:
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    git(path, "config", "user.name", "test")
    git(path, "config", "user.email", "test@example.com")
    for rel, text in files.items():
        write(os.path.join(path, rel), text)
    git(path, "add", "-A")
    git(path, "commit", "-qm", "base")
    # origin/<branche> : le dépôt est son propre origin
    git(path, "remote", "add", "origin", path)
    git(path, "fetch", "-q", "origin")
    return path


@pytest.mark.parametrize("raw, expected", [
    ("26.22", ("26.22", "26.22")), ("26.22.3", ("26.22", "26.22.3")),
    ("26.22-SNAPSHOT", ("26.22", "26.22-SNAPSHOT")), ("v26.22", ("26.22", "26.22")),
    ("release/26.22", ("26.22", "26.22")), ("origin/release/26.21.4", ("26.21", "26.21.4")),
    ("development", ("", "")), ("1.4-SNAPSHOT", ("", "")), ("", ("", ""))])
def test_normalize(raw, expected):
    assert sv.normalize(raw) == expected


def test_versions_in_parent_bom_and_property():
    groups = {STD_GROUP}
    assert [(v["emplacement"], v["normalisee"], v["version"]) for v in sv.pom_versions(PARENT, groups)] == [
        ("parent", "26.22", "26.22.3")]
    assert [(v["emplacement"], v["normalisee"]) for v in sv.pom_versions(BOM, groups)] == [("bom", "26.21")]
    found = sv.pom_versions(PROPERTY, groups, "app/pom.xml")
    # Dépendance d'un sous-groupe du Standard, version résolue par la propriété ; artefact hors Standard ignoré
    assert [(v["artefact"], v["version"]) for v in found] == [(f"{STD_GROUP}.wms:wms-core", "26.20.1")]
    assert found[0]["source"] == "app/pom.xml (propriété std.version)"


def test_version_in_a_maven_profile_and_foreign_formats_ignored():
    # Parent du Standard à version d'outillage (1.1.4) ignoré ; composant du Standard déclaré dans un profil
    text = pom(f"<parent><groupId>{STD_GROUP}</groupId><artifactId>o2-parent</artifactId><version>1.1.4</version>"
               f"</parent><profiles><profile><id>run</id><dependencies><dependency><groupId>{STD_GROUP}</groupId>"
               f"<artifactId>wms-contrib</artifactId><version>26.22-SNAPSHOT</version></dependency></dependencies>"
               f"</profile></profiles>")
    assert [(v["artefact"], v["version"]) for v in sv.pom_versions(text, {STD_GROUP})] == [
        (f"{STD_GROUP}:wms-contrib", "26.22-SNAPSHOT")]


def test_std_group_ids_come_from_the_standard_repos(tmp_path):
    std = str(tmp_path / "std")
    repo_with(std, "std-core", {"pom.xml": pom("", group=STD_GROUP, version="26.22.0")}, branch="development")
    assert sv.std_group_ids(std) == {STD_GROUP}


def test_code_source_and_line(tmp_path):
    root = str(tmp_path / "client")
    repo_with(root, f"{CLIENT}-odatio-app", {"pom.xml": PARENT})
    repo_with(root, f"{CLIENT}-cucumber", {"pom.xml": BOM})  # dépôt Cucumber : pas la source a
    code = sv.code_source(root, {STD_GROUP})
    assert (code["version"], code["complete"]) == ("26.22", "26.22.3")
    result = sv.resolve(code, {}, {})
    assert result["statut"] == "ok"
    assert sv.line(result) == f"Standard 26.22 — source : {CLIENT}-odatio-app/pom.xml, std-parent 26.22.3"


def test_jenkins_source_only_reads_the_client_builds():
    builds = [{"numero": 9, "parametres": {"REPO": "autre-cucumber", "KIS_VERSION": "26.30"}},
              {"numero": 8, "parametres": {"REPO": f"{CLIENT}-cucumber", "CUCUMBER_STANDARD_BRANCH": "development",
                                           "KIS_VERSION": "v26.21"}}]
    found = sv.jenkins_source(builds, f"{CLIENT}-cucumber")
    assert (found["version"], found["complete"]) == ("26.21", "v26.21") and "n° 8" in found["detail"]


def test_conflict_between_code_and_jenkins_is_reported():
    code = {"source": "code", "version": "26.22", "complete": "26.22.3", "detail": "app/pom.xml, std-parent 26.22.3"}
    jenkins = {"source": "jenkins", "version": "26.21", "complete": "v26.21", "detail": "build Jenkins n° 8"}
    result = sv.resolve(code, jenkins, {})
    assert result["statut"] == "conflit" and result["version"] == ""
    text = sv.line(result)
    assert "26.22 (app/pom.xml" in text and "26.21 (build Jenkins n° 8)" in text and "laquelle retenir" in text
    # Bibliothèque Cucumber plus ancienne : pas un conflit (repli seulement)
    library = sv.library_source("x-cucumber", "26.16")
    assert sv.resolve(code, {}, library)["statut"] == "ok"
    assert sv.resolve({}, {}, library)["version"] == "26.16"
    assert sv.resolve({}, {}, {})["statut"] == "introuvable" and "à saisir" in sv.line(sv.resolve({}, {}, {}))


def test_version_is_per_client(tmp_path, monkeypatch):
    monkeypatch.setattr(cucumber_tools, "CACHE_ROOT", str(tmp_path / "cache"))
    for client, version in ((CLIENT, "26.22"), ("clienty", "26.16")):
        sv.save(client, sv.resolve({"source": "code", "version": version, "complete": version, "detail": "pom"},
                                   {}, {}))
    assert sv.client_version(CLIENT, "99.1")[0] == "26.22"
    assert sv.client_version("clienty", "99.1")[0] == "26.16"
    assert sv.client_version("clientz", "99.1") == ("99.1", "repli global")
    assert cucumber_profile.effective_profile(cucumber_profile.load_profile(CLIENT))["server_version"] == "26.22"
    # Choix de l'utilisateur sur conflit : saisi dans le profil, il prime
    cucumber_profile.save_user_fields("clienty", {"server_version": "26.21"})
    assert sv.client_version("clienty", "99.1")[0] == "26.21"


def test_std_refs_put_the_version_first():
    assert sv.std_refs("26.22.3", ("origin/dev",))[:2] == ("origin/release/26.22", "origin/v26.22")
    assert sv.std_refs("", ("origin/dev",)) == ("origin/dev",)
