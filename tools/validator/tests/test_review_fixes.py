"""Regression tests of the review of 2026-11-06 (plans/addons_api_support/reviews/2026-11-06-review.md, section 2):
the catalog and the tools (F1-F22, G2, G3, G11, E5, B3, B6, B9, B15, B17, B19, B21, B22)."""

import json
import os
import shutil
import subprocess
import sys
import unittest
import zipfile

import util
from gwcat import (catalog_meta, cichanges, delta as delta_mod, index_build, indexcheck, manifest as manifest_mod,
                   newaddon, package, providers, resources, schema, snapshot_tools, strictjson)
from gwcat.indexcfg import IndexConfig
from gwcat.report import Report
from gwcat.validate import SnapshotStore, validate_version

from test_index import T0, Org, manifest, quiet


def tools(name):
    try:
        return snapshot_tools.import_module(name)
    except RuntimeError:
        return None


class IndexCase(unittest.TestCase):
    def setUp(self):
        self.tmp = util.TempDir()
        self.index = os.path.join(self.tmp.path, "index")
        shutil.copytree(util.INDEX_SKELETON, self.index, ignore=shutil.ignore_patterns(
            ".github", "ci", "tools", "files", "history", "state", "index.json", "stats.json", ".git"))
        self.org = Org(os.path.join(self.tmp.path, "org"))
        self.org.add_repo("my_addon", util.card_tree(), stars=5)
        self.provider = providers.LocalProvider(self.org.root, "Global-War-Addons")
        self.idx = index_build.Index(self.index)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self):
        return IndexConfig(self.index)

    def addon(self, version, files=None, **kw):
        rel = "src/" + version
        shutil.rmtree(os.path.join(self.tmp.path, "src", version), ignore_errors=True)
        return util.make_addon(self.tmp, rel, manifest=manifest(version, **kw), files=files)

    def ingest(self, **kw):
        return index_build.ingest(self.idx, self.provider, self.cfg(), kw.pop("snapshots", None), quiet,
                                  now=kw.pop("now", T0), **kw)

    def state(self, name="my_addon"):
        return strictjson.load_file(os.path.join(self.index, "state", name + ".json"))

    def set_repo(self, name, **fields):
        for r in self.org.repos:
            if r["name"] == name:
                r.update(fields)
        self.org.save()


class ManifestTest(unittest.TestCase):
    """F2, E5 and the [plugin] checks of the engine loader (A1/G13)"""

    def setUp(self):
        self.tmp = util.TempDir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_manifest_bomb_is_not_read(self):
        """F2: addon.ltx declared far over the limit is refused by its declared size, not read into memory"""
        z = util.make_zip(os.path.join(self.tmp.path, "a-1.0.0.zip"),
                          {"addon.ltx": "[addon]\nid = a\n" + " " * (4 * 1024 * 1024)})
        reads = []
        orig = zipfile.ZipFile.read

        def spy(zf, name, *a, **kw):
            reads.append(name)
            return orig(zf, name, *a, **kw)
        zipfile.ZipFile.read = spy
        try:
            rep, result = validate_version({"kind": "zips", "paths": [z]})
        finally:
            zipfile.ZipFile.read = orig
        self.assertIsNone(result)
        self.assertIn("manifest_too_large", rep.codes("error"))
        self.assertNotIn("addon.ltx", reads)

    def test_core_is_reserved_without_index(self):
        """E5: core names the base in the rules of addons.ltx"""
        rep = Report("m")
        manifest_mod.check_manifest(b"[addon]\nid = core\nversion = 1.0.0\ntype = content\n", rep)
        self.assertIn("manifest_reserved_id", rep.codes("error"))

    def test_plugin_keys(self):
        """The loader of the engine: no api_min - only a library with the export; unknown keys; a bad boolean"""
        rep = Report("m")
        manifest_mod.check_manifest(b"[addon]\nid = a\nversion = 1.0.0\ntype = content\napi_min = 0.1.0\n"
                                    b"[plugin]\nname = a\nrequired = ture\napimin = 0.1.0\n", rep)
        for code in ("manifest_plugin_no_api_min", "manifest_plugin_unknown_key", "manifest_plugin_bad_required"):
            self.assertIn(code, rep.codes("warning"))
        rep = Report("m")
        manifest_mod.check_manifest(b"[addon]\nid = a\nversion = 1.0.0\ntype = content\napi_min = 0.1.0\n"
                                    b"[plugin]\nname = a\napi_min = 0.1.0\nrequired = false\n", rep)
        self.assertFalse({"manifest_plugin_no_api_min", "manifest_plugin_unknown_key",
                          "manifest_plugin_bad_required"} & set(rep.codes("warning")))

    def test_api_min_nothing_the_engine_refuses(self):
        """The grammar of the engine (1-3 parts of 1-9 ASCII digits, MAJOR, MINOR <= 1023, PATCH <= 4095): the catalog
        (MAJOR.MINOR.PATCH only) accepts nothing the engine refuses"""
        from gwcat import ltx
        for bad in ("1.2x", "1..2", "+0.1.0", "0x2", "1 2", "1.2.3.4", "1024.0.0", "0.1024.0", "0.0.4096",
                    "4294967297.0.0", "1.2", "1", "\u00b2.0.0", " 1.0.0", "1.0.0 "):
            self.assertIsNone(ltx.parse_api_version(bad), bad)
        self.assertEqual(ltx.parse_api_version("1023.1023.4095"), (1023, 1023, 4095))


