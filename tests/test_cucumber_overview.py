"""État des tests de l'écran principal : échecs et raisons, désactivés, couvertes (dont par une autre), non testées."""
from tools import cucumber_coverage as cc
from tools import cucumber_overview as ov


def row(spec_id, titre, etat, scenarios=(), **extra):
    return {"id": spec_id, "titre": titre, "module": "WMS", "etat": etat, "scenarios": list(scenarios),
            "couvert_par": "", "chaine_titre": "", "cause": "", "phrase": "", "generation": {}, **extra}


def sc(identifier, etat, cause="", derniere=""):
    return {"id": identifier, "etat": etat, "cause": cause, "derniere": derniere}


ROWS = [
    row("a", "Réception", "echec", [sc("f/A.feature::S1", "echec", "OP introuvable", "import n° 7"),
                                    sc("f/A.feature::S2", "vert")]),
    row("b", "Expédition", "echec", [sc("f/A.feature::S1", "echec", "OP introuvable", "import n° 7")]),
    row("c", "Colisage", "desactive", [sc("f/C.feature::S3", "desactive", "mis de côté — liste vide")]),
    row("d", "Colisage détaillé", "non_couvert", couvert_par="c"),
    row("e", "Douane", "non_couvert", generation={"type": "blocage", "message": "endpoint absent"}),
    row("f", "Retours", "non_couvert"),
    row("g", "Inventaire", "a_confirmer", cause="quel statut attendu ?"),
    row("h", "Archivage", "sans_objet", cause="code mort"),
]


def test_failing_scenarios_are_listed_once_with_their_reason():
    failing = ov.failing(ROWS)
    assert len(failing) == 1 and failing[0]["fonctionnalites"] == ["Réception", "Expédition"]
    assert failing[0]["raison"] == "OP introuvable" and failing[0]["constate"] == "import n° 7"


def test_covered_includes_features_covered_by_another():
    covered = {c["fonctionnalite"]: c["couverture"] for c in ov.covered(ROWS)}
    assert covered["Colisage détaillé"] == "par « Colisage »"
    assert covered["Réception"] == "par son test (1 en échec, 1 vert)"
    sentence = " ".join(t for t, _ in cc.status_sentences(ROWS, {"date": "2026-10-29T10:00:00"}))
    assert "1 est couverte par une autre, soit 4 couvertes au total." in sentence


def test_untested_features_have_a_reason_and_disabled_who_decided():
    reasons = {u["fonctionnalite"]: u["raison"] for u in ov.untested(ROWS)}
    assert reasons["Douane"] == "Pas de test : blocage prouvé — endpoint absent"
    assert reasons["Retours"].startswith("pas encore traitée")
    assert reasons["Inventaire"] == "réponse attendue : quel statut attendu ?" and "Colisage détaillé" not in reasons
    assert [o["raison"] for o in ov.out_of_scope(ROWS)] == ["code mort"]
    disabled = ov.disabled(ROWS, [{"id": "f/C.feature::S3"}, {"id": "f/X.feature::Orphelin", "titre": "Orphelin",
                                                              "fichier": "f/X.feature", "preuve": "p"}])
    assert [(d["scenario"], d["par"]) for d in disabled] == [("S3", "toi (mis de côté)"),
                                                             ("Orphelin", "toi (mis de côté)")]


def test_agent_verification_after_the_imported_run_wins():
    sources = {"desactives": {}, "audit": {},
               "imports": {"f/A.feature::S1": {"statut": "echoue", "motif": "OP introuvable", "nature": "",
                                               "import": 7, "date": "2026-10-28T18:41:00"}},
               "verifies": cc.verified_results({"lots": [
                   {"numero": 54, "fin": "2026-10-28T10:00:00", "execution": {"scenarios": {
                       "@t": {"id": "f/A.feature::S1", "statut": "invalide", "motif": "ancien"}}}},
                   {"numero": 71, "fin": "2026-10-29T09:30:00", "execution": {"scenarios": {
                       "@t": {"id": "f/A.feature::S1", "statut": "pertinent", "motif": ""}}}}]})}
    status = cc.scenario_status("f/A.feature::S1", sources)
    assert status["etat"] == "vert" and status["derniere"] == "vérification du lot 71"
    sources["imports"]["f/A.feature::S1"]["date"] = "2026-10-29T12:00:00"   # run importé plus récent
    assert cc.scenario_status("f/A.feature::S1", sources)["etat"] == "echec"
