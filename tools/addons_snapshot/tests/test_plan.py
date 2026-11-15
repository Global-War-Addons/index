"""The merge decisions of gw_merge plan as the tools take them (gwplan.py, catalog_merge.Planner; decision 63): the
texts of the layers of an addon (the sets of the slots, [private], '@' folders, hidden and '.' entries, ignore_name, the
patch files mod_*), its patches and what a compatibility patch decides on a pair (applied, stale, not_a_side). The
Python ports of these rules are removed (gw_merge ships with the game): the tests check the answers of the plan.

    GW_MERGE=<gw_merge> python -m unittest discover -s GlobalWar/utils/addons_snapshot/tests

Skipped without gw_merge (or with one too old: no plan --all-files). The addons: GlobalWar/addons/tests (test_merge_*,
test_slot, test_patch_*, test_conflict_*) and synthetic ones written here.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

import catalog_merge  # noqa: E402
import gwmerge  # noqa: E402
import gwplan  # noqa: E402

GW = os.path.normpath(os.path.join(TOOLS, "..", "..", ".."))
TEST_ADDONS = os.path.join(GW, "GlobalWar", "addons", "tests")
GAMEDATA = os.path.join(GW, "GlobalWar", "gamedata")
TAG = "0.5.5-patch4"

# The files of the base: the real texts the test addons replace (merged by the pairs) and names only (the targets of
# the patch files)
REAL = ["scripts/ranks.script", "scripts/bind_faction.script", "configs/ai_tweaks/xr_help_wounded.ltx",
        "configs/text/eng/ui_st_loadscreen.xml", "configs/text/rus/ui_st_loadscreen.xml"]
NAMED = ["configs/system.ltx", "configs/ui/ui_mm_main.xml", "configs/ui/ui_mm_main_16.xml", "configs/x.ltx",
         "configs/priv.ltx", "configs/test_conflict/probe.ltx"]
FIXTURES = ["test_merge_a", "test_merge_b", "test_merge_c", "test_merge_d", "test_merge_bad", "test_merge_old",
            "test_merge_patch", "test_slot", "test_patch_a", "test_patch_b", "test_conflict_a", "test_conflict_b"]


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def hide(path):
    """The hidden attribute of Windows (the file system of the engine there does not mount such an entry)"""
    import ctypes
    if not ctypes.windll.kernel32.SetFileAttributesW(path, 2):
        raise OSError("cannot hide %s" % path)


class PlanBase:
    @classmethod
    def setUpClass(cls):
        cls.exe = gwmerge.find_gw_merge()
        if not cls.exe or gwplan.unavailable(cls.exe):
            raise unittest.SkipTest("gw_merge is not found or too old ($GW_MERGE): %s" %
                                    (gwmerge.too_old(cls.exe) if cls.exe else "not found"))
        cls.lua = gwmerge.find_luajit(cls.exe)
        cls.work = tempfile.mkdtemp(prefix="gw_plan_test_")
        gamedata = os.path.join(cls.work, "gamedata")
        for rel in REAL:
            target = os.path.join(gamedata, *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(os.path.join(GAMEDATA, *rel.split("/")), target)
        for rel in NAMED:
            write(os.path.join(gamedata, *rel.split("/")), "[s]\nk = 1\n" if rel.endswith(".ltx") else "<w/>\n")
        out = os.path.join(cls.work, "snap")
        r = subprocess.run([sys.executable, os.path.join(TOOLS, "gw_snapshot.py"), "build", gamedata, "--tag", TAG,
                            "--out", out, "--no-auto", "--no-hash"], capture_output=True)
        if r.returncode:
            raise AssertionError(r.stderr.decode("utf-8", "replace"))
        cls.snap = os.path.join(out, "base_snapshot.json.gz")
        cls.base_names = {rel.replace("/", "\\") for rel in REAL + NAMED}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def planner(self):
        return catalog_merge.Planner(self.exe, self.base_names, TAG)


def manifest_patches(addon):
    """[addon] patches of addon.ltx as written (the ids, ASCII lower case)"""
    sections = catalog_merge._manifest_sections(addon)
    value = next((v for k, v in sections.get("addon", []) if k.lower() == "patches"), "")
    return [p.strip().lower() for p in value.split(",") if p.strip()]


class LayerTextsTest(PlanBase, unittest.TestCase):
    """catalog_merge.addon_layer_texts by gw_merge plan"""

    def texts(self, addon):
        texts, warnings = catalog_merge.addon_layer_texts(addon, self.base_names, self.planner())
        for vpath, path in texts.items():
            # a file of the addon itself (the plan names the path in the case of its index: lower case on Windows)
            self.assertTrue(os.path.isfile(path), (vpath, path))
            self.assertTrue(os.path.normcase(os.path.abspath(path)).startswith(os.path.normcase(addon)), path)
        return texts, warnings

    def test_fixtures(self):
        for name in FIXTURES:
            addon = os.path.join(TEST_ADDONS, name)
            self.texts(addon)
            self.assertEqual(self.planner().patches(addon), manifest_patches(addon), name)
        texts, warnings = self.texts(os.path.join(TEST_ADDONS, "test_slot"))
        # the patches of ui_mm_main(_16).xml in the set are left out, the string tables of both layers are taken
        self.assertEqual(sorted(texts), ["configs\\text\\eng\\st_test_slot.xml", "configs\\text\\eng\\st_test_slot_set.xml",
                                         "configs\\text\\rus\\st_test_slot.xml", "configs\\text\\rus\\st_test_slot_set.xml",
                                         "scripts\\test_slot_main.script"])
        self.assertEqual(warnings, [])

    def synthetic(self, folder):
        windows = os.name == "nt"
        manifest = ("[addon]\nid = synth\nversion = 1.0.0\ntype = content\ndepends = gw_lib >= 1.0, other_lib\n"
                    "[slot.ui.menu]\nfiles = slots\\menu\\\n[slot.ui.hud]\nfiles = slots/hud\n"
                    "[private]\nconfigs\\priv.ltx\n/configs/wild/*.ltx\n" +
                    ("Configs\\Upper\\*.XML\n" if windows else ""))
        write(os.path.join(folder, "addon.ltx"), manifest)
        files = {
            "gamedata/configs/x.ltx": "[s]\nk = 2\n",                  # public, a base file
            "gamedata/configs/priv.ltx": "[s]\n",                       # [private]: no side
            "gamedata/configs/wild/a.ltx": "[s]\n",                     # [private] by a wildcard
            "gamedata/configs/mod_system_synth.ltx": "![s]\n",          # a DLTX patch: the base has system.ltx
            "gamedata/configs/mod_orphan_x.ltx": "[s]\n",               # no target: a plain file
            "gamedata/configs/new.ltx": "[s]\n",                        # a new file of the addon
            "gamedata/configs/mod_new_synth.ltx": "[s]\n",              # a patch of the file of the addon
            "gamedata/@alias/a.ltx": "[s]\n",                           # the alias roots are not mounted
            "gamedata/configs/.dot.ltx": "[s]\n",                       # '.*': hidden on POSIX, mounted on Windows
            "gamedata/.dotdir/b.ltx": "[s]\n",
            "gamedata/.svn/c.ltx": "[s]\n",                             # ignore_name
            "gamedata/configs/hidden.ltx": "[s]\n",                     # the hidden attribute (Windows)
            "gamedata/scripts/s.lua": "x = 1\n",
            "gamedata/textures/t.dds": "dds",                           # not a text
            "slots/menu/gamedata/configs/x.ltx": "[s]\nk = 3\n",        # in gamedata\ too: warned
            "slots/menu/gamedata/configs/ui/menu.xml": "<w/>\n",        # one set
            "slots/menu/gamedata/configs/ui/two.xml": "<w/>\n",         # two sets: warned, left out
            "slots/hud/gamedata/configs/ui/two.xml": "<w/>\n",
            "slots/menu/gamedata/configs/ui/mod_ui_mm_main_synth.xml": "<w/>\n",  # an XML patch in a set
        }
        if windows:
            files.update({"gamedata/Configs/Upper/u.xml": "<w/>\n",     # [private] in another case
                          "gamedata/Scripts/Big.SCRIPT": "x = 1\n"})    # KindOfName in any case
        for rel, text in files.items():
            write(os.path.join(folder, *rel.split("/")), text)
        if windows:
            hide(os.path.join(folder, "gamedata", "configs", "hidden.ltx"))
        return folder

    def test_synthetic(self):
        addon = self.synthetic(os.path.join(self.work, "synth"))
        texts, warnings = self.texts(addon)
        expected = ["configs\\mod_orphan_x.ltx", "configs\\new.ltx", "configs\\ui\\menu.xml", "configs\\x.ltx",
                    "scripts\\s.lua"]
        if os.name == "nt":
            expected += ["configs\\.dot.ltx", ".dotdir\\b.ltx", "scripts\\big.script"]
        self.assertEqual(sorted(texts), sorted(expected))
        self.assertEqual(len(warnings), 2, warnings)
        # gamedata\ wins over the set (the state without the provider chosen)
        self.assertNotIn("slots", texts["configs\\x.ltx"].replace("\\", "/").split("/"))

    def test_private_target(self):
        """A [private] file is not at its public name in the index of the engine (only under @<id>\\): it is not the
        target of a patch file - mod_secret_x.ltx is a plain file of the addon"""
        addon = os.path.join(self.work, "priv_target")
        write(os.path.join(addon, "addon.ltx"), "[addon]\nid = priv_target\nversion = 1.0.0\n[private]\n"
                                                "configs\\secret.ltx\n")
        write(os.path.join(addon, "gamedata", "configs", "secret.ltx"), "[s]\n")
        write(os.path.join(addon, "gamedata", "configs", "mod_secret_x.ltx"), "[s]\n")
        write(os.path.join(addon, "gamedata", "configs", "open.ltx"), "[s]\n")
        write(os.path.join(addon, "gamedata", "configs", "mod_open_x.ltx"), "[s]\n")
        texts, _ = self.texts(addon)
        self.assertEqual(sorted(texts), ["configs\\mod_secret_x.ltx", "configs\\open.ltx"])

    def test_all_files(self):
        """plan --all-files: a record of every text file of the addon alone, the note a file of the merge would have"""
        addon = self.synthetic(os.path.join(self.work, "synth_all"))
        base = gwplan.write_base_list(os.path.join(self.work, "all_base.txt"), self.base_names, TAG)
        stubs = []
        for n, dep in enumerate(("gw_lib", "other_lib")):
            stubs.append(write(os.path.join(self.work, "all_stub%d" % n, "addon.ltx"),
                               "[addon]\nid = %s\n" % dep)[:-len("addon.ltx") - 1])
        plan = gwplan.run(self.exe, stubs + [addon], base_list=base, all_files=True)
        self.assertIs(plan["all_files"], True)
        files = {f["file"]: f for f in plan["files"]}
        windows = os.name == "nt"
        expected = {"configs\\x.ltx": "one_addon", "configs\\mod_system_synth.ltx": "patch_file",
                    "configs\\mod_orphan_x.ltx": "no_base", "configs\\new.ltx": "no_base",
                    "configs\\mod_new_synth.ltx": "patch_file", "scripts\\s.lua": "no_base"}
        if windows:
            expected.update({"configs\\.dot.ltx": "no_base", ".dotdir\\b.ltx": "no_base",
                             "scripts\\big.script": "no_base"})
        self.assertEqual({k: v["note"] for k, v in files.items()}, expected)
        for f in files.values():
            self.assertEqual((f["merge"], f["extra"], f["status"]), (False, True, "skipped"), f["file"])
            self.assertEqual([(s["addon"], s["layer"]) for s in f["sides"]], [("synth", "addon:synth")], f["file"])
        # without --all-files the plan has no record of them (one addon, no delta)
        self.assertEqual(gwplan.run(self.exe, stubs + [addon], base_list=base)["files"], [])

    def test_slot_outside_addon(self):
        """[slot.*] files = ..\\outside\\ (a drive, a root): the engine ignores the provider (addon_slots.cpp
        SlotFilesValid) - the plan takes no file of that folder, it lies outside the package (the tools name the slots
        to the plan as they are: the plan decides, there is no Python check of the folder any more)"""
        addon = os.path.join(self.work, "trav", "addon")
        write(os.path.join(addon, "addon.ltx"), "[addon]\nid = trav\nversion = 1.0.0\n[slot.ui.up]\n"
                                                "files = ..\\outside\\\n[slot.ui.abs]\nfiles = %s\n" %
              os.path.join(self.work, "trav", "outside"))
        write(os.path.join(addon, "gamedata", "configs", "new.ltx"), "[s]\n")
        write(os.path.join(self.work, "trav", "outside", "gamedata", "configs", "x.ltx"), "[s]\nsecret = 1\n")
        texts, warnings = self.texts(addon)
        self.assertEqual(sorted(texts), ["configs\\new.ltx"])
        self.assertEqual(gwplan.addon_id_and_slots(addon), ("trav", ["ui.up", "ui.abs"]))
        self.assertFalse(any("outside" in path.replace("\\", "/").split("/") for path in texts.values()), texts)

    @unittest.skipUnless(os.name == "nt", "the index takes 2 * MAX_PATH bytes on Windows (2 * PATH_MAX on POSIX: 8192 on Linux)")
    def test_name_too_long(self):
        """A name too long for the index (string_path, 520 bytes on Windows) is skipped with the warning of the engine
        (LocatorAPI layer_walk); the walk of the layer goes on (it ended on the first error before)"""
        addon = os.path.join(self.work, "long_names")
        write(os.path.join(addon, "addon.ltx"), "[addon]\nid = long_names\n")
        gamedata = os.path.join(addon, "gamedata")
        folder = os.path.join(gamedata, "a" * max(1, 230 - len(gamedata)))  # a folder of ~230 chars: listed
        leaf = "\u0434" * 150 + ".ltx"  # 150 Cyrillic chars: 300 bytes of UTF-8, the path is over 520 bytes
        os.makedirs(folder, exist_ok=True)
        with open("\\\\?\\" + os.path.join(folder, leaf), "w", encoding="utf-8") as f:
            f.write("[s]\n")
        write(os.path.join(folder, "short.ltx"), "[s]\n")
        write(os.path.join(gamedata, "zz", "after.ltx"), "[s]\n")
        base = gwplan.write_base_list(os.path.join(self.work, "long_base.txt"), self.base_names, TAG)
        plan = gwplan.run(self.exe, [addon], base_list=base, all_files=True)
        rel = os.path.relpath(folder, gamedata).lower()
        self.assertEqual(sorted(f["file"] for f in plan["files"]), [rel + "\\short.ltx", "zz\\after.ltx"])
        self.assertEqual([w for w in plan["warnings"] if "name too long" in w],
                         ["FS: layer 'addon:long_names': name too long (520 bytes and more), skipped [%s]" %
                          os.path.join(folder, leaf)])
        texts = gwplan.addon_texts(self.exe, addon, self.base_names, TAG)
        self.assertEqual(sorted(texts.layers["gamedata"]), [rel + "\\short.ltx", "zz\\after.ltx"])
        self.assertTrue(any("name too long" in w for w in texts.warnings), texts.warnings)

    def test_location_takes_no_text(self):
        """A location addon is a pack: the engine serves its files whole, the plan merges none"""
        addon = os.path.join(self.work, "loc")
        write(os.path.join(addon, "addon.ltx"), "[addon]\nid = loc\nversion = 1.0.0\ntype = location\n")
        write(os.path.join(addon, "gamedata", "configs", "x.ltx"), "[s]\n")
        # a pack only with its all.spawn (InitLevelPacks): without it the content is common, as of any addon
        write(os.path.join(addon, "gamedata", "spawns", "all.spawn"), "spawn")
        texts, warnings = catalog_merge.addon_layer_texts(addon, self.base_names, self.planner())
        self.assertEqual(texts, {})
        self.assertTrue(any("location" in w for w in warnings), warnings)


class PatchTest(PlanBase, unittest.TestCase):
    """What the patch of a pair decides (find_pairs): gw_merge plan --merge"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gw_plan_patch_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def patch_copy(self, name, patches=None, stale=False):
        folder = os.path.join(self.tmp, name)
        shutil.copytree(os.path.join(TEST_ADDONS, "test_merge_patch"), folder)
        text = "[addon]\nid = %s\nversion = 0.1.0\ntype = content\npatches = %s\n" % (
            name, patches or "test_merge_a, test_merge_d")
        write(os.path.join(folder, "addon.ltx"), text)
        if stale:
            write(os.path.join(folder, "merge", "targets.ltx"), "[scripts\\ranks.script]\ntest_merge_a = %s\n" %
                  ("0" * 64))
        return folder

    def pair(self, patch):
        addons = [os.path.join(TEST_ADDONS, n) for n in ("test_merge_a", "test_merge_d")] + [patch]
        plan = catalog_merge.find_pairs(self.exe, addons, self.snap, self.lua, planner=self.planner())
        found = [p for p in plan[0]["pairs"] if p["file"] == "scripts\\ranks.script"]
        self.assertEqual(len(found), 1, plan[0])
        return found[0]

    def test_applied(self):
        record = self.pair(os.path.join(TEST_ADDONS, "test_merge_patch"))
        self.assertTrue(record["patch_resolves"])
        self.assertNotIn("patch_stale", record)

    def test_stale(self):
        record = self.pair(self.patch_copy("patch_stale", stale=True))
        self.assertFalse(record["patch_resolves"])
        self.assertEqual(record["patch_stale"], ["test_merge_a"])

    def test_not_a_side(self):
        """patches = a, d, x: x does not replace the file - the engine does not apply the patch (review B4)"""
        record = self.pair(self.patch_copy("patch_wide", "test_merge_a, test_merge_d, test_merge_x"))
        self.assertEqual(record["patch"].split("@")[0], "patch_wide")
        self.assertFalse(record["patch_resolves"])
        self.assertNotIn("patch_stale", record)


if __name__ == "__main__":
    unittest.main()
