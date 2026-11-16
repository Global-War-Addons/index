"""A6 (14-addon-resource-scoping.md): the base files an addon replaces by the rules of the engine, the model references,
the [private] checks; C7 (09-content-addons-vfs.md): OGF texture names, remap-textures, gw_addon_tool."""

import gzip
import io
import json
import os
import struct
import tarfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import util  # noqa: F401  (sys.path)
from gwcat import ogf, remap, resources, schema
from gwcat.validate import SnapshotStore, validate_version, version_file


def chunk(cid, payload):
    return struct.pack("<II", cid, len(payload)) + payload


def model(textures, shader="models\\model", children=(), motions=None, lod=None):
    """An OGF with a header, OGF_TEXTURE, children (each a list of chunks bytes) and motion refs"""
    out = chunk(1, b"\x04\x00" + b"\0" * 42)
    if textures is not None:
        out += chunk(ogf.OGF_TEXTURE, textures.encode("latin-1") + b"\0" + shader.encode("latin-1") + b"\0")
    if children:
        out += chunk(ogf.OGF_CHILDREN, b"".join(chunk(i, c) for i, c in enumerate(children)))
    if motions is not None:
        out += chunk(ogf.OGF_S_MOTION_REFS, motions.encode("latin-1") + b"\0")
    if lod is not None:
        out += chunk(ogf.OGF_S_LODS, lod.encode("latin-1") + b"\r\n")
    out += chunk(3, b"\x01\x02\x03\x04")  # vertices: copied as they are
    return out


def child(textures):
    return chunk(1, b"\0" * 44) + chunk(ogf.OGF_TEXTURE, textures.encode("latin-1") + b"\0shader\0")


def write_snapshot(folder, tag, vpaths):
    """A minimal snapshot: the files of the base (no texts)"""
    d = os.path.join(folder, tag)
    os.makedirs(d, exist_ok=True)
    with gzip.open(os.path.join(d, "base_snapshot.json.gz"), "wt", encoding="utf-8") as f:
        json.dump({"tag": tag, "files": {v: [1, None] for v in vpaths}}, f)
    with tarfile.open(os.path.join(d, "base_texts.tar.gz"), "w:gz"):
        pass
    return SnapshotStore(folder, latest=tag)


BASE = [
    "textures\\wpn\\wpn_ak74.dds", "textures\\wpn\\wpn_ak74.thm", "textures\\wpn\\wpn_ak74_bump.dds",
    "textures\\act\\act_stalker.dds", "textures\\ui\\ui_icons.dds", "spawns\\all.spawn", "particles.xr",
    "meshes\\dynamics\\weapons\\wpn_ak74.ogf", "meshes\\base\\anim\\walk.omf", "sounds\\weapons\\ak_shot.ogg",
    "configs\\system.ltx",
]

MANIFEST = """[addon]
id      = my_addon
version = 1.2.0
type    = content
api_min = 0.1.0

[lua]
entry = my_addon_main

[slot.music.menu]
files = slots\\menu_music

[private]
textures\\act\\act_stalker.dds
textures\\nothing\\*
"""