class PackageTest(unittest.TestCase):
    """G2, G3 / F20: a working folder as the author mode of the launcher takes it"""

    def setUp(self):
        self.tmp = util.TempDir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_repo_checkout_and_marker(self):
        folder = util.make_addon(self.tmp, files={
            ".github/workflows/validate.yml": "x", "catalog/catalog.ltx": "[catalog]\n", ".gitignore": "*.zip",
            ".gitattributes": "* text", ".gitmodules": "", ".gw_addon.json": "{}", ".gw_merge/delta.json": "{}",
            ".git/HEAD": "ref"})
        rep, _ = validate_version({"kind": "folder", "path": folder}, snapshot_check=False)
        self.assertTrue(rep.ok, rep.to_text())
        self.assertIn("package_excluded", rep.codes("note"))
        files = package.folder_files(folder, Report("f"))
        self.assertEqual(sorted(files), ["addon.ltx", "gamedata/scripts/my_addon_main.script"])
        # the same names deeper are a part of the package (only the root is a checkout)
        self.tmp.write("addon/gamedata/catalog/x.ltx", "x")
        self.assertIn("gamedata/catalog/x.ltx", package.folder_files(folder, Report("f")))

    def test_marker_in_a_zip_is_a_warning(self):
        z = util.make_zip(os.path.join(self.tmp.path, "my_addon-1.2.0.zip"), {
            "addon.ltx": util.GOOD_MANIFEST, "gamedata/scripts/my_addon_main.script": util.GOOD_SCRIPT,
            ".gw_addon.json": "{}"})
        rep, result = validate_version({"kind": "zips", "paths": [z]}, snapshot_check=False)
        self.assertTrue(rep.ok, rep.to_text())
        self.assertIn("package_launcher_marker", rep.codes("warning"))
        self.assertNotIn(".gw_addon.json", [f["path"] for f in result["files"]])

    def test_windows_names_as_the_launcher(self):
        """A name the launcher refuses (unpack.rs is_portable_path, its test the_portable_names_of_a_package) fails
        the package here too"""
        bad = ["gamedata/file.ltx:ads", "gamedata/CON", "gamedata/con.txt", "gamedata/trail.", "gamedata/CON .txt",
               "gamedata/COM0.txt", "gamedata/COM\u00b9.txt", "gamedata/lpt\u00b3.ltx", "gamedata/conin$",
               "gamedata/CONOUT$.log", "gamedata/ctl\u0001.txt", "gamedata/del\u007f.txt", "gamedata/c1\u0085.txt",
               "gamedata/lpt0", "gamedata/nul   .x.y", "gamedata/aux/x.ltx"]
        good = ["gamedata/configs/alife.ltx", "gamedata/console.ltx", "gamedata/com10.txt", "gamedata/.hidden",
                "addon.ltx", "gamedata/com.ltx", "gamedata/x.con", "gamedata/\u0436\u0443\u0440\u043d\u0430\u043b.ltx"]
        for name in bad:
            rep = Report("p")
            self.assertFalse(package._check_name(name, rep, "zip"), repr(name))
            self.assertIn("package_windows_name", rep.codes("error"), repr(name))
        for name in good:
            rep = Report("p")
            self.assertTrue(package._check_name(name, rep, "zip"), repr(name))


class CatalogMetaTest(unittest.TestCase):
    """F17, F21 / G11, F22"""

    def check(self, **over):
        rep = Report("c")
        card = catalog_meta.check_catalog(util.card_tree(**over), rep, {"weapons", "traders"})
        return rep, card

    def test_screenshot_names_of_the_launcher(self):
        for bad in ("my shot.png", "\u0441\u043d\u0438\u043c\u043e\u043a.png", "a(1).png", "a+b.png", "a#1.png"):
            rep, _ = self.check(**{"catalog/screenshots/" + bad: util.png(10, 10)})
            self.assertIn("catalog_screenshot_name", rep.codes("error"), bad)
        rep, card = self.check(**{"catalog/screenshots/ok_name-1.png": util.png(10, 10)})
        self.assertTrue(rep.ok, rep.to_text())
        self.assertIn("screenshots/ok_name-1.png", card["screenshots"])

    def test_template_leftovers(self):
        rep, _ = self.check(**{"catalog/description.md": b"Describe the addon here: what it adds or changes."})
        self.assertIn("catalog_template_description", rep.codes("warning"))
        if os.path.isfile(os.path.join(util.TEMPLATE, "catalog", "description.md")):
            with open(os.path.join(util.TEMPLATE, "catalog", "description.md"), "rb") as f:
                rep, _ = self.check(**{"catalog/description.md": f.read()})
            self.assertIn("catalog_template_description", rep.codes("warning"))
        tree = util.card_tree()
        tree["catalog/catalog.ltx"] = tree["catalog/catalog.ltx"].replace(b"tags = weapons, traders", b"tags =")
        rep = Report("c")
        catalog_meta.check_catalog(tree, rep, {"weapons"})
        self.assertIn("catalog_no_tags", rep.codes("warning"))

    def test_bad_maintainer_is_not_empty(self):
        tree = util.card_tree()
        tree["catalog/catalog.ltx"] = tree["catalog/catalog.ltx"].replace(b"ivan-petrov", b"{{maintainer}}")
        rep = Report("c")
        catalog_meta.check_catalog(tree, rep, {"weapons", "traders"})
        self.assertIn("catalog_bad_maintainer", rep.codes("error"))
        self.assertNotIn("catalog_no_maintainers", rep.codes("warning"))


class DeltaFormatTest(unittest.TestCase):
    """B15, B19, B16 / the entries as the engine reads them, B17 (strict JSON of the validator and of the tools)"""

    def delta(self, **over):
        d = {"format": 1, "algo": "gw-merge/2", "game": "v1", "files": {
            "configs\\a.ltx": {"kind": "ltx", "ancestor_sha256": "a" * 64, "addon_sha256": "b" * 64}}}
        d.update(over)
        return d

    def test_format_is_an_integer(self):
        self.assertIsNone(delta_mod.check_delta_object(self.delta()))
        for bad in (1.0, True, "1"):
            self.assertIsNotNone(delta_mod.check_delta_object(self.delta(format=bad)), bad)
        self.assertTrue(schema.validate(self.delta(format=1.0), "delta"))

    def test_algo_one_pattern(self):
        for name in ("delta", "version-file"):
            self.assertEqual(schema.load(name)["$defs"]["algo"]["pattern"], delta_mod.ALGO_PATTERN)
        for algo in ("gw merge/2", "gw-merge/2 ", "a:b/2", "gw-merge"):
            self.assertIsNotNone(delta_mod.check_delta_object(self.delta(algo=algo)), algo)
        name = delta_mod.recomputed_file_name("1.0.0", "gw-merge/3.1")
        self.assertTrue(indexcheck.RECOMPUTED_RE.match(name), name)

    def test_entries(self):
        files = {"configs\\a.ltx": {"kind": "ltx"}, "Configs/A.ltx": {"kind": "ltx"}}
        self.assertIn("one file", delta_mod.check_delta_object(self.delta(files=files)))
        for vpath in ("\\configs\\a.ltx", "configs\\\\a.ltx", "configs\\..\\a.ltx", "c:\\a.ltx"):
            self.assertIsNotNone(delta_mod.check_delta_object(self.delta(files={vpath: {"kind": "ltx"}})), vpath)
        upper = {"configs\\a.ltx": {"kind": "ltx", "addon_sha256": "B" * 64}}
        self.assertIsNotNone(delta_mod.check_delta_object(self.delta(files=upper)))

    def test_strict_json_validator_and_tools_agree(self):
        common = tools("common")
        vectors = [
            (b'{"a": 1}', True), (b'{"a": 1e400}', True), (b'{"a": NaN}', False), (b'{"a": Infinity}', False),
            (b'{"a": "\\ud800"}', False), (b'{"a": "\\ud83d\\ude00"}', True), (b'{"a": 1, "a": 2}', False),
            (b'{"a": "\\u0061", "\\u0061": 2}', False), (b'{"a": "x\x01"}', False), (b'\xff', False),
            (b"[" * 64 + b"]" * 64, True), (b"[" * 65 + b"]" * 65, False), (b'{"a": 1} x', False),
        ]
        for data, ok in vectors:
            try:
                strictjson.loads_bytes(data)
                got = True
            except strictjson.StrictJsonError:
                got = False
            self.assertEqual(got, ok, data[:40])
            if common:
                try:
                    common.loads_json(data, "x")
                    got = True
                except common.BrokenJsonError:
                    got = False
                self.assertEqual(got, ok, data[:40])
        # the BOM: the tools take it as the engine does, the files of the index must not have it
        self.assertRaises(strictjson.StrictJsonError, strictjson.loads_bytes, b'\xef\xbb\xbf{"a": 1}')
        if common:
            self.assertEqual(common.loads_json(b'\xef\xbb\xbf{"a": 1}', "x"), {"a": 1})


