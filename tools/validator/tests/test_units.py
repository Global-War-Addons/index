"""Unit tests: the strict ltx (port of addon_host.cpp), strict JSON, the manifest, catalog/, the Lua scanner, schemas."""

import os
import shutil
import tempfile
import unittest

import util  # noqa: F401  (sys.path)
from gwcat import catalog_meta, indexcfg, ltx, luacaps, manifest, schema, strictjson
from gwcat.report import Report


class LtxTest(unittest.TestCase):
    def test_engine_rules(self):
        ini = ltx.parse('﻿[Addon] ; c\nid = "x_1" // c\nkey\n[addon]\nversion=1.0.0\n')
        self.assertEqual(ini.get("addon", "id"), "x_1")
        self.assertEqual(ini.get("ADDON", "VERSION"), "1.0.0")  # a repeated section continues the first
        self.assertEqual(ini.items("addon")[1], ("key", ""))
        for bad in ("[a\n", "[a]:b\n", "#include \"x\"\n", "k = v\n", "[a]\n= v\n", "[]\n"):
            with self.assertRaises(ltx.LtxError, msg=bad):
                ltx.parse(bad)

    def test_url_safe_dialect(self):
        self.assertEqual(ltx.parse("[a]\nu = https://x/y\n").get("a", "u"), "https:")  # the engine
        ini = ltx.parse("[a]\nu = https://x//y ; c\nv = 1 // c\n", url_safe=True)
        self.assertEqual(ini.get("a", "u"), "https://x//y")
        self.assertEqual(ini.get("a", "v"), "1")

    def test_versions_and_ids(self):
        self.assertEqual(ltx.parse_version("1.2.3"), (1, 2, 3))
        for bad in ("1.2", "1.2.3-beta", "v1.2.3", "1.2.3.4", "1234567890.0.0", ""):
            self.assertIsNone(ltx.parse_version(bad), bad)
        self.assertIsNone(ltx.parse_api_version("1024.0.0"))
        self.assertEqual(ltx.api_major((0, 3, 1)), "0.3")
        self.assertEqual(ltx.api_major((2, 3, 1)), "2")
        self.assertTrue(ltx.is_valid_id("a_1"))
        for bad in ("A", "a-b", "a b", "ид", "x" * 65, "", "a\n"):
            self.assertFalse(ltx.is_valid_id(bad), bad)
        self.assertEqual(ltx.parse_game_tag("v1.4"), ltx.parse_game_tag("1.4.0"))

    def test_game_tags(self):
        """The release tags of the game (tools/addons_snapshot/gametag.py, as the engine MergeParseTag)"""
        order = ["0.5.5-Beta", "0.5.5", "0.5.5-patch1", "0.5.5-patch4", "0.5.6-Beta", "v0.5.6"]
        parsed = [ltx.parse_game_tag(t) for t in order]
        self.assertNotIn(None, parsed)
        self.assertEqual(parsed, sorted(parsed))
        self.assertEqual(len(set(parsed)), len(parsed))
        self.assertEqual(sorted(reversed(order), key=ltx.game_tag_sort_key), order)
        self.assertEqual(ltx.parse_game_tag(" V0.5.5-PATCH4 "), ltx.parse_game_tag("0.5.5-patch4"))
        self.assertTrue(ltx.same_game_tag("v1.4", "1.4.0"))
        self.assertTrue(ltx.same_game_tag("1.0-Beta", "v1.0.0-beta"))
        self.assertFalse(ltx.same_game_tag("1.0-beta", "1.0"))
        for bad in ("", "v", "1.", ".1", "1..2", "1.2-", "1.2-be ta", "1.2-a-b", "1.2;x", "x1.2", "1.2 beta"):
            self.assertIsNone(ltx.parse_game_tag(bad), bad)

    def test_game_releases_ltx(self):
        folder = tempfile.mkdtemp(prefix="gwcat_rel_")
        try:
            shutil.copytree(util.INDEX_SKELETON, os.path.join(folder, "i"),
                            ignore=shutil.ignore_patterns(".github", "ci", "tools", "files", "history", "state",
                                                          ".git"))
            path = os.path.join(folder, "i", "game_releases.ltx")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("[releases]\n0.5.5-patch1 = 2026-10-01\n0.5.5 = 2026-10-01\n0.5.6-Beta = 2026-10-10\n")
            cfg = indexcfg.IndexConfig(os.path.join(folder, "i"))
            self.assertEqual([t for t, _ in cfg.game_releases], ["0.5.5", "0.5.5-patch1", "0.5.6-Beta"])
            self.assertEqual(cfg.game_release_for("2026-10-05"), "0.5.5-patch1")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("[releases]\nrelease-1 = 2026-10-01\n")
            with self.assertRaises(indexcfg.ConfigError):
                indexcfg.IndexConfig(os.path.join(folder, "i"))
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_snapshot_of_same_tag(self):
        """game_version 1.4 or V1.4.0 finds the snapshot folder v1.4.0: the engine takes them as one release"""
        from gwcat.validate import SnapshotStore
        folder = tempfile.mkdtemp(prefix="gwcat_snaps_")
        try:
            for tag in ("v1.4.0", "v1.4.0-patch1"):
                os.makedirs(os.path.join(folder, tag))
                for name in SnapshotStore.NAMES:
                    open(os.path.join(folder, tag, name), "wb").close()
            store = SnapshotStore(folder)
            expected = os.path.join(folder, "v1.4.0", SnapshotStore.NAMES[0])
            for tag in ("v1.4.0", "1.4", "V1.4.0.0"):
                self.assertEqual(store.get(tag), expected, tag)
            self.assertEqual(store.get("1.4-PATCH1"), os.path.join(folder, "v1.4.0-patch1", SnapshotStore.NAMES[0]))
            for tag in ("1.4-patch2", "1.4-beta", "../v1.4.0"):
                self.assertIsNone(store.get(tag), tag)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_snapshot_download_of_same_tag(self):
        """game_version 1.4 downloads the snapshot published under the release tag v1.4.0 (game_releases.ltx); a
        failed download leaves no folder behind (gw_catalog.py would take it for the latest snapshot)"""
        import pathlib
        from gwcat.validate import SnapshotStore
        published = tempfile.mkdtemp(prefix="gwcat_published_")
        folder = tempfile.mkdtemp(prefix="gwcat_snaps_")
        try:
            os.makedirs(os.path.join(published, "v1.4.0"))
            for name in SnapshotStore.NAMES:
                with open(os.path.join(published, "v1.4.0", name), "wb") as f:
                    f.write(b"x")
            url = pathlib.Path(published).as_uri() + "/{tag}/{name}"
            store = SnapshotStore(folder, url, releases=["v1.4.0"])
            self.assertEqual(store.get("1.4"), os.path.join(folder, "v1.4.0", SnapshotStore.NAMES[0]))
            self.assertFalse(os.path.exists(os.path.join(folder, "1.4")))
            self.assertIsNone(store.get("9.9"))
            self.assertEqual(sorted(os.listdir(folder)), ["v1.4.0"])
        finally:
            shutil.rmtree(folder, ignore_errors=True)
            shutil.rmtree(published, ignore_errors=True)

    def test_depends(self):
        deps = ltx.parse_depends("gw_npc, my_lib >=1.2.0, tex >= 2 <3,")
        self.assertEqual([d["id"] for d in deps], ["gw_npc", "my_lib", "tex"])
        self.assertEqual(deps[2]["conditions"], [("ge", (2, 0, 0)), ("lt", (3, 0, 0))])
        self.assertTrue(ltx.condition_holds("ge", (1, 2, 0), (1, 10, 0)))
        for bad in ("My_lib", "a ~1.0", "a >=x", "a >="):
            with self.assertRaises(ltx.LtxError, msg=bad):
                ltx.parse_depends(bad)


