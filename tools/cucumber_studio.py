"""
Personnalisations Studio (vue Couverture, prompt des lots), 0 token.

Chaque différence entre le customized.xml d'un processus et sa référence (original.xml du dépôt client ou fichier du
Standard) est un COMPORTEMENT à couvrir : écran (interaction), champ, bouton, condition (transition), action
(action, sous-processus, script, déclenchement d'un listener) ou message (information, avertissement, erreur).
Sans référence (processus propre au client), tous ses comportements sont « ajoutés ».
"""
import re
import xml.etree.ElementTree as ET

BEHAVIOUR_TYPES = ("écran", "champ", "bouton", "condition", "action", "message")
CHANGES = ("ajouté", "modifié", "supprimé")
MAX_BEHAVIOURS = 40
_ACTIONS = {"action-ref", "process-ref", "action-script", "execute", "execution-event-ref"}
_MESSAGES = {"info": "information", "warning": "avertissement", "error": "erreur"}
_TRANSLATE = re.compile(r"""\$\{?\$translate\.message\(\s*["'](.*?)["']\s*\)\}?""")


def _local(tag) -> str:
    """Nom d'élément sans espace de noms ('' pour un commentaire)."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(element) -> str:
    """Texte normalisé d'un élément : espaces réduits, libellé des $translate.message("…") seul."""
    text = " ".join("".join(element.itertext()).split())
    return _TRANSLATE.sub(r"\1", text)


def _attributes(element) -> str:
    return " ".join(f"{_local(k)}={v}" for k, v in sorted(element.attrib.items()))


def _transitions(node, owner: str) -> list:
    """Conditions de sortie d'un nœud (transition et transition par défaut)."""
    found = []
    for transitions in node:
        if _local(transitions.tag) not in ("transitions", "initial-transitions"):
            continue
        if transitions.get("default"):
            found.append(("condition", owner, f"sinon → {transitions.get('default')}", "par défaut"))
        for transition in transitions:
            if _local(transition.tag) == "transition":
                condition = _text(transition) or "toujours"
                found.append(("condition", owner, f"{condition} → {transition.get('to', '?')}", condition))
    return found


def _node_behaviours(node) -> list:
    """Comportements d'un nœud du processus : [(type, élément, nom, signature)]."""
    tag, node_id = _local(node.tag), node.get("id", "")
    out = []
    if tag == "interaction":
        out.append(("écran", node_id, node.get("title") or node_id, _attributes(node)))
        for element in node.iter():
            name = _local(element.tag)
            if name == "field":
                label = element.get("label") or element.get("binding") or "?"
                out.append(("champ", node_id, label, _attributes(element)))
            elif name == "submit":
                out.append(("bouton", node_id, element.get("name") or "?", _attributes(element)))
            elif name in _MESSAGES:
                text = _text(element)
                out.append(("message", node_id, f"{_MESSAGES[name]} : {text[:120]}", text))
    elif tag in _ACTIONS:
        key = node.get("key") or node.get("name") or _text(node)[:80]
        out.append(("action", node_id, key, _attributes(node) + " " + _text(node)))
    elif tag in ("gateway", "foreach", "if"):
        out.append(("condition", node_id, f"{tag} {node_id}".strip(), _attributes(node) + " " + _text(node)))
    out += [(kind, owner, name, signature) for kind, owner, name, signature in _transitions(node, node_id)]
    return out


def process_behaviours(xml_text: str) -> dict:
    """
    Comportements d'un fichier Studio : {(type, élément, nom): signature}. Le déclenchement d'un listener (racine
    execution-event-*) est une action. {} si le XML est vide ou illisible.
    """
    try:
        root = ET.fromstring(xml_text.encode("utf-8")) if xml_text.strip() else None
    except ET.ParseError:
        return {}
    if root is None:
        return {}
    found = []
    if _local(root.tag).startswith("execution-event"):
        event = (root.get("on-event") or "").rsplit("/", 1)[-1]
        found.append(("action", "", f"déclenché sur {event}" if event else _local(root.tag), _attributes(root)))
    found += _transitions(root, "début")
    for node in root:
        found += _node_behaviours(node)
    behaviours = {}
    for kind, owner, name, signature in found:
        key, n = (kind, owner, name), 2
        while key in behaviours:  # même nom dans un même élément : numéroté
            key, n = (kind, owner, f"{name} ({n})"), n + 1
        behaviours[key] = signature
    return behaviours


def behaviours(original: str, customized: str, limit=MAX_BEHAVIOURS) -> list:
    """
    Différences entre `customized` et `original` (vide : processus nouveau) : [{type, element, nom, changement}],
    écrans d'abord, au plus `limit` (None : toutes).
    """
    before, after = process_behaviours(original), process_behaviours(customized)
    out = [{"type": k[0], "element": k[1], "nom": k[2], "changement": "ajouté" if k not in before else "modifié"}
           for k, signature in after.items() if k not in before or before[k] != signature]
    out += [{"type": k[0], "element": k[1], "nom": k[2], "changement": "supprimé"} for k in before if k not in after]
    out.sort(key=lambda b: (BEHAVIOUR_TYPES.index(b["type"]), CHANGES.index(b["changement"])))
    return out[:limit]


def behaviour_line(behaviour: dict) -> str:
    """« ajouté — bouton « OK » (écran scan) »."""
    where = f" (élément {behaviour['element']})" if behaviour.get("element") else ""
    return f"{behaviour['changement']} — {behaviour['type']} « {behaviour['nom']} »{where}"


def spec_behaviours(spec: dict) -> list:
    """Comportements Studio d'un spécifique (tous ses customized.xml, liste bornée), calculés à l'inventaire."""
    return [b for f in spec.get("fichiers") or [] for b in f.get("comportements_studio") or []]


def behaviour_counts(spec: dict) -> dict:
    """Nombre total de comportements Studio d'un spécifique par type ({} sans personnalisation Studio)."""
    counts = {}
    for f in spec.get("fichiers") or []:
        for kind, n in (f.get("nb_comportements_studio") or {}).items():
            counts[kind] = counts.get(kind, 0) + n
    return counts


def counts_line(counts: dict) -> str:
    """« 12 écran(s), 30 bouton(s) » dans l'ordre des types ; '' sans comportement."""
    return ", ".join(f"{counts[k]} {k}(s)" for k in BEHAVIOUR_TYPES if counts.get(k))
