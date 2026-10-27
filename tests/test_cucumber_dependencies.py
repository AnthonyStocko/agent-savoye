"""
Couverture indirecte (client fictif) : éléments Studio sans logique propre classés sans lot Claude, liens ticket ↔
tâche vérifiés et titre sans statut Jira, propagation de l'état de la tâche qui couvre et substitution à la
sélection, pas de relance inutile, cycles et chaînes qui doivent aboutir à un vrai test.
"""
import os

import pytest

from tests.conftest import CLIENT, git, write
from tools import cucumber_coverage as cov
from tools import cucumber_profile as cp
from tools import cucumber_testability as cte
from tools import cucumber_tools as ct

F = f"src/test/resources/cucumber/features/{CLIENT}/"
PROCESS = "fic-wms-goodsin-process-controle-dossier"
EVENT = "fic-wms-goodsin-event-controle-dossier"
LISTENER = "fic-wms-goodsin-listener-controle-dossier"
EVENT_XML = ('<execution-event-definition xmlns="x" name="{name}">\n'
             '  <variable name="dossierId" type="java.util.UUID" scope="in"/>\n</execution-event-definition>\n')
LISTENER_XML = ('<execution-event-listener-definition xmlns="x" on-event="a/{event}" execute="a/{process}">\n'
                '  <mappings><input name="dossierId">$event.dossierId</input></mappings>\n'
                '</execution-event-listener-definition>\n')
PROCESS_XML = ('<process-definition xmlns="x"><interaction id="saisie" title="SAISIE"/>'
               '<action-ref key="a/valider" id="valider"/></process-definition>\n')


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(cov, "import_results", lambda client: {})
    return tmp_path


def studio_spec(tmp_path, spec_id: str, name: str, xml: str) -> dict:
    local = str(tmp_path / "copies" / name / "customized.xml")
    write(local, xml)
    return {"id": spec_id, "libelle": f"studio · {name}", "type": "extension", "nature": "processus",
            "depot": "studio", "dossier": name, "nb_fichiers": 1, "mots_cles": [], "couverture": "non_couvert",
            "fichiers": [{"repo": "studio", "path": f"{name}/customized.xml", "local": local, "blob": "b-" + spec_id}]}


@pytest.fixture
def specifics(cache):
    tmp = cache
    return {"sp-p": studio_spec(tmp, "sp-p", PROCESS, PROCESS_XML),
            "sp-e": studio_spec(tmp, "sp-e", EVENT, EVENT_XML.format(name=EVENT)),
            "sp-l": studio_spec(tmp, "sp-l", LISTENER, LISTENER_XML.format(event=EVENT, process=PROCESS))}


# ---------------------------------------------------------------------------
# 1. Classement déterministe, sans lot Claude
# ---------------------------------------------------------------------------

def test_studio_element_without_logic_is_classified_without_claude_lot(specifics):
    dependencies = ct.studio_dependencies(specifics)
    assert set(dependencies) == {"sp-e", "sp-l"}
    assert dependencies["sp-e"]["couvert_par"] == "sp-p" and dependencies["sp-l"]["couvert_par"] == "sp-p"
    assert "éléments <variable> seuls" in dependencies["sp-e"]["preuve"]
    # Le processus n'est pas sélectionné : l'événement et le listener ne partent quand même jamais dans un lot
    remaining, sorted_out = ct.presort(["sp-e", "sp-l"], specifics)
    assert remaining == [] and {r["id"]: (r["statut"], r["couvert_par"], r["tri"]) for r in sorted_out} == {
        "sp-e": ("couvert_indirectement", "sp-p", True), "sp-l": ("couvert_indirectement", "sp-p", True)}
    # Complément : lot du tri ajouté, la tâche retirée de son ancien lot
    state = {"lots": [{"numero": 1, "ids": ["sp-e"], "statut": "termine",
                       "resultats": [{"id": "sp-e", "statut": "non_couvert", "raison": "x"}]}]}
    lot = ct.add_sorted_lot(state, sorted_out)
    assert lot["numero"] == 2 and lot["tri"] and state["lots"][0]["ids"] == []
    assert ct.latest_results(state)["sp-e"]["statut"] == "couvert_indirectement"


