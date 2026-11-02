"""
Isolation des clients : aucune valeur du profil d'un client (clear, sites, codes, chemins) dans les fichiers générés
pour un autre client ; valeurs partagées (même profil, initialisation du Standard) non comptées.
"""
import os

from tests.conftest import CLEAR, CLIENT, write
from tools import cucumber_isolation as iso

OTHER = {"autre": {"clear_name": "init-data-rest-clear-main-autre", "tag_client": "@AUTRE",
                   "features_root": "src/test/resources/cucumber/features/autre/",
                   "reference_codes": {"sites": ["SITE_AUTRE", "STD_SITE"]}, "spec_files": [], "init_features": []}}
OWN = {"clear_name": CLEAR, "tag_client": "@CLIENTX", "features_root": f"src/test/resources/cucumber/features/{CLIENT}/",
       "reference_codes": {"sites": ["SITEA"]}}


def test_foreign_clear_site_and_path_are_reported(tmp_path):
    root = str(tmp_path)
    write(os.path.join(root, "a.feature"), 'Given INIT- Purge de l\'environnement par le clear '
                                           '"init-data-rest-clear-main-autre"\n  | SITE_AUTRE |\n')
    write(os.path.join(root, "b.feature"), "| src/test/resources/cucumber/files/autre/x.csv |\n")
    write(os.path.join(root, "ok.feature"), f'par le clear "{CLEAR}"\n  | SITEA |\n')
    errors = iso.check_files(CLIENT, root, ["a.feature", "b.feature", "ok.feature"], OWN, others=OTHER)
    assert len(errors) == 3 and not any(e.startswith("ok.feature") for e in errors)
    assert any("clear « init-data-rest-clear-main-autre »" in e and "client autre" in e for e in errors)
    assert any("SITE_AUTRE" in e for e in errors) and any(e.startswith("b.feature") for e in errors)


def test_shared_values_are_not_reported(tmp_path):
    # Même clear que l'autre client (bundle Standard commun) et code de l'init du Standard : valeurs partagées
    root = str(tmp_path)
    write(os.path.join(root, "a.feature"), "| STD_SITE |\n")
    own = {**OWN, "clear_name": "init-data-rest-clear-main-autre"}
    write(os.path.join(root, "b.feature"), 'par le clear "init-data-rest-clear-main-autre"\n')
    assert iso.check_files(CLIENT, root, ["b.feature"], own, others=OTHER) == []
    assert iso.scan({"a.feature": "| STD_SITE |"},
                    iso.foreign_values(CLIENT, OWN, common_text="code\nSTD_SITE\n", others=OTHER)) == []
