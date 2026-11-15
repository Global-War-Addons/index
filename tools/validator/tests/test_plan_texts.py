"""The merged texts of a package by gw_merge plan (resources.plan_texts; decisions 63, 64 item 4 - the Python ports of
these rules are removed, gw_merge decides): the replaced texts (content.check_replacements, the delta) and
"replaces.texts" (resources.replaces_record) on the test addons and on a synthetic one - the sets of the slots,
[private], '@' folders, hidden and '.' entries, ignore_name, the patch files mod_*. Without gw_merge: the check of a
package that replaces texts of the base skips only them (a warning) locally and fails in the CI of the index.

The plan tests are skipped without a gw_merge with the command plan ($GW_MERGE, PATH, xray-16 build folders).
"""

import os
import unittest

import util
from gwcat import content, manifest as manifest_mod, package, resources, snapshot_tools, validate
from gwcat.report import Report
from gwcat.validate import validate_version

from test_resources import write_snapshot

FIXTURES = ["test_merge_a", "test_merge_b", "test_merge_c", "test_merge_d", "test_merge_bad", "test_merge_old",
            "test_merge_patch", "test_slot", "test_patch_a", "test_patch_b", "test_conflict_a", "test_conflict_b"]
BASE = ["scripts\\ranks.script", "scripts\\bind_faction.script", "configs\\ai_tweaks\\xr_help_wounded.ltx",
        "configs\\text\\eng\\ui_st_loadscreen.xml", "configs\\text\\rus\\ui_st_loadscreen.xml", "configs\\system.ltx",
        "configs\\ui\\ui_mm_main.xml", "configs\\ui\\ui_mm_main_16.xml", "configs\\x.ltx", "configs\\priv.ltx",
        "configs\\test_conflict\\probe.ltx", "configs\\test_conflict\\probe_choice.ltx",
        "configs\\text\\rus\\st_test_patch_a.xml", "configs\\ui\\menu.xml", "configs\\ui\\two.xml",
        "configs\\mod_orphan_x.ltx", "configs\\.dot.ltx", ".dotdir\\b.ltx", "configs\\hidden.ltx", "scripts\\s.lua",
        "configs\\mod_system_synth.ltx", "configs\\wild\\a.ltx", "configs\\mod_new_synth.ltx"]
NO_TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "no_such_dir", "gw_merge.exe")


def plan_tools():
    try:
        gwmerge = snapshot_tools.import_module("gwmerge")
        gwplan = snapshot_tools.import_module("gwplan")
    except RuntimeError:
        return None
    exe = gwmerge.find_gw_merge()
    return exe if exe and not gwplan.unavailable(exe) else None


