"""'build': gamedata folder -> base_snapshot.json.gz (+ base_texts.tar.gz)."""

import datetime
import fnmatch
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor

import ltxparse
import luascan
import xmlscan
from common import ascii_lower, dump_json_gz, log, vdir, vnorm
from textnorm import normalize_bytes, text_sha256

SCHEMA = "gw-base-snapshot"
SCHEMA_VERSION = 1
GENERATOR_VERSION = "1.0.0"

TEXT_EXTS = (".script", ".ltx", ".xml", ".lua")  # the texts the engine merges (KindOfName)

# Engine roots, as InitSettings loads them (xray-16/src/xrEngine/x_ray.cpp): pSettings, pSettingsOpenXRay,
# pSettingsFE, pGameIni. pSettingsAuth is system.ltx again with a filter - not a separate tree.
ENGINE_ROOTS = (
    "configs\\system.ltx",
    "configs\\game.ltx",
    "configs\\openxray.ltx",
    "configs\\faction_editor_config.ltx",
)

# Not part of a release: git and editor files, repo tooling of gamedata (helpers/*.pl, tools/*.py).
# A pattern is fnmatch on the whole virtual path, '*' crosses folders: "*.pl" is any .pl file at any depth.
DEFAULT_EXCLUDES = (
    "*.pl",          # perl helpers of the gamedata repo (branch.pl, clone.pl, helpers\*.pl, ...)
    "helpers\\*",
    "tools\\*",
)

LUA_STD = {
    "_G", "_VERSION", "assert", "bit", "collectgarbage", "coroutine", "debug", "dofile", "error", "ffi", "gcinfo",
    "getfenv", "getmetatable", "io", "ipairs", "jit", "load", "loadfile", "loadstring", "math", "module", "newproxy",
    "next", "os", "package", "pairs", "pcall", "print", "rawequal", "rawget", "rawlen", "rawset", "require",
    "select", "setfenv", "setmetatable", "string", "table", "tonumber", "tostring", "type", "unpack", "xpcall",
    "self",
}


# -- file walk ------------------------------------------------------------------------------------------------

def excluded(rel, patterns):
    parts = rel.split("\\")
    if any(p.startswith(".") or p == "__pycache__" for p in parts):
        return True
    for pat in patterns:
        if fnmatch.fnmatchcase(rel, ascii_lower(pat.replace("/", "\\"))):
            return True
    return False


def walk(root, patterns):
    """{vpath: (abs path, size, mtime_ns)} and the sorted list of case collisions.

    Folders and files are visited in name order, so which of two paths that differ only in case gets into files
    (the first one) does not depend on the order the file system lists them in (Linux CI)."""
    files = {}
    collisions = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = vnorm(os.path.relpath(full, root))
            if excluded(rel, patterns):
                continue
            st = os.stat(full)
            if rel in files:
                collisions.append(rel)
                continue
            files[rel] = (full, st.st_size, st.st_mtime_ns)
    return files, sorted(collisions)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def hash_files(files, cache_path, workers):
    cache = {}
    if cache_path and os.path.isfile(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)
    result = {}
    todo = []
    for vp, (full, size, mtime) in files.items():
        c = cache.get(vp)
        if c and c[0] == size and c[1] == mtime:
            result[vp] = c[2]
        else:
            todo.append(vp)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for vp, digest in zip(todo, pool.map(lambda v: sha256_file(files[v][0]), todo)):
            result[vp] = digest
    if cache_path:
        new_cache = {vp: [files[vp][1], files[vp][2], result[vp]] for vp in files}
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(new_cache, f)
    return result, len(todo)


# -- parts ----------------------------------------------------------------------------------------------------

INCLUDE_LINE = re.compile(r'^[ \t]*#include[ \t]*"([^"]+)"', re.M)


