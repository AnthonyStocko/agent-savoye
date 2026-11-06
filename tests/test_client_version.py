"""
Version du client choisie (client fictif) : versions proposées (branche, tag, artefact Maven), Standard lu dans le
pom de la version choisie, Cucumber Standard sur la référence la plus proche sans repli sur master, paramètres
Jenkins déduits, écarts signalés.
"""
import json
import os

import pytest

from tests.conftest import CLIENT, git, write
from tools import client_version as cv
from tools import cucumber_job as cj
from tools import cucumber_profile as cp
from tools import cucumber_tools as ct

GROUPS = {"com.editeur.std"}
POM = """<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.client</groupId>
  <artifactId>odatio-app</artifactId>
  <version>{own}</version>
  <parent><groupId>com.editeur.std</groupId><artifactId>std-parent</artifactId><version>{std}</version></parent>
</project>
"""
OTHER_POM = "<project><groupId>com.client</groupId><artifactId>outil</artifactId><version>1.0</version></project>"


def _clone(tmp_path, name, commits):
    """Dépôt distant nu + clone dans le projet client ; `commits` : [(branche, fichiers, tag)]."""
    bare = str(tmp_path / "remotes" / f"{name}.git")
    os.makedirs(bare)
    git(bare, "init", "-q", "--bare", "-b", "integration")
    seed = str(tmp_path / "seeds" / name)
    os.makedirs(seed)
    git(seed, "init", "-q", "-b", "integration")
    for key, value in (("user.name", "t"), ("user.email", "t@e.x"), ("core.autocrlf", "false")):
        git(seed, "config", key, value)
    git(seed, "remote", "add", "origin", bare)
    for branch, files, tag in commits:
        if not git(seed, "branch", "--list"):
            git(seed, "symbolic-ref", "HEAD", f"refs/heads/{branch}")          # premier commit
        elif git(seed, "branch", "--list", branch):
            git(seed, "checkout", "-q", branch)
        else:
            git(seed, "checkout", "-q", "-b", branch)
        for path, text in files.items():
            write(os.path.join(seed, path), text)
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", f"{branch} {tag or ''}")
        if tag:
            git(seed, "tag", tag)
        git(seed, "push", "-q", "origin", branch, "--tags")
    clone = str(tmp_path / "projet" / name)
    git(str(tmp_path), "clone", "-q", bare, clone)
    git(clone, "fetch", "-q", "--tags", "origin")
    return clone


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(cv, "KIS_REGISTRY", str(tmp_path / "cache" / "kis.json"))
    _clone(tmp_path, "clientx-odatio-app", [
        ("integration", {"pom.xml": POM.format(own="1.3", std="26.22.1")}, "odatio-app-1.3.0"),
        ("release/1.3", {"README.md": "release\n"}, ""),
        ("integration", {"pom.xml": POM.format(own="1.4-SNAPSHOT", std="26.24-SNAPSHOT")}, "")])
    _clone(tmp_path, "clientx-outil", [("integration", {"pom.xml": OTHER_POM}, "")])
    return str(tmp_path / "projet")


def test_versions_offered_branch_tag_and_maven_artifact(project):
    repos = cv.repositories(project)
    app = cv.app_repository(repos, GROUPS)
    assert app.name == "clientx-odatio-app"                                      # pom avec artefacts du Standard
    entries = cv.git_versions(app)
    integration = entries[0]
    assert integration["type"] == "integration" and integration["version"] == "1.4-SNAPSHOT"
    assert {(e["type"], e["version"]) for e in entries[1:]} == {("tag", "1.3.0"), ("release", "1.3")}
    assert all(e["date"] for e in entries)
    metadata = ("<metadata><versioning><versions><version>1.2.0</version><version>1.3.0</version>"
                "</versions></versioning></metadata>")
    maven = cv.maven_versions("clientx", "odatio-app", fetch=lambda url: metadata)
    assert [m["version"] for m in maven["versions"]] == ["1.3.0", "1.2.0"]
    offered = cv.merge_versions(entries, maven["versions"], "clientx", "odatio-app")
    assert offered[0]["type"] == "integration"                                   # par défaut : intégration
    tag = next(e for e in offered if e["type"] == "tag")
    assert tag.get("publiee") and "publiée" in tag["libelle"]                    # tag publié dans Maven
    only_maven = next(e for e in offered if e["type"] == "maven")
    assert only_maven["version"] == "1.2.0" and only_maven["git"] == ""
    assert "1.4-SNAPSHOT (integration)" in offered[0]["libelle"]