@unittest.skipUnless(plan_tools(), "no gw_merge with the command plan ($GW_MERGE)")
class PlanTextsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = util.TempDir()
        self.base = {v: [1, "0" * 64] for v in BASE}

    def tearDown(self):
        self.tmp.cleanup()

    def texts(self, root):
        """(the replaced texts, the "replaces" record) of the package folder root by gw_merge plan"""
        with open(os.path.join(root, "addon.ltx"), "rb") as f:
            m = manifest_mod.check_manifest(f.read(), Report("m"))
        self.assertIsNotNone(m, root)
        files = package.folder_files(root, Report("f"))
        plan, why = resources.plan_texts(root, self.base.keys())
        self.assertIsNone(why)
        layers, _, _ = resources.scan_layers(files, m)
        texts = content.check_replacements(m, Report("p"), self.base, plan)
        record = resources.replaces_record(layers, self.base, "v1", plan)
        # the plan names only files the layers serve with a name of the base and the extension of a text: the
        # pre-filter of validate.py never hides a merged text from it
        self.assertLessEqual(set(texts), set(resources.texts_to_decide(layers, self.base)), root)
        self.assertLessEqual(record["texts"], record["count"])
        return texts, record

    def test_fixtures(self):
        if not os.path.isdir(util.SAMPLES):
            self.skipTest("GlobalWar/addons/tests is not here")
        for name in FIXTURES:
            self.texts(os.path.join(util.SAMPLES, name))
        texts, record = self.texts(os.path.join(util.SAMPLES, "test_merge_a"))
        self.assertEqual(texts, ["configs\\ai_tweaks\\xr_help_wounded.ltx", "configs\\text\\eng\\ui_st_loadscreen.xml",
                                 "configs\\text\\rus\\ui_st_loadscreen.xml", "scripts\\ranks.script"])
        self.assertEqual((record["count"], record["texts"]), (4, 4))

    def test_synthetic(self):
        windows = os.name == "nt"
        manifest = ("[addon]\nid = synth\nversion = 1.0.0\ntype = content\napi_min = 0.1.0\n"
                    "depends = gw_lib >= 1.0\n[slot.ui.menu]\nfiles = slots\\menu\\\n[slot.ui.hud]\nfiles = slots\\hud\\\n"
                    "[private]\nconfigs\\priv.ltx\nconfigs\\wild\\*.ltx\n")
        files = {
            "gamedata/configs/x.ltx": "[s]\n", "gamedata/configs/priv.ltx": "[s]\n",
            "gamedata/configs/wild/a.ltx": "[s]\n", "gamedata/configs/mod_system_synth.ltx": "[s]\n",
            "gamedata/configs/mod_orphan_x.ltx": "[s]\n", "gamedata/configs/new.ltx": "[s]\n",
            "gamedata/configs/mod_new_synth.ltx": "[s]\n", "gamedata/@alias/configs/x.ltx": "[s]\n",
            "gamedata/configs/.dot.ltx": "[s]\n", "gamedata/.dotdir/b.ltx": "[s]\n", "gamedata/.svn/configs/x.ltx": "[s]\n",
            "gamedata/configs/hidden.ltx": "[s]\n", "gamedata/scripts/s.lua": "x = 1\n",
            "slots/menu/gamedata/configs/x.ltx": "[s]\n", "slots/menu/gamedata/configs/ui/menu.xml": "<w/>\n",
            "slots/menu/gamedata/configs/ui/two.xml": "<w/>\n", "slots/hud/gamedata/configs/ui/two.xml": "<w/>\n",
            "slots/menu/gamedata/configs/ui/mod_ui_mm_main_synth.xml": "<w/>\n",
        }
        root = util.make_addon(self.tmp, "synth", manifest, files)
        if windows:
            import ctypes
            self.assertTrue(ctypes.windll.kernel32.SetFileAttributesW(
                os.path.join(root, "gamedata", "configs", "hidden.ltx"), 2))
        texts, record = self.texts(root)
        # not merged: [private] (priv.ltx, wild\*), the patch files with a target (mod_system_synth, mod_new_synth,
        # mod_ui_mm_main_synth), '@' and ignore_name folders, the hidden file; '.' names - hidden on POSIX only
        expected = ["configs\\mod_orphan_x.ltx", "configs\\ui\\menu.xml", "configs\\ui\\two.xml", "configs\\x.ltx",
                    "scripts\\s.lua"] + ([".dotdir\\b.ltx", "configs\\.dot.ltx"] if windows else [])
        self.assertEqual(texts, sorted(expected))
        # x.ltx and two.xml are counted per layer that replaces them, as the files of "replaces"
        self.assertEqual(record["texts"], len(expected) + 2)


