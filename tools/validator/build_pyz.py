#!/usr/bin/env python3
"""Builds gw_addon_tool.pyz - the author tool as one file (review 2026-11-06 F/A5, decision 63): an author without git
downloads it and runs `python gw_addon_tool.pyz check <addon folder>`, no clone of the index repo.

    python build_pyz.py --out <folder or file.pyz> [--index DIR] [--snapshot-tools DIR]

What goes in (the layout of the index repo, so the tool finds everything as in a clone):
    index/<source.ltx, tags.ltx, reserved_ids.ltx, moderation.ltx, game_releases.ltx>
    index/tools/validator/   gw_addon_tool.py, gw_catalog.py, gwcat/, schemas/   (no tests)
    index/tools/addons_snapshot/*.py                                              (no tests)
The archive cannot be run from inside the zip (the schemas are read as files, gw_snapshot.py runs as a script): its
__main__.py unpacks it once into a cache folder named after the content hash (BUILD_ID) and runs gw_addon_tool from
there. The build is reproducible: sorted names, fixed times, the same bytes give the same archive.

Where it is published: the workflow tool.yml of the index repo builds it on every change of tools/ or of the files of
the index above and attaches it to a release of the index (`.../index/releases/latest/download/gw_addon_tool.pyz`).
"""

import argparse
import hashlib
import io
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

INDEX_FILES = ("source.ltx", "tags.ltx", "reserved_ids.ltx", "moderation.ltx", "game_releases.ltx")
SKIP_DIRS = {"tests", "__pycache__", ".pytest_cache"}
FIXED_TIME = (2020, 1, 1, 0, 0, 0)

MAIN = r'''"""gw_addon_tool.pyz: unpacks itself once into a cache folder and runs gw_addon_tool (see build_pyz.py)"""
import os
import shutil
import sys
import tempfile
import zipfile


def _cache_root():
    base = os.environ.get("GW_ADDON_TOOL_CACHE") or os.environ.get("LOCALAPPDATA") or \
        os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "gw_addon_tool")


def _unpack(archive):
    with zipfile.ZipFile(archive) as zf:
        build = zf.read("BUILD_ID").decode("ascii").strip()
        target = os.path.join(_cache_root(), build)
        if os.path.isfile(os.path.join(target, ".complete")):
            return target
        os.makedirs(_cache_root(), exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=build + ".", dir=_cache_root())
        for info in zf.infolist():
            name = info.filename
            if not name.startswith("payload/") or name.endswith("/"):
                continue
            rel = name[len("payload/"):]
            parts = rel.split("/")
            if not rel or any(p in ("", ".", "..") for p in parts) or ":" in rel:
                raise SystemExit("gw_addon_tool.pyz: a bad name in the archive: %r" % name)
            dest = os.path.join(tmp, *parts)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
        open(os.path.join(tmp, ".complete"), "w").close()
    try:
        os.replace(tmp, target)
    except OSError:
        # another run unpacked the same build meanwhile: take it
        shutil.rmtree(tmp, ignore_errors=True)
        if not os.path.isfile(os.path.join(target, ".complete")):
            raise
    return target


# python gw_addon_tool.pyz: __file__ is <the .pyz>/__main__.py
root = _unpack(os.path.dirname(os.path.abspath(__file__)))
validator = os.path.join(root, "index", "tools", "validator")
sys.path.insert(0, validator)
import gw_addon_tool  # noqa: E402

sys.exit(gw_addon_tool.main())
'''


def _walk(folder):
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if fn.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(dirpath, fn)
            yield full, os.path.relpath(full, folder).replace(os.sep, "/")


def collect(index_dir, snapshot_tools):
    """-> {archive name under payload/: bytes}"""
    files = {}
    for name in INDEX_FILES:
        with open(os.path.join(index_dir, name), "rb") as f:
            files["index/" + name] = f.read()
    for full, rel in _walk(HERE):
        top = rel.split("/", 1)[0]
        if rel in ("gw_addon_tool.py", "gw_catalog.py") or top in ("gwcat", "schemas"):
            with open(full, "rb") as f:
                files["index/tools/validator/" + rel] = f.read()
    for full, rel in _walk(snapshot_tools):
        if rel.endswith(".py") and "/" not in rel:
            with open(full, "rb") as f:
                files["index/tools/addons_snapshot/" + rel] = f.read()
    return files


def build(out, index_dir, snapshot_tools):
    files = collect(index_dir, snapshot_tools)
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode("utf-8") + b"\0" + hashlib.sha256(files[name]).digest())
    build_id = h.hexdigest()[:16]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        def put(name, data):
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
        put("__main__.py", MAIN.encode("utf-8"))
        put("BUILD_ID", build_id.encode("ascii") + b"\n")
        for name in sorted(files):
            put("payload/" + name, files[name])
    if os.path.isdir(out):
        out = os.path.join(out, "gw_addon_tool.pyz")
    with open(out, "wb") as f:
        f.write(b"#!/usr/bin/env python3\n" + buf.getvalue())
    return out, build_id


def main(argv=None):
    import gw_catalog
    from gwcat import snapshot_tools
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="a folder (gw_addon_tool.pyz is written there) or the .pyz path")
    ap.add_argument("--index", help="the index repo (default: the one next to the validator)")
    ap.add_argument("--snapshot-tools", help="tools/addons_snapshot (default: found as by the validator)")
    args = ap.parse_args(argv)
    index_dir = args.index or gw_catalog.find_index()
    tools = args.snapshot_tools or snapshot_tools.find()
    if not index_dir or not tools:
        raise SystemExit("the index repo or tools/addons_snapshot is not found: --index / --snapshot-tools")
    out, build_id = build(os.path.abspath(args.out), index_dir, tools)
    with open(out, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    print("%s  build %s  sha256 %s" % (out, build_id, digest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