def test_standard_read_in_the_pom_of_the_chosen_version(project):
    app = cv.app_repository(cv.repositories(project), GROUPS)
    entries = {e["type"]: e for e in cv.git_versions(app)}
    refs = ["master", "dev", "v26.21", "v26.22", "v26.23"]
    at_tag = cv.deduce(entries["tag"], app.file(entries["tag"], "pom.xml"), GROUPS, refs, "26.16", {})
    at_head = cv.deduce(entries["integration"], app.file(entries["integration"], "pom.xml"), GROUPS, refs, "26.16",
                        {})
    assert at_tag["standard"]["version"] == "26.22" and at_head["standard"]["version"] == "26.24"
    assert at_tag["ref_client"] == "odatio-app-1.3.0" and at_head["ref_client"] == "integration"
    # Version seulement publiée dans Maven : code du client lu sur intégration, avec un avertissement
    maven = cv.deduce({"type": "maven", "version": "1.2.0"}, app.file(entries["integration"], "pom.xml"), GROUPS,
                      refs, "26.16", {})
    assert maven["ref_client"] == "integration" and any("sans tag ni branche" in w for w in maven["avertissements"])


def test_cucumber_standard_nearest_lower_reference_never_master():
    refs = ["master", "dev", "main", "v26.2", "v26.21", "release/26.22", "v26.22", "v26.23", "v26.30"]
    assert cv.cucumber_std_ref("26.22", refs)["ref"] == "release/26.22"           # exacte, release d'abord
    picked = cv.cucumber_std_ref("26.24", refs)
    assert picked["ref"] == "v26.23" and "plus proche inférieure" in picked["avertissement"]
    none = cv.cucumber_std_ref("26.1", refs)
    assert none["ref"] == "" and "pas de repli sur master" in none["avertissement"]
    assert cv.cucumber_std_ref("26.3", ["v26.2", "v26.21", "master"])["ref"] == "v26.2"   # 26.21 > 26.3 : écarté


def _builds():
    """Builds de tous les clients ; la version KIS ne vient que d'un build qui a démarré une application de la
    même version du Standard."""
    return [
        {"numero": 30, "url": "b30", "resultat": "FAILURE",
         "parametres": {"CUCUMBER_STANDARD_BRANCH": "v26.24", "KIS_VERSION": "9.99", "COMPONENT_OWNER": "clientx"}},
        {"numero": 29, "url": "b29", "resultat": "UNSTABLE",
         "parametres": {"CUCUMBER_STANDARD_BRANCH": "v26.22", "KIS_VERSION": "3.12", "COMPONENT_OWNER": "autre"}},
        {"numero": 28, "url": "b28", "resultat": "SUCCESS",
         "parametres": {"CUCUMBER_STANDARD_BRANCH": "v26.22", "KIS_VERSION": "3.10", "COMPONENT_OWNER": "clientx"}},
    ]


def test_jenkins_parameters_deduced_never_copied_from_another_version(project, monkeypatch):
    registry = cv.kis_registry(_builds())
    assert registry == {"26.22": {"kis": "3.12", "build": "b29", "numero": 29, "source": "build"}}
    app = cv.app_repository(cv.repositories(project), GROUPS)
    entries = {e["type"]: e for e in cv.git_versions(app)}
    refs = ["v26.22", "v26.23"]
    deduction = cv.deduce(entries["tag"], app.file(entries["tag"], "pom.xml"), GROUPS, refs, "26.16", registry)
    assert deduction["jenkins"] == {"composant_version": "1.3.0", "standard_branche": "v26.22",
                                    "kis_version": "3.12", "composant_proprietaire": "com.client",
                                    "composant_nom": "odatio-app"}
    definitions = {name: {"type": "String", "defaut": ""} for name in (
        "COMPONENT_OWNER", "COMPONENT_NAME", "COMPONENT_VERSION", "CUCUMBER_STANDARD_BRANCH", "KIS_VERSION",
        "CUCUMBER_SPEC_BRANCH", "ADDITIONAL_ARGLINE")}
    profile = {"version_client": {"deduction": deduction}}
    fixed, warnings = cj.fixed_parameters(profile, definitions)
    assert fixed == {"COMPONENT_VERSION": "1.3.0", "CUCUMBER_STANDARD_BRANCH": "v26.22", "KIS_VERSION": "3.12",
                     "COMPONENT_OWNER": "com.client", "COMPONENT_NAME": "odatio-app"}
    # Les valeurs déduites remplacent celles du build de référence (autre version)
    reference = {"parametres": {"COMPONENT_VERSION": "1.4-SNAPSHOT", "CUCUMBER_STANDARD_BRANCH": "v26.24",
                                "KIS_VERSION": "9.99"}}
    params = cj.build_parameters(definitions, {"branche": "CUCUMBER_SPEC_BRANCH", "arguments": "ADDITIONAL_ARGLINE"},
                                 "agent", fixed=fixed, reference=reference)
    assert (params["COMPONENT_VERSION"], params["CUCUMBER_STANDARD_BRANCH"], params["KIS_VERSION"]) == (
        "1.3.0", "v26.22", "3.12")
    # 26.24 : seul un build en échec de démarrage la cite → KIS inconnue, question posée, run refusé
    head = cv.deduce(entries["integration"], app.file(entries["integration"], "pom.xml"), GROUPS, refs, "26.16",
                     registry)
    assert head["jenkins"]["kis_version"] == "" and any("version KIS inconnue" in w for w in head["avertissements"])
    refused = cj.client_configuration_error(reference, {}, "clientx-cucumber", {"version_client": {"deduction": head}})
    assert "version KIS inconnue pour le Standard 26.24" in refused


