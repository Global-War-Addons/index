"""Decision 63 (proposals of the review 2026-11-06, section 4): api_min only for code, `check` of the author tool and
its zipapp, the publish job of the index CI checks the package assets itself, review only of issues of the bot, the HTTP
cache of the polling build-index."""

import io
import json
import os
import shutil
import subprocess
import sys
import unittest
import urllib.error

import util
import gw_catalog
from gwcat import authorcheck, catalog_meta, cichanges, index_build, providers, snapshot_tools, strictjson
from gwcat.report import Report
from gwcat.validate import validate_version

from test_review_fixes import IndexCase

CONTENT_MANIFEST = "[addon]\nid = my_tex\nversion = 1.0.0\ntype = graphics\n"


def run_check(*argv):
    tmp = util.TempDir()
    try:
        out = os.path.join(tmp.path, "check.json")
        code = gw_catalog.main(["check"] + list(argv) + ["--format", "json", "--out", out, "--no-snapshot-check"],
                               prog="gw_addon_tool", only=("check",), auto_index=True)
        with open(out, encoding="utf-8") as f:
            return code, json.load(f)
    finally:
        tmp.cleanup()


def codes(summary, key=None):
    rows = summary["leftovers"] if key == "leftovers" else \
        [r for level in summary["findings"].values() for r in level] if key is None else summary["findings"][key]
    return [r["code"] for r in rows]


class ApiMinOnlyForCode(unittest.TestCase):
    def setUp(self):
        self.tmp = util.TempDir()

    def tearDown(self):
        self.tmp.cleanup()

    def validate(self, manifest, files):
        self.tmp.write("a/addon.ltx", manifest)
        for k, v in files.items():
            self.tmp.write("a/" + k, v)
        rep, _ = validate_version({"kind": "folder", "path": os.path.join(self.tmp.path, "a")}, snapshot_check=False)
        return rep

    def test_content_addon_needs_no_api_min(self):
        rep = self.validate(CONTENT_MANIFEST, {"gamedata/textures/my_tex/a.dds": b"DDS "})
        self.assertNotIn("manifest_no_api_min", rep.codes())
        rep = self.validate(CONTENT_MANIFEST + "api_min = 0.0.0\n", {"gamedata/configs/mod_system_my_tex.ltx": "[x]\n"})
        self.assertNotIn("manifest_no_api_min", rep.codes())

    def test_code_needs_api_min(self):
        # scripts without [lua] (a callback script of the base, a DLTX of scripts) run against the API too
        rep = self.validate(CONTENT_MANIFEST, {"gamedata/scripts/my_tex_util.script": "x = 1\n"})
        self.assertIn("manifest_no_api_min", rep.codes("warning"))
        rep = self.validate(CONTENT_MANIFEST + "[plugin]\nname = my_tex\n", {"gamedata/textures/a.dds": b"DDS "})
        self.assertIn("manifest_no_api_min", rep.codes("warning"))
        rep = self.validate(CONTENT_MANIFEST + "api_min = 0.1.0\n", {"gamedata/scripts/my_tex_util.script": "x = 1\n"})
        self.assertNotIn("manifest_no_api_min", rep.codes())

    def test_template_has_no_api_min(self):
        path = os.path.join(util.TEMPLATE, "addon.ltx")
        if not os.path.isfile(path):
            self.skipTest("the template repo is not next to the validator")
        with open(path, encoding="utf-8") as f:
            values = [line.split(";")[0] for line in f]
        self.assertFalse(any(v.strip().startswith("api_min") for v in values))


class CheckCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = util.TempDir()
        self.addon = util.make_addon(self.tmp)

    def tearDown(self):
        self.tmp.cleanup()

    def repo(self, rel="repo", **over):
        for k, v in util.card_tree(**over).items():
            self.tmp.write(rel + "/" + k, v)
        return os.path.join(self.tmp.path, rel)

    def test_ready(self):
        code, s = run_check(self.addon, "--repo", self.repo())
        self.assertEqual(code, 0, json.dumps(s, indent=1))
        self.assertTrue(s["ok"] and s["ready"])
        self.assertEqual(s["addon"]["id"], "my_addon")
        self.assertNotIn("repo_manifest_differs", codes(s))

    def test_template_leftovers_come_first(self):
        tree = util.card_tree()
        cat = tree["catalog/catalog.ltx"].replace(b"tags = weapons, traders", b"tags =") + b"permission = {{permission}}\n"
        repo = self.repo(**{"catalog/catalog.ltx": cat,
                            "catalog/description.md": b"Describe the addon here: what it adds or changes."})
        code, s = run_check(self.addon, "--repo", repo)
        left = codes(s, "leftovers")
        for c in ("catalog_template_description", "catalog_no_tags", "repo_placeholder"):
            self.assertIn(c, left)
        self.assertIn("catalog_bad_link", left)  # permission = {{permission}}: a leftover, not an ordinary error
        self.assertNotIn("catalog_template_description", codes(s))
        self.assertEqual(code, 1)
        self.assertFalse(s["ready"])
        self.assertTrue(all(r.get("hint") for r in s["leftovers"]))
        text = authorcheck.render_text(s)
        self.assertLess(text.index("TEMPLATE LEFTOVERS"), text.index("RESULT"))
        self.assertIn("fix: ", text)

    def test_leftovers_alone_fail_the_check(self):
        """Decision 64 (low items): template leftovers that are only warnings of the validator (the text of the template
        in description.md, empty tags) give exit code 1 - the card would show them; "ok" (no errors) stays true"""
        tree = util.card_tree()
        cat = tree["catalog/catalog.ltx"].replace(b"tags = weapons, traders", b"tags =")
        repo = self.repo(**{"catalog/catalog.ltx": cat,
                            "catalog/description.md": b"Describe the addon here: what it adds or changes."})
        code, s = run_check(self.addon, "--repo", repo)
        self.assertEqual(s["errors"], 0, json.dumps(s, indent=1))
        self.assertTrue(s["ok"])
        self.assertFalse(s["ready"])
        self.assertEqual(sorted(codes(s, "leftovers")), ["catalog_no_tags", "catalog_template_description"])
        self.assertEqual(code, 1)
        self.assertIn("RESULT: NOT READY - no errors", authorcheck.render_text(s))
        # filled in: ready, exit code 0
        code, s = run_check(self.addon, "--repo", self.repo("repo2"))
        self.assertEqual((code, s["ready"]), (0, True))

    def test_check_without_repo_has_no_card_leftovers(self):
        # the package alone (no repo, no catalog/): the leftovers of the card cannot appear, a clean package is ready
        code, s = run_check(self.addon)
        self.assertEqual((code, s["leftovers"]), (0, []))

    def test_repo_manifest_compared(self):
        other = util.GOOD_MANIFEST.replace("version  = 1.2.0", "version  = 1.1.0").replace("gw_npc, ", "")
        code, s = run_check(self.addon, "--repo", self.repo(**{"addon.ltx": other.encode("utf-8")}))
        self.assertEqual(code, 0)
        self.assertIn("repo_manifest_differs", codes(s, "warning"))
        self.assertIn("repo_manifest_keys_differ", codes(s, "note"))

    def test_folder_that_is_the_repo(self):
        for k, v in util.card_tree().items():
            if k.startswith("catalog/"):
                self.tmp.write("addon/" + k, v)
        code, s = run_check(self.addon)
        self.assertEqual(s["repo"], self.addon)
        self.assertEqual(code, 0, json.dumps(s, indent=1))
        # the manifest is checked once, not once as the package and once as the repo
        rows = [(r["code"], r.get("path")) for level in s["findings"].values() for r in level]
        self.assertEqual(len(rows), len(set(rows)))

    def test_no_repo_is_said(self):
        code, s = run_check(self.addon)
        self.assertIsNone(s["repo"])
        self.assertTrue(any("the repo of the addon" in n for n in s["not_checked"]))

    def test_placeholder_in_the_package(self):
        self.tmp.write("addon/addon.ltx", util.GOOD_MANIFEST.replace("type     = content", "type = {{type}}"))
        code, s = run_check(self.addon)
        self.assertEqual(code, 1)
        self.assertIn("package_placeholder", codes(s, "leftovers"))

    def test_catalog_placeholder_keys(self):
        self.assertEqual(catalog_meta.placeholder_keys(b"[catalog]\nname = x\nsource = {{source}} ; {{c}}\n"),
                         ["source"])