class MergeRulesTest(unittest.TestCase):
    """B21, B22, B9, B3 on the side of the validator (and the same rules of tools/addons_snapshot)"""

    def setUp(self):
        self.tmp = util.TempDir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_ascii_lower(self):
        common = tools("common")
        self.assertEqual(resources.ascii_lower("Configs\\\u0410\u0411.LTX"), "configs\\\u0410\u0411.ltx")
        if common:
            self.assertEqual(common.vnorm("Configs/\u0410\u0411.LTX"), "configs\\\u0410\u0411.ltx")

    def test_replaced_texts_of_the_engine(self):
        """the sets of the slots, .lua and mod_ without a target are merged; [private] and the patches are not - by
        gw_merge plan (decision 64 item 4: the Python ports are removed)"""
        if not util.find_gw_merge():
            self.skipTest("gw_merge is not found ($GW_MERGE, PATH, xray-16 build folders)")
        m = ("[addon]\nid = my_addon\nversion = 1.0.0\ntype = ui\n[slot.ui.menu]\nfiles = slots\\menu\\\n"
             "[private]\nconfigs\\priv.ltx\n")
        folder = util.make_addon(self.tmp, manifest=m, files={
            "gamedata/configs/priv.ltx": "x", "gamedata/configs/mod_system_my.ltx": "x",
            "gamedata/configs/mod_orphan_x.ltx": "x", "gamedata/scripts/b.lua": "x",
            "slots/menu/gamedata/configs/ui/menu.xml": "<a/>"})
        base = {v: [1, "0" * 64] for v in ("configs\\priv.ltx", "configs\\system.ltx", "configs\\mod_system_my.ltx",
                                          "configs\\mod_orphan_x.ltx", "scripts\\b.lua", "configs\\ui\\menu.xml")}
        m2 = manifest_mod.check_manifest(m.encode(), Report("m"))
        from gwcat import content
        plan, why = resources.plan_texts(folder, base.keys())
        self.assertIsNone(why)
        texts = content.check_replacements(m2, Report("r"), base, plan)
        self.assertEqual(texts, ["configs\\mod_orphan_x.ltx", "configs\\ui\\menu.xml", "scripts\\b.lua"])


