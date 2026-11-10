"""
Données de test contrôlées par un référentiel (0 token) : adresse de livraison de test et règle « jamais inventé ».

Adresse de livraison de test : GÉNÉRIQUE, valable pour tous les clients, réelle et acceptée par le contrôle des
lieux de livraison (validation de l'adresse par le TMS). Valeur par défaut de l'agent (DEFAULT_ADDRESS, chaque champ
surchargeable par la variable AGENT_TEST_ADDRESS_<CHAMP> du .env), remplacée par le champ « adresse_test » du profil
client quand il est renseigné (ex. client hors de France) : la valeur du profil l'emporte, en bloc.

Usages :
- prompt_block : injecté dans le prompt système des lots et dans les prompts de correction (fixed_values) ;
- normalize_files : adresses de livraison des fichiers de données GS1 (receiver, shipTo…) remplacées champ par champ
  par l'adresse de test, après chaque lot et chaque correction (contrôle des fichiers du lot) ;
- is_address_rejection : rejet « Adresse de livraison incohérente » → donnée de test hors référentiel.
"""
import os
import re


# (clé, libellé, variable d'environnement, balise GS1 de l'élément <address>, valeur par défaut)
ADDRESS_FIELDS = (
    ("nom", "Nom de l'adresse", "AGENT_TEST_ADDRESS_NOM", "name", "CLIENT TEST"),
    ("adresse", "Adresse", "AGENT_TEST_ADDRESS_ADRESSE", "streetAddressOne", "rue de la richelandière"),
    ("boite_postale", "Boîte aux lettres", "AGENT_TEST_ADDRESS_BOITE_POSTALE", "pOBoxNumber", ""),
    ("code_province", "Code province", "AGENT_TEST_ADDRESS_CODE_PROVINCE", "provinceCode", ""),
    ("code_postal", "Code postal", "AGENT_TEST_ADDRESS_CODE_POSTAL", "postalCode", "42000"),
    ("ville", "Ville", "AGENT_TEST_ADDRESS_VILLE", "city", "SAINT-ETIENNE"),
    ("code_pays", "Code pays", "AGENT_TEST_ADDRESS_CODE_PAYS", "countryCode", "FR"),
    ("pays", "Pays", "AGENT_TEST_ADDRESS_PAYS", "", "FRANCE"),
)
ADDRESS_KEYS = tuple(f[0] for f in ADDRESS_FIELDS)
ADDRESS_LABELS = {f[0]: f[1] for f in ADDRESS_FIELDS}
DEFAULT_ADDRESS = {f[0]: f[4] for f in ADDRESS_FIELDS}
# Lignes d'adresse sans équivalent dans l'adresse de test : vidées (jamais inventées)
EXTRA_STREET_TAGS = ("streetAddressTwo", "streetAddressThree", "streetAddressFour")
# Parties dont l'adresse est une adresse de livraison (fichiers GS1 : commandes WOI, clients, partenaires)
DELIVERY_PARTIES = ("receiver", "shipTo", "ultimateConsignee", "deliveryParty", "consignee", "buyer", "shipToParty")
DATA_EXTENSIONS = (".xml",)

ADDRESS_REJECTION = re.compile(r"adresse de livraison incoh[ée]rente|incoherent delivery address|"
                               r"inconsistent delivery address", re.I)
REFERENCE_NATURE = "donnee_hors_referentiel"
REFERENCE_LABEL = "donnée de test hors référentiel"


def default_address() -> dict:
    """Adresse de test par défaut de l'agent : DEFAULT_ADDRESS, champ par champ surchargé par AGENT_TEST_ADDRESS_*."""
    return {key: (os.getenv(env, "") or "").strip() or default for key, _, env, _, default in ADDRESS_FIELDS}


def profile_address(profile: dict) -> dict:
    """Adresse de test saisie dans le profil client (champ « adresse_test »), {} si aucun champ n'est renseigné."""
    value = (profile or {}).get("adresse_test") or {}
    if not isinstance(value, dict) or not any(str(value.get(k) or "").strip() for k in ADDRESS_KEYS):
        return {}
    return {key: str(value.get(key) or "").strip() for key in ADDRESS_KEYS}


def test_address(profile: dict = None) -> dict:
    """Adresse de livraison de test appliquée : celle du profil client si renseignée (en bloc), sinon le défaut."""
    return profile_address(profile) or default_address()


