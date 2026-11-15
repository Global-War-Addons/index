"""The Lua scanner of the catalog (11-catalog-distribution.md 4.7): the capabilities a script really uses.

    ffi       ffi.*, require "ffi", package.loadlib, package.cpath  (native code)
    os_exec   os.execute, io.popen
    fs_write  io.open(f, "w"/"a"/"+"), io.output(...), os.remove, os.rename, os.tmpname, lfs.mkdir/rmdir/touch/link/lock*

Review flags (the scanner "fired", 11 section 5.2 - a moderator looks at the version): load / loadstring / loadfile /
dofile of a string that is not a literal, string.dump, the debug library beyond traceback / getinfo, io.open with a mode
that is not a literal, os / io / package / ffi / debug / lfs taken as a value or indexed with [...] (aliasing that
hides the calls above).

Tokens come from tools/addons_snapshot/luascan.py (tokenize, scan) - the same lexer as the snapshot.
A light lexical scan, not a sandbox: it finds the plain uses; a determined author can hide a call, which is why the
first version and every expansion of capabilities go to a moderator.
"""

from . import snapshot_tools

KNOWN_CAPS = ("ffi", "fs_write", "os_exec")
LIB_NAMES = {"os", "io", "package", "ffi", "debug", "lfs"}
FS_WRITE_LFS = {"mkdir", "rmdir", "touch", "link", "lock", "lock_dir", "unlock", "setmode"}
DEBUG_OK = {"traceback", "getinfo"}
DYNAMIC_LOADERS = {"load", "loadstring", "loadfile", "dofile"}


def _luascan():
    return snapshot_tools.import_module("luascan")


def scan_text(text, path, result):
    """Adds the findings of one script to result {"caps": {cap: [where]}, "review": [(code, where, what)]}"""
    luascan = _luascan()
    toks = luascan.tokenize(text)
    try:
        info = luascan.scan(text)
        own = set(info.get("locals", ())) | {f.split(".")[0].split(":")[0] for f in info.get("functions", {})}
    except Exception:  # the scan is a heuristic; a script it does not understand is still tokenized
        own = set()
    caps = result.setdefault("caps", {})
    review = result.setdefault("review", [])

    def cap(name, line, what):
        caps.setdefault(name, []).append("%s:%d %s" % (path, line, what))

    def flag(code, line, what):
        review.append((code, "%s:%d" % (path, line), what))

    n = len(toks)
    for i, (kind, val, line) in enumerate(toks):
        prev = toks[i - 1][1] if i > 0 else ""
        nxt = toks[i + 1][1] if i + 1 < n else ""
        field = toks[i + 2][1] if i + 2 < n and toks[i + 2][0] == "name" else ""
        if kind == "name" and prev not in (".", ":") and val in LIB_NAMES and val not in own:
            if nxt in (".", ":") and field:
                what = val + "." + field
                if val == "ffi":
                    cap("ffi", line, what)
                elif val == "package" and field in ("loadlib", "cpath"):
                    cap("ffi", line, what)
                elif what in ("os.execute", "io.popen"):
                    cap("os_exec", line, what)
                elif what in ("os.remove", "os.rename", "os.tmpname", "io.output"):
                    cap("fs_write", line, what)
                elif val == "lfs" and field in FS_WRITE_LFS:
                    cap("fs_write", line, what)
                elif what == "io.open":
                    _io_open(toks, i + 3, line, cap, flag)
                elif val == "debug" and field not in DEBUG_OK:
                    flag("lua_debug_library", line, what)
            elif nxt == "[":
                flag("lua_dynamic_access", line, val + "[...]")
            elif nxt not in (".", ":") and prev not in ("local", "function"):
                # `local x = os`, `f(io)`: the library goes around under another name
                flag("lua_library_alias", line, val + " used as a value")
        elif kind == "name" and val == "require" and prev not in (".", ":"):
            arg = toks[i + 2] if nxt == "(" and i + 2 < n else (toks[i + 1] if i + 1 < n else None)
            if arg and arg[0] == "str" and arg[1] == "ffi":
                cap("ffi", line, 'require "ffi"')
        elif kind == "name" and val in DYNAMIC_LOADERS and prev not in (".", ":", "function") and val not in own:
            if nxt == "(" and i + 2 < n:
                arg = toks[i + 2]
                literal = arg[0] == "str" and i + 3 < n and toks[i + 3][1] in (")", ",")
                if not literal:
                    flag("lua_dynamic_code", line, val + "(<not a literal>)")
        elif kind == "name" and val == "string" and nxt == "." and field == "dump":
            flag("lua_string_dump", line, "string.dump")
    return result


def _io_open(toks, i, line, cap, flag):
    """io.open( <path> [, <mode>] ): toks[i] is '(' when called"""
    if i >= len(toks) or toks[i][1] != "(":
        flag("lua_library_alias", line, "io.open taken as a value")
        return
    depth = 0
    j = i
    comma_at = None
    while j < len(toks):
        v = toks[j][1]
        if toks[j][0] == "op" and v in ("(", "{", "["):
            depth += 1
        elif toks[j][0] == "op" and v in (")", "}", "]"):
            depth -= 1
            if depth == 0:
                break
        elif toks[j][0] == "op" and v == "," and depth == 1 and comma_at is None:
            comma_at = j
        j += 1
    if comma_at is None:
        return  # io.open(path): read mode
    mode = toks[comma_at + 1] if comma_at + 1 < len(toks) else None
    closes = comma_at + 2 < len(toks) and toks[comma_at + 2][1] in (")", ",")
    if mode and mode[0] == "str" and closes:
        if any(c in mode[1] for c in "wa+"):
            cap("fs_write", line, 'io.open(..., "%s")' % mode[1])
    else:
        flag("lua_io_open_dynamic_mode", line, "io.open(..., <mode is not a literal>)")


def declared_caps(ini):
    """[capabilities] of addon.ltx: a key alone or key = true/1/yes/on declares it -> (set, unknown keys)"""
    out, unknown = set(), []
    for key, value in ini.items("capabilities"):
        k = key.lower()
        if k not in KNOWN_CAPS:
            unknown.append(key)
            continue
        if value == "" or value.lower() in ("true", "1", "yes", "on"):
            out.add(k)
    return out, unknown
