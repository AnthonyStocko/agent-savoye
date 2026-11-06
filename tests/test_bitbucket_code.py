import argparse
import subprocess

from tools import bitbucket_code as bc


class FakeStd:
    project, base, token = "COMPONENT", "https://git.example", ""

    def __init__(self):
        self.files_by = {"wms-content-backend": ["a/Action.java", "a/README.md", "b/Other.java"]}
        self.text = {"a/Action.java": "class Action {\n  void batch() {}\n}\n", "b/Other.java": "x\n"}

    def repos(self):
        return sorted(self.files_by)

    def ref(self, repo, candidates):
        assert candidates[:2] == ["origin/dev", "origin/development"] and candidates[-1] == "HEAD"
        return "abc1234567890"

    def ref_name(self, repo, commit):
        return "development"

    def files(self, repo, ref):
        return self.files_by[repo]

    def show(self, repo, ref, path):
        return self.text.get(path, "")

    def search(self, query):
        return [("wms-content-backend", "a/Action.java")]


def ns(**kw):
    return argparse.Namespace(**kw)


def test_reads_the_latest_commit_of_the_base_branch(monkeypatch):
    monkeypatch.delenv("STD_BASE_BRANCHES", raising=False)
    std = FakeStd()
    assert bc.cmd_repos(std, ns(like="content")) == ["wms-content-backend"]
    assert bc.cmd_search(std, ns(query="batch", limit=5)) == ["wms-content-backend/a/Action.java"]
    files = bc.cmd_files(std, ns(repo="wms-content-backend", prefix="a/", like="", max=300))
    assert "@ development (abc1234567)" in files[0] and files[1:] == ["a/Action.java", "a/README.md"]
    shown = bc.cmd_show(std, ns(repo="wms-content-backend", path="a/Action.java", start=2, end=2))
    assert shown[1] == "2\t  void batch() {}"
    grep = bc.cmd_grep(std, ns(repo="wms-content-backend", regex=r"batch\(", prefix="", ext=".java",
                                max_files=300, max=100))
    assert grep[1] == "a/Action.java:2: void batch() {}"


def test_unknown_repo_is_an_error():
    try:
        bc.cmd_files(FakeStd(), ns(repo="absent", prefix="", like="", max=10))
        raise AssertionError("erreur attendue")
    except bc.BitbucketUnavailable as e:
        assert "absent" in str(e)


def test_checkout_clones_only_the_modified_repo_without_credentials(monkeypatch, tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "development", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    (seed / "f.txt").write_text("v1\n")
    for args in (["add", "f.txt"], ["-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "v1"],
                 ["push", "-q", "origin", "HEAD:development"]):
        subprocess.run(["git", *args], cwd=seed, check=True)
    std = FakeStd()
    std.files_by["origin"] = []
    monkeypatch.setattr(bc, "ROOT", str(tmp_path / "ws"))
    real_git = bc._git
    monkeypatch.setattr(bc, "_git", lambda args, cwd=None, token="": real_git(
        [str(origin) if a.endswith("/origin.git") else a for a in args], cwd, token))
    out = bc.cmd_checkout(std, ns(repo="origin"))
    target = tmp_path / "ws" / ".tmp" / "std_COMPONENT_travail" / "origin"
    assert str(target) in out[0] and "development" in out[1] and (target / "f.txt").read_text() == "v1\n"
    config = subprocess.run(["git", "config", "--list"], cwd=target, capture_output=True, text=True).stdout
    assert "extraheader" not in config.lower()
    (seed / "f.txt").write_text("v2\n")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qam", "v2"], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:development"], cwd=seed, check=True)
    bc.cmd_checkout(std, ns(repo="origin"))
    assert (target / "f.txt").read_text() == "v2\n"


def test_prompt_block_renders():
    import importlib, io, sys
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        main = importlib.import_module("main")
    finally:
        sys.stdout, sys.stderr = saved
    text = main.render_prompt("std_bitbucket_code", cli="PY BB", work_root="/w")
    assert "PY BB grep DEPOT" in text and "PY BB checkout DEPOT" in text and "'/w'" in text
