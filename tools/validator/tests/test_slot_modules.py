"""[slot.<slot>] entry / panel must be scripts of the package itself (review of 00-roadmap row 65, item 3): the engine
runs them under the provider only when the layer of the addon serves the script (gw_slots.check_provider_module)."""

import os
import unittest

import util
from gwcat import authorcheck
from gwcat.validate import validate_version

MANIFEST = """[addon]
id      = my_addon
version = 1.0.0
type    = ui
api_min = 0.1.0

[slots_declare]
my_addon.theme = st_my_addon_theme

[slot.my_addon.theme]
name    = st_my_addon_theme_name
files   = slots\\theme\\
"""

SCRIPT = "function f() end\n"


class SlotModules(unittest.TestCase):
    def setUp(self):
        self.tmp = util.TempDir()

    def tearDown(self):
        self.tmp.cleanup()

    def validate(self, keys, files):
        self.tmp.cleanup()
        self.tmp = util.TempDir()
        self.tmp.write("a/addon.ltx", MANIFEST + keys)
        for name in files:
            self.tmp.write("a/" + name, SCRIPT)
        rep, _ = validate_version({"kind": "folder", "path": os.path.join(self.tmp.path, "a")}, snapshot_check=False)
        return rep

    def test_good(self):
        rep = self.validate("entry   = my_addon_slot\npanel   = my_addon_panel\n",
                            ["slots/theme/gamedata/scripts/my_addon_slot.script",
                             "gamedata/scripts/my_addon_panel.script"])
        for code in ("slot_entry_missing", "slot_panel_missing", "manifest_bad_slot_module"):
            self.assertNotIn(code, rep.codes(), rep.to_text())

    def test_entry_in_common_content_and_any_case(self):
        rep = self.validate("entry   = My_Addon_Slot\npanel   = MY_ADDON_PANEL\n",
                            ["gamedata/scripts/my_addon_slot.script", "gamedata/scripts/my_addon_panel.script"])
        self.assertNotIn("slot_entry_missing", rep.codes(), rep.to_text())
        self.assertNotIn("slot_panel_missing", rep.codes(), rep.to_text())

    def test_missing(self):
        rep = self.validate("entry   = my_addon_slot\npanel   = my_addon_panel\n", [])
        self.assertIn("slot_entry_missing", rep.codes("error"), rep.to_text())
        self.assertIn("slot_panel_missing", rep.codes("error"), rep.to_text())

    def test_panel_in_the_set_is_not_enough(self):
        """the dialog loads the panel while the provider is not chosen: its set is not mounted then"""
        rep = self.validate("panel   = my_addon_panel\n", ["slots/theme/gamedata/scripts/my_addon_panel.script"])
        self.assertIn("slot_panel_missing", rep.codes("error"), rep.to_text())
        self.assertIn("it lies in the set", rep.to_text())

    def test_base_module_name(self):
        """a module of the base (or of another addon) is not in the package: an error, the engine would not use it"""
        rep = self.validate("panel   = ui_mm_appearance\nentry   = gw_slots\n", [])
        self.assertIn("slot_panel_missing", rep.codes("error"), rep.to_text())
        self.assertIn("slot_entry_missing", rep.codes("error"), rep.to_text())

    def test_bad_module_name(self):
        rep = self.validate("panel   = my-panel\nentry   = scripts\\x\n", [])
        self.assertIn("manifest_bad_slot_module", rep.codes("warning"), rep.to_text())
        self.assertNotIn("slot_panel_missing", rep.codes(), rep.to_text())
        self.assertNotIn("slot_entry_missing", rep.codes(), rep.to_text())

    def test_hints(self):
        for code in ("slot_entry_missing", "slot_panel_missing", "manifest_bad_slot_module"):
            self.assertIn(code, authorcheck.HINTS)


if __name__ == "__main__":
    unittest.main()