class StrictJsonTest(unittest.TestCase):
    def test_rejects(self):
        for bad in (b'{"a":1,"a":2}', b'\xef\xbb\xbf{}', b'{"a":NaN}', b'{"a":Infinity}', b'\xff', b'{'):
            with self.assertRaises(strictjson.StrictJsonError, msg=bad):
                strictjson.loads_bytes(bad)
        self.assertEqual(strictjson.loads_bytes('{"a":{"b":"ж"}}'.encode("utf-8")), {"a": {"b": "ж"}})

    def test_canonical(self):
        self.assertEqual(strictjson.dumps({"b": 1, "a": "ж"}, compact=True), '{"a":"ж","b":1}\n'.encode("utf-8"))


def check(text, **kw):
    r = Report("t")
    m = manifest.check_manifest(text.encode("utf-8"), r, **kw)
    return r, m


class ManifestTest(unittest.TestCase):
    def test_good(self):
        r, m = check(util.GOOD_MANIFEST, expect_id="my_addon", expect_version="1.2.0")
        self.assertTrue(r.ok, r.to_text())
        self.assertEqual(m["depends"], "gw_npc, my_lib >=1.2 <2")
        self.assertEqual(m["api"], "0.1.0")
        self.assertEqual(m["lua_entry"], "my_addon_main")

    def test_errors(self):
        cases = {
            "manifest_syntax": "[addon]:x\n",
            "manifest_no_id": "[addon]\ntype = content\n",
            "manifest_bad_id": "[addon]\nid = My-Addon\ntype = content\nversion = 1.0.0\n",
            "manifest_no_type": "[addon]\nid = a\nversion = 1.0.0\n",
            "manifest_unknown_type": "[addon]\nid = a\nversion = 1.0.0\ntype = weapons\n",
            "manifest_bad_version": "[addon]\nid = a\nversion = 1.0\ntype = content\n",
            "manifest_bad_depends": "[addon]\nid = a\nversion = 1.0.0\ntype = content\ndepends = B\n",
            "manifest_self_depends": "[addon]\nid = a\nversion = 1.0.0\ntype = content\ndepends = a\n",
            "manifest_bad_load_after": "[addon]\nid = a\nversion = 1.0.0\ntype = content\nload_after = x-y\n",
            "manifest_bad_api_min": "[addon]\nid = a\nversion = 1.0.0\ntype = content\napi_min = 1.2\n",
            "manifest_plugin_required": "[addon]\nid = a\nversion = 1.0.0\ntype = content\n"
                                        "[plugin]\nname = a\nrequired = true\n",
            "manifest_bad_lua_entry": "[addon]\nid = a\nversion = 1.0.0\ntype = content\n[lua]\nentry = 1x\n",
            "manifest_bad_save_compat": "[addon]\nid = a\nversion = 1.0.0\ntype = location\n"
                                        "[location]\nsave_compat = 0\n",
            "manifest_bad_slot_files": "[addon]\nid = a\nversion = 1.0.0\ntype = ui\n"
                                       "[slot.ui.main_menu]\nfiles = ..\\x\n",
        }
        for code, text in cases.items():
            r, _ = check(text)
            self.assertIn(code, r.codes("error"), "%s: %s" % (code, r.to_text()))
        # the tag and the files of the catalog are named after the version: one spelling, no leading zeros
        r, _ = check("[addon]\nid = a\nversion = 01.0.0\ntype = content\n")
        self.assertIn("manifest_bad_version", r.codes("error"))
        for sc in ("²", "4294967296"):
            r, _ = check("[addon]\nid = a\nversion = 1.0.0\ntype = location\n[location]\nsave_compat = %s\n" % sc)
            self.assertIn("manifest_bad_save_compat", r.codes("error"), sc)

    def test_expectations_and_reserved(self):
        res = indexcfg.Reserved(["gw_eoc"], ["gw_"])
        r, _ = check(util.GOOD_MANIFEST.replace("my_addon", "gw_x"), expect_id="gw_x", reserved=res)
        self.assertIn("manifest_reserved_id", r.codes("error"))
        r, _ = check(util.GOOD_MANIFEST, expect_id="other", expect_version="1.3.0")
        self.assertIn("manifest_id_mismatch", r.codes("error"))
        self.assertIn("manifest_version_mismatch", r.codes("error"))

    def test_warnings(self):
        r, m = check("[addon]\nid = a\nversion = 1.0.0\ntype = content\n[capabilities]\nfs_write\nnet\n"
                     "[lua]\nentry = other_main\n")
        self.assertTrue(r.ok)
        self.assertEqual(m["caps"], ["fs_write"])
        for code in ("manifest_no_api_min", "manifest_unknown_capability", "manifest_lua_entry_prefix"):
            self.assertIn(code, r.codes("warning"))
        # api_min = 0.0.0: the engine reads it as no requirement - not set, no group of API major "0.0" in the index
        r, m = check("[addon]\nid = a\nversion = 1.0.0\ntype = content\napi_min = 0.0.0\n[plugin]\nname = a\n"
                     "api_min = 0.0.0\n")
        self.assertTrue(r.ok, r.to_text())
        self.assertIn("manifest_no_api_min", r.codes("warning"))
        self.assertEqual((m["api"], m["plugin_api"]), ("", ""))
        self.assertEqual(ltx.api_major(ltx.parse_api_version("0.0.0")), "")
        self.assertEqual(ltx.api_major(ltx.parse_api_version("0.0.1")), "0.0")
        self.assertEqual(ltx.api_major(ltx.parse_api_version("0.1.0")), "0.1")
        self.assertEqual(ltx.api_value("0.0.0"), "")
        self.assertEqual(ltx.api_value("0.1.0"), "0.1.0")
        # latest of the index (11 section 4.3): 0.0.0 (a state written before) and unset are one group, not two
        from gwcat import index_build
        entries = [{"v": v, "api": api, "pre": False, "yanked": False}
                   for v, api in (("1.0.0", "0.0.0"), ("1.1.0", ""), ("1.2.0", "0.1.0"))]
        self.assertEqual(sorted(e["v"] for e in index_build._latest(entries)), ["1.1.0", "1.2.0"])
        for gv, warned in (("0.5.5-patch4", False), ("v0.5.6-Beta", False), ("1.4", False), ("release 5", True)):
            r, m = check("[addon]\nid = a\nversion = 1.0.0\ntype = content\ngame_version = %s\n" % gv)
            self.assertEqual("manifest_game_version_form" in r.codes("warning"), warned, gv)
            self.assertEqual(m["game_version"], gv)