def build_ltx(texts, all_files, read=None):
    """read(vpath) -> normalized text or None; default texts.get. The builder passes a reader that also opens
    files of other extensions (#include "x.inc" is loaded by the engine whatever the extension)."""
    read = read or texts.get
    trees = {}
    covered = set()
    for root in ENGINE_ROOTS:
        tree = ltxparse.LtxTree(root, read, all_files).load()
        trees[root] = tree.to_json()
        covered.update(tree.files)
    # Standalone ltx under configs (loaded by scripts with ini_file / by plugins): roots of what is left.
    rest = sorted(p for p in texts if p.startswith("configs\\") and p.endswith(".ltx") and p not in covered)
    rest_set = set(rest)
    included = set()
    for p in rest:
        for _no, line in ltxparse.split_lines(texts[p]):
            s = ltxparse.strip_comment(line.strip(ltxparse.WS))
            if s.startswith("#") and "#include" in s:
                inc = ltxparse.include_name(s)
                if inc and "*" not in inc:
                    target = vnorm(vdir(p) + inc)
                    if target != p:
                        included.add(target)
    standalone = []
    for p in rest:
        if p in covered or (p in included and p in rest_set):
            continue
        tree = ltxparse.LtxTree(p, read, all_files).load()
        trees[p] = tree.to_json()
        covered.update(tree.files)
        standalone.append(p)
    # Files that only form include cycles among themselves
    for p in rest:
        if p not in covered:
            tree = ltxparse.LtxTree(p, read, all_files).load()
            trees[p] = tree.to_json()
            covered.update(tree.files)
            standalone.append(p)
    other = sorted(p for p in texts if p.endswith(".ltx") and p not in covered)
    return {
        "engine_roots": list(ENGINE_ROOTS),
        "standalone_roots": standalone,
        "not_parsed": other,
        "trees": trees,
    }


def build_strings(texts):
    """{lang: {id: file}}: the files in name order, an id given twice takes the LATER file (CStringTable: the value
    of the later file replaces the earlier one). Duplicates: {lang: {id: [files in load order]}}."""
    strings = {}
    dups = {}
    for p in sorted(texts):
        m = re.match(r"^configs\\text\\([^\\]+)\\.+\.xml$", p)
        if not m:
            continue
        lang = m.group(1)
        table = strings.setdefault(lang, {})
        for sid, _line in xmlscan.string_ids(texts[p]):
            prev = table.get(sid)
            if prev is not None and prev != p:
                dups.setdefault(lang, {}).setdefault(sid, [prev]).append(p)
            table[sid] = p
    return strings, dups


def build_textures(texts):
    """{id: {xml, texture}}: as UICore::ReadTextureInfo, the files of textures_descr in name order, an id given
    twice takes the LATER file (ParseShTexInfo with override). Duplicates: {id: [files in load order]}."""
    out = {}
    dups = {}
    for p in sorted(texts):
        if not (p.startswith("configs\\ui\\textures_descr\\") and p.endswith(".xml")):
            continue
        for tid, owner, _line in xmlscan.texture_ids(texts[p]):
            prev = out.get(tid)
            if prev is not None and prev["xml"] != p:
                dups.setdefault(tid, [prev["xml"]]).append(p)
            out[tid] = {"xml": p, "texture": ascii_lower(owner).replace("/", "\\")}
    return out, dups


def build_xml(texts):
    out = {}
    for p in sorted(texts):
        if not (p.startswith("configs\\ui\\") and p.endswith(".xml")) or p.startswith("configs\\ui\\textures_descr\\"):
            continue
        item = {"paths": xmlscan.ui_paths(texts[p])}
        inc = xmlscan.includes(texts[p])
        if inc:
            item["includes"] = inc
        out[p] = item
    return out


