"""Helpers of the tests: temp addons, zips, images, the GW workspace samples."""

import os
import shutil
import struct
import sys
import tempfile
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
VALIDATOR = os.path.dirname(HERE)
sys.path.insert(0, VALIDATOR)

GW = os.path.normpath(os.path.join(VALIDATOR, "..", ".."))
SAMPLES = os.path.join(GW, "GlobalWar", "addons", "tests")
# The GW workspace: catalog/validator next to catalog/index and catalog/template. The assembled index repo
# (catalog/assemble.py): tools/validator inside the index repo, no template
if os.path.isfile(os.path.join(VALIDATOR, "..", "..", "source.ltx")):
    INDEX_SKELETON = os.path.normpath(os.path.join(VALIDATOR, "..", ".."))
else:
    INDEX_SKELETON = os.path.normpath(os.path.join(VALIDATOR, "..", "index"))
TEMPLATE = os.path.normpath(os.path.join(VALIDATOR, "..", "template"))

GOOD_MANIFEST = """; a test addon
[addon]
id       = my_addon
version  = 1.2.0
type     = content
api_min  = 0.1.0
depends  = gw_npc, my_lib >=1.2 <2

[lua]
entry = my_addon_main
"""

GOOD_SCRIPT = """-- my_addon_main
function on_load(ctx)
  utils.debug("[my_addon] loaded %s", ctx.version)
end
"""


def png(w, h):
    raw = b"".join(b"\0" + b"\0\0\0" * w for _ in range(h))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def jpeg_header(w, h):
    """A minimal JPEG: SOI, SOF0 with the size, EOI (enough for the header parser)"""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + sof + b"\xff\xd9"


class TempDir:
    def __init__(self):
        self.path = tempfile.mkdtemp(prefix="gwcat_test_")

    def cleanup(self):
        shutil.rmtree(self.path, ignore_errors=True)

    def write(self, rel, data):
        p = os.path.join(self.path, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
        return p


def make_addon(tmp, rel="addon", manifest=GOOD_MANIFEST, files=None):
    """An addon folder: addon.ltx + gamedata/scripts/my_addon_main.script + files {rel: str|bytes}"""
    tmp.write(rel + "/addon.ltx", manifest)
    tmp.write(rel + "/gamedata/scripts/my_addon_main.script", GOOD_SCRIPT)
    for k, v in (files or {}).items():
        tmp.write(rel + "/" + k, v)
    return os.path.join(tmp.path, *rel.split("/"))


def make_zip(path, entries, method=zipfile.ZIP_DEFLATED):
    """entries: {name: bytes|str} or list of (name, data, extra dict)"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    items = entries.items() if isinstance(entries, dict) else entries
    with zipfile.ZipFile(path, "w", method) as zf:
        for item in items:
            name, data = item[0], item[1]
            extra = item[2] if len(item) > 2 else {}
            info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
            info.compress_type = method
            info.external_attr = extra.get("attr", 0o644 << 16)
            zf.writestr(info, data.encode("utf-8") if isinstance(data, str) else data)
    return path


def find_gw_merge():
    """$GW_MERGE, PATH, the xray-16 build folders (tools/addons_snapshot gwmerge.find_gw_merge)"""
    from gwcat import snapshot_tools
    try:
        gwmerge = snapshot_tools.import_module("gwmerge")
    except RuntimeError:
        return None
    return gwmerge.find_gw_merge()


def card_tree(**over):
    """A valid repo tree: addon.ltx + catalog/"""
    tree = {
        "addon.ltx": GOOD_MANIFEST.encode("utf-8"),
        "catalog/catalog.ltx": ("[catalog]\nname = My addon\nname_ru = Мой аддон\nsummary = A test\n"
                                "tags = weapons, traders\nauthors = Иван, Some Modder\nmaintainers = ivan-petrov\n"
                                "homepage = https://example.com/a//b ; a comment\n").encode("utf-8"),
        "catalog/description.md": b"text",
        "catalog/description.ru.md": "текст".encode("utf-8"),
        "catalog/icon.png": png(256, 256),
        "catalog/screenshots/01.png": png(640, 360),
        "catalog/screenshots/02.jpg": jpeg_header(1920, 1080),
    }
    tree.update(over)
    return {k: v for k, v in tree.items() if v is not None}
