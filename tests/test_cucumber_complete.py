"""Complément d'une génération (--complete), recettes de test, synonymes et couverture indirecte."""
import os

from tests.conftest import git, write
from tools import cucumber_tools as ct

FEATURE_REL = "src/test/resources/cucumber/features/apparel/commande.feature"


def _feature(path, title, words):
    return {"path": path, "feature": title, "scenarios": [title], "tags": [], "lines": 20,
            "words": sorted(set(ct._words(f"{path} {title} {words}"))), "norm": ct._norm(f"{path} {title} {words}"),
            "domains": [], "step_definitions": []}


def _index():
    """Index std_test_index réduit : une feature de reconditionnement pilotée par les steps KIS- d'interaction."""
    features = [_feature("features/WMS/Stock/TOW-90.feature", "Reconditionner un contenu",
                         "KIS- Ouvrir une interaction \"Reconditionnement\" KIS- Entrer sur cette interaction "
                         "KISInteractionStep")]
    features += [_feature(f"features/WMS/Divers/TOW-{n}.feature", f"Cas {n}", f"commande expédition transporteur {n}")
                 for n in range(30)]
    return {"features": features, "domains": []}


def _spec(**extra):
    spec = {"id": "sp-1", "libelle": "studio · spec-wms-content-process-interactive-repackcontent",
            "type": "extension", "nature": "processus", "depot": "studio",
            "dossier": "spec-wms-content-process-interactive-repackcontent",
            "mots_cles": ["interactif", "reconditionnement"], "fichiers": []}
    spec.update(extra)
    return spec


def test_synonyms_bring_interactive_repack_feature(monkeypatch):
    monkeypatch.delenv("CUCUMBER_SYNONYMS", raising=False)
    # « processus interactif de reconditionnement » : interactif -> interaction (steps KIS-)
    found = ct.related_std_tests(_spec(), _index(), min_score=0.4, client_name="apparel")
    assert found and found[0]["path"].endswith("TOW-90.feature")
    # Nom anglais collé du client : repackcontent -> repack (synonyme de reconditionnement) + content
    found = ct.related_std_tests(_spec(mots_cles=["repackcontent"]), _index(), min_score=0.4, client_name="apparel")
    assert found and found[0]["path"].endswith("TOW-90.feature")


def test_custom_synonyms(monkeypatch):
    monkeypatch.setenv("CUCUMBER_SYNONYMS", "palette|pallet|hu;Écran|vue")
    assert {"palette", "pallet", "hu"} <= ct.synonyms("Pallet")
    assert "vue" in ct.synonyms("ecran") and "screen" in ct.synonyms("ecran")


def test_recipes_match_and_reach_lot_block(tmp_path, monkeypatch):
    monkeypatch.delenv("PROMPTS_DIR", raising=False)
    recipes = ct.load_recipes()
    titles = [r["titre"] for r in recipes]
    assert len(recipes) == 3 and "Process interactif Studio" in titles
    assert recipes[0]["exemple"] == "TOW-14.feature" and "KIS- Ouvrir une interaction" in recipes[0]["steps"]
    interactive = _spec(mots_cles=["content", "repackcontent"])
    listener = _spec(id="sp-2", libelle="studio · spec-wms-goodsin-listener-controlandvalidate-goodsinfolder",
                     dossier="spec-wms-goodsin-listener-controlandvalidate-goodsinfolder",
                     mots_cles=["goodsin"])
    process = tmp_path / "customized.xml"
    process.write_text('<action name="set"><param name="customString1" value="Dangerous"/></action>',
                       encoding="utf-8")
    custom_field = _spec(id="sp-3", libelle="studio · managedangerous",
                         dossier="spec-wms-preparation-process-managedangerous", mots_cles=[],
                         fichiers=[{"path": "studio/managedangerous/customized.xml", "local": str(process)}])
    assert [r["titre"] for r in ct.matching_recipes(interactive, recipes)] == ["Process interactif Studio"]
    assert [r["titre"] for r in ct.matching_recipes(listener, recipes)] == ["Listener / événement sans logique propre"]
    assert [r["titre"] for r in ct.matching_recipes(custom_field, recipes)] == [
        "Champ ou entité modifié sans step dédié"]
    # Nature différente : la recette des process interactifs ne s'applique pas
    assert not ct.matching_recipes(_spec(nature="objet métier"), recipes)

    inventory = {"specifiques": [{**interactive, "couverture": "non_couvert", "nb_fichiers": 0},
                                 {**listener, "couverture": "non_couvert", "nb_fichiers": 0}]}
    assert ct.attach_recipes(inventory, "") == 2
    block = ct.lot_block(inventory["specifiques"])
    assert block.startswith("### Recettes applicables")
    assert "#### Process interactif Studio — spécifiques : sp-1" in block
    assert "Statut attendu : couvert_indirectement" in block