@unittest.skipUnless(tools("catalog_merge"), "tools/addons_snapshot is not here")
class ToolsTest(unittest.TestCase):
    """B3, B9 (the texts of gw_merge delta), B6 (the delta in CI), F19 (--catalog does not write <ver>.json)"""

    def setUp(self):
        self.tmp = util.TempDir()
        self.cm = tools("catalog_merge")

    def tearDown(self):
        self.tmp.cleanup()

    def test_layer_texts(self):
        if not util.find_gw_merge():
            self.skipTest("gw_merge is not here: the tools decide the layers by gw_merge plan only")
        m = ("[addon]\nid = my_addon\nversion = 1.0.0\ntype = ui\n[slot.ui.menu]\nfiles = slots\\menu\\\n"
             "[slot.ui.hud]\nfiles = slots\\hud\\\n[private]\nconfigs\\priv.ltx\n")
        folder = util.make_addon(self.tmp, manifest=m, files={
            "gamedata/configs/priv.ltx": "x", "gamedata/configs/both.ltx": "x", "gamedata/.hidden/a.ltx": "x",
            "gamedata/@alias/a.ltx": "x", "slots/menu/gamedata/configs/both.ltx": "y",
            "slots/menu/gamedata/configs/ui/menu.xml": "<a/>", "slots/menu/gamedata/configs/ui/two.xml": "<a/>",
            "slots/hud/gamedata/configs/ui/two.xml": "<b/>"})
        texts, warnings = self.cm.addon_layer_texts(folder)
        # '.hidden': hidden on POSIX only - the engine of Windows mounts a name '.*' (only the attribute hides there)
        self.assertEqual(sorted(texts), (["configs\\both.ltx"] if os.name != "nt" else
                                         [".hidden\\a.ltx", "configs\\both.ltx"]) +
                         ["configs\\ui\\menu.xml", "scripts\\my_addon_main.script"])
        self.assertTrue(texts["configs\\both.ltx"].replace("\\", "/").endswith("gamedata/configs/both.ltx"))
        self.assertNotIn("slots", texts["configs\\both.ltx"].replace("\\", "/").split("/")[-4:-3])
        self.assertEqual(len(warnings), 2)

    def test_index_never_reads_the_delta_of_the_package(self):
        folder = util.make_addon(self.tmp, manifest="[addon]\nid = my_addon\nversion = 1.0.0\ntype = content\n",
                                 files={".gw_merge/delta.json": json.dumps(
                                     {"format": 1, "algo": "gw-merge/2", "game": "v1", "files": {}})})
        index = os.path.join(self.tmp.path, "index")
        os.makedirs(os.path.join(index, "files", "my_addon"))
        self.assertEqual(self.cm.find_delta(folder, "gw-merge/2", index), (None, None))
        self.assertIsNotNone(self.cm.find_delta(folder, "gw-merge/2")[0])

    def test_delta_of_a_set_and_catalog_mode(self):
        gw_merge = util.find_gw_merge()
        if not gw_merge:
            self.skipTest("gw_merge is not here")
        base = os.path.join(self.tmp.path, "base")
        for rel, text in (("configs/ui/menu.xml", "<w>\n  <a>1</a>\n</w>\n"),
                          ("configs/x.ltx", "[s]\nk = 1\n")):
            p = os.path.join(base, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        snaps = os.path.join(self.tmp.path, "snaps", "v1.0.0")
        code, _, err = snapshot_tools.run(["build", base, "--tag", "v1.0.0", "--out", snaps, "--no-auto"])
        self.assertEqual(code, 0, err)
        m = ("[addon]\nid = my_addon\nversion = 1.0.0\ntype = ui\ngame_version = v1.0.0\n"
             "[slot.ui.menu]\nfiles = slots\\menu\\\n")
        folder = util.make_addon(self.tmp, manifest=m, files={
            "gamedata/configs/x.ltx": "[s]\nk = 2\n",
            "slots/menu/gamedata/configs/ui/menu.xml": "<w>\n  <a>2</a>\n</w>\n"})
        index = os.path.join(self.tmp.path, "index")
        code, out, err = snapshot_tools.run(["delta", folder, "--snapshot",
                                             os.path.join(snaps, "base_snapshot.json.gz"), "--catalog", index,
                                             "--gw-merge", gw_merge, "--format", "json"])
        self.assertEqual(code, 0, err)
        d = strictjson.load_file(os.path.join(index, "files", "my_addon", "1.0.0.merge.json"))
        self.assertEqual(sorted(d["files"]), ["configs\\ui\\menu.xml", "configs\\x.ltx"])
        self.assertFalse(os.path.exists(os.path.join(index, "files", "my_addon", "1.0.0.json")))


class GwMergeAgreementTest(unittest.TestCase):
    """The rules the validator still keeps against the library itself (gw_merge.exe): the same inputs, the same answers.
    strict JSON (DeltaJson.cpp, via gw_merge apply), KindOfName (gw_merge atoms auto: the pre-filter TEXT_MERGED of
    resources.texts_to_decide), the ASCII lower case of a vpath (gw_merge targets). A change of the C++ rules fails
    here, not in the game. IsPatchFile is not ported any more (decision 64 item 4: gw_merge plan decides it)"""

    def setUp(self):
        self.gw_merge = util.find_gw_merge()
        if not self.gw_merge:
            self.skipTest("gw_merge is not found ($GW_MERGE, PATH, xray-16 build folders)")
        self.tmp = util.TempDir()
        self.common = tools("common")
        self.gwmerge = tools("gwmerge")
        self.cm = tools("catalog_merge")

    def tearDown(self):
        self.tmp.cleanup()

    def run_gw(self, *args):
        r = subprocess.run([self.gw_merge] + list(args), capture_output=True)
        return r.returncode, r.stdout, r.stderr.decode("utf-8", "replace")

    def test_strict_json(self):
        base = self.tmp.write("base.ltx", "[a]\nk = 1\n")
        head = b'{"format": 1, "algo": "gw-merge/2", "game": "t", "files": {}, "x": '
        vectors = [b"1", b"1e400", b"NaN", b"-Infinity", b'"\\ud800"', b'"\\udc00\\ud800"', b'"\\ud83d\\ude00"',
                   b'"\xff"', b'"\xc0\x80"', b'"\xed\xa0\x80"', b'"a\x01"', b'{"a": 1, "\\u0061": 2}', b"01", b"1.",
                   b"[1,]", b"\x0c1", b"[" * 62 + b"]" * 62, b"[" * 63 + b"]" * 63, b"[" * 64 + b"]" * 64]
        texts = [head + v + b"}" for v in vectors] + [b'\xef\xbb\xbf' + head + b"1}", head + b"1} 1"]
        texts += [head.replace(b'"format": 1', b'"format": ' + f) + b"1}" for f in (b"1.0", b"1e0", b"10e-1")]
        for data in texts:
            path = self.tmp.write("d.json", data)
            code, _, err = self.run_gw("apply", "ltx", "--delta", path, "--vpath", "configs\\x.ltx", base, "-o",
                                       os.path.join(self.tmp.path, "o.ltx"), "--report",
                                       os.path.join(self.tmp.path, "r.json"))
            engine = not (code == 2 and "is not a delta" in err)
            if self.cm:
                # the tools read a delta as find_delta does: load_json + check_delta
                try:
                    self.cm.check_delta(self.common.loads_json(data, "d"), "d")
                    tool = True
                except self.common.ToolError:
                    tool = False
                self.assertEqual(tool, engine, data[:70])
            try:
                validator = delta_mod.check_delta_object(strictjson.loads_bytes(data)) is None
            except strictjson.StrictJsonError:
                validator = False
            # the files of the index must not start with a BOM (the engine takes it)
            self.assertEqual(validator, engine and not data.startswith(b"\xef\xbb\xbf"), data[:70])

    def test_kind_of_name(self):
        a = self.tmp.write("a.txt", "x\n")
        b = self.tmp.write("b.txt", "y\n")
        for name in ("x.ltx", "x.LTX", "x.XmL", "x.script", "x.Lua", "x.ltx.bak", ".ltx", "x.", "x", "x.lt",
                     "a.b\\x", "\u0416.ltx", "x.\u0130tx", "x.\u212altx"):
            code, out, err = self.run_gw("atoms", "auto", a, b, "--name", name)
            engine = json.loads(out.decode("utf-8"))["kind"]
            low = resources.ascii_lower(name)
            self.assertEqual(low.endswith(resources.TEXT_MERGED), engine != "unknown", name)
            if self.gwmerge:
                self.assertEqual(self.gwmerge.kind_of(name) or "unknown", engine, name)

    def test_ascii_lower_of_a_vpath(self):
        a = self.tmp.write("a.txt", "x\n")
        for vpath in ("Configs/A.LTX", "configs\\\u0416\\\u0414.LTX", "Scripts/\u00c4.script", "a/\u0130\u212a.xml"):
            code, out, err = self.run_gw("targets", vpath, "my_addon=" + a)
            section = out.decode("utf-8").splitlines()[0][1:-1]
            self.assertEqual(resources.ascii_lower(vpath).replace("/", "\\"), section, vpath)
            if self.common:
                self.assertEqual(self.common.vnorm(vpath), section, vpath)


class IndexFixesTest(IndexCase):
    @unittest.skipUnless(shutil.which("git"), "git is not here")
    def test_hand_over_of_a_real_build_is_accepted(self):
        """F3: what build-index really writes (a listed addon, a revoked one) passes the checks of apply-changes"""
        def git(*args, cwd=self.index):
            return subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t"] + list(args),
                                  check=True, capture_output=True)
        self.org.add_repo("bad_addon", util.card_tree())
        with open(os.path.join(self.index, "moderation.ltx"), "a", encoding="utf-8") as f:
            f.write("\n[revoked]\nbad_addon = malware\n")
        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "-m", "init")
        self.org.release("my_addon", "v1.0.0", self.addon("1.0.0"))
        self.org.release("bad_addon", "v1.0.0", util.make_addon(
            self.tmp, "src_bad", manifest=manifest("1.0.0").replace("id = my_addon", "id = bad_addon")))
        self.ingest()
        for name in ("my_addon", "bad_addon"):
            if os.path.isfile(os.path.join(self.index, "state", name + ".json")) and \
                    self.state(name)["versions"].get("1.0.0", {}).get("status") == "pending":
                index_build.review(self.idx, name, "1.0.0", "approve", now=T0)
        index_build.render(self.idx, self.cfg(), quiet, now=T0)
        index = strictjson.load_file(os.path.join(self.index, "index.json"))
        self.assertIn("my_addon", [a["id"] for a in index["addons"]])
        self.assertIn("bad_addon", index.get("revoked_ids", []))
        out = os.path.join(self.tmp.path, "handover")
        changed, _ = cichanges.export(self.index, out)
        self.assertIn("index.json", changed)
        dst = os.path.join(self.tmp.path, "clone")
        subprocess.run(["git", "clone", "-q", self.index, dst], check=True, capture_output=True)
        cichanges.apply(out, dst)
        self.assertTrue(indexcheck.check(dst).ok)

    def test_outbox_and_publish_are_idempotent(self):
        """F3 / F10: the issues of the check job go to an outbox; publish opens them once and records their numbers"""
        self.org.release("my_addon", "v1.0.0", self.addon("1.0.0"))
        outbox = index_build.IssueOutbox()
        s = self.ingest(issues=outbox)
        self.assertEqual(s["pending"], ["my_addon@1.0.0"])
        self.assertEqual(self.state()["versions"]["1.0.0"]["issue"], 0)
        self.assertEqual(self.org.issues(), [])
        path = os.path.join(self.tmp.path, "issues.json")
        outbox.write(path)
        for _ in range(2):  # the second time: a push lost after the issues were opened - no second issue
            self.assertLessEqual(index_build.publish_issues(self.idx, self.provider, path, self.cfg(), "bot", quiet), 1)
        self.assertEqual(len(self.org.issues()), 1)
        self.assertIn("<!-- gw-review my_addon 1.0.0 -->", self.org.issues()[0]["body"])
        self.assertEqual(self.state()["versions"]["1.0.0"]["issue"], 1)
        # an issue of somebody else with the marker is not taken
        with open(path, encoding="utf-8") as f:
            bad = json.load(f)
        bad["issues"][0]["labels"] = ["admin"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(bad, f)
        self.assertRaises(RuntimeError, index_build.publish_issues, self.idx, self.provider, path, self.cfg(), "bot",
                          quiet)

    def test_repo_replaced(self):
        """F6: a repo made again under the same name: no carried-over versions and capabilities"""
        self.set_repo("my_addon", repo_id=1)
        self.org.release("my_addon", "v1.0.0", self.addon("1.0.0"))
        self.ingest()
        index_build.review(self.idx, "my_addon", "1.0.0", "approve")
        self.set_repo("my_addon", repo_id=2)
        self.org.set_releases("my_addon", [])
        self.org.release("my_addon", "v1.1.0", self.addon("1.1.0"))
        s = self.ingest()
        self.assertEqual(s["replaced"], ["my_addon"])
        st = self.state()
        self.assertEqual(st["versions"]["1.0.0"]["status"], "gone")
        self.assertEqual(st["versions"]["1.1.0"]["status"], "pending")
        self.assertIn("first publication", st["versions"]["1.1.0"]["review"])
        self.assertEqual(st["repo_id"], 2)
        # the request form: an id of a repo that is gone is not given again without a moderator
        problems = newaddon.check_request({"addon_id": "my_addon", "type": "content", "name": "x",
                                           "maintainer": "ivan"}, self.cfg(), set())
        self.assertTrue(any("gone" in p for p in problems), problems)

    def test_catalog_read_only_when_changed(self):
        """F8: the same commit, tags and exceptions - catalog/ is not read again; large files are not read at all"""
        self.ingest()
        calls = []
        orig = self.provider.read_tree
        self.provider.read_tree = lambda *a, **kw: calls.append(a) or orig(*a, **kw)
        self.ingest()
        self.assertEqual(calls, [])
        big = os.path.join(self.org.root, "my_addon", "head", "catalog", "screenshots", "03.png")
        with open(big, "wb") as f:
            f.write(b"\0" * (2 * 1024 * 1024))
        s = self.ingest()
        self.assertEqual(len(calls), 1)
        self.assertEqual(s["catalog_errors"], ["my_addon"])
        self.assertTrue(any(e.startswith("catalog_screenshot_bytes") for e in self.state()["catalog_errors"]))
        plan = providers.plan_tree([("catalog/notes/big.bin", 10 ** 9), ("catalog/catalog.ltx", 10)], ("catalog/",))
        self.assertEqual([w for _, w, _ in plan], ["read", "name"])

    def test_disk_space_is_a_ci_problem(self):
        """F7: a package larger than the disk: retried (not invalid), nothing downloaded, an issue after a while"""
        self.org.release("my_addon", "v1.0.0", self.addon("1.0.0"))
        orig = shutil.disk_usage
        index_build.shutil.disk_usage = lambda p: orig(p)._replace(free=1024)
        downloads = []
        dl = self.provider.download_asset
        self.provider.download_asset = lambda *a: downloads.append(a) or dl(*a)
        try:
            for i in range(index_build.RETRY_NOTIFY_AFTER):
                # a try at most once in RETRY_MIN_MINUTES: a run right after it waits (decision 63, cron of 15 min)
                s = self.ingest(now="2026-11-05T%02d:%02d:00Z" % (10 + i // 2, 30 * (i % 2)))
                self.assertEqual(s["retry"], ["my_addon@1.0.0"])
                s2 = self.ingest(now="2026-11-05T%02d:%02d:00Z" % (10 + i // 2, 30 * (i % 2) + 15))
                self.assertEqual((s2["retry"], s2["retry_later"]), ([], ["my_addon@1.0.0"]))
        finally:
            index_build.shutil.disk_usage = orig
        self.assertEqual(s["retry"], ["my_addon@1.0.0"])
        self.assertEqual(downloads, [])
        st = self.state()
        self.assertNotIn("1.0.0", st["versions"])
        self.assertEqual(st["retry"]["1.0.0"]["count"], index_build.RETRY_NOTIFY_AFTER)
        self.assertTrue(st["retry"]["1.0.0"]["errors"][0].startswith("ci_disk_space"))
        self.assertEqual([i["title"] for i in self.org.issues()], ["CI cannot check my_addon v1.0.0"])

    def test_retry_pauses(self):
        """F16: after RETRY_MAX runs the version waits for a change of its inputs"""
        self.org.release("my_addon", "v1.0.0", self.addon("1.0.0"))
        st = {"schema": 1, "id": "my_addon", "repo": "Global-War-Addons/my_addon", "accepted_caps": [],
              "versions": {}}
        rel = self.provider.list_releases("my_addon")[0]
        key = index_build._retry_key(rel, index_build.exceptions_key(self.cfg(), "my_addon"), "")
        st["retry"] = {"1.0.0": {"count": index_build.RETRY_MAX, "since": T0, "key": key}}
        self.idx.save_state(st)
        s = self.ingest()
        self.assertEqual(s["paused"], ["my_addon@1.0.0"])
        self.org.release("my_addon", "v1.0.0", self.addon("1.0.0"), date="2026-11-02T00:00:00Z")  # a new upload
        s = self.ingest()
        self.assertEqual(s["paused"], [])
        self.assertEqual(s["pending"], ["my_addon@1.0.0"])
        self.assertNotIn("retry", self.state())

    def test_paused_retry_is_tried_once_a_day(self):
        """F16: a fix that changes no input of retry_key (the snapshot published, a larger runner) is picked up within a
        day"""
        self.org.release("my_addon", "v1.0.0", self.addon("1.0.0"))
        st = {"schema": 1, "id": "my_addon", "repo": "Global-War-Addons/my_addon", "accepted_caps": [],
              "versions": {}}
        rel = self.provider.list_releases("my_addon")[0]
        key = index_build._retry_key(rel, index_build.exceptions_key(self.cfg(), "my_addon"), "")
        st["retry"] = {"1.0.0": {"count": index_build.RETRY_MAX, "since": "2026-11-01T00:00:00Z", "last": T0,
                                 "key": key}}
        self.idx.save_state(st)
        s = self.ingest(now="2026-11-05T20:00:00Z")
        self.assertEqual(s["paused"], ["my_addon@1.0.0"])
        s = self.ingest(now="2026-11-06T10:00:00Z")
        self.assertEqual(s["paused"], [])
        self.assertEqual(s["pending"], ["my_addon@1.0.0"])

    def test_recheck_of_an_invalid_version_opens_no_second_issue(self):
        """F14: an invalid version checked again (a game release added, or a state written before "exceptions") and
        refused for the same reasons keeps its issue"""
        script = 'function on_load(ctx) local f = io.open("x", "w") end\n'
        self.org.release("my_addon", "v1.0.0", self.addon("1.0.0", files={
            "gamedata/scripts/my_addon_io.script": script}))
        self.ingest()
        issue = self.state()["versions"]["1.0.0"]["issue"]
        self.org_close_issues()
        with open(os.path.join(self.index, "game_releases.ltx"), "a", encoding="utf-8") as f:
            f.write("v0.0.1 = 2026-10-01\n")
        s = self.ingest()
        self.assertEqual(s["invalid"], ["my_addon@1.0.0"])
        self.assertEqual(s["issues"], 0)
        self.assertEqual(self.state()["versions"]["1.0.0"]["issue"], issue)
        self.assertEqual(len(self.org.issues()), 1)

    def org_close_issues(self):
        """The author closed the issues (find_issue takes only an open one)"""
        issues = self.org.issues()
        for i in issues:
            i["state"] = "closed"
        with open(os.path.join(self.org.root, "issues.json"), "w", encoding="utf-8") as f:
            json.dump(issues, f)

    def test_exception_rechecks_an_invalid_version(self):
        """F14: an exception of moderation.ltx granted after the rejection makes the version pass"""
        script = 'function on_load(ctx) local f = io.open("x", "w") end\n'
        self.org.release("my_addon", "v1.0.0", self.addon("1.0.0", files={
            "gamedata/scripts/my_addon_io.script": script}))
        self.ingest()
        self.assertEqual(self.state()["versions"]["1.0.0"]["status"], "invalid")
        with open(os.path.join(self.index, "moderation.ltx"), "a", encoding="utf-8") as f:
            f.write("\n[exceptions]\nmy_addon = caps_undeclared\n")
        s = self.ingest()
        self.assertEqual(s["pending"], ["my_addon@1.0.0"])

    def test_revoked_covers_gone_versions(self):
        """F15: the packages of a revoked addon whose releases are deleted stay revoked"""
        self.org.release("my_addon", "v1.0.0", self.addon("1.0.0"))
        self.ingest()
        index_build.review(self.idx, "my_addon", "1.0.0", "approve")
        sha = self.state()["versions"]["1.0.0"]["assets"][0]["sha256"]
        self.org.set_releases("my_addon", [])
        self.ingest()
        self.assertEqual(self.state()["versions"]["1.0.0"]["status"], "gone")
        with open(os.path.join(self.index, "moderation.ltx"), "a", encoding="utf-8") as f:
            f.write("\n[revoked]\nmy_addon = malware\n")
        index_build.render(self.idx, self.cfg(), quiet, now=T0)
        idx = strictjson.load_file(os.path.join(self.index, "index.json"))
        self.assertIn(sha, idx["revoked_sha256"])
        self.assertEqual(idx["revoked_ids"], ["my_addon"])
        self.assertEqual(idx["addons"], [])
        self.assertTrue(indexcheck.check(self.index).ok)

    def test_latest_pre_per_major(self):
        """F18: a beta under API 0.1 next to a stable version under 0.2"""
        entries = [{"v": "1.4.0", "api": "0.1.0", "pre": False, "yanked": False},
                   {"v": "1.5.0", "api": "0.1.0", "pre": True, "yanked": False},
                   {"v": "2.0.0", "api": "0.2.0", "pre": False, "yanked": False},
                   {"v": "1.9.0", "api": "0.2.0", "pre": True, "yanked": False}]
        self.assertEqual([e["v"] for e in index_build._latest(entries)], ["2.0.0", "1.5.0", "1.4.0"])

    def test_snapshot_digests(self):
        """F13: game_releases.ltx carries the sha256 of the snapshot; another file under the tag is not used"""
        with open(os.path.join(self.index, "game_releases.ltx"), "a", encoding="utf-8") as f:
            f.write("v1.0.0 = 2026-10-01, %s, %s\n" % ("a" * 64, "b" * 64))
        cfg = self.cfg()
        self.assertEqual(cfg.snapshot_digests["v1.0.0"], ("a" * 64, "b" * 64))
        snaps = os.path.join(self.tmp.path, "snaps")
        for n in SnapshotStore.NAMES:
            self.tmp.write("snaps/v1.0.0/" + n, "x")
        store = SnapshotStore(snaps, None, "v1.0.0", ["v1.0.0"], cfg.snapshot_digests)
        self.assertIsNone(store.get("v1.0.0"))
        self.assertFalse(os.path.isfile(os.path.join(snaps, "v1.0.0", SnapshotStore.NAMES[0])))
        with open(os.path.join(self.index, "game_releases.ltx"), "a", encoding="utf-8") as f:
            f.write("v1.1.0 = 2026-10-02, abc\n")
        from gwcat.indexcfg import ConfigError
        self.assertRaises(ConfigError, self.cfg)


@unittest.skipUnless(shutil.which("git"), "git is not here")
class HandOverTest(unittest.TestCase):
    """F3: what the job that read the packages hands over is checked by the job that pushes"""

    def setUp(self):
        self.tmp = util.TempDir()
        self.repo = os.path.join(self.tmp.path, "repo")
        # a real configuration: apply-changes checks index.json / state/ against source.ltx and moderation.ltx
        shutil.copytree(util.INDEX_SKELETON, self.repo, ignore=shutil.ignore_patterns(
            ".github", "ci", "tools", "files", "history", "state", "index.json", "stats.json", ".git"))
        os.makedirs(os.path.join(self.repo, "files", "a"))
        with open(os.path.join(self.repo, "files", "a", "1.0.0.json"), "w") as f:
            f.write('{"x": 1}\n')
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
        self.out = os.path.join(self.tmp.path, "out")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        subprocess.run(["git", "-C", cwd or self.repo] + list(args), check=True, capture_output=True)

    def clone(self):
        self.clones = getattr(self, "clones", 0) + 1
        dst = os.path.join(self.tmp.path, "clone%d" % self.clones)
        subprocess.run(["git", "clone", "-q", self.repo, dst], check=True, capture_output=True)
        return dst

    def test_round_trip(self):
        os.makedirs(os.path.join(self.repo, "state"))
        with open(os.path.join(self.repo, "state", "a.json"), "w") as f:
            f.write('{"id": "a", "repo": "Global-War-Addons/a"}\n')
        changed, deleted = cichanges.export(self.repo, self.out)
        self.assertEqual((changed, deleted), (["state/a.json"], []))
        dst = self.clone()
        self.assertEqual(cichanges.apply(self.out, dst), (["state/a.json"], []))
        self.assertTrue(os.path.isfile(os.path.join(dst, "state", "a.json")))

    def test_refused(self):
        with open(os.path.join(self.repo, "files", "a", "1.0.0.json"), "w") as f:
            f.write('{"x": 2}\n')  # a published file changed
        cichanges.export(self.repo, self.out)
        self.assertRaises(cichanges.ChangesError, cichanges.apply, self.out, self.clone())
        for path in ("ci/commit.sh", ".github/workflows/x.yml", "state/../x.json", "tools/validator/gw_catalog.py"):
            shutil.rmtree(self.out, ignore_errors=True)
            os.makedirs(os.path.join(self.out, "files"))
            target = os.path.join(self.out, "files", *path.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w") as f:
                f.write("{}")
            base = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True,
                                  check=True).stdout.decode().strip()
            strictjson.write_file(os.path.join(self.out, "changes.json"),
                                  {"schema": 1, "base": base, "changed": [path], "deleted": []})
            self.assertRaises(cichanges.ChangesError, cichanges.apply, self.out, self.clone())

    def hand_over(self, path, obj):
        shutil.rmtree(self.out, ignore_errors=True)
        target = os.path.join(self.out, "files", *path.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        strictjson.write_file(target, obj)
        base = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True,
                              check=True).stdout.decode().strip()
        strictjson.write_file(os.path.join(self.out, "changes.json"),
                              {"schema": 1, "base": base, "changed": [path], "deleted": []})

    def test_configuration_is_not_taken_from_the_hand_over(self):
        """A job taken over by a package cannot redirect the downloads (the templates, the repo of another addon) or
        drop what moderation.ltx decides"""
        cfg = IndexConfig(self.repo)
        good = {"schema": 1, "generated_at": "2026-11-06T00:00:00Z", "source": cfg.source, "templates": cfg.templates,
                "mirrors": cfg.mirrors, "revoked_sha256": [], "addons": []}
        self.hand_over("index.json", good)
        cichanges.apply(self.out, self.clone())
        bad_templates = dict(cfg.templates, asset="https://evil.example/{name}")
        for bad in (dict(good, templates=bad_templates), dict(good, mirrors=["https://evil.example/{name}"]),
                    dict(good, addons=[{"id": "a", "repo": "evil/a", "status": "listed"}]),
                    dict(good, addons=[{"id": "a", "repo": "Global-War-Addons/b", "status": "listed"}])):
            self.hand_over("index.json", bad)
            self.assertRaises(cichanges.ChangesError, cichanges.apply, self.out, self.clone())
        self.hand_over("state/a.json", {"id": "a", "repo": "evil/a"})
        self.assertRaises(cichanges.ChangesError, cichanges.apply, self.out, self.clone())
        self.hand_over("state/a.json", {"id": "b", "repo": "Global-War-Addons/b"})
        self.assertRaises(cichanges.ChangesError, cichanges.apply, self.out, self.clone())
        # moderation.ltx: a revoked sha256 and a blocked addon stay as it says
        with open(os.path.join(self.repo, "moderation.ltx"), "a", encoding="utf-8") as f:
            f.write("\n[revoked_sha256]\n%s = malware\n\n[blocked]\na = spam\n" % ("ab" * 32))
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-am", "moderation")
        self.hand_over("index.json", good)
        self.assertRaises(cichanges.ChangesError, cichanges.apply, self.out, self.clone())
        listed = [{"id": "a", "repo": "Global-War-Addons/a", "status": "listed"}]
        self.hand_over("index.json", dict(good, revoked_sha256=["ab" * 32], addons=listed))
        self.assertRaises(cichanges.ChangesError, cichanges.apply, self.out, self.clone())
        self.hand_over("index.json", dict(good, revoked_sha256=["ab" * 32], addons=[dict(listed[0], status="blocked")]))
        cichanges.apply(self.out, self.clone())


class WorkflowsTest(unittest.TestCase):
    """F1, F4, F9, F12: the policies of the workflows"""

    def workflows(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not here")
        root = os.path.normpath(os.path.join(util.VALIDATOR, ".."))
        out = {}
        for sub in ("index", "template"):
            d = os.path.join(root, sub, ".github", "workflows")
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    text = f.read()
                out["%s/%s" % (sub, fn)] = (text, yaml.safe_load(text))
        if not out:
            self.skipTest("no workflows here")
        return out

    def test_policies(self):
        import re
        for name, (text, doc) in self.workflows().items():
            for line in text.splitlines():
                if "uses:" in line:
                    # owner/repo or owner/repo/path (actions/cache/restore) at a commit SHA with its version
                    self.assertRegex(line, r"uses: [\w.-]+/[\w.-]+(?:/[\w.-]+)?@[0-9a-f]{40} # v\d+\.\d+\.\d+$", name)
            # decision 63: no App "dispatch" anywhere (no secret in the addon repos, no bot that starts index runs)
            self.assertNotIn("CATALOG_DISPATCH", text, name)
            if not name.startswith("index/"):
                continue
            for job_name, job in doc["jobs"].items():
                steps = json.dumps(job.get("steps", []))
                # the key of the catalog App is a secret of an environment restricted to the default branch
                if "CATALOG_APP_PRIVATE_KEY" in steps:
                    self.assertIn(job.get("environment"), ("catalog-admin", "catalog-publish"),
                                  "%s %s" % (name, job_name))
                # the jobs that read the packages hold no key of the App that writes
                if "gw_merge" in steps or "build-index \"${args" in steps or "recompute-deltas" in steps:
                    self.assertNotIn("CATALOG_APP_PRIVATE_KEY", steps, "%s %s" % (name, job_name))
                    self.assertIn("persist-credentials", steps, "%s %s" % (name, job_name))
                    self.assertIsNone(job.get("environment"), "%s %s" % (name, job_name))
            if "gw_merge" in text:
                # gw_merge only through ci/fetch_gw_merge.sh (https, the sha256 of the release or of vars.GW_MERGE_SHA256)
                # and its algo shown in the Summary (decision of the developer 2026-11-14)
                self.assertIn("GW_MERGE_SHA256", text, name)
                self.assertIn("ci/fetch_gw_merge.sh", text, name)
                self.assertIn("ci/gw_merge_info.py", text, name)
                self.assertNotIn("curl", text, name)
        text, doc = self.workflows()["index/recompute-deltas.yml"]
        self.assertNotIn("gw_merge_url", text)
        self.assertNotIn("inputs.gw", text)
        text, _ = self.workflows()["template/release.yml"]
        self.assertNotIn("edited", re.findall(r"types: \[[^\]]*\]", text)[0])
        self.assertNotIn("create-github-app-token", text)
        self.assertNotIn("gh workflow run", text)

    def test_build_index_polls(self):
        """Decision 63: every 15 minutes (minute 0 without the HTTP cache), by hand for moderators, no dispatch from the
        addon repos; publish only on the default branch, with the asset check of apply-changes"""
        text, doc = self.workflows()["index/build-index.yml"]
        on = doc.get("on", doc.get(True))  # YAML 1.1 reads the key "on" as True
        self.assertEqual(sorted(c["cron"] for c in on["schedule"]), ["0 * * * *", "15,30,45 * * * *"])
        self.assertIn("workflow_dispatch", on)
        self.assertNotIn("repository_dispatch", on)
        self.assertNotIn("client_payload", text)
        publish = doc["jobs"]["publish"]
        self.assertEqual(publish["environment"], "catalog-publish")
        self.assertIn("default_branch", publish["if"])
        self.assertIn("needs.check.outputs.work", publish["if"])
        steps = json.dumps(publish["steps"])
        self.assertIn("apply-changes", steps)
        self.assertNotIn("--no-verify-assets", steps)
        self.assertIn("--http-cache", json.dumps(doc["jobs"]["check"]["steps"]))

    def test_review_takes_only_issues_of_the_bot(self):
        text, doc = self.workflows()["index/review.yml"]
        self.assertIn("github.event.issue.user.login", text)
        self.assertIn("steps.app.outputs.app-slug", text)
        self.assertIn('--issue "$ISSUE"', text)

    def test_codeowners(self):
        path = os.path.join(util.INDEX_SKELETON, ".github", "CODEOWNERS")
        if not os.path.isfile(path):
            self.skipTest("no CODEOWNERS here")
        with open(path, encoding="utf-8") as f:
            owned = {line.split()[0] for line in f if line.strip() and not line.startswith("#")}
        self.assertTrue({"/.github/", "/tools/", "/ci/"} <= owned, owned)


if __name__ == "__main__":
    unittest.main()
