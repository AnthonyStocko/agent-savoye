"""Adresse de livraison de test : défaut injecté, surcharge du profil, aucune adresse inventée, rejet classé et corrigé."""
import os
import re
from string import Template

import pytest

from tools import cucumber_failures, cucumber_profile, cucumber_test_data as td, cucumber_verify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT = {"nom": "CLIENT TEST", "adresse": "rue de la richelandière", "boite_postale": "", "code_province": "",
           "code_postal": "42000", "ville": "SAINT-ETIENNE", "code_pays": "FR", "pays": "FRANCE"}
BELGIUM = {"nom": "CLIENT TEST BE", "adresse": "rue de la Loi 16", "code_postal": "1000", "ville": "BRUXELLES",
           "code_pays": "BE", "pays": "BELGIQUE"}

WOI = """<?xml version="1.0" encoding="UTF-8"?>\r
<wo:message xmlns:wo="urn:x">\r
    <shipper>\r
        <additionalPartyIdentification>VDA</additionalPartyIdentification>\r
        <address>\r
            <city>SITE VILLE</city>\r
            <postalCode>69000</postalCode>\r
        </address>\r
    </shipper>\r
    <receiver>\r
        <additionalPartyIdentification>CUST-CANSON-APPBL</additionalPartyIdentification>\r
        <address>\r
            <city>VILLE TEST</city>\r
            <countryCode>FR</countryCode>\r
            <languageOfThePartyCode>fr</languageOfThePartyCode>\r
            <name>CLIENT TEST</name>\r
            <postalCode>59780</postalCode>\r
            <streetAddressOne>Adresse1 destinataire</streetAddressOne>\r
            <streetAddressTwo>Bâtiment B</streetAddressTwo>\r
        </address>\r
    </receiver>\r
</wo:message>\r
"""


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    for _, _, env, _, _ in td.ADDRESS_FIELDS:
        monkeypatch.delenv(env, raising=False)


def test_default_address_is_the_generic_one_and_env_overrides_a_field(monkeypatch):
    assert td.default_address() == DEFAULT
    assert td.test_address({}) == DEFAULT and td.test_address(None) == DEFAULT
    monkeypatch.setenv("AGENT_TEST_ADDRESS_VILLE", "SAINT-ÉTIENNE")
    assert td.default_address()["ville"] == "SAINT-ÉTIENNE"
    # Variables documentées dans .env.example
    with open(os.path.join(ROOT, ".env.example"), encoding="utf-8") as f:
        example = f.read()
    assert all(f"\n{env}=" in example for _, _, env, _, _ in td.ADDRESS_FIELDS)


def test_default_address_injected_in_generation_and_correction_prompts():
    block = td.prompt_block({})
    for label, value in (("Nom de l'adresse", "CLIENT TEST"), ("Adresse", "rue de la richelandière"),
                         ("Boîte aux lettres", "(vide)"), ("Code province", "(vide)"), ("Code postal", "42000"),
                         ("Ville", "SAINT-ETIENNE"), ("Code pays", "FR"), ("Pays", "FRANCE")):
        assert f"  {label} : {value}" in block
    assert "Interdit : inventer une adresse" in block and "valeur par défaut de l'agent" in block
    assert "transporteur, mode d'expédition" in block and "jamais inventées" in block
    # Les trois prompts (génération, correction d'un lot, correction de l'init) reçoivent le bloc
    for name in ("cucumber_client_lot_systeme", "cucumber_client_fix", "cucumber_init_correction"):
        with open(os.path.join(ROOT, "prompts", f"{name}.md"), encoding="utf-8") as f:
            template = Template(f.read())
        names = {m[1] or m[2] for m in re.findall(r"\$(?:(\w+)|\{(\w+)\})", template.template)}
        rendered = template.substitute({**{n: "" for n in names}, "donnees_test": block})
        assert "rue de la richelandière" in rendered and "42000" in rendered, name
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
        main = f.read()
    assert '"donnees_test": cucumber_test_data.prompt_block(profile)' in main
    assert main.count('render_prompt("cucumber_client_fix", **{"logs_block": "", "donnees_test": ') == 3


def test_profile_address_wins_over_default_as_a_whole(monkeypatch, tmp_path):
    monkeypatch.setattr(cucumber_profile, "profile_path", lambda client: str(tmp_path / f"{client}.json"))
    cucumber_profile.save_user_fields("acme", {"adresse_test": BELGIUM})
    profile = cucumber_profile.effective_profile(cucumber_profile.load_profile("acme"))
    assert profile["sources"]["adresse_test"] == "saisi"
    address = td.test_address(profile)
    # En bloc : pas de champ français repris du défaut
    assert address == {**{k: "" for k in td.ADDRESS_KEYS}, **BELGIUM}
    block = td.prompt_block(profile)
    assert "(profil client)" in block and "BRUXELLES" in block and "SAINT-ETIENNE" not in block
    # Profil sans adresse (tous champs vides) : défaut
    assert td.test_address({"adresse_test": {k: "" for k in td.ADDRESS_KEYS}}) == DEFAULT
    assert cucumber_profile.empty_profile()["adresse_test"] == {}
    assert "adresse_test" in cucumber_profile.OPTIONAL_FIELDS