def test_line_warnings_choice_saved_and_kis_answer(project, monkeypatch):
    app = cv.app_repository(cv.repositories(project), GROUPS)
    entries = {e["type"]: e for e in cv.git_versions(app)}
    head = cv.deduce(entries["integration"], app.file(entries["integration"], "pom.xml"), GROUPS,
                     ["master", "v26.23"], "26.16", {})
    assert cv.line(head) == ("Version client 1.4-SNAPSHOT (integration) → Standard 26.24 → Cucumber Standard v26.23 → "
                             "bibliothèque Cucumber 26.24 (actuelle : 26.16) → KIS ?")
    warnings = " | ".join(head["avertissements"])
    assert "plus proche inférieure retenue, v26.23" in warnings and "montée proposée" in warnings
    with open(cp.profile_path(CLIENT), "w", encoding="utf-8") as f:
        json.dump({"detecte": {}, "saisi": {}}, f)
    cv.save(CLIENT, entries["integration"], head)
    document = cp.load_profile(CLIENT)
    assert cv.standard_of(document) == "26.24" and cv.client_git_ref(document) == ""
    from tools import cucumber_library_upgrade, standard_version
    assert cucumber_library_upgrade.target_version(document)["version"] == "26.24"
    assert standard_version.client_version(CLIENT, "26.16")[0] == "26.24"       # ODATIO_VERSION ignoré
    answered = cv.answer_kis(CLIENT, "3.14")
    assert answered["jenkins"]["kis_version"] == "3.14" and cv.load_kis_registry()["26.24"]["source"] == "saisie"
    assert not any("version KIS" in w for w in answered["avertissements"])
    # Version choisie sur un tag : code du client lu à ce tag, sinon intégration avec avertissement
    tag = cv.deduce(entries["tag"], app.file(entries["tag"], "pom.xml"), GROUPS, ["v26.22"], "26.22", {})
    cv.save(CLIENT, entries["tag"], tag)
    assert cv.client_git_ref(cp.load_profile(CLIENT)) == "refs/tags/odatio-app-1.3.0"
    monkeypatch.setattr(ct, "CLIENT_VERSION_REF", ["refs/tags/odatio-app-1.3.0"])
    monkeypatch.setattr(ct, "_CLIENT_REF_WARNED", set())
    assert ct.client_ref(os.path.join(project, "clientx-odatio-app")) == "refs/tags/odatio-app-1.3.0"
    assert ct.client_ref(os.path.join(project, "clientx-outil")) == "origin/integration"


def test_cucumber_standard_checkout_without_master_fallback(tmp_path, monkeypatch, capsys):
    import importlib
    import io
    import sys
    saved = sys.stdout
    sys.stdout = io.StringIO()
    try:
        main = importlib.import_module("main")
    finally:
        sys.stdout = saved
    clone = _clone(tmp_path, "cucumber", [("master", {"a.txt": "m\n"}, ""), ("v26.22", {"a.txt": "22\n"}, ""),
                                           ("v26.23", {"a.txt": "23\n"}, "")])
    git(clone, "fetch", "-q", "origin")
    assert main.checkout_cucumber_std_ref(clone, "26.24") == "origin/v26.23"
    assert "plus proche inférieure" in capsys.readouterr().out
    assert main.checkout_cucumber_std_ref(clone, "26.20") == ""                  # rien d'inférieur : pas de master


