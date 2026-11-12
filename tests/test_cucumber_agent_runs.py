"""
Runs de vérification de l'agent et bilan d'un lot : paramètres du job propres au client (artefact détecté dans son
application, suites du Standard décochées, contrôle avant lancement), bilan sans contradiction, avertissement sans
init sur un environnement remonté, clear du socle prouvé, part des recherches en base dans le coût.
"""
import os

from tools import client_version, cucumber_bilan, cucumber_job as cjob, cucumber_profile, cucumber_simple, db_tools

DEFINITIONS = {name: {"type": kind, "defaut": default} for name, kind, default in (
    ("COMPONENT_OWNER", "StringParameterDefinition", "autreclient"),
    ("COMPONENT_NAME", "StringParameterDefinition", "odatio-app"),
    ("COMPONENT_VERSION", "StringParameterDefinition", "1.7-SNAPSHOT"),
    ("ADDITIONAL_ARGLINE", "StringParameterDefinition", "-DurlTdm='http://h'"),
    ("hasStandard", "BooleanParameterDefinition", True),
    ("CUCUMBER_STANDARD_BRANCH", "StringParameterDefinition", "master"),
    ("INIT_WMS", "BooleanParameterDefinition", True),
    ("DEBUT", "BooleanParameterDefinition", True),
    ("PARAMETRAGE", "BooleanParameterDefinition", True),
    ("hasSpecific", "BooleanParameterDefinition", False),
    ("CUCUMBER_SPEC_REPOSITORY", "StringParameterDefinition", "autreclient-cucumber"),
    ("CUCUMBER_SPEC_BRANCH", "StringParameterDefinition", "development"),
    ("wait", "BooleanParameterDefinition", True))}
PROFILE = {"verify_branch": "agent", "version_client": {"deduction": {"jenkins": {
    "composant_version": "1.3.0", "standard_branche": "v26.22", "kis_version": "3.12",
    "composant_proprietaire": "monclient", "composant_nom": "odatio-app"}}}}


def roles():
    return cjob.effective_roles(PROFILE, DEFINITIONS)[0]


def agent_params(profile=PROFILE):
    fixed, _ = cjob.fixed_parameters(profile, DEFINITIONS)
    return cjob.build_parameters(DEFINITIONS, roles(), "agent", "monclient-cucumber", "@X", "abc", fixed=fixed)


def test_component_comes_from_the_client_profile_never_from_the_last_client_launched():
    params = agent_params()
    assert params["COMPONENT_OWNER"] == "monclient" and params["COMPONENT_NAME"] == "odatio-app"
    assert params["COMPONENT_VERSION"] == "1.3.0" and params["CUCUMBER_SPEC_REPOSITORY"] == "monclient-cucumber"


def test_standard_suites_are_unchecked_for_agent_runs():
    params = agent_params()
    assert [params[n] for n in ("hasStandard", "INIT_WMS", "DEBUT", "PARAMETRAGE")] == ["false"] * 4
    assert params["hasSpecific"] == "true" and params["wait"] == "false"
    assert cjob.standard_switches({"X": {"type": "BooleanParameterDefinition", "defaut": True}}) == {}


def test_profile_job_params_still_win_over_standard_switches():
    params = agent_params({**PROFILE, "job_params": ["hasStandard=true"]})
    assert params["hasStandard"] == "true" and params["INIT_WMS"] == "false"


def test_launch_is_refused_with_parameters_of_another_client(tmp_path, monkeypatch):
    from tools import cucumber_tools
    monkeypatch.setattr(cucumber_tools, "CACHE_ROOT", str(tmp_path))
    assert cjob.launch_errors(agent_params(), roles(), "monclient", PROFILE, DEFINITIONS) == []
    # Artefact du dernier client lancé (configuration actuelle du job)
    foreign = {**agent_params(), "COMPONENT_OWNER": "autreclient"}
    errors = cjob.launch_errors(foreign, roles(), "monclient", PROFILE, DEFINITIONS)
    assert errors and all(e.startswith("paramètres d'un autre client, run arrêté") for e in errors)
    assert any("COMPONENT_OWNER" in e and "monclient" in e for e in errors)
    # Valeur qui cite un autre client connu (dépôt de la configuration actuelle du job)
    cited = {**agent_params(), "ADDITIONAL_ARGLINE": "-Dschema=autreclient_test"}
    assert any("ADDITIONAL_ARGLINE" in e for e in cjob.launch_errors(cited, roles(), "monclient", PROFILE,
                                                                     DEFINITIONS))
    # Artefact du client inconnu : arrêt, la valeur du job serait celle d'un autre client
    unknown = {"verify_branch": "agent"}
    assert any("inconnu" in e for e in cjob.launch_errors(agent_params(unknown), roles(), "monclient", unknown,
                                                          DEFINITIONS))


def test_pom_component_reads_the_project_not_its_parent_or_dependencies():
    pom = ("<project><parent><groupId>std.parent</groupId><artifactId>std-parent</artifactId></parent>"
           "<groupId>monclient</groupId><artifactId>odatio-app</artifactId><dependencies><dependency>"
           "<groupId>a-sis</groupId><artifactId>kis</artifactId></dependency></dependencies></project>")
    assert client_version.pom_component(pom) == ("monclient", "odatio-app")
    inherited = "<project><parent><groupId>monclient</groupId></parent><artifactId>app</artifactId></project>"
    assert client_version.pom_component(inherited) == ("monclient", "app")