def test_custom_recipes_file(tmp_path, monkeypatch):
    write(str(tmp_path / ct.RECIPES_FILE), "## Cas maison\n- Mots : palette\n- Consigne : utiliser le step X\n  "
                                           "puis vérifier Y.\n")
    monkeypatch.setenv("PROMPTS_DIR", str(tmp_path))
    recipes = ct.load_recipes()
    assert [r["titre"] for r in recipes] == ["Cas maison"]
    assert recipes[0]["consigne"] == "utiliser le step X puis vérifier Y."


def _commit_lot(worktree, number, title):
    path = os.path.join(worktree, *FEATURE_REL.split("/"))
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(f"\n  Scenario: {title}\n    Then le champ \"statut\" vaut \"VALIDE\"\n")
    assert ct.commit_lot(worktree, "apparel", number)


def test_complete_keeps_previous_lots(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.delenv("CUCUMBER_KEEP_AGENT_TAGS", raising=False)
    write(os.path.join(repo, *FEATURE_REL.split("/")), "Feature: Commandes\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    worktree = ct.prepare_worktree(repo, "apparel", "integration", reset=True)
    _commit_lot(worktree, 1, "Lot un")
    _commit_lot(worktree, 2, "Lot deux")
    state = {"base": "integration", "lots": [
        {"numero": 1, "ids": ["a", "b"], "statut": "termine", "resultats": [
            {"id": "a", "statut": "couvert"},
            {"id": "b", "statut": "non_couvert", "raison": "Le Standard n'a aucun step de process interactif."}]},
        {"numero": 2, "ids": ["c", "d"], "statut": "termine", "resultats": [
            {"id": "c", "statut": "non_couvert", "raison": "Aucun endpoint n'expose customString1."},
            {"id": "d", "statut": "non_couvert", "raison": "Comportement purement graphique."}]}]}

    wanted = ct.completable_specifics(state)
    assert wanted == ["b", "c"]
    new = ct.add_completion_lots(state, wanted, 3)
    assert [lot["numero"] for lot in new] == [3] and new[0]["ids"] == ["b", "c"] and new[0]["complement"]
    first, second = state["lots"][:2]
    assert first["ids"] == ["a"] and [r["id"] for r in first["resultats"]] == ["a"]
    assert first["historique"] == [{"id": "b", "statut": "non_couvert", "raison": "Le Standard n'a aucun step de "
                                    "process interactif.", "repris_par_lot": 3}]
    assert second["ids"] == ["d"] and second["statut"] == "termine"
    assert [lot["numero"] for lot in ct.pending_lots(state)] == [3]
    # Déjà repris par un complément : pas proposé une seconde fois
    assert ct.completable_specifics(state) == []

    # Complément : worktree et commits conservés, même si le dossier du worktree a disparu
    ct._git(repo, "worktree", "remove", "--force", worktree)
    worktree = ct.prepare_worktree(repo, "apparel", "integration", reset=False)
    assert "lot 2" in git(worktree, "log", "-1", "--format=%s")
    _commit_lot(worktree, 3, "Lot trois")
    patch = str(tmp_path / "apparel-cucumber.patch")
    assert ct.write_patch(worktree, "integration", patch, "apparel")
    text = open(patch, encoding="utf-8").read()
    assert text.count("Subject: [PATCH") == 3 and all(t in text for t in ("Lot un", "Lot deux", "Lot trois"))
    # Nouvelle génération : repart de la base
    worktree = ct.prepare_worktree(repo, "apparel", "integration", reset=True)
    assert git(worktree, "log", "-1", "--format=%s") == "base"


def test_requalify_indirect():
    state = {"lots": [
        {"numero": 1, "ids": ["process", "listener", "orphelin", "deja"], "resultats": [
            {"id": "process", "statut": "couvert"},
            {"id": "listener", "statut": "couvert_indirectement", "couvert_par": "process"},
            {"id": "orphelin", "statut": "couvert_indirectement", "couvert_par": "inconnu"},
            {"id": "deja", "statut": "couvert_indirectement", "couvert_par": "ancien"}]}]}
    inventory = {"specifiques": [{"id": "ancien", "couverture": "deja_couvert"}]}
    assert ct.requalify_indirect(state, inventory) == ["orphelin"]
    results = ct.latest_results(state)
    assert results["listener"]["statut"] == "couvert_indirectement"
    assert results["deja"]["statut"] == "couvert_indirectement"
    assert results["orphelin"]["statut"] == "non_couvert" and "inconnu" in results["orphelin"]["requalification"]
    # Réévalué : le spécifique cité devient couvert par un complément
    state["lots"].append({"numero": 2, "ids": ["inconnu"], "resultats": [{"id": "inconnu", "statut": "couvert"}]})
    assert ct.requalify_indirect(state, inventory) == []
    assert ct.latest_results(state)["orphelin"]["statut"] == "couvert_indirectement"