def address_source(profile: dict = None) -> str:
    return "profil client" if profile_address(profile) else "valeur par défaut de l'agent"


def address_lines(address: dict) -> list:
    return [f"  {ADDRESS_LABELS[key]} : {address.get(key) or '(vide)'}" for key in ADDRESS_KEYS]


def prompt_block(profile: dict = None) -> str:
    """Bloc des prompts de génération et de correction : adresse de livraison de test et données de référentiel."""
    address = test_address(profile)
    tags = ", ".join(f"{tag} = « {address[key]} »" for key, _, _, tag, _ in ADDRESS_FIELDS if tag)
    return "\n".join([
        "DONNÉES DE TEST CONTRÔLÉES PAR UN RÉFÉRENTIEL (jamais inventées) :",
        f"- Adresse de livraison de test ({address_source(profile)}), réelle et acceptée par le contrôle des lieux de "
        f"livraison (validation de l'adresse par le TMS) :",
        *address_lines(address),
        "- Tout test qui crée un client, une commande (ex. fichiers WOI) ou une adresse de livraison reprend CETTE "
        "adresse champ par champ, casse comprise. Interdit : inventer une adresse (nom, rue, code postal, ville).",
        f"- Fichiers GS1 : dans l'élément <address> du destinataire (receiver, shipTo…) : {tags} ; "
        f"{', '.join(EXTRA_STREET_TAGS[:2])} absents. Champ vide de l'adresse de test : balise ABSENTE, "
        "jamais de balise vide (ex. <pOBoxNumber></pOBoxNumber>) : le fichier GS1 serait rejeté et jamais intégré. "
        "Ordre des balises dans <address> : city, countryCode, languageOfThePartyCode, name, pOBoxNumber, "
        "postalCode, provinceCode, streetAddressOne.",
        "- Rejet « Adresse de livraison incohérente » sur une donnée de test : donnée de test hors référentiel ; "
        "remplace l'adresse par cette adresse de test, sans toucher aux contrôles du test.",
        "- Même principe pour toute autre donnée contrôlée par un référentiel (transporteur, mode d'expédition, "
        "produit, site, donneur d'ordres…) : valeurs reprises d'objets réels valides du client (init du client, "
        "codes de référence du profil, base de l'environnement), jamais inventées.",
    ])


def is_address_rejection(text: str) -> bool:
    return bool(ADDRESS_REJECTION.search(str(text or "")))


# Ordre des éléments de l'AddressType GS1 (séquence XSD) : un champ ajouté prend sa place dans cet ordre
GS1_ADDRESS_ORDER = ("city", "cityCode", "countryCode", "countyCode", "crossStreet", "currencyOfPartyCode",
                     "languageOfThePartyCode", "name", "pOBoxNumber", "postalCode", "provinceCode", "state",
                     "streetAddressOne", "streetAddressTwo", "streetAddressThree", "streetAddressFour",
                     "geographicalCoordinates")


def _tag_pattern(tag: str):
    return re.compile(rf"<((?:[\w.-]+:)?{tag})(\s[^>]*)?(?:/>|>(.*?)</\1\s*>)", re.S)


def _set_tag(block: str, tag: str, value: str) -> str:
    """
    Valeur de la balise `tag` (préfixe d'espace de noms permis) dans `block`. Valeur vide : balise retirée avec sa
    ligne (jamais de balise vide : la validation GS1 rejette le fichier, WOI jamais intégré). Balise absente et valeur
    non vide : ajoutée à sa place dans l'ordre GS1_ADDRESS_ORDER.
    """
    match = _tag_pattern(tag).search(block)
    if match:
        if value:
            return block[:match.start()] + f"<{match.group(1)}{match.group(2) or ''}>{value}</{match.group(1)}>" \
                + block[match.end():]
        start, end = match.start(), match.end()
        line_start = block.rfind("\n", 0, start) + 1
        if not block[line_start:start].strip():
            start = line_start - 1 if line_start > 0 else 0
            if start > 0 and block[start - 1] == "\r":
                start -= 1
        return block[:start] + block[end:]
    if not value:
        return block
    close = re.search(r"(\s*)</((?:[\w.-]+:)?address)\s*>\s*$", block)
    if not close:
        return block
    prefix = close.group(2).rsplit(":", 1)[0] + ":" if ":" in close.group(2) else ""
    element = f"<{prefix}{tag}>{value}</{prefix}{tag}>"
    rank = GS1_ADDRESS_ORDER.index(tag) if tag in GS1_ADDRESS_ORDER else len(GS1_ADDRESS_ORDER)
    for child in re.finditer(r"(\r?\n[ \t]*)?<(?:[\w.-]+:)?(\w+)[\s/>]", block[:close.start()]):
        name = child.group(2)
        if name in GS1_ADDRESS_ORDER and GS1_ADDRESS_ORDER.index(name) > rank:
            # Avant le premier champ qui le suit dans l'ordre GS1, à la même indentation
            sep = child.group(1) or ""
            at = child.start() + len(sep)
            return block[:at] + element + sep + block[at:]
    indent = re.search(r"(\r?\n[ \t]*)<(?:[\w.-]+:)?(?:city|postalCode|name|countryCode)\b", block)
    return block[:close.start()] + (indent.group(1) if indent else "") + element + block[close.start():]