LOT = {"indicateurs": {"scenarios": [{"fichier": "a.feature", "titre": "S1", "spec": "sp-1"},
                                     {"fichier": "b.feature", "titre": "S2", "spec": "sp-2"}]},
       "execution": {"mode": "branche", "scenarios": {
           "@t1": {"id": "a.feature::S1", "statut": "non_verifie"},
           "@t2": {"id": "b.feature::S2", "statut": "pertinent"}}}}


def test_an_unverified_test_is_never_counted_as_covered():
    results = [{"id": "sp-1", "statut": "couvert"}]
    files = ["src/x/F.feature", "src/files/D.xml", "src/java/S.java"]
    outcome = cucumber_bilan.lot_outcome(LOT, results, files, False, 1)
    assert outcome["couverts"] == [] and outcome["non_verifies"] == ["sp-1"]
    assert "0/1 spécifique(s) couvert(s) et vérifié(s)" in outcome["ligne"]
    assert "NON vérifié" in outcome["ligne"] and "aucun fichier écrit" not in outcome["ligne"]
    assert "1 feature, 1 fichier de données, 1 classe Java" in outcome["ligne"]
    pushed = cucumber_bilan.lot_outcome({**LOT, "commit": True}, results, files, False, 1)
    assert "commités et poussés lors de la vérification" in pushed["ligne"]
    verified = cucumber_bilan.lot_outcome(LOT, [{"id": "sp-2", "statut": "couvert"}], files, True, 1)
    assert verified["couverts"] == ["sp-2"] and verified["ligne"].startswith("✅")
    assert "aucun fichier écrit" in cucumber_bilan.lot_outcome(LOT, [], [], False, 1)["ligne"]


def test_rejected_modifications_are_explained(tmp_path):
    ecarts = tmp_path / "x.patch.ecarts.txt"
    ecarts.write_text("Modifications de l'agent non livrées (à reprendre si besoin) :\n"
                      "- src/a/F.feature : créé par l'agent mais déjà présent, différent, sur la branche (version de la "
                      "branche conservée) — lot 1\n"
                      "- src/a/S.java : modification de l'agent en conflit avec une modification faite depuis sur la "
                      "branche (version de la branche conservée) — lot 1\n"
                      "- src/a/D.xml : suppression écartée (jamais livrée) — lot 1\n", encoding="utf-8")
    lines = cucumber_bilan.rejected_explanation(str(ecarts))
    assert len(lines) == 3
    assert any("F.feature" in line and "version de la branche est conservée" in line for line in lines)
    assert any("D.xml" in line and "jamais de suppression" in line for line in lines)
    assert cucumber_bilan.rejected_explanation(str(tmp_path / "absent.txt")) == []


def test_db_share_of_the_lot_cost():
    entries = [{"ok": True}] * 25 + [{"ok": False}] * 4
    share = cucumber_bilan.db_share(entries, {"tours": 99, "cout_usd": 9.17})
    assert share["commandes"] == 29 and share["erreurs"] == 4 and share["cout_usd"] == round(29 / 99 * 9.17, 2)
    assert "29 commande(s) base dont 4 en erreur" in share["ligne"] and "29/99 tours" in share["ligne"]
    assert cucumber_bilan.db_share([], {})["ligne"] == "aucune recherche en base"


def test_no_init_warning_on_an_environment_rebuilt_for_each_run():
    console = "[Pipeline] Build running on slot 1\nPod containers with incomplete status: [dropschema]"
    proof = cucumber_simple.rebuilt_environment(["rien", console])
    assert proof
    warning = cucumber_simple.no_init_warning(proof, "postgresql u@h:5432/db", "schema_1")
    assert "schema_1" in warning and "PAS garanties" in warning and "cochez « Initialisation" in warning
    assert "recréé" not in warning and "Configuration copiée" in warning
    assert cucumber_simple.rebuilt_environment(["Finished: SUCCESS"]) == ""
    # « dropschema » ne recrée que le schéma des tests du Standard (cucumber_spec_<N>) : ce n'est pas une preuve
    assert cucumber_simple.rebuilt_environment(["containers with incomplete status: [dropschema]"]) == ""
    assert cucumber_simple.no_init_warning("") == ""


def test_socle_clear_proven_by_a_green_run_is_kept_in_the_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(cucumber_profile, "profile_path", lambda client: str(tmp_path / f"{client}.json"))
    route = "module/all"
    assert cucumber_profile.choose_clear(route, {}, True)[1]          # non prouvé : question posée
    document = cucumber_profile.record_proven_clear("monclient", [route], "https://jenkins/job/J/274", ["preuve"],
                                                    "routes init-data du socle")
    assert document[cucumber_profile.PROVEN_CLEARS][route]["build"].endswith("/274")
    assert cucumber_profile.choose_clear(route, {}, True, cucumber_profile.proven_clears("monclient")) == (route, "")
    profile = cucumber_profile.effective_profile(document)
    assert profile["clear_name"] == route and route in profile[cucumber_profile.PROVEN_CLEARS]
    from tools import cucumber_detection
    assert cucumber_detection.expected_clear(profile) == route


def test_unknown_column_error_hint_only_for_column_errors():
    assert db_tools.column_hint("SELECT a FROM t", "relation t does not exist") == ""
    assert db_tools._UNKNOWN_COLUMN.search("(psycopg2.errors.UndefinedColumn) column s.name does not exist")
    assert os.path.basename(db_tools.__file__) == "db_tools.py"