def test_listener_with_condition_or_standard_process(cache, specifics):
    conditional = LISTENER_XML.format(event=EVENT, process=PROCESS).replace(
        "<mappings>", "<if>$event.dossierId != null</if><mappings>")
    specifics["sp-l"] = studio_spec(cache, "sp-l", LISTENER, conditional)
    assert "sp-l" not in ct.studio_dependencies(specifics)
    assert ct.presort(["sp-l"], specifics)[0] == ["sp-l"]           # logique propre : lot Claude
    # Processus du Standard (absent de l'inventaire) : sans objet, avec sa preuve
    del specifics["sp-p"]
    specifics["sp-l"] = studio_spec(cache, "sp-l", LISTENER, LISTENER_XML.format(event=EVENT, process=PROCESS))
    _, sorted_out = ct.presort(["sp-l"], specifics)
    assert cte.assess(sorted_out[0], {})["etat"] == "sans_objet"


# ---------------------------------------------------------------------------
# 2. Lien ticket ↔ tâche vérifié, titre sans statut Jira
# ---------------------------------------------------------------------------

def test_ticket_link_verified_by_commit_or_reference(cache, tmp_path):
    root = str(tmp_path / "client")
    repo = os.path.join(root, "app")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "integration")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.com")
    write(os.path.join(repo, "a", "Regle.java"), "class Regle {}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "FIC-12 règle de contrôle du dossier")
    write(os.path.join(repo, "b", "Autre.java"), "// FIC-9 : contrôle du dossier\nclass Autre {}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "ajout d'une classe")
    jira = str(tmp_path / "jira.md")
    write(jira, "# Tickets\n\n## FIC-7 — Ticket rapproché par mots-clés [Analysée]\ntexte\n")
    inventory = {"specifiques": [
        {"id": "sp-a", "libelle": "app · regle", "dossier": "regle", "jira": jira,
         "fichiers": [{"repo": "app", "path": "a/Regle.java"}]},
        {"id": "sp-b", "libelle": "app · autre", "dossier": "autre", "jira": "",
         "fichiers": [{"repo": "app", "path": "b/Autre.java", "local": os.path.join(repo, "b", "Autre.java")}]}]}
    tickets = [{"key": "FIC-12", "summary": "Contrôle du dossier\xa0d'arrivée [Released]"},
               {"key": "FIC-9", "summary": "Autre règle [Analysée]"}]
    links = cov.verify_ticket_links(CLIENT, inventory, root, tickets)
    assert [l["cle"] for l in links["sp-a"]] == ["FIC-12"] and links["sp-a"][0]["preuve"].startswith("commit ")
    assert links["sp-b"] == [{"cle": "FIC-9", "titre": "Autre règle", "preuve": "référence dans app/b/Autre.java"}]
    assert cov.load_ticket_links(CLIENT) == links
    # Titre : ticket vérifié sans statut ; le ticket rapproché par mots-clés seul (FIC-7) ne donne pas le titre
    assert cov.business_title(inventory["specifiques"][0], links["sp-a"]) == "Contrôle du dossier d'arrivée"
    assert cov.business_title(inventory["specifiques"][0]) == "Regle"


# ---------------------------------------------------------------------------
# 3. Propagation et substitution ; 5. chaînes et cycles
# ---------------------------------------------------------------------------

def process_state(verdict: str = "") -> dict:
    """Génération : le processus couvert par un scénario (vérifié `verdict`, sinon jamais exécuté)."""
    lot = {"numero": 1, "ids": ["sp-p"], "statut": "termine", "resultats": [
        {"id": "sp-p", "statut": "couvert", "scenarios": [{"nom": "Feature / p1", "fichier": F + "p.feature"}]}]}
    if verdict:
        lot["execution"] = {"scenarios": {"@t1": {"id": F + "p.feature::p1", "statut": verdict, "motif": "m"}}}
    return {"lots": [lot]}


def rows_of(specifics, state) -> dict:
    return {r["id"]: r for r in cov.coverage_rows(CLIENT, {"specifiques": list(specifics.values())}, state)}


def test_dependents_wait_for_the_covering_task_then_follow_its_state(specifics):
    rows = rows_of(specifics, {"lots": []})
    assert rows["sp-e"]["etat"] == "en_attente" and rows["sp-e"]["couvert_par"] == "sp-p"
    assert rows["sp-e"]["chaine"] == ["sp-e", "sp-p"] and "Générez d'abord" in rows["sp-e"]["phrase"]
    assert rows["sp-p"]["etat"] == "non_couvert"
    # Cocher une tâche dépendante : la tâche dont elle dépend est proposée à sa place
    assert cov.substitute_dependents(list(rows.values()), ["sp-e", "sp-l"]) == (["sp-p"], [("sp-e", "sp-p"),
                                                                                           ("sp-l", "sp-p")])
    # Processus vert : dépendants couverts indirectement ; en échec ou jamais exécuté : ils suivent son état
    for verdict, expected in (("pertinent", "vert"), ("invalide", "echec"), ("", "non_execute")):
        rows = rows_of(specifics, process_state(verdict))
        assert rows["sp-p"]["etat"] == expected
        assert rows["sp-e"]["etat"] == rows["sp-l"]["etat"] == expected
        assert rows["sp-e"]["cause"].startswith("couvert indirectement par «")


def test_cycle_and_chain_without_real_test(cache):
    specifics = {i: {"id": i, "libelle": f"app · {i}", "type": "extension", "nature": "objet métier", "depot": "app",
                     "dossier": i, "nb_fichiers": 1, "mots_cles": [], "couverture": "non_couvert",
                     "fichiers": [{"repo": "app", "path": f"{i}.java"}]} for i in ("sp-a", "sp-b", "sp-c", "sp-d")}
    state = {"lots": [{"numero": 1, "ids": list(specifics), "statut": "termine", "resultats": [
        {"id": "sp-a", "statut": "couvert_indirectement", "couvert_par": "sp-b"},
        {"id": "sp-b", "statut": "couvert_indirectement", "couvert_par": "sp-a"},
        {"id": "sp-c", "statut": "couvert_indirectement", "couvert_par": "sp-d"},
        {"id": "sp-d", "statut": "non_couvert", "raison": "DTO.", "sans_objet": {"preuve": "D.java:1"}}]}]}
    rows = rows_of(specifics, state)
    assert rows["sp-a"]["etat"] == "non_couvert" and "cycle de couverture indirecte" in rows["sp-a"]["cause"]
    assert rows["sp-a"]["chaine"] == ["sp-a", "sp-b", "sp-a"]
    assert rows["sp-c"]["etat"] == "non_couvert" and "n'aboutit à aucun vrai test" in rows["sp-c"]["cause"]
    assert ct.studio_dependencies(specifics) == {}


# ---------------------------------------------------------------------------
# 4. Pas de relance inutile
# ---------------------------------------------------------------------------

def test_no_useless_relaunch_when_inputs_unchanged(cache):
    spec = {"id": "sp-a", "libelle": "app · regle", "type": "extension", "nature": "objet métier", "depot": "app",
            "dossier": "regle", "nb_fichiers": 1, "mots_cles": [], "couverture": "non_couvert",
            "fichiers": [{"repo": "app", "path": "Regle.java", "blob": "b1"}]}
    profile = cp.effective_profile(cp.load_profile(CLIENT))
    empty = {"id": "sp-a", "statut": "non_couvert", "resume": "Aucun comportement observable trouvé.",
             "scenarios": [], "fichiers": [], "entrees": cte.task_inputs(spec, profile)}
    state = {"lots": [{"numero": 7, "ids": ["sp-a"], "statut": "termine", "resultats": [empty]}]}
    row = rows_of({"sp-a": spec}, state)["sp-a"]
    assert row["relance_inutile"] == {"lot": 7, "raison": "Aucun comportement observable trouvé."}
    # Une réponse de l'utilisateur (ou le code, ou le profil) change : la relance redevient utile
    cte.save_answer(CLIENT, "Quel code de dossier utiliser pour le scénario ?", "DOSSIER-1", "sp-a")
    assert rows_of({"sp-a": spec}, state)["sp-a"]["relance_inutile"] == {}
    changed = {**spec, "fichiers": [{"repo": "app", "path": "Regle.java", "blob": "b2"}]}
    assert cte.task_inputs(changed, profile) != empty["entrees"]
    # Sans empreinte (lot antérieur) : jamais bloquant
    del empty["entrees"]
    assert rows_of({"sp-a": spec}, state)["sp-a"]["relance_inutile"] == {}
