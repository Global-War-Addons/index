"""Packages: good and bad zips / folders, parts, the samples of GlobalWar/addons/tests, pack, the delta."""

import os
import shutil
import unittest
import zipfile

import util
from gwcat import package, strictjson
from gwcat.report import Report
from gwcat.validate import SnapshotStore, validate_version

MANIFEST = util.GOOD_MANIFEST.encode("utf-8")
SCRIPT = util.GOOD_SCRIPT.encode("utf-8")


def run_zips(paths, **kw):
    return validate_version({"kind": "zips", "paths": paths}, snapshot_check=False, **kw)


class ZipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = util.TempDir()

    def tearDown(self):
        self.tmp.cleanup()

    def zip(self, name, entries, **kw):
        return util.make_zip(os.path.join(self.tmp.path, name), entries, **kw)

    def good_entries(self, **extra):
        e = {"addon.ltx": MANIFEST, "gamedata/scripts/my_addon_main.script": SCRIPT,
             "gamedata/configs/text/rus/st_my_addon.xml": "<string_table/>", "README.md": "x"}
        e.update(extra)
        return e

    def test_good(self):
        rep, res = run_zips([self.zip("my_addon-1.2.0.zip", self.good_entries())], expect_id="my_addon",
                            expect_version="1.2.0")
        self.assertTrue(rep.ok, rep.to_text())
        self.assertEqual([f["path"] for f in res["files"]][0], "README.md")
        self.assertIsNone(res["merge"])

    def test_stored_and_unicode_names(self):
        rep, res = run_zips([self.zip("a.zip", self.good_entries(**{"gamedata/textures/ж.dds": b"d"}),
                                      method=zipfile.ZIP_STORED)])
        self.assertTrue(rep.ok, rep.to_text())

    def test_bad_layouts(self):
        cases = {
            "package_nested": {"my_addon/addon.ltx": MANIFEST},
            "package_executable": self.good_entries(**{"gamedata/x.dll": b"MZ"}),
            "package_bin": self.good_entries(**{"bin/windows/x64/a.txt": b""}),
            "package_catalog": self.good_entries(**{"catalog/catalog.ltx": b""}),
            "package_db_v1": self.good_entries(**{"my_addon.db": b""}),
            "package_unknown_folder": self.good_entries(**{"textures/a.dds": b""}),
            "package_unknown_file": self.good_entries(**{"notes.txt": b""}),
            "package_bad_path": self.good_entries(**{"gamedata/../../evil.txt": b""}),
            "package_absolute_path": self.good_entries(**{"/etc/passwd": b""}),
            "package_windows_name": self.good_entries(**{"gamedata/con.ltx": b""}),
            "package_case_collision": self.good_entries(**{"gamedata/Scripts/MY_ADDON_MAIN.script": b""}),
            "package_foreign_namespace": self.good_entries(**{"gamedata/textures/addons/other/a.dds": b""}),
            "lua_entry_missing": {"addon.ltx": MANIFEST},
            "caps_undeclared": self.good_entries(**{"gamedata/scripts/my_addon_x.script": b'os.execute("rm")'}),
            "ref_without_depends": self.good_entries(**{"gamedata/configs/my_addon.ltx": b"tex = @other\\wpn\\a"}),
        }
        for code, entries in cases.items():
            rep, _ = run_zips([self.zip("c_%s.zip" % code, entries)])
            self.assertIn(code, rep.codes("error"), "%s: %s" % (code, rep.to_text()))

    def test_backslash_name(self):
        """zipfile on Windows cannot write a backslash in a name: the bytes of a written zip are patched"""
        z = self.zip("bs.zip", self.good_entries(**{"gamedata/zz.ltx": b"x"}))
        with open(z, "rb") as f:
            data = f.read()
        with open(z, "wb") as f:
            f.write(data.replace(b"gamedata/zz.ltx", b"gamedata\\zz.ltx"))
        rep, _ = run_zips([z])
        self.assertIn("package_backslash", rep.codes("error"), rep.to_text())

    def test_symlink_duplicate_and_compression(self):
        z = self.zip("s.zip", list(self.good_entries().items()) +
                     [("gamedata/link", "/etc/passwd", {"attr": (0o120777 << 16)})])
        rep, _ = run_zips([z])
        self.assertIn("package_symlink", rep.codes("error"))
        z = self.zip("d.zip", list(self.good_entries().items()) + [("README.md", "again")])
        rep, _ = run_zips([z])
        self.assertIn("package_duplicate_entry", rep.codes("error"))
        z = self.zip("b.zip", self.good_entries(), method=zipfile.ZIP_BZIP2)
        rep, _ = run_zips([z])
        self.assertIn("package_compression", rep.codes("error"))
        with open(os.path.join(self.tmp.path, "n.zip"), "wb") as f:
            f.write(b"not a zip")
        rep, _ = run_zips([os.path.join(self.tmp.path, "n.zip")])
        self.assertIn("package_bad_zip", rep.codes("error"))

    def test_broken_entries_are_errors(self):
        """An encrypted addon.ltx and a bad CRC are errors of the package, not exceptions that stop the index CI"""
        z = self.zip("e.zip", self.good_entries())
        with open(z, "rb") as f:
            data = bytearray(f.read())
        data[data.find(b"PK\x03\x04") + 6] |= 1  # the "encrypted" flag of the first entry (addon.ltx)
        data[data.find(b"PK\x01\x02") + 8] |= 1
        with open(z, "wb") as f:
            f.write(data)
        rep, _ = run_zips([z])
        self.assertIn("package_encrypted", rep.codes("error"), rep.to_text())
        z = self.zip("c.zip", self.good_entries(), method=zipfile.ZIP_STORED)
        with open(z, "rb") as f:
            data = f.read()
        with open(z, "wb") as f:
            f.write(data.replace(b"utils.debug", b"Xtils.debug"))
        rep, _ = run_zips([z])
        self.assertIn("package_bad_zip", rep.codes("error"), rep.to_text())
        self.assertNotIn("package_unpack_failed", rep.codes("error"))  # not retried at every run

    def test_parts(self):
        p1 = self.zip("my_addon-1.2.0.part1.zip", {"addon.ltx": MANIFEST, "README.md": "x"})
        p2 = self.zip("my_addon-1.2.0.part2.zip", {"gamedata/scripts/my_addon_main.script": SCRIPT})
        rep, res = run_zips([p1, p2], expect_id="my_addon", expect_version="1.2.0")
        self.assertTrue(rep.ok, rep.to_text())
        self.assertEqual(len(res["files"]), 3)
        p3 = self.zip("my_addon-1.2.0.part3.zip", {"README.md": "again"})
        rep, _ = run_zips([p1, p2, p3])
        self.assertIn("package_parts_overlap", rep.codes("error"))
        rep, _ = run_zips([p2, p1])
        self.assertIn("package_manifest_not_in_part1", rep.codes("error"))

    def test_classify_assets(self):
        r = Report("a")
        parts = package.classify_assets(["a-1.0.0.part2.zip", "a-1.0.0.part1.zip", "a-1.0.0.part10.zip",
                                         "a-1.0.0-bin-windows-x64.zip", "notes.txt"], "a", "1.0.0", r)
        self.assertEqual(parts, [])
        self.assertIn("asset_parts_gap", r.codes("error"))
        self.assertIn("asset_plugin_build_v1", r.codes("warning"))
        r = Report("a")
        self.assertEqual(package.classify_assets(["a-1.0.0.zip", "a-1.0.0.part1.zip"], "a", "1.0.0", r), [])
        self.assertIn("asset_whole_and_parts", r.codes("error"))
        r = Report("a")
        self.assertEqual(package.classify_assets(["a-1.0.0.part%d.zip" % i for i in (2, 1, 3)], "a", "1.0.0", r),
                         ["a-1.0.0.part1.zip", "a-1.0.0.part2.zip", "a-1.0.0.part3.zip"])
        r = Report("a")
        package.classify_assets(["b-1.0.0.zip"], "a", "1.0.0", r)
        self.assertIn("asset_no_package", r.codes("error"))

    def test_pack_roundtrip(self):
        folder = util.make_addon(self.tmp, files={"gamedata/textures/my_addon/a.dds": b"x" * 5000,
                                                  "gamedata/textures/my_addon/b.dds": b"y" * 5000})
        files = package.folder_files(folder, Report("f"))
        out = package.make_zips(folder, files, os.path.join(self.tmp.path, "dist"), "my_addon", "1.2.0", 6000)
        self.assertGreater(len(out), 1)
        with zipfile.ZipFile(out[0]) as zf:
            self.assertEqual(zf.namelist()[0], "addon.ltx")
        rep, res = run_zips(out, expect_id="my_addon", expect_version="1.2.0")
        self.assertTrue(rep.ok, rep.to_text())
        self.assertEqual(sorted(f["path"] for f in res["files"]), sorted(files))


