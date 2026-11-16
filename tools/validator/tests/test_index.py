"""The CI of the index on a local organization (LocalProvider): ingest -> review -> render -> stats, immutability,
check-index, the new addon form, the YAML of the workflows."""

import json
import os
import shutil
import subprocess
import unittest

import util
from gwcat import index_build, indexcheck, newaddon, package, providers, strictjson
from gwcat.indexcfg import IndexConfig
from gwcat.report import Report
from gwcat.validate import SnapshotStore

T0 = "2026-11-05T10:00:00Z"


def quiet(_msg):
    pass


class Org:
    """A local organization: repos.json, <name>/head, releases.json, assets/<tag>/"""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.repos = []
        self.next_id = 1

    def save(self):
        with open(os.path.join(self.root, "repos.json"), "w", encoding="utf-8") as f:
            json.dump(self.repos, f)

    def add_repo(self, name, tree, stars=0):
        self.repos.append({"name": name, "stars": stars})
        self.save()
        for rel, data in tree.items():
            p = os.path.join(self.root, name, "head", *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)

    def releases(self, name):
        p = os.path.join(self.root, name, "releases.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return []

    def set_releases(self, name, rels):
        with open(os.path.join(self.root, name, "releases.json"), "w", encoding="utf-8") as f:
            json.dump(rels, f, indent=1)

    def release(self, name, tag, folder, pre=False, date="2026-11-01T00:00:00Z", body="", downloads=0):
        """Packs an addon folder into the assets of a release"""
        files = package.folder_files(folder, Report("f"))
        ver = tag[1:]
        zips = package.make_zips(folder, files, os.path.join(self.root, name, "assets", tag), name, ver)
        rels = [r for r in self.releases(name) if r["tag"] != tag]
        assets = []
        for z in zips:
            assets.append({"id": self.next_id, "name": os.path.basename(z), "updated_at": date,
                           "download_count": downloads})
            self.next_id += 1
        rels.append({"id": self.next_id, "tag": tag, "body": body, "draft": False, "prerelease": pre,
                     "published_at": date, "assets": assets})
        self.next_id += 1
        self.set_releases(name, rels)

    def issues(self):
        p = os.path.join(self.root, "issues.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return []


def manifest(version, api="0.1.0", caps="", extra=""):
    return ("[addon]\nid = my_addon\nversion = %s\ntype = content\napi_min = %s\n%s\n[lua]\nentry = my_addon_main\n"
            "[capabilities]\n%s\n" % (version, api, extra, caps))


def snapshot_bytes(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = f.read()
    return out


class IndexFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = util.TempDir()
        self.index = os.path.join(self.tmp.path, "index")
        shutil.copytree(util.INDEX_SKELETON, self.index, ignore=shutil.ignore_patterns(".github", "ci", "tools", "files", "history", "state", "index.json", "stats.json", ".git"))
        self.org = Org(os.path.join(self.tmp.path, "org"))
        self.org.add_repo("my_addon", util.card_tree(), stars=5)
        self.provider = providers.LocalProvider(self.org.root, "Global-War-Addons")
        self.idx = index_build.Index(self.index)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self):
        return IndexConfig(self.index)

    def addon(self, version, **kw):
        folder = os.path.join(self.tmp.path, "src", version)
        shutil.rmtree(folder, ignore_errors=True)
        util.make_addon(self.tmp, "src/" + version, manifest=manifest(version, **{k: v for k, v in kw.items()
                                                                                 if k in ("api", "caps", "extra")}),
                        files=kw.get("files"))
        return folder

    def ingest(self, now=T0, snapshots=None, gw_merge=None):
        return index_build.ingest(self.idx, self.provider, self.cfg(), snapshots, quiet, now=now, gw_merge=gw_merge)

    def render(self, now=T0):
        return index_build.render(self.idx, self.cfg(), quiet, now=now)

    def index_json(self):
        return strictjson.load_file(os.path.join(self.index, "index.json"))

    def state(self):
        return strictjson.load_file(os.path.join(self.index, "state", "my_addon.json"))

    def assert_consistent(self):
        rep = indexcheck.check(self.index)
        self.assertTrue(rep.ok, rep.to_text())

    def test_flow(self):
        # 1. the first version waits for a moderator
        self.org.release("my_addon", "v1.0.0", self.addon("1.0.0"), body="first")
        s = self.ingest()
        self.assertEqual(s["pending"], ["my_addon@1.0.0"])
        issue = self.org.issues()[-1]
        self.assertEqual((issue["repo"], issue["labels"]), ("index", ["review"]))
        self.assertIn("<!-- gw-review my_addon 1.0.0 -->", issue["body"])
        self.assertTrue(os.path.isfile(os.path.join(self.index, "files", "my_addon", "1.0.0.json")))
        self.render()
        self.assertEqual(self.index_json()["addons"], [])  # pending: not in the index at all

        # 2. approve -> listed
        index_build.review(self.idx, "my_addon", "1.0.0", "approve", now=T0)
        self.assertTrue(self.render())
        ix = self.index_json()
        self.assertEqual([a["id"] for a in ix["addons"]], ["my_addon"])
        a = ix["addons"][0]
        self.assertEqual(a["name"], {"": "My addon", "ru": "Мой аддон"})
        self.assertEqual([e["v"] for e in a["latest"]], ["1.0.0"])
        self.assertEqual(a["latest"][0]["depends"], "")
        self.assertEqual(a["latest"][0]["assets"][0]["name"], "my_addon-1.0.0.zip")
        self.assert_consistent()

        # 3. a run without changes changes nothing (no commit, the ETag stays)
        before = snapshot_bytes(self.index)
        self.ingest(now="2026-11-05T11:00:00Z")
        self.assertFalse(self.render(now="2026-11-05T11:00:00Z"))
        self.assertEqual(before, snapshot_bytes(self.index))

        # 4. an update with the same capabilities: automatic
        self.org.release("my_addon", "v1.1.0", self.addon("1.1.0"), date="2026-11-02T00:00:00Z")
        s = self.ingest()
        self.assertEqual(s["new"], ["my_addon@1.1.0"])
        self.render()
        self.assertEqual([e["v"] for e in self.index_json()["addons"][0]["latest"]], ["1.1.0"])

        # 5. new capability -> review; the scanner sees what the script does
        script = 'function on_load(ctx) local f = io.open("x", "w") end\n'
        self.org.release("my_addon", "v1.2.0", self.addon("1.2.0", caps="fs_write",
                                                          files={"gamedata/scripts/my_addon_io.script": script}),
                         date="2026-11-03T00:00:00Z")
        s = self.ingest()
        self.assertEqual(s["pending"], ["my_addon@1.2.0"])
        self.assertIn("capabilities expanded: +fs_write", self.state()["versions"]["1.2.0"]["review"])
        self.assertEqual(self.state()["versions"]["1.2.0"]["caps_found"], ["fs_write"])

        # 6. undeclared capability -> invalid, an issue in the repo of the addon
        self.org.release("my_addon", "v1.3.0", self.addon("1.3.0", files={
            "gamedata/scripts/my_addon_io.script": script}), date="2026-11-04T00:00:00Z")
        s = self.ingest()
        self.assertEqual(s["invalid"], ["my_addon@1.3.0"])
        self.assertEqual(self.org.issues()[-1]["repo"], "my_addon")
        self.assertIn("caps_undeclared", self.org.issues()[-1]["body"])
        self.assertFalse(os.path.isfile(os.path.join(self.index, "files", "my_addon", "1.3.0.json")))

        # 7. the asset of a published version replaced -> yanked, latest falls back
        rels = self.org.releases("my_addon")
        r11 = next(r for r in rels if r["tag"] == "v1.1.0")
        r11["assets"][0]["updated_at"] = "2026-11-06T00:00:00Z"
        self.org.set_releases("my_addon", rels)
        s = self.ingest()
        self.assertEqual(s["yanked"], ["my_addon@1.1.0"])
        self.assertEqual(self.org.issues()[-1]["labels"], ["moderation", "asset-changed"])
        self.render()
        a = self.index_json()["addons"][0]
        self.assertEqual([e["v"] for e in a["latest"]], ["1.0.0"])
        hist = strictjson.load_file(os.path.join(self.index, *a["history"].split("/")))
        self.assertEqual([(e["v"], e["yanked"]) for e in hist], [("1.1.0", True), ("1.0.0", False)])

        # 8. another API major: one latest per major; the newest pre-release on top
        self.org.release("my_addon", "v2.0.0", self.addon("2.0.0", api="1.0.0"), date="2026-11-05T00:00:00Z")
        self.org.release("my_addon", "v2.1.0", self.addon("2.1.0", api="1.0.0"), pre=True,
                         date="2026-11-05T01:00:00Z", body="breaks-saves: the new squads")
        self.ingest()
        self.render()
        latest = self.index_json()["addons"][0]["latest"]
        self.assertEqual([(e["v"], e["pre"], e["breaks_saves"]) for e in latest],
                         [("2.1.0", True, True), ("2.0.0", False, False), ("1.0.0", False, False)])
        self.assert_consistent()

        # 9. moderation: blocked; old history files go after 14 days
        with open(os.path.join(self.index, "moderation.ltx"), "a", encoding="utf-8") as f:
            f.write("\n[blocked]\nmy_addon = a test\n")
        self.render()
        self.assertEqual(self.index_json()["addons"][0]["status"], "blocked")
        old = [h["file"] for h in self.state().get("old_history", [])]
        self.assertTrue(old and all(os.path.isfile(os.path.join(self.index, *p.split("/"))) for p in old))
        self.render(now="2026-11-25T00:00:00Z")
        self.assertFalse(any(os.path.isfile(os.path.join(self.index, *p.split("/"))) for p in old))
        self.assertNotIn("old_history", self.state())
        self.assert_consistent()

        # 10. stats
        self.assertTrue(index_build.stats(self.idx, self.provider, quiet, now=T0))
        st = strictjson.load_file(os.path.join(self.index, "stats.json"))
        self.assertEqual(st["stats"], {"my_addon": [0, 5]})
        self.assertFalse(index_build.stats(self.idx, self.provider, quiet, now="2026-11-06T00:00:00Z"))

    def test_bad_catalog_keeps_the_old_card(self):
        self.org.release("my_addon", "v1.0.0", self.addon("1.0.0"))
        self.ingest()
        index_build.review(self.idx, "my_addon", "1.0.0", "approve")
        card = self.state()["catalog"]
        os.remove(os.path.join(self.org.root, "my_addon", "head", "catalog", "description.md"))
        os.remove(os.path.join(self.org.root, "my_addon", "head", "catalog", "description.ru.md"))
        s = self.ingest()
        self.assertEqual(s["catalog_errors"], ["my_addon"])
        self.assertEqual(self.state()["catalog"], card)
        self.ingest()  # the same errors: no second issue
        self.assertEqual(len([i for i in self.org.issues() if i["title"].startswith("Catalog card")]), 1)

    def test_invalid_version_with_other_assets_is_checked_once(self):
        """An asset that is not a part of the package (notes, a screenshot) does not re-check an invalid version and
        open a new issue at every run"""
        script = 'function on_load(ctx) local f = io.open("x", "w") end\n'
        self.org.release("my_addon", "v1.0.0", self.addon("1.0.0", files={
            "gamedata/scripts/my_addon_io.script": script}))
        rels = self.org.releases("my_addon")
        with open(os.path.join(self.org.root, "my_addon", "assets", "v1.0.0", "notes.txt"), "w") as f:
            f.write("x")
        rels[0]["assets"].append({"id": 999, "name": "notes.txt", "updated_at": "2026-11-01T00:00:00Z"})
        self.org.set_releases("my_addon", rels)
        for _ in range(3):
            self.ingest()
        self.assertEqual(self.state()["versions"]["1.0.0"]["status"], "invalid")
        self.assertEqual(len(self.org.issues()), 1)

    def test_one_addon_does_not_stop_the_run(self):
        """A tag the schemas refuse is skipped; an exception in one repo is reported and the others go on"""
        self.org.release("my_addon", "v01.0.0", self.addon("01.0.0"))
        self.org.add_repo("other_addon", util.card_tree())
        broken = self.provider.read_tree

        def read_tree(name, *a, **kw):
            if name == "other_addon":
                raise providers.ProviderError("the tree is too large for the API")
            return broken(name, *a, **kw)
        self.provider.read_tree = read_tree
        s = self.ingest()
        self.assertEqual(s["failed"], ["other_addon"])
        self.assertEqual(self.state()["versions"], {})
        self.assertFalse(os.path.isfile(os.path.join(self.index, "state", "other_addon.json")))

    def test_delta_in_the_index(self):
        """test_merge_old published: files/test_merge_old/0.1.0.json refers to 0.1.0.merge.json"""
        gw_merge = util.find_gw_merge()
        if not gw_merge or not os.path.isdir(util.SAMPLES):
            self.skipTest("gw_merge or GlobalWar/addons/tests is not here")
        from gwcat import snapshot_tools
        snaps = os.path.join(self.tmp.path, "snaps")
        code, _, err = snapshot_tools.run(["build", os.path.join(util.SAMPLES, "test_merge_old", "s9", "old_base"),
                                           "--tag", "v0.0.1", "--out", os.path.join(snaps, "v0.0.1"), "--no-auto"])
        self.assertEqual(code, 0, err)
        src = os.path.join(util.SAMPLES, "test_merge_old")
        folder = os.path.join(self.tmp.path, "tmo")
        os.makedirs(folder)
        shutil.copyfile(os.path.join(src, "addon.ltx"), os.path.join(folder, "addon.ltx"))
        shutil.copytree(os.path.join(src, "gamedata"), os.path.join(folder, "gamedata"))
        self.org.add_repo("test_merge_old", util.card_tree(**{"addon.ltx": None}))
        self.org.release("test_merge_old", "v0.1.0", folder)
        s = self.ingest(snapshots=SnapshotStore(snaps, latest="v0.0.1"), gw_merge=gw_merge)
        self.assertIn("test_merge_old@0.1.0", s["pending"])
        index_build.review(self.idx, "test_merge_old", "0.1.0", "approve")
        self.render()
        vf = strictjson.load_file(os.path.join(self.index, "files", "test_merge_old", "0.1.0.json"))
        self.assertEqual(vf["merge"]["file"], "0.1.0.merge.json")
        e = next(a for a in self.index_json()["addons"] if a["id"] == "test_merge_old")["latest"][0]
        self.assertEqual((e["game"], e["merge_files"]), ("v0.0.1", 4))
        self.assert_consistent()

    def test_delta_waits_without_tools(self):
        """A version that replaces texts of the base, no snapshot of its game_version: retried, nothing written"""
        if not os.path.isdir(util.SAMPLES):
            self.skipTest("GlobalWar/addons/tests is not here")
        from gwcat import snapshot_tools
        snaps = os.path.join(self.tmp.path, "snaps")
        code, _, err = snapshot_tools.run(["build", os.path.join(util.SAMPLES, "test_merge_old", "s9", "old_base"),
                                           "--tag", "v5.0.0", "--out", os.path.join(snaps, "v5.0.0"), "--no-auto"])
        self.assertEqual(code, 0, err)
        src = os.path.join(util.SAMPLES, "test_merge_old")
        folder = os.path.join(self.tmp.path, "tmo")
        os.makedirs(folder)
        shutil.copyfile(os.path.join(src, "addon.ltx"), os.path.join(folder, "addon.ltx"))
        shutil.copytree(os.path.join(src, "gamedata"), os.path.join(folder, "gamedata"))
        self.org.add_repo("test_merge_old", util.card_tree(**{"addon.ltx": None}))
        self.org.release("test_merge_old", "v0.1.0", folder)
        s = self.ingest(snapshots=SnapshotStore(snaps, latest="v5.0.0"))
        if not util.find_gw_merge():
            # decision 64 item 4: no gw_merge in the CI - the replaced texts are not decided (no Python fallback), the
            # version waits for a run that has it (TRANSIENT), nothing written
            self.assertEqual(s["retry"], ["test_merge_old@0.1.0"])
            st = strictjson.load_file(os.path.join(self.index, "state", "test_merge_old.json"))
            self.assertEqual([e.split(":")[0] for e in st["retry"]["0.1.0"]["errors"]], ["merge_plan_unavailable"])
            self.assertIn("gw_merge-linux-x86_64.tar.gz", st["retry"]["0.1.0"]["errors"][0])
            self.assertFalse(os.path.exists(os.path.join(self.index, "files", "test_merge_old", "0.1.0.json")))
            return
        # game_version v0.0.1 is no release of game_releases.ltx: no reason to wait (review F16) - invalid
        self.assertEqual(s["invalid"], ["test_merge_old@0.1.0"])
        st = strictjson.load_file(os.path.join(self.index, "state", "test_merge_old.json"))
        self.assertTrue(st["versions"]["0.1.0"]["errors"][0].startswith("delta_unknown_game_version"))
        # the release is added: the invalid version is checked again (exceptions_key) and waits for its snapshot
        with open(os.path.join(self.index, "game_releases.ltx"), "a", encoding="utf-8") as f:
            f.write("v0.0.1 = 2026-10-01\n")
        s = self.ingest(snapshots=SnapshotStore(snaps, latest="v5.0.0"))
        self.assertEqual(s["retry"], ["test_merge_old@0.1.0"])
        st = strictjson.load_file(os.path.join(self.index, "state", "test_merge_old.json"))
        self.assertEqual(st["retry"]["0.1.0"]["count"], 1)
        self.assertEqual(st["versions"]["0.1.0"]["status"], "invalid")


class CheckIndexTest(unittest.TestCase):
    """check-index on hand-made files/: the delta references, names, strict JSON, git immutability"""

    def setUp(self):
        self.tmp = util.TempDir()
        self.index = os.path.join(self.tmp.path, "index")
        shutil.copytree(util.INDEX_SKELETON, self.index, ignore=shutil.ignore_patterns(".github", "ci", "tools", "files", "history", "state", "index.json", "stats.json", ".git"))
        self.files = os.path.join(self.index, "files", "a")
        os.makedirs(self.files)
        self.delta = {"format": 1, "algo": "gw-merge/2", "game": "v1.0.0", "guessed": False,
                      "files": {"scripts\\x.script": {"kind": "lua", "ancestor_sha256": "1" * 64,
                                                      "addon_sha256": "2" * 64,
                                                      "atoms": [{"atom": "x.f", "op": "modify"}]}}}
        self.write("1.0.0.merge.json", self.delta)
        data = strictjson.dumps(self.delta)
        self.write("1.0.0.json", {"schema": 1, "id": "a", "version": "1.0.0", "tag": "v1.0.0",
                                  "files": [{"path": "addon.ltx", "size": 1}],
                                  "merge": {"file": "1.0.0.merge.json", "algo": "gw-merge/2",
                                            "sha256": strictjson.sha256_bytes(data), "size": len(data)}})

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, obj, raw=None):
        with open(os.path.join(self.files, name), "wb") as f:
            f.write(raw if raw is not None else strictjson.dumps(obj))

    def codes(self):
        return indexcheck.check(self.index).codes("error")

    def test_good(self):
        self.write("1.0.0.merge.gw-merge-3.json", dict(self.delta, algo="gw-merge/3"))
        self.assertEqual(self.codes(), [])

    def test_bad(self):
        self.write("1.0.0.merge.json", dict(self.delta, game="v9"))
        self.assertIn("merge_ref_hash", self.codes())
        self.write("1.0.0.merge.json", self.delta)
        self.write("1.0.0.merge.gw-merge-9.json", dict(self.delta, algo="gw-merge/3"))
        self.assertIn("merge_recomputed_name", self.codes())
        os.remove(os.path.join(self.files, "1.0.0.merge.gw-merge-9.json"))
        self.write("1.0.0.merge.gw-merge-2.json", self.delta)
        self.assertIn("merge_recomputed_same", self.codes())
        os.remove(os.path.join(self.files, "1.0.0.merge.gw-merge-2.json"))
        self.write("notes.txt", None, b"x")
        self.assertIn("files_stray", self.codes())
        os.remove(os.path.join(self.files, "notes.txt"))
        self.write("2.0.0.merge.json", self.delta)
        self.assertIn("merge_orphan", self.codes())
        os.remove(os.path.join(self.files, "2.0.0.merge.json"))
        self.write("1.1.0.json", None, b'{"schema": 1, "schema": 1}')
        self.assertIn("json_invalid", self.codes())

    @unittest.skipUnless(shutil.which("git"), "git is not here")
    def test_git_immutability(self):
        def git(*args):
            subprocess.run(["git", "-C", self.index] + list(args), check=True, capture_output=True)
        git("init", "-q", "-b", "master")
        git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
        git("checkout", "-q", "-b", "pr")
        with open(os.path.join(self.index, "tags.ltx"), "a", encoding="utf-8") as f:
            f.write("vehicles = cars\n")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-am", "tags")
        self.assertTrue(indexcheck.check(self.index, "master").ok)
        os.remove(os.path.join(self.files, "1.0.0.merge.json"))
        self.write("1.0.0.json", {"schema": 1, "id": "a", "version": "1.0.0", "tag": "v1.0.0", "files": []})
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-am", "evil")
        codes = indexcheck.check(self.index, "master").codes("error")
        self.assertIn("immutable_changed", codes)
        self.assertIn("generated_by_ci", codes)


class NewAddonTest(unittest.TestCase):
    BODY = ("### Addon id / id аддона\n\nvector_weapons\n\n### Type / тип\n\ncontent\n\n### Name / название\n\n"
            "Vector weapons pack\n\n### Summary / кратко\n\n_No response_\n\n### Source or page / исходники или "
            "страница\n\nhttps://example.com/x\n\n### License / лицензия\n\n_No response_\n\n### Authors / авторы\n\n"
            "Иван Петров\n\n### Permission link / ссылка на разрешение\n\n_No response_\n\n"
            "### Maintainer GitHub login / логин мейнтейнера\n\n@ivan-petrov\n\n### Rules / правила\n\n- [X] ok\n")

    def test_parse_and_fill(self):
        if not os.path.isfile(os.path.join(util.TEMPLATE, "addon.ltx")):
            self.skipTest("the template repo is not next to the validator")
        cfg = IndexConfig(util.INDEX_SKELETON)
        fields = newaddon.parse_body(self.BODY)
        self.assertEqual(fields["addon_id"], "vector_weapons")
        self.assertEqual(fields["summary"], "")
        self.assertEqual(newaddon.check_request(fields, cfg, set()), [])
        self.assertTrue(newaddon.check_request(fields, cfg, {"vector_weapons"}))
        bad = dict(fields, addon_id="gw_x", type="weapons", maintainer="a b")
        self.assertEqual(len(newaddon.check_request(bad, cfg, set())), 3)
        tpl = {}
        for rel in ("addon.ltx", "catalog/catalog.ltx"):
            with open(os.path.join(util.TEMPLATE, *rel.split("/")), encoding="utf-8") as f:
                tpl[rel] = f.read()
        out = newaddon.initial_files(fields, tpl)
        tree = {k: v.encode("utf-8") for k, v in out.items()}
        tree.update({"catalog/description.md": b"x"})
        rep = Report("filled")
        from gwcat import catalog_meta, manifest as manifest_mod
        m = manifest_mod.check_manifest(tree["addon.ltx"], rep, expect_id="vector_weapons", reserved=cfg.reserved)
        card = catalog_meta.check_catalog(tree, rep, cfg.tags)
        self.assertTrue(rep.ok, rep.to_text())
        # decision 63: the template sets no api_min (a content addon needs none) and gets no warning for it
        self.assertEqual((m["type"], m["api"]), ("content", ""))
        self.assertNotIn("manifest_no_api_min", rep.codes("warning"))
        self.assertEqual(card["maintainers"], ["ivan-petrov"])
        self.assertEqual(card["links"]["source"], "https://example.com/x")
        self.assertEqual(card["license"], "CC-BY-NC-4.0")

    def test_form_labels_match(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not here")
        with open(os.path.join(util.INDEX_SKELETON, ".github", "ISSUE_TEMPLATE", "new-addon.yml"),
                  encoding="utf-8") as f:
            form = yaml.safe_load(f)
        labels = {b["id"]: b["attributes"]["label"] for b in form["body"] if "id" in b and b["id"] in newaddon.FIELDS}
        self.assertEqual(labels, newaddon.FIELDS)
        types = next(b for b in form["body"] if b.get("id") == "type")["attributes"]["options"]
        from gwcat.manifest import TYPES
        self.assertEqual(set(types), set(TYPES) - {"total_conversion"})


class YamlTest(unittest.TestCase):
    def test_workflows_parse(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not here")
        root = os.path.normpath(os.path.join(util.VALIDATOR, ".."))
        found = 0
        for sub in ("index", "template"):
            for dirpath, _, filenames in os.walk(os.path.join(root, sub, ".github")):
                for fn in filenames:
                    if fn.endswith((".yml", ".yaml")):
                        with open(os.path.join(dirpath, fn), encoding="utf-8") as f:
                            doc = yaml.safe_load(f)
                        found += 1
                        if os.path.basename(dirpath) == "workflows":
                            self.assertIn("jobs", doc, fn)
                            self.assertIn(True, doc, fn)  # "on:" is read by YAML 1.1 as True
        if found == 0:
            self.skipTest("no workflows here (an index repo checkout has only its own)")


if __name__ == "__main__":
    unittest.main()