def _apply(address_block: str, address: dict) -> str:
    out = address_block
    for key, _, _, tag, _ in ADDRESS_FIELDS:
        if tag:
            out = _set_tag(out, tag, address.get(key) or "")
    for tag in EXTRA_STREET_TAGS:
        if re.search(rf"<(?:[\w.-]+:)?{tag}\b", out):
            out = _set_tag(out, tag, "")
    return out


_PARTY = re.compile(rf"<((?:[\w.-]+:)?(?:{'|'.join(DELIVERY_PARTIES)}))(\s[^>]*)?>(.*?)</\1\s*>", re.S)
_ADDRESS = re.compile(r"<((?:[\w.-]+:)?address)(\s[^>]*)?>.*?</\1\s*>", re.S)


def normalize_text(text: str, address: dict) -> str:
    """Adresses de livraison d'un fichier GS1 (élément <address> des parties DELIVERY_PARTIES) remplacées par
    `address`, champ par champ ; le reste du fichier (adresse de l'expéditeur comprise) est inchangé."""
    def in_party(match):
        body = _ADDRESS.sub(lambda m: _apply(m.group(0), address), match.group(3))
        return match.group(0)[:match.start(3) - match.start(0)] + body + match.group(0)[match.end(3) - match.start(0):]
    return _PARTY.sub(in_party, text)


def invented_addresses(text: str, address: dict) -> list:
    """Champs des adresses de livraison qui diffèrent de l'adresse de test : [(balise, valeur trouvée)]."""
    out = []
    for party in _PARTY.finditer(text):
        for block in _ADDRESS.finditer(party.group(3)):
            if _apply(block.group(0), address) == block.group(0):
                continue
            for key, _, _, tag, _ in ADDRESS_FIELDS + tuple((t, "", "", t, "") for t in EXTRA_STREET_TAGS):
                expected = address.get(key) or "" if tag not in EXTRA_STREET_TAGS else ""
                for found in re.findall(rf"<(?:[\w.-]+:)?{tag}(?:\s[^>]*)?>(.*?)</", block.group(0), re.S):
                    # Champ vide de l'adresse de test : balise présente (même vide) = à retirer
                    if tag and (found != expected or not expected) and (tag, found) not in out:
                        out.append((tag, found))
            if not out:
                out.append(("address", "champ de l'adresse de test manquant"))
    return out


def normalize_files(root: str, files: list, address: dict) -> list:
    """
    Fichiers de données `files` (chemins relatifs à `root`) dont une adresse de livraison inventée est remplacée par
    l'adresse de test ; renvoie [(fichier, [(balise, ancienne valeur)])] des fichiers modifiés.
    """
    changed = []
    for rel in files or []:
        path = os.path.join(root, *rel.split("/"))
        if not rel.lower().endswith(DATA_EXTENSIONS) or not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            text = f.read()  # fins de ligne conservées
        normalized = normalize_text(text, address)
        if normalized == text:
            continue
        found = invented_addresses(text, address)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(normalized)
        changed.append((rel, found))
    return changed


def normalized_line(rel: str, found: list) -> str:
    values = ", ".join(f"{tag} « {value} »" for tag, value in found[:4])
    return f"adresse de livraison inventée remplacée par l'adresse de test dans {rel}" + (f" ({values})" if values
                                                                                           else "")
