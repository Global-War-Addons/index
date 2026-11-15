"""The shared test vectors (testvectors/*.json, next to this folder): the validator side.

The same files run against the engine (GlobalWar/utils/testvectors/engine_stand.py in the GW workspace: the engine functions
copied out of xray-16 and built with cl) and the launcher (launcher-new/src-tauri/src/service/testvectors_tests.rs,
on its copy src-tauri/testdata/vectors/, kept identical by GlobalWar/utils/testvectors/sync.py). A vector that fails here means
the validator drifted from the engine: fix the code, not the vector (unless the rule itself changed - then all sides).
"""

import json
import os
import shutil
import tempfile
import unittest

import util  # noqa: F401  (sys.path)
from gwcat import ltx, package
from gwcat.report import Report

VECTORS = os.path.join(util.VALIDATOR, "testvectors")
# The copy of the launcher in the GW workspace (catalog/validator -> launcher-new/src-tauri/testdata/vectors)
LAUNCHER_COPY = os.path.normpath(os.path.join(util.VALIDATOR, "..", "..", "launcher-new", "src-tauri", "testdata",
                                              "vectors"))
FILES = ("api_version.json", "game_tags.json", "addons_ltx.json", "package_paths.json")


def load(name):
    with open(os.path.join(VECTORS, name), encoding="utf-8") as f:
        return json.load(f)


def version_tuple(text):
    return tuple(int(x) for x in text.split("."))


class ApiVersionVectors(unittest.TestCase):
    def test_parse_strict(self):
        # The catalog takes only the strict form: parse_api_version is ParseVersion + ParseAddonVersion of the engine
        for c in load("api_version.json")["parse"]:
            want = version_tuple(c["value"]) if c["strict"] else None
            self.assertEqual(ltx.parse_api_version(c["in"]), want, msg=repr(c["in"]))

    def test_strict_implies_value(self):
        for c in load("api_version.json")["parse"]:
            if c["strict"]:
                self.assertIsNotNone(c["value"], msg=repr(c["in"]))

    def test_major(self):
        # api_major of the index (11 section 6): the compat vectors with the same major are "ok" / "newer_game"
        for c in load("api_version.json")["compat"]:
            engine, api_min = ltx.parse_api_version(c["engine"]), ltx.parse_api_version(c["api_min"])
            if engine is None or api_min is None:
                continue  # a short form: the catalog refuses it, the engine reads it
            if c["expect"] == "unset":
                self.assertEqual(ltx.api_major(api_min), "", msg=str(c))
            elif c["expect"] == "ok":
                self.assertEqual(ltx.api_major(api_min), ltx.api_major(engine), msg=str(c))
            elif c["expect"] == "addon_update":
                self.assertNotEqual(ltx.api_major(api_min), ltx.api_major(engine), msg=str(c))


class GameTagVectors(unittest.TestCase):
    def setUp(self):
        self.tags = ltx.game_tags()  # tools/addons_snapshot/gametag.py, the one the validator imports

    def test_parse(self):
        for c in load("game_tags.json")["parse"]:
            got = self.tags.parse(c["in"])
            e = c["expect"]
            if e is None:
                self.assertIsNone(got, msg=repr(c["in"]))
                continue
            kind = {"pre": self.tags.PRE, "release": self.tags.RELEASE, "patch": self.tags.PATCH}[e["kind"]]
            self.assertEqual(got, self.tags.GameTag(e["numbers"], kind, e["patch"], e["pre"]), msg=repr(c["in"]))

    def test_compare(self):
        for c in load("game_tags.json")["compare"]:
            self.assertEqual(self.tags.compare(c["a"], c["b"]), c["expect"], msg=str(c))
            self.assertEqual(self.tags.compare(c["b"], c["a"]), -c["expect"], msg=str(c))

    def test_older(self):
        for c in load("game_tags.json")["older"]:
            self.assertEqual(self.tags.is_older(c["game_version"], c["tag"]), c["expect"], msg=str(c))


class AddonsLtxVectors(unittest.TestCase):
    def test_syntax(self):
        # The validator reads addon.ltx with the same strict reader (ParseSimpleIni): the syntax verdict of the
        # vectors holds for it too. Its input is UTF-8 text (it refuses other bytes before parsing)
        for c in load("addons_ltx.json")["cases"]:
            data = bytes.fromhex(c["hex"]) if "hex" in c else c["text"].encode("utf-8")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            readable = c["expect"]["readable"]
            if readable == "syntax":
                with self.assertRaises(ltx.LtxError, msg=c["name"]):
                    ltx.parse(text)
                continue
            ini = ltx.parse(text)
            self.assertEqual(ini.names() == [], readable == "no_section", msg=c["name"])


class PackagePathVectors(unittest.TestCase):
    def test_excluded_lists(self):
        ex = load("package_paths.json")["excluded"]
        self.assertEqual(list(package.EXCLUDED_DIRS), ex["dirs"])
        self.assertEqual(list(package.EXCLUDED_FILES), ex["files"])

    def test_names(self):
        for c in load("package_paths.json")["names"]:
            r = Report()
            ok = package._check_name(c["in"], r, "t")
            errors = [i["code"] for i in r.items if i["level"] == "error"]
            warnings = [i["code"] for i in r.items if i["level"] == "warning"]
            self.assertEqual("ok" if ok else errors[0], c["expect"], msg=repr(c["in"]))
            self.assertEqual(warnings, [c["warning"]] if "warning" in c else [], msg=repr(c["in"]))

    def test_root(self):
        # folder_files is the walk of validate / pack of a folder (gw_catalog.py, gw_addon_tool.py)
        for c in load("package_paths.json")["root"]:
            root = tempfile.mkdtemp(prefix="gwtv_")
            try:
                with open(os.path.join(root, "addon.ltx"), "w") as f:
                    f.write("[addon]\n")
                full = os.path.join(root, *c["path"].split("/"))
                os.makedirs(full if c["dir"] else os.path.dirname(full), exist_ok=True)
                inner = c["path"] + "/x.txt" if c["dir"] else c["path"]
                with open(os.path.join(root, *inner.split("/")), "w") as f:
                    f.write("x")
                r = Report()
                files = package.folder_files(root, r)
                self.assertEqual(inner not in files, c["excluded"], msg=str(c))
                self.assertEqual("addon.ltx" in files, True)
            finally:
                shutil.rmtree(root, ignore_errors=True)


class LauncherCopy(unittest.TestCase):
    def test_launcher_copy_is_identical(self):
        # Only in the GW workspace: GlobalWar/utils/testvectors/sync.py copies the canonical files to the launcher
        if not os.path.isdir(LAUNCHER_COPY):
            self.skipTest("no launcher-new next to catalog (not the GW workspace)")
        for name in FILES:
            with open(os.path.join(VECTORS, name), "rb") as a, open(os.path.join(LAUNCHER_COPY, name), "rb") as b:
                self.assertEqual(a.read(), b.read(), msg="%s differs: run GlobalWar/utils/testvectors/sync.py" % name)


if __name__ == "__main__":
    unittest.main()
