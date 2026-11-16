"""Tests of GlobalWar/utils/addons_snapshot (and GlobalWar/utils/release/write_gw_release.py, which uses its gametag.py).

    python -m unittest discover -s GlobalWar/utils/addons_snapshot/tests

The pairs tests need gw_merge ($GW_MERGE, PATH or xray-16/bin/x64/Release) and LuaJIT next to it or in
xray-16/bin/x64/Release; without them they are skipped. They use the test addons GlobalWar/addons/tests/test_merge_*
and a snapshot built from the files of GlobalWar/gamedata they replace. GwMergeRequiredTest runs without gw_merge: the
commands that need it fail with a clear error (decision 63: no Python fallback).
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

import gametag  # noqa: E402

GW = os.path.normpath(os.path.join(TOOLS, "..", "..", ".."))
WRITE_RELEASE = os.path.join(GW, "GlobalWar", "utils", "release", "write_gw_release.py")
PUT_GW_MERGE = os.path.join(GW, "GlobalWar", "utils", "release", "put_gw_merge.py")
TEST_ADDONS = os.path.join(GW, "GlobalWar", "addons", "tests")
GAMEDATA = os.path.join(GW, "GlobalWar", "gamedata")


class GameTagTest(unittest.TestCase):
    ORDER = ["0.5.5-Beta", "0.5.5", "0.5.5-patch1", "0.5.5-patch4", "0.5.6-Beta", "v0.5.6"]

    def test_order(self):
        for i, a in enumerate(self.ORDER):
            for j, b in enumerate(self.ORDER):
                self.assertEqual(gametag.compare(a, b), (i > j) - (i < j), "%s ? %s" % (a, b))
        self.assertEqual(sorted(reversed(self.ORDER), key=gametag.sort_key), self.ORDER)

    def test_missing_parts_and_case(self):
        self.assertEqual(gametag.compare("v1.4", "1.4.0"), 0)
        self.assertEqual(gametag.compare("1", "1.0.0.0"), 0)
        self.assertEqual(gametag.compare("1.4", "1.4.0.1"), -1)
        self.assertEqual(gametag.compare("1.10", "1.9"), 1)  # numbers, not strings
        self.assertEqual(gametag.compare("V1.0-PATCH2", "1.0-patch2"), 0)
        self.assertEqual(gametag.compare("1.0-patch10", "1.0-patch9"), 1)
        self.assertEqual(gametag.compare("1.0-Beta", "1.0-beta"), 0)
        self.assertEqual(gametag.compare("1.0-alpha", "1.0-Beta"), -1)  # pre-releases: strings, case ignored
        self.assertEqual(gametag.compare("1.0-rc.2", "1.0-rc.1"), 1)
        self.assertEqual(gametag.compare("1.0-patch", "1.0"), -1)  # no number: a pre-release named "patch"

    def test_parse(self):
        self.assertEqual(gametag.parse("v1.4.0"), gametag.GameTag((1, 4), gametag.RELEASE))
        self.assertEqual(gametag.parse("0.5.5-patch4"), gametag.GameTag((0, 5, 5), gametag.PATCH, 4))
        self.assertEqual(gametag.parse("0.5.6-Beta"), gametag.GameTag((0, 5, 6), gametag.PRE, pre="beta"))
        for bad in ("", "v", "V", "vv1", "1.", ".1", "1..2", "-1", "1.2-", "1.2--x", "1.2-a-b", "1.2-a b", "1.2-a_b",
                    "1.2;x", " 1.2", "1.2 ", "1.2\n", "x1.2", "١.2", None):
            self.assertIsNone(gametag.parse(bad), repr(bad))
            if bad is not None:
                with self.assertRaises(ValueError):
                    gametag.compare(bad, "1.0")

    # The cases of the C++ test of the engine (MergeParseTag / MergeCompareTags / ReleaseTagOlder of addon_merge.cpp),
    # one to one: the Python tools and the engine must agree on every tag
    ENGINE_PAIRS = [
        ("0.5.5", "0.5.5.0", 0), ("v1.4", "1.4.0", 0), ("V1.4.0", "1.4", 0),
        ("0.5.5-beta", "0.5.5-Beta", 0), ("0.5.5-PATCH4", "0.5.5-patch4", 0), ("0.5.5-patch04", "0.5.5-patch4", 0),
        ("0.5.5-alpha", "0.5.5-beta", -1), ("0.5.5-Beta", "0.5.5-rc.1", -1), ("0.5.5-rc.1", "0.5.5-rc.2", -1),
        ("0.5.5-rc.10", "0.5.5-rc.2", -1),  # pre-releases: plain strings
        ("0.5.5-patch2", "0.5.5-patch10", -1),  # patches: numbers
        ("0.5.5-patch", "0.5.5", -1),  # "patch" without N - a pre-release
        ("0.5.5-patch4b", "0.5.5", -1), ("0.5.5-patch4.1", "0.5.5-patch1", -1),
        ("0.5.5-patch0", "0.5.5", 1), ("0.5.5-patch9", "0.5.6-Beta", -1), ("0.5.10", "0.5.9-patch9", 1),
        ("1", "0.9.9-patch99", 1), ("0.5.5-Beta", "0.5.4-patch4", 1), ("4294967295", "4294967294", 1),
    ]
    ENGINE_BAD = ["", "v", "V", "1.", ".1", "1..2", "1.2-", "-beta", "1.2-beta-1", "1.2 beta", "1.2-be ta",
                  "1.2_beta", "1,2", "x1.2", "vv1.2", "1.2-бета", "4294967296", "1.2-patch4294967296", "0.5.5-patch4 ",
                  " 0.5.5"]
    ENGINE_GOOD = ["0", "v0", "1.2.3.4.5", "0.5.5-Beta", "0.5.5-patch4", "0.5.5-rc.1", "0.5.5-1", "1.0-.",
                   "0.5.5-Patch4", "4294967295.0-patch4294967295"]

    def test_engine_cases(self):
        for i, a in enumerate(self.ORDER):
            for j, b in enumerate(self.ORDER):
                self.assertEqual(gametag.is_older(a, b), i < j, "%s older than %s" % (a, b))
        for a, b, want in self.ENGINE_PAIRS:
            self.assertEqual(gametag.compare(a, b), want, "%s ? %s" % (a, b))
            self.assertEqual(gametag.compare(b, a), -want, "%s ? %s" % (b, a))
        for bad in self.ENGINE_BAD:
            self.assertIsNone(gametag.parse(bad), repr(bad))
            if bad:
                self.assertTrue(gametag.is_older(bad, "0.5.5"), repr(bad))
                self.assertTrue(gametag.is_older("0.5.5", bad), repr(bad))
                self.assertFalse(gametag.is_older(bad, bad), repr(bad))
        for good in self.ENGINE_GOOD:
            self.assertIsNotNone(gametag.parse(good), good)
        self.assertFalse(gametag.is_older("", "0.5.5"))
        self.assertFalse(gametag.is_older("0.5.5", ""))
        self.assertFalse(gametag.is_older("0.5.5-patch4", "0.5.5-patch4"))
        self.assertFalse(gametag.is_older("0.5.6", "0.5.5-patch4"))
        self.assertIsNone(gametag.parse("1." + "9" * 5000))  # no int() of a huge string

    def test_is_older_as_engine(self):
        """ReleaseTagOlder of the engine: empty or equal - no; not a tag - any difference is older"""
        self.assertFalse(gametag.is_older("", "1.0"))
        self.assertFalse(gametag.is_older("1.0", ""))
        self.assertFalse(gametag.is_older("custom", "custom"))
        self.assertTrue(gametag.is_older("custom", "1.0"))
        self.assertTrue(gametag.is_older("1.0", "custom"))
        self.assertTrue(gametag.is_older("0.5.5", "0.5.5-patch1"))
        self.assertTrue(gametag.is_older("0.5.5-Beta", "0.5.5"))
        self.assertFalse(gametag.is_older("0.5.5-patch1", "0.5.5"))
        self.assertFalse(gametag.is_older("v1.4", "1.4.0"))

    def test_same(self):
        self.assertTrue(gametag.same("v1.4", "1.4.0"))
        self.assertTrue(gametag.same("custom", "custom"))
        self.assertFalse(gametag.same("custom", "Custom"))
        self.assertFalse(gametag.same("1.4-patch1", "1.4"))


class WriteReleaseTest(unittest.TestCase):
    def setUp(self):
        self.gamedata = tempfile.mkdtemp(prefix="gw_release_test_")
        os.makedirs(os.path.join(self.gamedata, "configs"))

    def tearDown(self):
        shutil.rmtree(self.gamedata, ignore_errors=True)

    def run_script(self, *args):
        r = subprocess.run([sys.executable, WRITE_RELEASE] + list(args) + ["--gamedata", self.gamedata],
                           capture_output=True)
        return r.returncode, r.stderr.decode("utf-8", "replace")

    def test_tags(self):
        for tag in ("v1.4.0", "0.5.5-patch4", "0.5.6-Beta", "V2"):
            code, err = self.run_script(tag)
            self.assertEqual(code, 0, err)
            self.assertNotIn("warning", err, tag)
            code, err = self.run_script(tag, "--check")
            self.assertEqual(code, 0, err)
        with open(os.path.join(self.gamedata, "configs", "gw_release.ltx"), "rb") as f:
            data = f.read()
        self.assertTrue(data.endswith(b"tag = V2\n") and b"\r" not in data)
        code, err = self.run_script("release-5")
        self.assertEqual(code, 0, err)
        self.assertIn("warning", err)
        for bad in ("a;b", "1.0\n2", ""):
            code, err = self.run_script(bad)
            self.assertEqual(code, 2, bad)


class PutGwMergeTest(unittest.TestCase):
    """GlobalWar/utils/release/put_gw_merge.py (decision 63/4): gw_merge beside the engine of the release folder (bin/), checked
    the way the tools check it; the parts without a real gw_merge run always"""

    def setUp(self):
        self.game = tempfile.mkdtemp(prefix="gw_put_merge_test_")
        os.makedirs(os.path.join(self.game, "bin"))
        self.target = os.path.join(self.game, "bin", "gw_merge.exe" if os.name == "nt" else "gw_merge")

    def tearDown(self):
        shutil.rmtree(self.game, ignore_errors=True)

    def run_script(self, *args):
        env = dict(os.environ)
        env.pop("GW_MERGE", None)
        r = subprocess.run([sys.executable, PUT_GW_MERGE] + list(args), capture_output=True, env=env)
        return r.returncode, r.stderr.decode("utf-8", "replace")

    def test_missing_and_bad(self):
        code, err = self.run_script(self.game, "--check")
        self.assertEqual(code, 1, err)
        self.assertIn("from the game", err)
        code, err = self.run_script(self.game, "--exe", os.path.join(self.game, "no_such.exe"))
        self.assertEqual(code, 1, err)
        self.assertIn("gw_merge is not found", err)
        # Not a gw_merge at all: refused, nothing copied
        code, err = self.run_script(self.game, "--exe", sys.executable)
        self.assertEqual(code, 1, err)
        self.assertIn("too old", err)
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(self.run_script(self.game, "--build-dir", self.game)[0], 2)
        self.assertEqual(self.run_script(os.path.join(self.game, "nothing"), "--check")[0], 2)
        # bin/ without an engine is only a warning
        self.assertIn("has no engine", err)

    def test_put_and_check(self):
        exe, _ = _tools()
        if not exe:
            self.skipTest("no gw_merge new enough (set $GW_MERGE)")
        code, err = self.run_script(self.game, "--exe", exe)
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.isfile(self.target))
        code, err = self.run_script(self.game, "--check")
        self.assertEqual(code, 0, err)


def _tools():
    """(gw_merge new enough for the tools or None, LuaJIT or None)"""
    import gwmerge
    exe = gwmerge.find_gw_merge()
    if exe and gwmerge.too_old(exe):
        exe = None
    return exe, (gwmerge.find_luajit(exe) if exe else None)


class GwMergeRequiredTest(unittest.TestCase):
    """Decision 63: gw_merge ships with the game, the Python ports of its decisions are removed - delta / rebase / pairs
    and the API of catalog_merge require it: missing or too old - "error: ..." on stderr, exit code 2, where it was
    looked for and how to get it. Runs without gw_merge"""

    HOW = ["<game>\\bin\\gw_merge.exe", "gw_merge-linux-x86_64.tar.gz", "catalog/index/ci/fetch_gw_merge.sh",
           "cmake -S xray-16/src/utils/gw_merge -B build-gw_merge", "--gw-merge", "$GW_MERGE"]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gw_required_")
        self.missing = os.path.join(self.tmp, "no_such_dir", "gw_merge.exe")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, args, env_gw_merge=None):
        env = dict(os.environ)
        env.pop("GW_MERGE", None)
        if env_gw_merge:
            env["GW_MERGE"] = env_gw_merge
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run([sys.executable, os.path.join(TOOLS, "gw_snapshot.py")] + args, capture_output=True,
                           env=env)
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")

    def assert_help(self, text):
        for part in self.HOW:
            self.assertIn(part, text)

    def test_cli_missing(self):
        snap = os.path.join(self.tmp, "base_snapshot.json.gz")  # never read: the tool is checked first
        for args in (["delta", self.tmp, "--snapshot", snap], ["rebase", os.path.join(self.tmp, "d.json"),
                                                                 "--snapshot", snap],
                     ["pairs", self.tmp, self.tmp, "--snapshot", snap]):
            code, out, err = self.cli(args + ["--gw-merge", self.missing])
            self.assertEqual(code, 2, (args, err))
            self.assertEqual(out, "", args)
            self.assertTrue(err.startswith("error: gw_merge is not found"), err)
            self.assertIn("--gw-merge %s (no such file)" % self.missing, err)
            self.assert_help(err)
        # $GW_MERGE to a missing file: named, the other places are not tried
        code, _, err = self.cli(["delta", self.tmp, "--snapshot", snap], env_gw_merge=self.missing)
        self.assertEqual(code, 2, err)
        self.assertIn("$GW_MERGE = %s (no such file)" % self.missing, err)
        self.assert_help(err)

    def test_cli_too_old(self):
        """A program that is no gw_merge of today (here: python, it has no command version) - too old"""
        code, out, err = self.cli(["delta", self.tmp, "--snapshot", os.path.join(self.tmp, "s.json.gz"),
                                   "--gw-merge", sys.executable])
        self.assertEqual(code, 2, err)
        self.assertIn("is too old for the tools: no command version", err)
        self.assert_help(err)

    def test_too_old_needs_the_revision(self):
        """too_old: gw_merge version with a revision gwmerge.MIN_REVISION or newer (read leniently), then the command
        plan with --all-files; the answers of the tool are faked here"""
        import gwmerge
        self.assertEqual(gwmerge.MIN_REVISION, 3)
        for info, revision in (({"revision": 3}, 3), ({"revision": "4"}, 4), ({"revision": " 3.1-dev"}, 3),
                               ({"revision": 3.0}, 3), ({"revision": 3.5}, None), ({"revision": -1}, None),
                               ({"revision": True}, None), ({"revision": "dev"}, None), ({}, None), (None, None)):
            self.assertEqual(gwmerge.revision_of(info), revision, info)
        usage = "usage:\n  gw_merge plan    (--base <gamedata> ...) [--all-files] [-o plan.json]\n"
        saved = (gwmerge.version, gwmerge.run, dict(gwmerge._TOO_OLD))
        try:
            gwmerge.run = lambda exe, args, check=True, what=None: (2, b"", usage)
            cases = [({"algorithm": "gw-merge/2", "revision": 3, "delta_format": 1}, None),
                     ({"algorithm": "gw-merge/2", "revision": "5"}, None),
                     ({"algorithm": "gw-merge/2", "revision": 2}, "revision 2 of the library, needs 3 or newer"),
                     ({"algorithm": "gw-merge/2"}, "gw_merge version reports no revision (needs 3 or newer)"),
                     ({}, "no command version")]
            for n, (info, why) in enumerate(cases):
                exe = "fake_gw_merge_%d" % n
                gwmerge._TOO_OLD.pop(exe, None)
                gwmerge.version = lambda _exe, info=info: info
                self.assertEqual(gwmerge.too_old(exe), why, info)
            # a new revision without plan --all-files is too old still
            gwmerge.run = lambda exe, args, check=True, what=None: (2, b"", "usage:\n  gw_merge plan (--base ...)\n")
            gwmerge.version = lambda _exe: {"algorithm": "gw-merge/2", "revision": 3}
            gwmerge._TOO_OLD.pop("fake_no_all_files", None)
            self.assertEqual(gwmerge.too_old("fake_no_all_files"), "no command plan with --all-files")
        finally:
            gwmerge.version, gwmerge.run = saved[0], saved[1]
            gwmerge._TOO_OLD.clear()
            gwmerge._TOO_OLD.update(saved[2])

    def test_api(self):
        import catalog_merge
        import common
        import gwmerge
        import gwplan
        with self.assertRaises(gwmerge.GwMergeMissing) as e:
            gwmerge.require_gw_merge(self.missing)
        self.assert_help(str(e.exception))
        self.assertIsInstance(e.exception, common.ToolError)  # exit code 2 of gw_snapshot.py
        self.assertIn("gw_merge is not found", gwplan.unavailable(None))
        self.assertIn("too old", gwplan.unavailable(sys.executable))
        with self.assertRaises(gwmerge.GwMergeMissing):
            catalog_merge.Planner(sys.executable)
        # addon_layer_texts without a Planner takes the gw_merge of $GW_MERGE ...: a missing one - the error
        old = os.environ.get("GW_MERGE")
        os.environ["GW_MERGE"] = self.missing
        try:
            with self.assertRaises(gwmerge.GwMergeMissing):
                catalog_merge.addon_layer_texts(self.tmp)
        finally:
            if old is None:
                del os.environ["GW_MERGE"]
            else:
                os.environ["GW_MERGE"] = old


class PairsBase:
    """A snapshot of the files of GlobalWar/gamedata the test addons test_merge_* replace (setUpClass of the pairs
    tests); the tests are skipped without gw_merge or LuaJIT"""

    @classmethod
    def setUpClass(cls):
        cls.exe, cls.lua = _tools()
        if not cls.exe or not cls.lua:
            raise unittest.SkipTest("gw_merge or LuaJIT is not found ($GW_MERGE)")
        cls.work = tempfile.mkdtemp(prefix="gw_pairs_test_")
        gamedata = os.path.join(cls.work, "gamedata")
        for name in ("bind_faction.script", "ranks.script"):
            os.makedirs(os.path.join(gamedata, "scripts"), exist_ok=True)
            shutil.copyfile(os.path.join(GAMEDATA, "scripts", name), os.path.join(gamedata, "scripts", name))
        cls.snap = os.path.join(cls.work, "snap")
        r = subprocess.run([sys.executable, os.path.join(TOOLS, "gw_snapshot.py"), "build", gamedata, "--tag",
                            "0.5.5-patch4", "--out", cls.snap, "--no-auto", "--no-hash"], capture_output=True)
        if r.returncode:
            raise AssertionError(r.stderr.decode("utf-8", "replace"))
        cls.addons = [os.path.join(TEST_ADDONS, n) for n in ("test_merge_a", "test_merge_b", "test_merge_d",
                                                                "test_merge_bad")]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def run_pairs(self, lua, addons=None, **kwargs):
        """find_pairs -> ({(a id, b id, file): record}, merge_stats.json)"""
        import catalog_merge
        conflicts, stats = catalog_merge.find_pairs(self.exe, addons or self.addons,
                                                    os.path.join(self.snap, "base_snapshot.json.gz"), lua, **kwargs)
        self.assertEqual(conflicts["game"], "0.5.5-patch4")
        return {(p["a"].split("@")[0], p["b"].split("@")[0], p["file"]): p for p in conflicts["pairs"]}, stats

    def pairs(self, lua):
        return self.run_pairs(lua)[0]


RANKS = ("test_merge_a", "test_merge_d", "scripts\\ranks.script")
BIND_FACTION = ("test_merge_b", "test_merge_bad", "scripts\\bind_faction.script")


class PairsLuaTest(PairsBase, unittest.TestCase):
    """pairs merges every pair that changes one script, even with different atoms, and compiles the result:
    test_merge_b + test_merge_bad change different functions of bind_faction.script, together > 200 locals"""

    def test_with_lua(self):
        found = self.pairs(self.lua)
        self.assertEqual(sorted(found), [("test_merge_a", "test_merge_d", "scripts\\ranks.script"),
                                         ("test_merge_b", "test_merge_bad", "scripts\\bind_faction.script")])
        bad = found[("test_merge_b", "test_merge_bad", "scripts\\bind_faction.script")]
        self.assertEqual(bad["atoms"], [])
        self.assertIn("more than 200 local variables", bad["fallback"])
        self.assertEqual(found[("test_merge_a", "test_merge_d", "scripts\\ranks.script")]["atoms"],
                         ["ranks.parse_ranks"])

    def test_without_lua(self):
        """No LuaJIT: only the pairs with a common atom, as before"""
        self.assertEqual(sorted(self.pairs(None)), [("test_merge_a", "test_merge_d", "scripts\\ranks.script")])


class PairsCacheLimitTest(PairsBase, unittest.TestCase):
    """pairs --cache (a rerun merges only the pairs whose inputs changed) and --max-pairs-per-file (the merges of one
    file per run; the pairs of --changed first). With LuaJIT ranks.script has 3 candidate pairs: a+b, a+d, b+d"""

    def setUp(self):
        self.cache_dir = tempfile.mkdtemp(prefix="gw_pairs_cache_")

    def tearDown(self):
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    def cache(self):
        import catalog_merge
        return catalog_merge.PairCache(self.cache_dir)

    def entries(self):
        return sorted(os.path.join(d, n) for d, _, names in os.walk(self.cache_dir) for n in names)

    def test_cache(self):
        # with the compatibility patch of a+d: its check (patch_resolves) is cached with the merge
        addons = self.addons + [os.path.join(TEST_ADDONS, "test_merge_patch")]
        first_cache = self.cache()
        first, first_stats = self.run_pairs(self.lua, addons, cache=first_cache)
        self.assertEqual(sorted(first), [RANKS, BIND_FACTION])
        self.assertEqual(first[RANKS]["patch"].split("@")[0], "test_merge_patch")
        self.assertTrue(first[RANKS]["patch_resolves"])
        self.assertEqual(first_cache.hits, 0)
        self.assertGreater(first_cache.misses, 2)  # the pairs without a conflict are cached too
        self.assertEqual(len(self.entries()), first_cache.misses)
        self.assertFalse(any(os.path.basename(e).startswith(".tmp_") for e in self.entries()))

        again = self.cache()
        second, second_stats = self.run_pairs(self.lua, addons, cache=again)
        self.assertEqual((again.hits, again.misses), (first_cache.misses, 0))
        self.assertEqual(second, first)
        self.assertEqual(second_stats, first_stats)

        # a broken entry: merged again (a warning), the result is the same
        with open(self.entries()[0], "w") as f:
            f.write("{")
        broken = self.cache()
        third, _ = self.run_pairs(self.lua, addons, cache=broken)
        self.assertEqual((broken.hits, broken.misses), (first_cache.misses - 1, 1))
        self.assertEqual(third, first)

        # another check (no LuaJIT): another key, nothing is taken from the cache
        no_lua = self.cache()
        self.assertEqual(sorted(self.run_pairs(None, addons, cache=no_lua)[0]), [RANKS])
        self.assertEqual(no_lua.hits, 0)

    def test_limit(self):
        full, _ = self.run_pairs(self.lua, max_pairs_per_file=0)
        self.assertEqual(sorted(full), [RANKS, BIND_FACTION])

        # 1 merge per file: ranks.script merges a+b only (the order of the addons): no conflict, 2 pairs skipped
        found, stats = self.run_pairs(self.lua, max_pairs_per_file=1)
        self.assertEqual(sorted(found), [BIND_FACTION])
        ranks = [s for s in stats["skipped"] if s["file"] == "scripts\\ranks.script"]
        self.assertEqual(ranks, [{"file": "scripts\\ranks.script", "pairs": 3, "checked": 1, "skipped": 2}])
        self.assertEqual(stats["max_pairs_per_file"], 1)
        self.assertEqual(stats["skipped_pairs"], sum(s["skipped"] for s in stats["skipped"]))

        # test_merge_d changed since the last run: its pairs first, a+d is found
        found, _ = self.run_pairs(self.lua, max_pairs_per_file=1, changed=["test_merge_d"])
        self.assertEqual(sorted(found), [RANKS, BIND_FACTION])
        self.assertEqual(found[RANKS], full[RANKS])

    def test_limit_with_cache(self):
        """The pairs from the cache are not counted: each run checks 1 more pair of a file, the 3rd run has them all"""
        skipped = []
        for _ in range(3):
            found, stats = self.run_pairs(self.lua, cache=self.cache(), max_pairs_per_file=1)
            skipped.append([s["skipped"] for s in stats["skipped"] if s["file"] == "scripts\\ranks.script"])
        self.assertEqual(skipped, [[2], [1], []])
        self.assertEqual(stats["skipped_pairs"], 0)
        self.assertEqual(found, self.run_pairs(self.lua, max_pairs_per_file=0)[0])


class EngineRulesTest(unittest.TestCase):
    """The rules of the engine the tools follow (review 2026-11-06: B3, B6, B9, B17, B21, B22, F19)"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gw_rules_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        path = os.path.join(self.tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return path

    def test_strict_json(self):
        import common
        self.assertEqual(common.loads_json(b'\xef\xbb\xbf{"a": 1e400}', "x")["a"], float("inf"))
        for bad in (b'{"a": NaN}', b'{"a": "\\ud800"}', b'{"a": 1, "a": 2}', b"[" * 65 + b"]" * 65, b'"\xff"'):
            self.assertRaises(common.BrokenJsonError, common.loads_json, bad, "x")

    def test_ascii_lower_and_kinds(self):
        import common
        import gwmerge
        self.assertEqual(common.vnorm("Configs/Д/A.LTX"), "configs\\Д\\a.ltx")
        self.assertEqual(gwmerge.kind_of("scripts\\X.LUA"), "lua")

    def test_layers_private_and_sets(self):
        """The layers by gw_merge plan (the API without a Planner: the gw_merge of $GW_MERGE ...)"""
        import catalog_merge
        if not _tools()[0]:
            self.skipTest("gw_merge is not found or too old ($GW_MERGE)")
        self.write("addon.ltx", "[addon]\nid = a\nversion = 1.0.0\n[slot.ui.menu]\n[private]\nconfigs\\p.ltx\n")
        self.write("gamedata/configs/p.ltx", "x")
        self.write("gamedata/configs/mod_system_a.ltx", "x")
        self.write("slots/ui.menu/gamedata/configs/ui/m.xml", "<a/>")
        texts, warnings = catalog_merge.addon_layer_texts(self.tmp, {"configs\\system.ltx"})
        self.assertEqual(sorted(texts), ["configs\\ui\\m.xml"])
        self.assertEqual(warnings, [])

    def test_ci_ignores_the_delta_of_the_package(self):
        import catalog_merge
        self.write("addon.ltx", "[addon]\nid = a\nversion = 1.0.0\n")
        self.write(".gw_merge/delta.json", '{"format": 1, "algo": "gw-merge/2", "game": "v1", "files": {}}')
        index = os.path.join(self.tmp, "index")
        os.makedirs(os.path.join(index, "files", "a"))
        self.assertEqual(catalog_merge.find_delta(self.tmp, "gw-merge/2", index), (None, None))
        self.assertIsNotNone(catalog_merge.find_delta(self.tmp, "gw-merge/2")[0])

    def test_only_delta_json_of_the_package(self):
        """Review 2026-11-06 section 4.1 item 1: the engine reads <addon>/.gw_merge/delta.json only"""
        import catalog_merge
        self.write("addon.ltx", "[addon]\nid = a\nversion = 1.0.0\n")
        tagged = '{"format": 1, "algo": "gw-merge/2", "game": "tagged", "files": {}}'
        self.write(".gw_merge/delta.gw-merge-2.json", tagged)
        self.assertEqual(catalog_merge.find_delta(self.tmp, "gw-merge/2"), (None, None))
        self.write(".gw_merge/delta.json", '{"format": 1, "algo": "gw-merge/2", "game": "plain", "files": {}}')
        path, delta = catalog_merge.find_delta(self.tmp, "gw-merge/2")
        self.assertEqual(os.path.basename(path), "delta.json")
        self.assertEqual(delta["game"], "plain")


if __name__ == "__main__":
    unittest.main()
