"""catalog/index/tools is a copy of its sources (catalog/validator, GlobalWar/utils/addons_snapshot), mirrored by
`python catalog/assemble.py --sync-index`: the repo index is pushed straight from catalog/index, so a copy that is behind
would publish an old validator.

Runs only in the GW workspace, where catalog/assemble.py and the sources are found: from catalog/validator/tests and
from catalog/index/tools/validator/tests (assemble.py --sync-index --check). In the repo index itself (validate-pr.yml
`tests`, a clone) and in an assembled --out folder there are no sources: skipped."""

import importlib.util
import os
import shutil
import sys
import unittest

import util


def find_catalog():
    """The catalog folder of the GW workspace (assemble.py + validator/ + index/), or None"""
    for root in (os.path.join(util.VALIDATOR, ".."),               # catalog/validator
                 os.path.join(util.VALIDATOR, "..", "..", "..")):  # catalog/index/tools/validator
        root = os.path.normpath(root)
        if (os.path.isfile(os.path.join(root, "assemble.py")) and os.path.isdir(os.path.join(root, "validator"))
                and os.path.isfile(os.path.join(root, "index", "source.ltx"))):
            return root
    return None


def load_assemble(catalog):
    spec = importlib.util.spec_from_file_location("gw_catalog_assemble", os.path.join(catalog, "assemble.py"))
    module = importlib.util.module_from_spec(spec)
    dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # no catalog/__pycache__
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = dont_write
    return module


CATALOG = find_catalog()
ASSEMBLE = load_assemble(CATALOG) if CATALOG else None
SOURCES_FOUND = bool(ASSEMBLE) and all(os.path.isdir(src) for _, src in ASSEMBLE.TOOLS_SOURCES)


@unittest.skipUnless(SOURCES_FOUND, "not the GW workspace: catalog/assemble.py and the sources of tools/ are not here")
class ToolsSyncTest(unittest.TestCase):
    def test_index_tools_are_the_sources(self):
        diff = ASSEMBLE.sync_diff()
        self.assertEqual(diff, [], "catalog/index/tools is behind its sources, run "
                                   "`python catalog/assemble.py --sync-index`:\n" + ASSEMBLE.format_diff(diff))


@unittest.skipUnless(ASSEMBLE, "not the GW workspace: catalog/assemble.py is not here")
class MirrorTest(unittest.TestCase):
    """The mirror and the comparison on temp folders (the module constants pointed there)"""

    def setUp(self):
        self.tmp = util.TempDir()
        self.saved = (ASSEMBLE.INDEX, ASSEMBLE.INDEX_TOOLS, ASSEMBLE.TOOLS_SOURCES)
        ASSEMBLE.INDEX = os.path.join(self.tmp.path, "index")
        ASSEMBLE.INDEX_TOOLS = os.path.join(ASSEMBLE.INDEX, "tools")
        ASSEMBLE.TOOLS_SOURCES = (("validator", os.path.join(self.tmp.path, "src_v")),
                                  ("addons_snapshot", os.path.join(self.tmp.path, "src_s")))
        self.tmp.write("index/source.ltx", "[source]\n")
        self.tmp.write("index/ci/commit.sh", "#!/bin/sh\n")
        self.tmp.write("src_v/gw_catalog.py", "print(1)\n")
        self.tmp.write("src_v/tests/test_a.py", "a\n")
        self.tmp.write("src_v/__pycache__/x.cpython-313.pyc", b"\0")
        self.tmp.write("src_s/common.py", "c\n")

    def tearDown(self):
        ASSEMBLE.INDEX, ASSEMBLE.INDEX_TOOLS, ASSEMBLE.TOOLS_SOURCES = self.saved
        self.tmp.cleanup()

    def sync(self):
        stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        try:
            ASSEMBLE.sync_index()
        finally:
            sys.stdout.close()
            sys.stdout = stdout

    def test_differences_found_and_mirrored(self):
        self.assertIn(("missing", "tools/validator/gw_catalog.py"), ASSEMBLE.sync_diff())
        self.sync()
        self.assertEqual(ASSEMBLE.sync_diff(), [])
        tools = ASSEMBLE.INDEX_TOOLS
        self.assertFalse(os.path.exists(os.path.join(tools, "validator", "__pycache__")))  # skipped as by the build
        # a changed source, a file gone from the source, junk and a stray entry in the copy
        self.tmp.write("src_v/gw_catalog.py", "print(2)\n")
        shutil.rmtree(os.path.join(self.tmp.path, "src_v", "tests"))
        self.tmp.write("index/tools/addons_snapshot/__pycache__/common.cpython-313.pyc", b"\0")
        self.tmp.write("index/tools/stray.txt", "s\n")
        self.assertEqual(sorted(ASSEMBLE.sync_diff()), [("changed", "tools/validator/gw_catalog.py"),
                                                        ("extra", "tools/stray.txt"),
                                                        ("extra", "tools/validator/tests/test_a.py")])
        self.sync()
        self.assertEqual(ASSEMBLE.sync_diff(), [])
        for gone in ("stray.txt", "validator/tests", "addons_snapshot/__pycache__"):
            self.assertFalse(os.path.exists(os.path.join(tools, *gone.split("/"))), gone)
        # nothing outside tools/ is touched
        self.assertTrue(os.path.isfile(os.path.join(ASSEMBLE.INDEX, "ci", "commit.sh")))
        with open(os.path.join(tools, "validator", "gw_catalog.py"), "rb") as f:
            self.assertEqual(f.read(), b"print(2)\n")


if __name__ == "__main__":
    unittest.main()