class Zipapp(unittest.TestCase):
    def test_build_and_run(self):
        import build_pyz
        if not gw_catalog.find_index() or not snapshot_tools.find():
            self.skipTest("no index repo / tools/addons_snapshot next to the validator")
        tmp = util.TempDir()
        try:
            out, build = build_pyz.build(os.path.join(tmp.path, "t.pyz"), gw_catalog.find_index(), snapshot_tools.find())
            again, _ = build_pyz.build(os.path.join(tmp.path, "u.pyz"), gw_catalog.find_index(), snapshot_tools.find())
            with open(out, "rb") as a, open(again, "rb") as b:
                self.assertEqual(a.read(), b.read())  # reproducible
            addon = util.make_addon(tmp)
            env = dict(os.environ, GW_ADDON_TOOL_CACHE=os.path.join(tmp.path, "cache"), PYTHONIOENCODING="utf-8")
            env.pop("GW_SNAPSHOT_TOOLS", None)
            r = subprocess.run([sys.executable, out, "check", addon, "--no-snapshot-check"], capture_output=True,
                               env=env, cwd=tmp.path, timeout=300)
            text = r.stdout.decode("utf-8", "replace")
            self.assertEqual(r.returncode, 0, text + r.stderr.decode("utf-8", "replace"))
            self.assertIn("RESULT: READY", text)
            self.assertTrue(os.path.isfile(os.path.join(tmp.path, "cache", "gw_addon_tool", build, "index",
                                                        "source.ltx")))
        finally:
            tmp.cleanup()