class PatternTest(unittest.TestCase):
    def test_pattern_match_as_xrcore(self):
        cases = [
            ("textures\\wpn\\x.dds", "textures\\wpn\\*", True), ("a", "*", True), ("abc", "a?c", True),
            ("abc", "a*d", False), ("textures\\a\\b.dds", "*b.dds", True), ("ab", "ab*", True),
            ("ab", "abc", False), ("abc", "ab", False), ("abcbd", "a*bd", True), ("", "*", True), ("", "", True),
            ("a", "", False), ("textures\\wpn\\x_bump.dds", "textures\\wpn\\x.*", False),
            ("textures\\wpn\\x.thm", "textures\\wpn\\x.*", True), ("xx", "x?x", False),
        ]
        for s, mask, want in cases:
            self.assertEqual(resources.pattern_match(s, mask), want, (s, mask))
            if mask:  # normalize_mask drops an empty one
                self.assertEqual(resources.MaskSet([mask]).match(s), want, (s, mask))

    def test_mask_set_equals_pattern_match(self):
        # the fast matcher of the checks against the port of PatternMatch: random names and masks of a small alphabet
        import random
        rnd = random.Random(7)
        alpha = "ab\\."
        for _ in range(20000):
            s = "".join(rnd.choice(alpha) for _ in range(rnd.randint(0, 7)))
            mask = "".join(rnd.choice(alpha + "*?") for _ in range(rnd.randint(1, 6)))
            self.assertEqual(resources.MaskSet([mask]).match(s), resources.pattern_match(s, mask), (s, mask))
        both = resources.MaskSet(["textures\\a.dds", "textures\\*.thm", "textures\\?.dds"])
        self.assertEqual(both.hits("textures\\a.dds"), ["textures\\a.dds", "textures\\?.dds"])

    def test_ignore_name_quirks(self):
        # ENDS_WITH(n) of the engine: len > sizeof(n), sizeof counts the zero - one char of prefix is not enough
        self.assertTrue(resources.ignore_name("Thumbs.db"))
        self.assertFalse(resources.ignore_name("xThumbs.db"))
        self.assertTrue(resources.ignore_name("xxThumbs.db"))
        self.assertFalse(resources.ignore_name("a.sln"))
        self.assertTrue(resources.ignore_name("ab.sln"))
        self.assertFalse(resources.ignore_name("thumbs.db"))  # case-sensitive, as strcmp
        self.assertTrue(resources.ignore_name(".svn"))


class OgfTest(unittest.TestCase):
    def test_read_refs(self):
        data = model("wpn\\wpn_x, wpn\\wpn_x_det", children=[child("act\\a"), child("act\\b")],
                     motions="anims\\set\\*.omf,anims\\one", lod="dynamics\\lod")
        refs = ogf.read_refs(data)
        self.assertEqual(refs["textures"], ["wpn\\wpn_x", "wpn\\wpn_x_det", "act\\a", "act\\b"])
        self.assertEqual(refs["motions"], ["anims\\set\\*.omf", "anims\\one"])
        self.assertEqual(refs["lods"], ["dynamics\\lod"])
        refs2 = ogf.read_refs(chunk(1, b"\0" * 44) + chunk(ogf.OGF_S_MOTION_REFS2,
                                                          struct.pack("<I", 2) + b"a\\b\0c\\*.omf\0"))
        self.assertEqual(refs2["motions"], ["a\\b", "c\\*.omf"])

    def test_broken(self):
        # as IReader::find_chunk: a cut chunk is read up to the end, junk after the chunks is skipped (and kept)
        data = model("a\\b") + b"junk!"
        self.assertEqual(ogf.read_refs(data)["textures"], ["a\\b"])
        new, pairs, _ = ogf.rewrite_textures(data, lambda n: "@x\\" + n)
        self.assertTrue(new.endswith(b"junk!"))
        self.assertEqual(ogf.read_refs(new)["textures"], ["@x\\a\\b"])
        cut = chunk(1, b"\0" * 44) + chunk(ogf.OGF_S_MOTION_REFS, b"m\\a,m\\b\0")[:-3]
        self.assertEqual(ogf.read_refs(cut)["motions"], ["m\\a", "m"])
        with self.assertRaises(ogf.OgfError):
            ogf.read_refs(struct.pack("<II", ogf.OGF_TEXTURE | ogf.COMPRESSED, 4) + b"abcd")

    def test_hostile(self):
        # an untrusted package: a flood of empty chunks, children nested too deep, sizes past the end - an OgfError
        # or a clamped read, never a hang or a list of millions of chunks
        flood = struct.pack("<II", 5, 0) * (ogf.MAX_CHUNKS + 1)
        for parse in (ogf.read_refs, lambda d: ogf.rewrite_textures(d, lambda n: None)):
            with self.assertRaises(ogf.OgfError):
                parse(flood)
        deep = b""
        for _ in range(ogf.MAX_DEPTH + 2):
            deep = chunk(ogf.OGF_CHILDREN, chunk(0, deep))
        with self.assertRaises(ogf.OgfError):
            ogf.read_refs(deep)
        huge = struct.pack("<II", ogf.OGF_TEXTURE, 0xFFFFFFFF) + b"a\\b\0s\0"
        self.assertEqual(ogf.read_refs(huge)["textures"], ["a\\b"])
        self.assertEqual(ogf.rewrite_textures(huge, lambda n: "@x\\" + n)[0], huge)  # left in the tail as is
        refs2 = chunk(ogf.OGF_S_MOTION_REFS2, struct.pack("<I", 0xFFFFFFFF) + b"a\0")
        with self.assertRaises(ogf.OgfError):
            ogf.read_refs(refs2)

    def test_rewrite_keeps_the_rest(self):
        data = model("wpn\\wpn_x,act\\keep", children=[child("wpn\\wpn_x"), child("act\\keep")])
        new, pairs, problems = ogf.rewrite_textures(
            data, lambda n: "@my\\" + n if n == "wpn\\wpn_x" else None)
        self.assertEqual(problems, [])
        self.assertEqual(len(pairs), 2)
        refs = ogf.read_refs(new)
        self.assertEqual(refs["textures"], ["@my\\wpn\\wpn_x", "act\\keep", "@my\\wpn\\wpn_x", "act\\keep"])
        self.assertEqual(refs["shaders"], ["models\\model", "shader", "shader"])
        self.assertTrue(new.endswith(chunk(3, b"\x01\x02\x03\x04")))
        # nothing to rename: the same bytes
        same, pairs, _ = ogf.rewrite_textures(data, lambda n: None)
        self.assertIs(same, data)
        # a list the engine would cut (string256): left as is
        long_name = "t" * 250
        _, pairs, problems = ogf.rewrite_textures(model(long_name), lambda n: "@my_addon\\" + n)
        self.assertEqual(pairs, [])
        self.assertEqual(len(problems), 1)


class ReplacesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = util.TempDir()
        self.snaps = write_snapshot(os.path.join(self.tmp.path, "snaps"), "v1.0.0", BASE)

    def tearDown(self):
        self.tmp.cleanup()

    def addon(self, files, manifest=MANIFEST):
        return util.make_addon(self.tmp, "addon", manifest, files)

    def run_validate(self, folder):
        return validate_version({"kind": "folder", "path": folder}, snapshots=self.snaps, guessed_tag="v1.0.0",
                                snapshot_check=False)

    def test_replaces_by_the_engine_rules(self):
        folder = self.addon({
            "gamedata/textures/wpn/wpn_ak74.dds": b"dds",             # replaces (public)
            "gamedata/textures/wpn/WPN_AK74.thm": b"thm",             # replaces: names are compared in lower case
            "gamedata/textures/act/act_stalker.dds": b"dds",          # [private]: the base keeps its own
            "gamedata/textures/@other/wpn/wpn_ak74_bump.dds": b"dds",  # '@...' folder: not mounted
            "gamedata/textures/wpn/Thumbs.db": b"x",                   # ignore_name
            "gamedata/spawns/all.spawn": b"spawn",                     # replaces, breaks saves
            "gamedata/particles.xr": b"xr",                            # single-file base
            "gamedata/textures/my_addon/new.dds": b"dds",              # new
            "gamedata/meshes/my_addon/gun.ogf": model("wpn\\wpn_ak74,my_addon\\new,my_addon\\missing",
                                                      motions="base\\anim\\*.omf,my_addon\\nope"),
            "slots/menu_music/gamedata/sounds/weapons/ak_shot.ogg": b"ogg",  # the set of a slot: replaces too
        })
        rep, result = self.run_validate(folder)
        self.assertTrue(rep.ok, rep.to_text())
        rec = result["replaces"]
        self.assertEqual(rec["game"], "v1.0.0")
        self.assertEqual(rec["files"], sorted([
            "gamedata/particles.xr", "gamedata/spawns/all.spawn",
            "gamedata/textures/wpn/WPN_AK74.thm", "gamedata/textures/wpn/wpn_ak74.dds", "slots/menu_music/gamedata/sounds/weapons/ak_shot.ogg"]))
        self.assertEqual((rec["count"], rec["texts"], rec["truncated"]), (5, 0, False))
        warnings = rep.codes("warning")
        for code in ("replaces_save_breaking", "replaces_single_file_base", "package_alias_folder",
                     "replaces_texture_own_model", "private_mask_unused", "model_texture_missing",
                     "model_motion_missing"):
            self.assertIn(code, warnings, rep.to_text())
        self.assertIn("private_over_base", rep.codes("note"))
        self.assertIn("replaces_base", rep.codes("note"))
        missing = [i["message"] for i in rep.items if i["code"] == "model_texture_missing"][0]
        self.assertIn("my_addon\\missing", missing)
        self.assertNotIn("wpn\\wpn_ak74 ", missing)
        motion = [i["message"] for i in rep.items if i["code"] == "model_motion_missing"][0]
        self.assertIn("my_addon\\nope", motion)
        self.assertNotIn("*.omf", motion)
        vf = version_file("my_addon", "1.2.0", "v1.2.0", result)
        self.assertEqual(schema.validate(vf, "version-file"), [])
        self.assertEqual(vf["replaces"], rec)

    def test_texts_truncation_and_no_snapshot(self):
        files = {"gamedata/textures/x/t%03d.dds" % i: b"d" for i in range(205)}
        folder = self.addon(files, MANIFEST.replace("textures\\nothing\\*\n", ""))
        snaps = write_snapshot(os.path.join(self.tmp.path, "snaps2"), "v1.0.0",
                               BASE + ["textures\\x\\t%03d.dds" % i for i in range(205)])

        def run():
            return validate_version({"kind": "folder", "path": folder}, snapshots=snaps, guessed_tag="v1.0.0",
                                    snapshot_check=False)
        # no text of the base replaced: the record needs no gw_merge (nothing to merge)
        rep, result = run()
        rec = result["replaces"]
        self.assertEqual((rec["count"], rec["texts"], len(rec["files"]), rec["truncated"]), (205, 0, 200, True))
        self.assertNotIn("merge_plan_unavailable", rep.codes())
        # a text of the base replaced: its "texts" are decided by gw_merge plan only (decision 64 item 4)
        util.make_addon(self.tmp, "addon", MANIFEST.replace("textures\\nothing\\*\n", ""),
                        {"gamedata/configs/system.ltx": "[a]\n"})
        rep, result = run()
        self.assertIn("replaces_root_config", rep.codes("warning"))
        if util.find_gw_merge():
            rec = result["replaces"]
            self.assertEqual((rec["count"], rec["texts"], len(rec["files"]), rec["truncated"]), (206, 1, 200, True))
            self.assertEqual(schema.validate(version_file("my_addon", "1.2.0", "v1.2.0", result), "version-file"),
                             [])
        else:
            # no gw_merge: no record (its "texts" would be unknown), the count is still told, the check is not failed
            self.assertIsNone(result["replaces"])
            self.assertIn("merge_plan_unavailable", rep.codes("warning"))
            self.assertIn("replaces_base", rep.codes("note"))
            self.assertTrue(rep.ok)
            self.assertNotIn("replaces", version_file("my_addon", "1.2.0", "v1.2.0", result))
        # no snapshot: no record, the version file has no key
        rep, result = validate_version({"kind": "folder", "path": folder}, snapshot_check=False)
        self.assertIsNone(result["replaces"])
        self.assertNotIn("replaces", version_file("my_addon", "1.2.0", "v1.2.0", result))

    def test_location_and_private_bump(self):
        manifest = MANIFEST.replace("type    = content", "type    = location\n\n[location]\nsave_compat = 1") \
            .replace("textures\\nothing\\*", "textures\\my_addon\\*")
        folder = self.addon({"gamedata/spawns/all.spawn": b"s",
                             "gamedata/textures/my_addon/skin.dds": b"d",
                             "gamedata/textures/my_addon/skin_bump.dds": b"d"}, manifest)
        rep, result = self.run_validate(folder)
        self.assertNotIn("replaces_save_breaking", rep.codes("warning"))
        self.assertIn("private_texture_no_thm", rep.codes("warning"))
        self.assertEqual(result["replaces"]["files"], ["gamedata/spawns/all.spawn"])

    def test_too_many_wildcard_masks(self):
        # each mask with '*' / '?' is matched against every file: a hostile addon.ltx must not stall the CI
        many = "".join("textures\\*x%d*\n" % i for i in range(resources.limits.PRIVATE_WILDCARD_MAX + 3))
        folder = self.addon({"gamedata/textures/wpn/wpn_ak74.dds": b"d"}, MANIFEST + many)
        rep, _ = self.run_validate(folder)
        self.assertIn("private_too_many_masks", rep.codes("error"))
        masks, dropped = resources.private_masks({"private": ["a\\*"] * 3 + ["B/c.dds", "\\b\\c.dds"]})
        self.assertEqual((masks, dropped), (["a\\*", "b\\c.dds"], []))

    def test_refs_of_models_are_names_not_bytes(self):
        """@<id>\\ of a model: its texture / motion / LOD names; the geometry holds "@1\\" by chance"""
        geometry = chunk(3, b"\x00@1\\\x07@cz/\x00")
        folder = self.addon({
            "gamedata/meshes/my_addon/a.ogf": model("my_addon\\new") + geometry,
            "gamedata/meshes/my_addon/b.ogf": model("@other_lib\\wpn\\x", motions="@my_addon\\anim"),
            "gamedata/textures/my_addon/new.dds": b"d",
            "gamedata/textures/my_addon/new.thm": (b"\x12\x08\x00\x00\x10\x00\x00\x00A@2\\x\x01"
                                                  b"\x00\x00\x00@thm_lib\\bump\x00"),
        })
        rep, _ = self.run_validate(folder)
        bad = sorted(i["message"].split("\\")[0] for i in rep.items if i["code"] == "ref_without_depends")
        self.assertEqual(bad, ["@other_lib", "@thm_lib"], rep.to_text())

    def test_schema_rejects_bad_records(self):
        good = {"schema": 1, "id": "a", "version": "1.0.0", "tag": "v1.0.0", "files": [],
                "replaces": {"game": "v1.0.0", "count": 1, "texts": 0, "files": ["gamedata/x"], "truncated": False}}
        self.assertEqual(schema.validate(good, "version-file"), [])
        for bad in ({"count": -1}, {"files": ["gamedata/x"] * 2}, {"game": "a b"}, {"extra": 1},
                    {"files": ["x"] * 201}):
            rec = dict(good["replaces"], **bad)
            self.assertTrue(schema.validate(dict(good, replaces=rec), "version-file"), bad)


class RemapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = util.TempDir()
        self.base = {v: [1, None] for v in BASE + ["textures\\wpn\\wpn_hud.dds"]}
        manifest = MANIFEST.replace("textures\\nothing\\*\n", "").replace("\n", "\r\n")
        self.folder = util.make_addon(self.tmp, "addon", manifest, {
            "gamedata/textures/wpn/wpn_ak74.dds": b"dds",
            "gamedata/textures/wpn/wpn_ak74.thm": b"thm",
            "gamedata/textures/wpn/wpn_hud.dds": b"dds",       # named by a config: stays public
            "gamedata/textures/my_addon/own.dds": b"dds",      # not a base name: untouched
            "gamedata/configs/my_addon/hud.ltx": "[my_hud]\ntexture = wpn/wpn_hud ; the hud\n",
            "gamedata/meshes/my_addon/gun.ogf": model("wpn\\wpn_ak74,my_addon\\own,wpn\\wpn_hud",
                                                      children=[child("WPN\\wpn_ak74")]),
        })

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, rel):
        with open(os.path.join(self.folder, *rel.split("/")), "rb") as f:
            return f.read()

    def test_dry_run_writes_nothing(self):
        before = self.read("addon.ltx"), self.read("gamedata/meshes/my_addon/gun.ogf")
        rep = remap.remap_textures(self.folder, base_files=self.base, dry_run=True)
        self.assertTrue(rep.ok, rep.to_text())
        self.assertEqual((self.read("addon.ltx"), self.read("gamedata/meshes/my_addon/gun.ogf")), before)
        self.assertEqual(rep.data["remap"]["private_masks"], ["textures\\wpn\\wpn_ak74.dds",
                                                              "textures\\wpn\\wpn_ak74.thm"])
        self.assertEqual(rep.data["remap"]["public_kept"], ["wpn\\wpn_hud"])

    def test_remap(self):
        rep = remap.remap_textures(self.folder, base_files=self.base)
        self.assertTrue(rep.ok, rep.to_text())
        self.assertIn("remap_text_reference", rep.codes("warning"))
        manifest = self.read("addon.ltx")
        self.assertNotIn(b"\n", manifest.replace(b"\r\n", b""))  # CRLF kept
        text = manifest.decode("utf-8")
        self.assertIn("textures\\act\\act_stalker.dds\r\n; gw_addon_tool remap-textures", text)  # into [private]
        self.assertIn("textures\\wpn\\wpn_ak74.dds\r\ntextures\\wpn\\wpn_ak74.thm\r\n", text)
        refs = ogf.read_refs(self.read("gamedata/meshes/my_addon/gun.ogf"))
        self.assertEqual(refs["textures"], ["@my_addon\\wpn\\wpn_ak74", "my_addon\\own", "wpn\\wpn_hud",
                                            "@my_addon\\WPN\\wpn_ak74"])
        # the result passes the validator: the texture is private now, nothing of it replaces the base
        snaps = write_snapshot(os.path.join(self.tmp.path, "snaps"), "v1.0.0", list(self.base))
        vrep, result = validate_version({"kind": "folder", "path": self.folder}, snapshots=snaps,
                                        guessed_tag="v1.0.0", snapshot_check=False)
        self.assertTrue(vrep.ok, vrep.to_text())
        self.assertEqual(result["replaces"]["files"], ["gamedata/textures/wpn/wpn_hud.dds"])
        self.assertNotIn("model_texture_missing", vrep.codes("warning"))
        # a second run: nothing new
        again = remap.remap_textures(self.folder, base_files=self.base)
        self.assertEqual(again.data["remap"]["private_masks"], [])
        self.assertEqual(again.data["remap"]["models"], 0)

    def test_new_private_section_and_all(self):
        path = os.path.join(self.folder, "addon.ltx")
        with open(path, "wb") as f:
            f.write(b"[addon]\nid = my_addon\nversion = 1.2.0\n[lua]\nentry = my_addon_main\n")
        rep = remap.remap_textures(self.folder, all_textures=True, only=["textures\\my_addon\\*"])
        self.assertTrue(rep.ok, rep.to_text())
        self.assertTrue(self.read("addon.ltx").endswith(
            b"\n\n[private]\n; gw_addon_tool remap-textures: seen only as textures\\@my_addon\\...\n"
            b"textures\\my_addon\\own.dds\n"))
        # no snapshot and no --all: an error, nothing written
        rep = remap.remap_textures(self.folder)
        self.assertIn("remap_no_snapshot", rep.codes("error"))

    def test_unusable_manifest_edit_writes_nothing(self):
        # a texture whose name the ltx cuts (';' opens a comment): addon.ltx cannot take its mask - the models are not
        # rewritten either (they would name @<id>\ textures that stay public)
        os.makedirs(os.path.join(self.folder, "gamedata", "textures", "my_addon"), exist_ok=True)
        with open(os.path.join(self.folder, "gamedata", "textures", "my_addon", "a;b.dds"), "wb") as f:
            f.write(b"dds")
        before = self.read("addon.ltx"), self.read("gamedata/meshes/my_addon/gun.ogf")
        rep = remap.remap_textures(self.folder, all_textures=True)
        self.assertIn("remap_manifest_broken", rep.codes("error"))
        self.assertEqual((self.read("addon.ltx"), self.read("gamedata/meshes/my_addon/gun.ogf")), before)

    def test_leading_separator(self):
        with open(os.path.join(self.folder, "gamedata", "meshes", "my_addon", "gun.ogf"), "wb") as f:
            f.write(model("\\wpn\\wpn_ak74"))
        rep = remap.remap_textures(self.folder, base_files=self.base)
        self.assertTrue(rep.ok, rep.to_text())
        refs = ogf.read_refs(self.read("gamedata/meshes/my_addon/gun.ogf"))
        self.assertEqual(refs["textures"], ["@my_addon\\wpn\\wpn_ak74"])


class CliTest(unittest.TestCase):
    def test_addon_tool_commands(self):
        import gw_addon_tool
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            gw_addon_tool.main(["build-index", "--index", "x"])
        tmp = util.TempDir()
        try:
            folder = util.make_addon(tmp, "addon", MANIFEST, {"gamedata/textures/wpn/wpn_ak74.dds": b"d"})
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = gw_addon_tool.main(["remap-textures", folder, "--all", "--dry-run", "--format", "json"])
            self.assertEqual(code, 0, out.getvalue())
            self.assertIn("remap_summary", out.getvalue())
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
