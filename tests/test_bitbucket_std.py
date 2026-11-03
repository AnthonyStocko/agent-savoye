"""
Standard lu dans Bitbucket (API REST simulée) : références de version résolues en commit, fichiers et contenus en
cache disque par commit (aucun nouvel appel), nom trouvé par la recherche (branche par défaut) confirmé à la version
ou signalé non confirmé ; API en erreur : BitbucketUnavailable, jamais une absence.
"""
import json

import pytest

from tools import bitbucket_std as bs
from tools import cucumber_jobs as cj

TREE = {  # dépôt -> branche -> (commit, {chemin: contenu})
    "wms-preparation-workflow": {
        "v26.24": ("c2624", {"wms-preparation-event/src/main/java/Job.java": 'String N = "Schedule precubing";',
                             "wms-preparation-event/pom.xml": "<project/>",
                             "wms-preparation-listener/pom.xml":
                                 "<project><artifactId>wms-preparation-event</artifactId></project>"}),
        "development": ("cdev", {"wms-preparation-event/src/main/java/Job.java": 'String N = "Schedule precubing";'}),
    },
    "odatio-contrib": {
        "v26.24": ("o2624", {"wms-contrib-odatio/src/main/resources/bundle.list":
                                 "runtime:a-sis/wms-preparation-listener-start@26.24\n"}),
    },
}


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self.payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        return self.payload


def fake_http(calls):
    def http(method, url, params=None, json=None):
        calls.append((method, url, dict(params or {})))
        path = url.split("https://bb", 1)[1]
        if method == "POST":
            query = json["query"]
            hits = [{"repository": {"slug": r}, "file": p} for r, b in TREE.items() for p, c in b.get(
                "development", ("", {}))[1].items() if query.split('"')[1] in c] if '"' in query else \
                [{"repository": {"slug": "odatio-contrib"}, "file": "wms-contrib-odatio/pom.xml"}]
            return FakeResponse(200, {"code": {"values": hits}})
        if path == "/rest/api/1.0/projects/COMPONENT/repos":
            return FakeResponse(200, {"values": [{"slug": r} for r in TREE], "isLastPage": True})
        repo = path.split("/repos/", 1)[1].split("/", 1)[0]
        rest = path.split(f"/repos/{repo}/", 1)[1]
        branches = TREE.get(repo, {})
        if rest == "branches":
            name = params["filterText"]
            values = [{"displayId": name, "latestCommit": branches[name][0]}] if name in branches else []
            return FakeResponse(200, {"values": values, "isLastPage": True})
        if rest == "tags":
            return FakeResponse(200, {"values": [], "isLastPage": True})
        if rest == "branches/default":
            return FakeResponse(200, {"latestCommit": branches["development"][0]})
        commit = params.get("at")
        content = next((b[1] for b in branches.values() if b[0] == commit), {})
        if rest == "files":
            return FakeResponse(200, {"values": sorted(content), "isLastPage": True})
        if rest.startswith("raw/"):
            file = rest[len("raw/"):]
            return FakeResponse(200, content[file]) if file in content else FakeResponse(404, "")
        return FakeResponse(404, "")
    return http


def test_remote_standard_resolves_version_reads_and_caches(tmp_path):
    calls = []
    std = bs.RemoteStandard("COMPONENT", base="https://bb", http=fake_http(calls), cache_dir=str(tmp_path))
    candidates = cj._std_candidates("26.24")
    hits, unconfirmed = std.find_quoted("Schedule precubing", candidates)
    assert hits == [("wms-preparation-workflow", "c2624", "wms-preparation-event/src/main/java/Job.java")]
    assert unconfirmed == [] and bs.ref_label(std, "wms-preparation-workflow", "c2624") == "v26.24"
    # Modules déployés lus dans la contrib à la version, module bibliothèque embarqué : nom prouvé
    deployed = {"wms-preparation-listener-start"}
    found, elsewhere = cj.names_in_standard(std, "26.24", ["Schedule precubing"], deployed)
    assert "wms-preparation-workflow@v26.24" in found["Schedule precubing"] and not elsewhere
    # Cache par commit : relire fichiers et contenus ne rappelle pas l'API
    before = len([c for c in calls if "/files" in c[1] or "/raw/" in c[1]])
    fresh = bs.RemoteStandard("COMPONENT", base="https://bb", http=fake_http(calls), cache_dir=str(tmp_path))
    fresh.ref("wms-preparation-workflow", candidates)
    fresh.files("wms-preparation-workflow", "c2624")
    fresh.show("wms-preparation-workflow", "c2624", "wms-preparation-event/src/main/java/Job.java")
    assert len([c for c in calls if "/files" in c[1] or "/raw/" in c[1]]) == before


def test_name_only_on_default_branch_is_unconfirmed_not_absent(tmp_path):
    tree = TREE["wms-preparation-workflow"]["v26.24"][1]
    saved = dict(tree)
    tree["wms-preparation-event/src/main/java/Job.java"] = "String N = \"autre\";"
    try:
        std = bs.RemoteStandard("COMPONENT", base="https://bb", http=fake_http([]), cache_dir=str(tmp_path))
        errors = []
        found, _ = cj.names_in_standard(std, "26.24", ["Schedule precubing"], None, errors)
        assert not found and "absent ou différent à v26.24" in errors[0]["erreur"]
    finally:
        tree.clear()
        tree.update(saved)


def test_api_error_is_unavailable_never_absent(tmp_path):
    std = bs.RemoteStandard("COMPONENT", base="https://bb", cache_dir=str(tmp_path),
                            http=lambda *a, **k: FakeResponse(503, ""))
    with pytest.raises(bs.BitbucketUnavailable):
        std.find_quoted("x", ("origin/v26.24",))
    assert bs.project_key("https://git.example/projects/component") == "COMPONENT"
    assert bs.project_key("https://git.example/scm/x/y.git") == ""