def build_lua(texts):
    scans = {}
    for p in sorted(texts):
        if p.startswith("scripts\\") and p.endswith(".script"):
            scans[p] = luascan.scan(texts[p])
    modules = {}
    global_names = set()
    for p in scans:
        rel = p[len("scripts\\"):-len(".script")]
        if "\\" not in rel:
            global_names.add(rel)
    for p, sc in scans.items():
        rel = p[len("scripts\\"):-len(".script")]
        kind = "global" if "\\" not in rel else "require"
        name = rel if kind == "global" else rel.replace("\\", ".")
        funcs = {}
        for fname, f in sc["functions"].items():
            item = {"params": f["params"], "line": f["line"]}
            if f["vararg"]:
                item["vararg"] = True
            funcs[fname] = item
        mod = {"file": p, "kind": kind, "functions": funcs,
               "tables": sorted(sc["tables"]), "vars": sorted(sc["vars"]), "classes": sorted(sc["classes"])}
        if sc["requires"]:
            mod["requires"] = sorted(set(sc["requires"]))
        own_top = set(funcs) | set(sc["tables"]) | set(sc["vars"]) | set(sc["classes"])
        runtime = sorted(set(sc["runtime"]) - own_top)
        if runtime:
            mod["runtime"] = runtime
        modules[name] = mod
    # Functions that one module defines in another one: function sim_smart.foo() in other.script
    injected = {}
    external = {}
    for name, mod in modules.items():
        own = set(mod["tables"]) | set(mod["vars"]) | set(mod["classes"]) | \
            {f for f in mod["functions"] if "." not in f and ":" not in f}
        for fname, f in mod["functions"].items():
            head = re.split(r"[.:]", fname, 1)[0]
            if head != fname and head in global_names and head not in own:
                rest = re.split(r"[.:]", fname[len(head) + 1:], 1)[0]
                injected.setdefault(head, {})[rest] = {"from": name, "kind": "function", "line": f["line"]}
        sc = scans[mod["file"]]
        for field in sc["runtime_fields"]:
            head, rest = field.split(".", 1)
            rest = rest.split(".", 1)[0]
            if head in global_names and head not in own and head not in sc["locals"]:
                target = injected.setdefault(head, {})
                if rest not in target:
                    target[rest] = {"from": name, "kind": "field"}
        for a, _b, _line in sc["refs"]:
            if a in global_names or a in sc["locals"] or a in own or a in LUA_STD:
                continue
            external[a] = external.get(a, 0) + 1
    return {
        "modules": modules,
        # Names that scripts put into a module from outside its own top-level names: function m.f() (kind
        # "function") or m.x = ... at top level / inside functions (kind "field"); m.script itself included.
        "injected": injected,
        # Names used as X.y by the base that are neither modules nor locals: engine exports (level, alife, ...),
        # globals from _g.script and similar. 'check' treats them as known.
        "external_globals": sorted(external),
    }


def parse_intercepts(text):
    out = {}
    m = re.search(r"^\s*local\s+intercepts\s*=\s*\{\s*$", text, re.M)
    if not m:
        return None
    for line in text[m.end():].split("\n"):
        code = line.split("--", 1)[0].strip()
        if not code:
            continue
        if code.startswith("}"):
            break
        mm = re.match(r'^([A-Za-z_]\w*)\s*=\s*"([^"]*)"\s*,?$', code)
        if mm:
            out[mm.group(1)] = mm.group(2)
    return out


def parse_builtin_events(xrgame_dir):
    path = os.path.join(xrgame_dir, "addon_event_bus.cpp")
    if not os.path.isfile(path):
        return None
    text = open(path, "rb").read().decode("utf-8", errors="replace")

    def block(name):
        m = re.search(r"constexpr\s+pcstr\s+" + name + r"\s*\[\s*\]\s*=\s*\{(.*?)\}\s*;", text, re.S)
        if not m:
            return None
        body = "\n".join(line.split("//", 1)[0] for line in m.group(1).split("\n"))
        return re.findall(r'"((?:[^"\\]|\\.)*)"', body)

    names, schemas = block("kBuiltinNames"), block("kBuiltinSchemas")
    if not names or not schemas or len(names) != len(schemas):
        return None
    return dict(zip(names, schemas))