class PublishChecksAssets(unittest.TestCase):
    """The job that pushes downloads every new package asset of the hand-over and hashes it itself"""

    def setUp(self):
        self.tmp = util.TempDir()
        self.root = os.path.join(self.tmp.path, "root")
        shutil.copytree(util.INDEX_SKELETON, self.root, ignore=shutil.ignore_patterns(
            ".github", "ci", "tools", "files", "history", "state", "index.json", "stats.json", ".git"))
        self.src = os.path.join(self.tmp.path, "src")
        self.urls = {}

    def tearDown(self):
        self.tmp.cleanup()

    def state(self, status="listed", sha="a" * 64, size=10, name="my_addon-1.0.0.zip", tag="v1.0.0"):
        return {"id": "my_addon", "versions": {"1.0.0": {"status": status, "tag": tag,
                                                          "assets": [{"name": name, "sha256": sha, "size": size}]}}}

    def put(self, base, path, obj):
        p = os.path.join(base, *path.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        strictjson.write_file(p, obj)

    def fetch(self, url, size):
        self.urls[url] = size
        return 10, "a" * 64

    def verify(self, obj):
        self.put(self.src, "files/state/my_addon.json", obj)
        return cichanges.verify_assets(self.src, self.root, ["state/my_addon.json"], self.fetch)

    def test_new_asset_is_downloaded_from_the_address_of_the_launcher(self):
        self.assertEqual(self.verify(self.state()), 1)
        self.assertEqual(list(self.urls), [
            "https://github.com/Global-War-Addons/my_addon/releases/download/v1.0.0/my_addon-1.0.0.zip"])

    def test_mismatch_refused(self):
        for bad in (self.state(sha="b" * 64), self.state(size=11), self.state(name="evil.zip"),
                    self.state(tag="v9.9.9"), self.state(status="pending", sha="c" * 64)):
            self.assertRaises(cichanges.ChangesError, self.verify, bad)

    def test_known_and_unlisted_are_not_downloaded(self):
        self.assertEqual(self.verify(self.state(status="invalid", sha="b" * 64)), 0)  # not listed: not checked
        self.put(self.root, "state/my_addon.json", self.state(sha="b" * 64))
        self.assertEqual(self.verify(self.state(sha="b" * 64)), 0)  # published already
        # the rows of an invalid version were never checked: when it is listed later, it is checked
        self.put(self.root, "state/my_addon.json", self.state(status="invalid", sha="b" * 64))
        self.assertRaises(cichanges.ChangesError, self.verify, self.state(sha="b" * 64))

    def test_retired_rows_must_be_known(self):
        # an unchecked row brought in as yanked / gone / rejected would be "known" to the next hand-over that lists it
        for status in ("yanked", "gone", "rejected"):
            self.assertRaises(cichanges.ChangesError, self.verify, self.state(status=status, sha="e" * 64))
        # the honest way: listed (checked then) -> yanked keeps its rows - nothing to download
        self.put(self.root, "state/my_addon.json", self.state(sha="e" * 64))
        for status in ("yanked", "gone", "rejected"):
            self.assertEqual(self.verify(self.state(status=status, sha="e" * 64)), 0)

    def test_index_and_history_rows(self):
        self.put(self.src, "files/history/my_addon.0123abcd.json",
                 [{"v": "1.0.0", "tag": "v1.0.0", "assets": [{"name": "my_addon-1.0.0.zip", "sha256": "d" * 64,
                                                               "size": 10}]}])
        self.assertRaises(cichanges.ChangesError, cichanges.verify_assets, self.src, self.root,
                          ["history/my_addon.0123abcd.json"], self.fetch)


class ReviewIssue(IndexCase):
    def test_review_only_of_its_issue(self):
        self.org.release("my_addon", "v1.0.0", self.addon("1.0.0"))
        s = index_build.ingest(self.idx, self.provider, IndexCase.cfg(self), None, lambda *a: None)
        self.assertEqual(s["pending"], ["my_addon@1.0.0"])
        number = self.state()["versions"]["1.0.0"]["issue"]
        self.assertRaises(RuntimeError, index_build.review, self.idx, "my_addon", "1.0.0", "approve",
                          issue=number + 100)
        self.assertEqual(self.state()["versions"]["1.0.0"]["status"], "pending")
        index_build.review(self.idx, "my_addon", "1.0.0", "approve", issue=number)
        self.assertEqual(self.state()["versions"]["1.0.0"]["status"], "listed")


class _Resp:
    def __init__(self, body, headers):
        self.body = json.dumps(body).encode("utf-8")
        self.headers = headers

    def read(self, limit=None):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class HttpCache(unittest.TestCase):
    """build-index every 15 minutes: an unchanged answer is read with If-None-Match (304 - no rate limit)"""

    def setUp(self):
        self.tmp = util.TempDir()
        self.cache = os.path.join(self.tmp.path, "http", "github.json")
        self.sent = []
        self.etag = '"e1"'
        self.orig = providers.urllib.request.urlopen
        providers.urllib.request.urlopen = self.urlopen

    def tearDown(self):
        providers.urllib.request.urlopen = self.orig
        self.tmp.cleanup()

    def urlopen(self, req, timeout=None):
        sent = req.get_header("If-none-match")
        self.sent.append(sent)
        if sent is not None and sent == self.etag:
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {"ETag": self.etag}, io.BytesIO(b""))
        return _Resp([{"id": 1, "tag_name": "v1.0.0", "body": "", "draft": False, "prerelease": False,
                       "published_at": "2026-11-01T00:00:00Z", "assets": []}], {"ETag": self.etag})

    def provider(self):
        return providers.GitHubProvider("Org", "t", http_cache=self.cache)

    def test_not_modified_is_taken_from_the_cache(self):
        p = self.provider()
        first = p.list_releases("a")
        p.save_http_cache()
        p = self.provider()
        self.assertEqual(p.list_releases("a"), first)
        self.assertEqual(self.sent, [None, '"e1"'])
        self.assertEqual(p.http_stats, {"requests": 1, "not_modified": 1})
        self.etag = '"e2"'  # changed: a full answer
        self.assertEqual(p.list_releases("a"), first)
        self.assertEqual(p.http_stats["not_modified"], 1)

    def test_a_wildcard_etag_is_never_sent(self):
        os.makedirs(os.path.dirname(self.cache))
        with open(self.cache, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "entries": {
                "https://api.github.com/repos/Org/a/releases?per_page=100": {"etag": "*", "data": [], "link": ""}}}, f)
        self.assertEqual(len(self.provider().list_releases("a")), 1)
        self.assertEqual(self.sent, [None])


if __name__ == "__main__":
    unittest.main()