class CatalogTest(unittest.TestCase):
    TAGS = {"weapons", "traders"}

    def test_good(self):
        r = Report("t")
        card = catalog_meta.check_catalog(util.card_tree(), r, self.TAGS)
        self.assertTrue(r.ok, r.to_text())
        self.assertEqual(card["name"], {"": "My addon", "ru": "Мой аддон"})
        self.assertEqual(card["license"], "CC-BY-NC-4.0")
        self.assertEqual(card["links"]["homepage"], "https://example.com/a//b")
        self.assertEqual(card["description"], {"": "description.md", "ru": "description.ru.md"})
        self.assertEqual(card["screenshots"], ["screenshots/01.png", "screenshots/02.jpg"])
        self.assertEqual(card["icon"], "icon.png")

    def test_errors(self):
        cases = {
            "catalog_missing": {"catalog/catalog.ltx": None},
            "catalog_no_name": {"catalog/catalog.ltx": b"[catalog]\nsummary = s\n"},
            "catalog_no_summary": {"catalog/catalog.ltx": b"[catalog]\nname = s\n"},
            "catalog_unknown_tag": {"catalog/catalog.ltx": b"[catalog]\nname = n\nsummary = s\ntags = nsfw\n"},
            "catalog_bad_maintainer": {"catalog/catalog.ltx": b"[catalog]\nname = n\nsummary = s\nmaintainers = a b\n"},
            "catalog_bad_link": {"catalog/catalog.ltx": b"[catalog]\nname = n\nsummary = s\ndonate = ftp://x\n"},
            "catalog_no_description": {"catalog/description.md": None, "catalog/description.ru.md": None},
            "catalog_icon_size": {"catalog/icon.png": util.png(128, 128)},
            "catalog_icon_format": {"catalog/icon.png": util.jpeg_header(256, 256)},
            "catalog_screenshot_size": {"catalog/screenshots/03.png": util.png(2000, 100)},
            "catalog_screenshot_format": {"catalog/screenshots/03.webp": util.png(10, 10)},
            "catalog_screenshot_type": {"catalog/screenshots/03.gif": b"GIF89a"},
            "catalog_too_many_screenshots": {"catalog/screenshots/%02d.png" % i: util.png(8, 8)
                                             for i in range(3, 13)},
        }
        for code, over in cases.items():
            r = Report("t")
            catalog_meta.check_catalog(util.card_tree(**over), r, self.TAGS)
            self.assertIn(code, r.codes("error"), "%s: %s" % (code, r.to_text()))