def build_events(texts, files, gamedata, events_meta, engine_src, auto):
    out = {"script": None, "builtin": None, "meta": None, "sources": {}}
    axr = texts.get("scripts\\axr_main.script")
    if axr is not None:
        out["script"] = parse_intercepts(axr)
        out["sources"]["script"] = "scripts\\axr_main.script (local intercepts)"
    used = set()
    for p, text in texts.items():
        if p.startswith("scripts\\") and p.endswith(".script") and "ScriptCallback" in text:
            used.update(name for name, _line in luascan.callbacks(text))
    out["used_by_base"] = sorted(used)
    meta_path = events_meta
    if not meta_path:
        inside = sorted(p for p in files if p.endswith("\\events_meta.json") or p == "events_meta.json")
        if inside:
            meta_path = files[inside[0]][0]
        elif auto:
            guess = os.path.normpath(os.path.join(gamedata, "..", "..", "xray-16", "src", "gw_plugins",
                                                  "events_meta.json"))
            if os.path.isfile(guess):
                meta_path = guess
    if meta_path:
        with open(meta_path, "rb") as f:
            out["meta"] = json.loads(f.read().decode("utf-8"))
        out["sources"]["meta"] = os.path.basename(meta_path)
    src = engine_src
    if not src and auto:
        guess = os.path.normpath(os.path.join(gamedata, "..", "..", "xray-16", "src", "xrGame"))
        if os.path.isdir(guess):
            src = guess
    if src:
        out["builtin"] = parse_builtin_events(src)
        if out["builtin"] is not None:
            out["sources"]["builtin"] = "addon_event_bus.cpp (kBuiltinNames/kBuiltinSchemas)"
    return out, meta_path, src


HELP_LINE = re.compile(r"^(\S+)\s+\((.*?)\)\s+---\s*(.*)$")


def parse_console_dump(path):
    """Output of the console command 'help' in a log: the last block between the listing markers."""
    text, _ = normalize_bytes(open(path, "rb").read())
    lines = text.split("\n")
    start = end = None
    for i, line in enumerate(lines):
        if "Command listing: start" in line:
            start, end = i, None
        elif "Command listing: end" in line and start is not None:
            end = i
    if start is None:
        return None
    out = {}
    for line in lines[start + 1: end if end is not None else len(lines)]:
        m = HELP_LINE.match(line.strip())
        if m and not line.startswith("Key:"):
            out[m.group(1)] = {"status": m.group(2).strip(), "info": m.group(3).strip()}
    return out


# -- texts blob -----------------------------------------------------------------------------------------------

def write_texts_tar(texts, path):
    """Deterministic tar.gz: names = virtual paths with '/', UTF-8 normalized texts, mtime 0, sorted."""
    with open(path, "wb") as raw, \
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for vp in sorted(texts):
            data = texts[vp].encode("utf-8")
            info = tarfile.TarInfo(vp.replace("\\", "/"))
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))


# -- main -----------------------------------------------------------------------------------------------------