CUCUMBER_POM = ("<project><groupId>savoye</groupId><artifactId>clientx-cucumber</artifactId><version>1.0</version>"
                "<dependencies><dependency><groupId>io.cucumber</groupId><artifactId>cucumber-java</artifactId>"
                "<version>7.13.0</version></dependency><dependency><groupId>a-sis</groupId>"
                "<artifactId>Cucumber</artifactId><version>{lib}</version><type>test-jar</type>"
                "<scope>test</scope></dependency></dependencies></project>")


def test_library_read_on_the_cucumber_branch_of_the_chosen_version(project, tmp_path):
    """Cas Canson : intégration du dépôt Cucumber en 26.16, branche v1.3 en 26.22 ; version client 1.3.0."""
    _clone(tmp_path, f"{CLIENT}-cucumber", [("integration", {"pom.xml": CUCUMBER_POM.format(lib="26.16")}, ""),
                                           ("v1.3", {"pom.xml": CUCUMBER_POM.format(lib="26.22")}, "")])
    app = cv.app_repository(cv.repositories(project), GROUPS)
    assert app.name == "clientx-odatio-app"                       # le dépôt Cucumber n'est pas l'application
    entries = {e["type"]: e for e in cv.git_versions(app)}
    tag = cv.library_at_version(project, CLIENT, entries["tag"])
    assert tag == {"version": "26.22", "depot": f"{CLIENT}-cucumber", "ref": "v1.3", "repli": False}
    assert cv.library_at_version(project, CLIENT, entries["integration"])["version"] == "26.16"
    # Aucune branche pour la version : intégration, repli signalé
    other = cv.library_at_version(project, CLIENT, {"type": "tag", "version": "2.0.1", "ref": "odatio-app-2.0.1"})
    assert other["ref"] == "integration" and other["repli"]
    assert "aucune branche du dépôt Cucumber" in cv.library_where(other)
    deduced = cv.deduce(entries["tag"], app.file(entries["tag"], "pom.xml"), GROUPS, ["v26.22"], tag["version"], {},
                        library_source=cv.library_where(tag))
    assert not any("montée proposée" in w for w in deduced["avertissements"])
    assert "(actuelle" not in cv.line(deduced)
    stale = cv.deduce(entries["tag"], app.file(entries["tag"], "pom.xml"), GROUPS, ["v26.22"], "26.16", {},
                      library_source=cv.library_where(tag))
    assert any(f"({CLIENT}-cucumber, branche v1.3)" in w for w in stale["avertissements"])
    assert cv.cucumber_library_ref({"type": "release", "version": "1.3", "ref": "release/1.3"},
                                   ["integration", "release/1.3"]) == "release/1.3"


MODULE_POM = ("<project><parent><groupId>a-sis</groupId><artifactId>o2-parent-jvm11</artifactId><version>1.1.4"
              "</version></parent><artifactId>spec-core</artifactId><dependencies><dependency><groupId>a-sis</groupId>"
              "<artifactId>kis-core</artifactId><version>{kis}</version></dependency></dependencies></project>")


def test_kis_read_in_the_application_poms_when_no_build(project, tmp_path):
    """Cas Canson 1.2 : aucun build du Standard 26.21 -> version KIS lue dans les poms de l'application (modules)."""
    root = POM.format(own="1.2", std="26.21.0").replace("</project>", "<modules><module>wms</module></modules></project>")
    wms = "<project><modules><module>core</module></modules></project>"
    _clone(tmp_path, "clienty-odatio-app", [("integration", {"pom.xml": root, "wms/pom.xml": wms,
                                                             "wms/core/pom.xml": MODULE_POM.format(kis="3.11-SNAPSHOT")},
                                            "odatio-app-1.2.0")])
    app = next(r for r in cv.repositories(project) if r.name == "clienty-odatio-app")
    entry = next(e for e in cv.git_versions(app) if e["type"] == "tag")
    found = cv.kis_from_poms(app, entry)
    assert found["kis"] == "3.11" and found["source"] == "pom" and "wms/core/pom.xml : kis-core 3.11-SNAPSHOT" in found["detail"]
    deduced = cv.deduce(entry, app.file(entry, "pom.xml"), GROUPS, ["v26.21"], "26.21", {}, kis_pom=found)
    assert deduced["kis"]["kis"] == "3.11" and deduced["jenkins"]["kis_version"] == "3.11"
    assert not any("version KIS" in w for w in deduced["avertissements"])
    assert cv.line(deduced).endswith("KIS 3.11 (pom de l'application)")
    # Un build réel ou une saisie l'emporte sur le pom
    built = cv.deduce(entry, app.file(entry, "pom.xml"), GROUPS, ["v26.21"], "26.21",
                      {"26.21": {"kis": "3.10", "source": "build"}}, kis_pom=found)
    assert built["kis"]["kis"] == "3.10"