class LuaCapsTest(unittest.TestCase):
    def scan(self, text):
        return luacaps.scan_text(text, "x.script", {})

    def test_caps(self):
        res = self.scan('os.execute("x")\nlocal p = io.popen("ls")\nlocal f = io.open(p, "wb")\n'
                        'local g = io.open(p)\nlocal ffi = require("ffi")\npackage.loadlib("a", "b")\n'
                        'lfs.mkdir("d")\nos.remove("x")\n')
        self.assertEqual(sorted(res["caps"]), ["ffi", "fs_write", "os_exec"])
        self.assertEqual(len(res["caps"]["fs_write"]), 3)
        self.assertEqual(res.get("review"), [])

    def test_review_flags(self):
        res = self.scan('local s = read()\nloadstring(s)()\nload("return 1")\ndebug.sethook(f)\n'
                        'debug.traceback()\nlocal o = os\no.execute("x")\nos["exec" .. "ute"]("x")\n'
                        'string.dump(f)\nio.open(p, mode)\n')
        codes = sorted({c for c, _, _ in res["review"]})
        self.assertEqual(codes, ["lua_debug_library", "lua_dynamic_access", "lua_dynamic_code",
                                 "lua_io_open_dynamic_mode", "lua_library_alias", "lua_string_dump"])
        self.assertEqual(len([c for c, _, _ in res["review"] if c == "lua_dynamic_code"]), 1)

    def test_no_false_positives(self):
        res = self.scan('-- os.execute("x") in a comment\nlocal s = "io.popen"\nfunction my.os() end\n'
                        't.os.execute = 1\nlocal function load(x) return x end\nload(y)\n')
        self.assertEqual(res.get("caps"), {})
        self.assertEqual(res.get("review"), [])


class SchemaTest(unittest.TestCase):
    def test_version_file(self):
        good = {"schema": 1, "id": "a", "version": "1.0.0", "tag": "v1.0.0", "files": [{"path": "addon.ltx", "size": 3}],
                "merge": {"file": "1.0.0.merge.json", "algo": "gw-merge/2", "sha256": "0" * 64, "size": 10}}
        self.assertEqual(schema.validate(good, "version-file"), [])
        bad = dict(good, merge={"file": "x.json", "algo": "gw merge", "sha256": "z", "size": 0, "extra": 1})
        self.assertGreaterEqual(len(schema.validate(bad, "version-file")), 4)
        self.assertTrue(schema.validate(dict(good, schema=True), "version-file"))  # a bool is not 1


if __name__ == "__main__":
    unittest.main()