def test_no_generated_test_keeps_an_invented_delivery_address(tmp_path):
    rel = "files/canson/WOI-CANSON-APPBL1.xml"
    path = tmp_path / "files" / "canson" / "WOI-CANSON-APPBL1.xml"
    path.parent.mkdir(parents=True)
    path.write_bytes(WOI.encode("utf-8"))
    assert td.invented_addresses(WOI, DEFAULT) == [("streetAddressOne", "Adresse1 destinataire"),
                                                   ("postalCode", "59780"), ("city", "VILLE TEST"),
                                                   ("streetAddressTwo", "Bâtiment B")]
    changed = td.normalize_files(str(tmp_path), [rel, "absent.xml", "x.feature"], DEFAULT)
    assert [c[0] for c in changed] == [rel]
    text = path.read_bytes().decode("utf-8")
    receiver = text.split("<receiver>")[1]
    for tag, value in (("city", "SAINT-ETIENNE"), ("postalCode", "42000"), ("name", "CLIENT TEST"),
                       ("streetAddressOne", "rue de la richelandière"), ("countryCode", "FR")):
        assert f"<{tag}>{value}</{tag}>" in receiver
    # Champs vides de l'adresse de test : balises absentes, jamais vides
    assert "streetAddressTwo" not in receiver and "pOBoxNumber" not in receiver
    assert "VILLE TEST" not in text and "59780" not in text
    # Adresse de l'expéditeur (site) intacte, fins de ligne CRLF conservées, idempotent
    assert "<city>SITE VILLE</city>" in text and "<postalCode>69000</postalCode>" in text
    assert text.count("\r\n") == WOI.count("\r\n") - 1 and text.count("\n") == text.count("\r\n")  # ligne retirée
    assert td.invented_addresses(text, DEFAULT) == [] and td.normalize_files(str(tmp_path), [rel], DEFAULT) == []
    assert "VILLE TEST" in td.normalized_line(rel, changed[0][1])


def test_missing_address_field_is_added_with_the_namespace_prefix():
    text = ("<a:receiver><a:address>\n  <a:city>X</a:city>\n</a:address></a:receiver>")
    out = td.normalize_text(text, DEFAULT)
    assert "<a:city>SAINT-ETIENNE</a:city>" in out and "<a:postalCode>42000</a:postalCode>" in out
    assert "<a:name>CLIENT TEST</a:name>" in out and "pOBoxNumber" not in out


def test_address_rejection_classified_as_out_of_referential_and_corrected():
    message = "Ordre de préparation CANSON-APPBL-1 rejeté : Adresse de livraison incohérente"
    # Vérification d'un lot : échec du test (corrigé), avec la piste de l'adresse de test
    nature, _ = cucumber_verify.failure_kind(message)
    assert nature == "technique" and cucumber_verify.failure_category(nature) == "test"
    assert "adresse de livraison de test" in cucumber_verify.failure_hint(message)
    # Run importé : donnée de test hors référentiel, correction proposée avec l'adresse du profil, sinon le défaut
    result = cucumber_failures.classify({"message": message, "step": "WMS- Intégration du fichier"})
    assert (result["categorie"], result["nature"]) == ("correction", td.REFERENCE_NATURE)
    assert "hors référentiel" in result["cause"] and "42000 SAINT-ETIENNE" in result["action"]
    assert "sans toucher aux contrôles" in result["action"] and not result["action"].endswith("…")
    assert result["categorie"] in cucumber_failures.TO_WRITE and td.REFERENCE_NATURE in cucumber_failures.NATURES
    own = cucumber_failures.classify({"message": message, "contexte": {"profil": {"adresse_test": BELGIUM}}})
    assert "1000 BRUXELLES, BE" in own["action"]
    # Sans rejet d'adresse : classement inchangé
    assert cucumber_failures.classify({"message": "expected:<1> but was:<2>"})["nature"] != td.REFERENCE_NATURE


def test_empty_address_fields_are_removed_never_left_empty_and_fields_keep_the_gs1_order():
    """Run Canson du 09/11 (build 345) : <pOBoxNumber></pOBoxNumber> et <provinceCode></provinceCode> vides → WOI
    jamais intégré (checkGS1Message en délai dépassé), OP jamais créés."""
    lines = ["<receiver>", "    <address>", "        <city>SAINT-ETIENNE</city>", "        <countryCode>FR</countryCode>",
             "        <name>CLIENT TEST</name>", "        <pOBoxNumber></pOBoxNumber>",
             "        <postalCode>42000</postalCode>", "        <provinceCode/>",
             "        <streetAddressOne>rue de la richelandière</streetAddressOne>", "    </address>", "</receiver>"]
    crlf = "\r\n"
    woi = crlf.join(lines)
    assert ("pOBoxNumber", "") in td.invented_addresses(woi, DEFAULT)
    out = td.normalize_text(woi, DEFAULT)
    assert "pOBoxNumber" not in out and "provinceCode" not in out
    assert out == crlf.join(line for line in lines if "pOBox" not in line and "province" not in line)
    assert td.invented_addresses(out, DEFAULT) == []
    # Champ manquant : inséré à sa place dans l'ordre GS1 (après countryCode, avant postalCode), même indentation
    missing = crlf.join(line for line in lines if "<name>" not in line and "pOBox" not in line
                        and "province" not in line)
    assert td.normalize_text(missing, DEFAULT) == out
    block = td.prompt_block({})
    assert "jamais de balise vide" in block and "<pOBoxNumber></pOBoxNumber>" in block