def created_time():
    """UTC time of the build; SOURCE_DATE_EPOCH (reproducible builds convention) fixes it, so that a rebuild of the
    same gamedata gives a byte-identical base_snapshot.json.gz."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if epoch.isdigit():
        when = datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc)
    else:
        when = datetime.datetime.now(datetime.timezone.utc)
    return when.replace(microsecond=0).isoformat()


def git_head(path):
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def build(args):
    t0 = time.perf_counter()
    timings = {}
    gamedata = os.path.abspath(args.gamedata)
    patterns = ([] if args.no_default_excludes else list(DEFAULT_EXCLUDES)) + list(args.exclude or [])
    files, collisions = walk(gamedata, patterns)
    timings["walk"] = time.perf_counter() - t0
    log("files: %d (%.1f GB)" % (len(files), sum(f[1] for f in files.values()) / 1e9))

    t = time.perf_counter()
    if args.no_hash:
        hashes, hashed = {}, 0
    else:
        hashes, hashed = hash_files(files, args.hash_cache, args.workers)
    timings["hash"] = time.perf_counter() - t
    log("sha256: %d files hashed, %.1f s" % (hashed, timings["hash"]))

    t = time.perf_counter()
    texts = {}
    text_info = {}
    for vp in sorted(files):
        if vp.endswith(TEXT_EXTS):
            with open(files[vp][0], "rb") as f:
                text, info = normalize_bytes(f.read())
            texts[vp] = text
            info["sha256"] = text_sha256(text)
            info["size"] = len(text.encode("utf-8"))
            text_info[vp] = info
    timings["texts"] = time.perf_counter() - t
    log("texts: %d files, %.1f MB normalized" % (len(texts), sum(i["size"] for i in text_info.values()) / 1e6))

    def read_any(vp):
        """Text of a file for the ltx loader: an #include of another extension (x.inc) is a text too - it goes
        into texts (and the texts blob), as the engine loads it."""
        if vp in texts:
            return texts[vp]
        if vp not in files:
            return None
        with open(files[vp][0], "rb") as f:
            text, info = normalize_bytes(f.read())
        texts[vp] = text
        info["sha256"] = text_sha256(text)
        info["size"] = len(text.encode("utf-8"))
        text_info[vp] = info
        return text

    all_files = sorted(files)
    t = time.perf_counter()
    ltx = build_ltx(texts, all_files, read_any)
    timings["ltx"] = time.perf_counter() - t
    log("ltx: %d trees, %d sections in system.ltx, %.1f s" % (
        len(ltx["trees"]), len(ltx["trees"][ENGINE_ROOTS[0]]["sections"]), timings["ltx"]))

    t = time.perf_counter()
    strings, string_dups = build_strings(texts)
    textures, texture_dups = build_textures(texts)
    xml = build_xml(texts)
    timings["xml"] = time.perf_counter() - t

    t = time.perf_counter()
    lua = build_lua(texts)
    timings["lua"] = time.perf_counter() - t
    log("lua: %d modules, %.1f s" % (len(lua["modules"]), timings["lua"]))

    events, meta_path, engine_src = build_events(texts, files, gamedata, args.events_meta, args.engine_src,
                                                 not args.no_auto)
    if meta_path:
        log("events_meta.json: %s" % meta_path)
    if engine_src:
        log("engine sources (built-in events): %s" % engine_src)

    console = parse_console_dump(args.console_dump) if args.console_dump else None
    bindings = None
    if args.bindings_dump:
        btext, _ = normalize_bytes(open(args.bindings_dump, "rb").read())
        bindings = {"source": os.path.basename(args.bindings_dump), "lines": btext.split("\n")}

    os.makedirs(args.out, exist_ok=True)
    snapshot_path = os.path.join(args.out, "base_snapshot.json.gz")
    texts_path = os.path.join(args.out, "base_texts.tar.gz")

    t = time.perf_counter()
    texts_section = {"normalization": "utf-8 (fallback cp1251), BOM removed, CRLF -> LF", "files": text_info}
    if args.texts == "tar":
        write_texts_tar(texts, texts_path)
        with open(texts_path, "rb") as f:
            blob_sha = hashlib.sha256(f.read()).hexdigest()
        texts_section["storage"] = {"mode": "tar", "file": "base_texts.tar.gz", "sha256": blob_sha,
                                    "size": os.path.getsize(texts_path)}
    elif args.texts == "inline":
        texts_section["storage"] = {"mode": "inline"}
        for vp in text_info:
            text_info[vp]["text"] = texts[vp]
    else:
        texts_section["storage"] = {"mode": "none"}
    timings["texts_blob"] = time.perf_counter() - t

    snapshot = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generator": {"name": "gw_snapshot", "version": GENERATOR_VERSION},
        "game": {
            "tag": args.tag,
            "created": created_time(),
            "gamedata_commit": git_head(gamedata),
        },
        "files_format": "vpath -> [size, sha256]; vpath relative to gamedata, lowercase, backslashes",
        "files": {vp: [files[vp][1], hashes.get(vp)] for vp in sorted(files)},
        "case_collisions": collisions,
        "texts": texts_section,
        "ltx": ltx,
        "strings": strings,
        "strings_duplicates": string_dups,
        "textures_descr": textures,
        "textures_descr_duplicates": texture_dups,
        "xml": xml,
        "lua": lua,
        "events": events,
        "console": console,
        "engine_bindings": bindings,
    }
    t = time.perf_counter()
    raw_size = dump_json_gz(snapshot, snapshot_path)
    timings["write_json"] = time.perf_counter() - t
    timings["total"] = time.perf_counter() - t0
    log("written: %s (%.1f MB gz, %.1f MB json)" % (snapshot_path, os.path.getsize(snapshot_path) / 1e6,
                                                    raw_size / 1e6))
    if args.texts == "tar":
        log("written: %s (%.1f MB)" % (texts_path, os.path.getsize(texts_path) / 1e6))
    log("timings: " + ", ".join("%s %.1fs" % (k, v) for k, v in timings.items()))
    return 0