class WithoutGwMerge(unittest.TestCase):
    """Decision 64 item 4: no Python fallback. A missing gw_merge skips only the checks of the merged texts locally
    (warning merge_plan_unavailable with how to get the tool) and fails the version in the CI of the index (TRANSIENT);
    a package that replaces no text of the base needs no gw_merge at all"""

    def setUp(self):
        self.tmp = util.TempDir()
        self.snaps = write_snapshot(os.path.join(self.tmp.path, "snaps"), "v1.0.0",
                                    ["configs\\system.ltx", "textures\\wpn\\a.dds", "configs\\x.xml"])

    def tearDown(self):
        self.tmp.cleanup()

    def run_validate(self, files, require_delta=False, extra_manifest=""):
        folder = util.make_addon(self.tmp, "addon", util.GOOD_MANIFEST.replace("depends  = gw_npc, my_lib >=1.2 <2\n",
                                                                              "") + extra_manifest, files)
        return validate_version({"kind": "folder", "path": folder}, snapshots=self.snaps, guessed_tag="v1.0.0",
                                gw_merge=NO_TOOL, require_delta=require_delta, snapshot_check=False)

    def test_local_check_skips_only_the_texts(self):
        rep, result = self.run_validate({"gamedata/configs/system.ltx": "[a]\n", "gamedata/textures/wpn/a.dds": b"d"})
        self.assertTrue(rep.ok, rep.to_text())
        warning = next(i for i in rep.items if i["code"] == "merge_plan_unavailable")
        self.assertEqual(warning["level"], "warning")
        for part in ("configs\\system.ltx", "--gw-merge %s is not a file" % NO_TOOL, "<game>\\bin\\gw_merge.exe",
                     "gw_merge-linux-x86_64.tar.gz", "cmake -S xray-16/src/utils/gw_merge -B build-gw_merge",
                     "$GW_MERGE"):
            self.assertIn(part, warning["message"])
        # the other checks of the base are made: the count of the replaced files, the risky replacements
        self.assertIn("replaces_base", rep.codes("note"))
        self.assertIn("replaces_root_config", rep.codes("warning"))
        self.assertNotIn("merge_plan_simplified", rep.codes())
        self.assertIsNone(result["replaces"])  # its "texts" would be unknown
        self.assertIsNone(result["merge"])

    def test_ci_fails_the_version(self):
        rep, _ = self.run_validate({"gamedata/configs/system.ltx": "[a]\n"}, require_delta=True)
        self.assertEqual(rep.codes("error"), ["merge_plan_unavailable"])
        self.assertIn("merge_plan_unavailable", validate.TRANSIENT)  # retried at a later run, not invalid
        self.assertIn("gw_merge-linux-x86_64.tar.gz", rep.errors[0]["message"])

    def test_no_text_of_the_base_needs_no_tool(self):
        # a texture of the base, a text that is no file of the base: nothing is merged, gw_merge is never asked
        for require_delta in (False, True):
            rep, result = self.run_validate({"gamedata/textures/wpn/a.dds": b"d", "gamedata/configs/new.xml": "<a/>"},
                                            require_delta)
            self.assertNotIn("merge_plan_unavailable", rep.codes())
            self.assertTrue(rep.ok, rep.to_text())
            self.assertEqual((result["replaces"]["count"], result["replaces"]["texts"]), (1, 0))

    def test_a_private_text_of_the_base_asks_the_tool(self):
        """texts_to_decide keeps the [private] files: which file a mask takes is decided by gw_merge plan, so a text
        the Python MaskSet takes as private (and the engine might not) still makes the validator ask gw_merge"""
        rep, result = self.run_validate({"gamedata/configs/system.ltx": "[a]\n"},
                                        extra_manifest="\n[private]\nconfigs\\system.ltx\n")
        self.assertIn("merge_plan_unavailable", rep.codes("warning"))
        self.assertIsNone(result["replaces"])
        layers = [{"label": "gamedata", "public": {"configs\\x.xml": "p"},
                   "private": {"configs\\system.ltx": "q", "textures\\wpn\\a.dds": "t", "configs\\new.ltx": "n"}}]
        base = {"configs\\system.ltx": [1, ""], "textures\\wpn\\a.dds": [1, ""], "configs\\x.xml": [1, ""]}
        self.assertEqual(resources.texts_to_decide(layers, base), ["configs\\system.ltx", "configs\\x.xml"])
        # the counts of "replaces" keep the rule of the engine: the private files replace nothing
        self.assertEqual(resources.replaced_by(layers, base), ["p"])

    def test_author_check_without_gw_merge(self):
        """gw_addon_tool check: exit 0 (nothing else wrong), the warning with the how-to as its hint, NOT CHECKED says
        what was skipped"""
        from test_wave63 import run_check
        folder = util.make_addon(self.tmp, "addon", util.GOOD_MANIFEST.replace("depends  = gw_npc, my_lib >=1.2 <2\n",
                                                                              ""),
                                 {"gamedata/configs/system.ltx": "[a]\n"})
        code, s = run_check(folder, "--snapshots", self.snaps.folder, "--latest", "v1.0.0", "--game", "v1.0.0",
                            "--gw-merge", NO_TOOL)
        self.assertEqual(code, 0, s)
        row = next(r for r in s["findings"]["warning"] if r["code"] == "merge_plan_unavailable")
        self.assertIn("<game>\\bin\\gw_merge.exe", row["hint"])
        self.assertTrue(any("no gw_merge" in n for n in s["not_checked"]), s["not_checked"])

    def test_recompute_deltas_needs_the_tool(self):
        import io
        from contextlib import redirect_stderr
        import gw_catalog
        org = os.path.join(self.tmp.path, "org")
        os.makedirs(org)
        err = io.StringIO()
        with redirect_stderr(err):
            code = gw_catalog.main(["recompute-deltas", "--index", util.INDEX_SKELETON, "--local-org", org,
                                    "--gw-merge", NO_TOOL])
        self.assertEqual(code, 2)
        self.assertIn("<game>\\bin\\gw_merge.exe", err.getvalue())

    def test_recompute_deltas_needs_a_new_enough_tool(self):
        """a program that is there but is no gw_merge of today (python: no command version) - too old, exit code 2,
        nothing recomputed"""
        import io
        import sys
        from contextlib import redirect_stderr
        import gw_catalog
        org = os.path.join(self.tmp.path, "org")
        os.makedirs(org)
        err = io.StringIO()
        with redirect_stderr(err):
            code = gw_catalog.main(["recompute-deltas", "--index", util.INDEX_SKELETON, "--local-org", org,
                                    "--gw-merge", sys.executable])
        self.assertEqual(code, 2)
        self.assertIn("is too old (no command version)", err.getvalue())
        self.assertIn("<game>\\bin\\gw_merge.exe", err.getvalue())

    def test_missing_message(self):
        self.assertIn("--gw-merge x is not a file", snapshot_tools.gw_merge_missing("x"))
        text = snapshot_tools.gw_merge_missing()
        for part in ("<game>\\bin\\gw_merge.exe", "gw_merge-linux-x86_64.tar.gz",
                     "cmake --build build-gw_merge --config Release", "--gw-merge <path>", "$GW_MERGE=<path>"):
            self.assertIn(part, text)


if __name__ == "__main__":
    unittest.main()