@unittest.skipUnless(os.path.isdir(util.SAMPLES), "GlobalWar/addons/tests is not here (an index repo checkout)")
class SamplesTest(unittest.TestCase):
    """The test addons of the engine (GlobalWar/addons/tests) as catalog packages"""

    def setUp(self):
        self.tmp = util.TempDir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_samples_but_merge_old_pass(self):
        for name in sorted(os.listdir(util.SAMPLES)):
            path = os.path.join(util.SAMPLES, name)
            if not os.path.isfile(os.path.join(path, "addon.ltx")):
                continue
            rep, _ = validate_version({"kind": "folder", "path": path}, snapshot_check=False)
            if name == "test_merge_old":
                # s9/ (the old base of the test) is not a folder of a package; .gw_merge/ is the launcher's: a working
                # folder leaves it out (as the author mode of the launcher), a zip has it with a warning
                self.assertEqual(rep.codes("error"), ["package_unknown_folder"], rep.to_text())
                self.assertIn("package_excluded", rep.codes("note"))
            else:
                self.assertTrue(rep.ok, "%s: %s" % (name, rep.to_text()))

    def staged_merge_old(self):
        dst = os.path.join(self.tmp.path, "test_merge_old")
        src = os.path.join(util.SAMPLES, "test_merge_old")
        os.makedirs(dst)
        shutil.copyfile(os.path.join(src, "addon.ltx"), os.path.join(dst, "addon.ltx"))
        shutil.copytree(os.path.join(src, "gamedata"), os.path.join(dst, "gamedata"))
        return dst

    def test_merge_old_package_and_delta(self):
        """The delta of the catalog for test_merge_old: a snapshot of its old base (s9/old_base, v0.0.1) made by
        gw_snapshot.py build, the delta by gw_snapshot.py delta (gw_merge) -> <ver>.merge.json with the files and kinds
        of the .gw_merge/delta.json of the test, and the reference in <ver>.json"""
        folder = self.staged_merge_old()
        dist = os.path.join(self.tmp.path, "dist")
        files = package.folder_files(folder, Report("f"))
        zips = package.make_zips(folder, files, dist, "test_merge_old", "0.1.0")
        rep, res = run_zips(zips, expect_id="test_merge_old", expect_version="0.1.0")
        self.assertTrue(rep.ok, rep.to_text())
        self.assertIn("no_snapshot", rep.codes("note"))

        gw_merge = util.find_gw_merge()
        if not gw_merge:
            self.skipTest("gw_merge is not found ($GW_MERGE, PATH, xray-16 build folders): the delta is not checked")
        from gwcat import snapshot_tools
        snaps = os.path.join(self.tmp.path, "snaps")
        code, out, err = snapshot_tools.run(["build", os.path.join(util.SAMPLES, "test_merge_old", "s9", "old_base"),
                                             "--tag", "v0.0.1", "--out", os.path.join(snaps, "v0.0.1"), "--no-auto"])
        self.assertEqual(code, 0, err)
        files_out = os.path.join(self.tmp.path, "files", "test_merge_old")
        rep, res = run_zips(zips, expect_id="test_merge_old", expect_version="0.1.0",
                            snapshots=SnapshotStore(snaps, latest="v0.0.1"), files_dir=files_out, gw_merge=gw_merge,
                            require_delta=True)
        self.assertTrue(rep.ok, rep.to_text())
        ref = res["merge"]
        self.assertEqual(ref["file"], "0.1.0.merge.json")
        path = os.path.join(files_out, "0.1.0.merge.json")
        self.assertEqual(ref["sha256"], strictjson.sha256_file(path))
        self.assertEqual(ref["size"], os.path.getsize(path))
        delta = strictjson.load_file(path)
        self.assertEqual(ref["algo"], delta["algo"])
        self.assertEqual(delta["game"], "v0.0.1")
        self.assertEqual(sorted(delta["files"]), ["configs\\ai_tweaks\\xr_help_wounded.ltx",
                                                  "configs\\text\\eng\\ui_st_loadscreen.xml",
                                                  "configs\\text\\rus\\ui_st_loadscreen.xml", "scripts\\ranks.script"])
        # structural, not byte for byte: the atoms gw_merge finds may change with the library (Diff.cpp, Delta.cpp);
        # the files of the delta and their kinds are the ones of the delta of the test addon
        from gwcat import delta as delta_mod
        self.assertIsNone(delta_mod.check_delta_object(delta))
        sample = strictjson.load_file(os.path.join(util.SAMPLES, "test_merge_old", ".gw_merge", "delta.json"))
        self.assertEqual({k: v["kind"] for k, v in delta["files"].items()},
                         {k: v["kind"] for k, v in sample["files"].items()})
        self.assertTrue(all(v["atoms"] for v in delta["files"].values()), delta)

    def test_merge_old_without_snapshot_is_retried(self):
        folder = self.staged_merge_old()
        snaps = os.path.join(self.tmp.path, "snaps_empty")
        os.makedirs(snaps)
        # game_version v0.0.1, no snapshot of it; the latest one knows the replaced files
        if not util.find_gw_merge():
            self.skipTest("gw_merge is not found")
        from gwcat import snapshot_tools
        code, _, err = snapshot_tools.run(["build", os.path.join(util.SAMPLES, "test_merge_old", "s9", "old_base"),
                                           "--tag", "v9.9.9", "--out", os.path.join(snaps, "v9.9.9"), "--no-auto"])
        self.assertEqual(code, 0, err)
        rep, _ = validate_version({"kind": "folder", "path": folder}, snapshots=SnapshotStore(snaps, latest="v9.9.9"),
                                  require_delta=True, snapshot_check=False)
        self.assertEqual(rep.codes("error"), ["delta_unavailable"], rep.to_text())


if __name__ == "__main__":
    unittest.main()
