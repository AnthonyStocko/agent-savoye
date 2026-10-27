"""
Carte d'une tâche (vue Couverture), rendue par Streamlit sans navigateur sur un projet fictif : réponse proposée
avec Confirmer / Corriger, question précise avec champ de saisie, enregistrement automatique, « Générer le test »
désactivé tant qu'une réponse est attendue, aucun champ de saisie pour un facteur détectable, titre métier.
"""
import os

import pytest
from streamlit.testing.v1 import AppTest

from tests.conftest import CLIENT, CLIENT_TAG, write
from tools import cucumber_coverage as cov
from tools import cucumber_detection as cd
from tools import cucumber_profile as cp
from tools import cucumber_testability as cte
from tools import cucumber_tools as ct

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC_KEYS = ("DOC_FICTIF_A", "DOC_FICTIF_B")
PROCESS = "ref-report-listener-render-reportdoc-request"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Projet fictif : une tâche Studio dont deux documents sont connus (init) et un troisième introuvable."""
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(ct, "WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(cd, "start", lambda *args, **kwargs: False)
    monkeypatch.setattr(cd, "running", lambda *args, **kwargs: False)
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    cd.save_project(CLIENT, {"client_repo": repo, "client_name": CLIENT, "db_schema": "", "application": "",
                             "std_repo": repo})
    ct.save_inventory(CLIENT, {"genere_le": "2026-10-27T10:00:00", "specifiques": [
        {"id": "sp-1", "libelle": f"studio · {PROCESS}", "type": "override", "nature": "processus", "depot": "studio",
         "dossier": PROCESS, "fichiers": [{"repo": "studio", "path": f"{PROCESS}/customized.xml", "kind": "override"}],
         "steps_candidats": ["REPORT- x"]}]})
    ct.save_state(CLIENT, {"lots": [{"numero": 1, "ids": ["sp-1"], "statut": "termine", "resultats": [
        {"id": "sp-1", "statut": "non_couvert",
         "raison": f"Documents {DOC_KEYS[0]}, {DOC_KEYS[1]} et DOC_FICTIF_Z exclus : fixtures non disponibles, clés "
                   f"non vérifiées."}]}]})
    cte.save_index(CLIENT, {v: [{"source": "init", "ou": "init.feature:3", "entite": "RefReportDoc"}]
                            for v in DOC_KEYS})
    return tmp_path


def open_card(project) -> AppTest:
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=120)
    at.session_state[f"cucumber_step_{CLIENT}"] = "couverture"
    at.session_state[f"cucumber_card_{CLIENT}"] = "sp-1"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def card_keys(at, kind: str) -> list:
    return [w.key for w in getattr(at, kind) if (w.key or "").startswith(("cucumber_fix_", "cucumber_answer_",
                                                                          "cucumber_card_generate_", "cucumber_risk_"))]


def test_business_title_and_sentence(project):
    spec = ct.load_inventory(CLIENT)["specifiques"][0]
    assert cov.business_title(spec) == "Rendu : document, demande (déclencheur)"
    row = cov.coverage_rows(CLIENT, ct.load_inventory(CLIENT))[0]
    assert row["titre"] == "Rendu : document, demande (déclencheur)" and PROCESS not in row["phrase"]
    assert "fixtures" not in row["phrase"]


def test_card_proposes_answer_asks_precise_question_and_saves_automatically(project):
    at = open_card(project)
    markdown = " ".join(m.value for m in at.markdown)
    assert "Rendu : document, demande (déclencheur)" in markdown and "Réponse proposée" in markdown
    assert "DOC_FICTIF_A, DOC_FICTIF_B" in markdown and "DOC_FICTIF_Z est cité mais introuvable" in markdown
    assert "Quelle donnée, clé ou valeur faut-il" not in markdown
    # Champ de saisie : seulement la question sans réponse proposée (pas de champ avant « Corriger »)
    assert [k for k in card_keys(at, "text_input")] == [k for k in card_keys(at, "text_input")
                                                       if k.startswith("cucumber_answer_")]
    assert len(card_keys(at, "text_input")) == 1
    generate = [b for b in at.button if (b.key or "").startswith("cucumber_card_generate_")][0]
    assert generate.disabled and any("1 réponse(s) attendue(s)" in c.value for c in at.caption)
    assert not [b for b in at.button if b.label == "Enregistrer"]
    # « Confirmer » : enregistré aussitôt dans le profil, sans bouton « Enregistrer »
    [b for b in at.button if b.label == "Confirmer"][0].click().run()
    saved = cte.answers(cp.effective_profile(cp.load_profile(CLIENT)))
    assert [a["statut"] for a in saved.values()] == ["confirmee"]
    assert list(saved.values())[0]["reponse"] == ", ".join(DOC_KEYS)
    # Réponse à la question : enregistrée à la validation du champ, la génération devient possible
    [t for t in at.text_input if (t.key or "").startswith("cucumber_answer_")][0].input("créer dans le test").run()
    saved = cte.answers(cp.effective_profile(cp.load_profile(CLIENT)))
    assert sorted(a["statut"] for a in saved.values()) == ["confirmee", "saisie"]
    generate = [b for b in at.button if (b.key or "").startswith("cucumber_card_generate_")][0]
    assert not generate.disabled


def test_correct_opens_input_only_after_click(project):
    at = open_card(project)
    assert not [k for k in card_keys(at, "text_input") if k.startswith("cucumber_fix_")]
    [b for b in at.button if b.label == "Corriger"][0].click().run()
    fix = [t for t in at.text_input if (t.key or "").startswith("cucumber_fix_")]
    assert len(fix) == 1 and fix[0].value == ", ".join(DOC_KEYS)
    fix[0].input(DOC_KEYS[0]).run()
    saved = list(cte.answers(cp.effective_profile(cp.load_profile(CLIENT))).values())
    assert saved[0]["statut"] == "corrigee" and saved[0]["reponse"] == DOC_KEYS[0]


def test_no_input_for_detectable_factors(project):
    at = open_card(project)
    labels = [w.label for kind in ("selectbox", "segmented_control", "radio", "text_input", "multiselect")
              for w in getattr(at, kind) if (w.key or "").startswith(("cucumber_fix_", "cucumber_answer_",
                                                                      "cucumber_risk_"))]
    for factor in cov.CRITERIA.values():
        assert not any(factor.lower() in (label or "").lower() for label in labels)
    # Facteurs affichés en lecture seule, « non déterminé » avec la raison ; seul le risque se modifie
    markdown = " ".join(m.value for m in at.markdown)
    assert f"Personnalisation studio déployée : **{cov.UNDETERMINED}** — aucun scénario lié exécuté" in markdown
    assert card_keys(at, "segmented_control") == [f"cucumber_risk_{CLIENT}_sp-1"]


def test_testable_card_shows_only_title_sentence_and_generate(project):
    cte.save_index(CLIENT, {v: [{"source": "init", "ou": "init.feature:3"}] for v in (*DOC_KEYS, "DOC_FICTIF_Z")})
    at = open_card(project)
    assert not card_keys(at, "text_input")
    assert not [b for b in at.button if b.label in ("Confirmer", "Corriger")]
    generate = [b for b in at.button if (b.key or "").startswith("cucumber_card_generate_")][0]
    assert not generate.disabled
    assert any(CLIENT_TAG not in m.value and "🟢" not in m.value and "non couvert" in m.value for m in at.markdown)


def test_waiting_card_opens_covering_task_and_useless_relaunch_is_explained(project):
    inventory = ct.load_inventory(CLIENT)
    inventory["specifiques"].append({"id": "sp-2", "libelle": "app · dossier", "type": "extension",
                                     "nature": "objet métier", "depot": "app", "dossier": "dossier",
                                     "fichiers": [{"repo": "app", "path": "Dossier.java", "blob": "b"}]})
    ct.save_inventory(CLIENT, inventory)
    state = ct.load_state(CLIENT)
    state["lots"][0]["ids"].append("sp-2")
    state["lots"][0]["resultats"].append({"id": "sp-2", "statut": "non_couvert", "statut_declare":
                                          "couvert_indirectement", "couvert_par": "sp-1"})
    ct.save_state(CLIENT, state)
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=120)
    at.session_state[f"cucumber_step_{CLIENT}"] = "couverture"
    at.session_state[f"cucumber_card_{CLIENT}"] = "sp-2"
    at.run()
    assert not at.exception
    assert not [b for b in at.button if (b.key or "").startswith("cucumber_card_generate_")]
    opener = [b for b in at.button if (b.key or "").startswith("cucumber_card_open_")]
    assert len(opener) == 1 and opener[0].label == "Ouvrir « Rendu : document, demande (déclencheur) »"
    opener[0].click().run()
    assert at.session_state[f"cucumber_card_{CLIENT}"] == "sp-1"
    # Dernier lot sans rien écrit, entrées inchangées : la carte explique au lieu de proposer une relance
    profile = cp.effective_profile(cp.load_profile(CLIENT))
    spec = ct.load_inventory(CLIENT)["specifiques"][0]
    state = ct.load_state(CLIENT)
    state["lots"][0]["resultats"][0].update(resume="Aucun scénario écrit.", entrees=cte.task_inputs(spec, profile))
    cte.save_index(CLIENT, {v: [{"source": "init", "ou": "init.feature:3"}] for v in (*DOC_KEYS, "DOC_FICTIF_Z")})
    ct.save_state(CLIENT, state)
    at = open_card(project)
    assert not [b for b in at.button if (b.key or "").startswith("cucumber_card_generate_")]
    assert any("la relancer donnerait le même résultat" in c.value for c in at.caption)
