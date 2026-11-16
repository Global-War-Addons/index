"""Shared helpers: virtual paths, gzip json io, console output."""

import gzip
import io
import json
import sys
import zlib


# The engine lowers the names of the file system and of the merge byte by byte, ASCII only (MergeIndexName, the
# Lower of gw_merge, ContentPatchLower): str.lower() would also lower Cyrillic and other letters, and a path with
# such a letter in upper case would name another file than the engine and gw_merge see
_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def ascii_lower(text):
    """A-Z -> a-z, nothing else (the engine's lower case of a name)"""
    return text.translate(_ASCII_LOWER)


def vnorm(path):
    """Virtual path as the engine FS sees it on Windows: ASCII lowercase, backslashes, no '.', '..' resolved."""
    parts = []
    for part in ascii_lower(path.replace("/", "\\")).split("\\"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "\\".join(parts)


def vdir(path):
    """Folder of a virtual path with a trailing backslash ('' for a file in the root)."""
    i = path.rfind("\\")
    return path[: i + 1] if i >= 0 else ""


class ToolError(Exception):
    """An error of a run the tools report as one line ("error: ...", exit code 2) instead of a traceback"""


class BrokenJsonError(ToolError, ValueError):
    """A JSON the tools read (a delta, a file of the catalog, a report of gw_merge) is broken: not JSON or a duplicate
    key. A ValueError too: the callers that skip a broken delta (pairs, as the engine) catch ValueError"""


# The strict JSON of the delta, as the engine reads it (xrCore/ContentMerge/DeltaJson.cpp) and as the validator of the
# catalog does (catalog/validator/gwcat/strictjson.py): strict UTF-8, no duplicate key, no NaN / Infinity (a number
# token like 1e400 is a number, as there), no raw control character in a string, no lone surrogate in a \\u escape, at
# most MAX_JSON_DEPTH arrays / objects one in another. A UTF-8 BOM is taken, as the engine and the launcher take it (the validator refuses it in the files of the
# index only, which nobody edits by hand)
MAX_JSON_DEPTH = 64  # DeltaJson.cpp kMaxDepth


def _no_constant(name):
    raise ValueError("%s is not JSON" % name)


def _check_tree(value):
    """Lone surrogates (json takes "\\ud800") and the depth, walked without recursion"""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list)):
            if depth > MAX_JSON_DEPTH:
                raise ValueError("the values are nested deeper than %d" % MAX_JSON_DEPTH)
            for key, child in (item.items() if isinstance(item, dict) else enumerate(item)):
                if isinstance(key, str):
                    stack.append((key, depth))
                stack.append((child, depth + 1))
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("a lone surrogate in a \\u escape") from None


def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            # json.load keeps the last value silently: a delta with two "files" or two entries of one vpath would be
            # read differently by another reader (the engine, the launcher) - such a file is broken, not ambiguous
            raise ValueError("duplicate key %r" % key)
        seen[key] = value
    return seen


def loads_json(data, what):
    """JSON text (str or UTF-8 bytes) with duplicate keys refused. what - the file or the command, for the message"""
    try:
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        if data.startswith("\ufeff"):
            data = data[1:]
        value = json.loads(data, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
        _check_tree(value)
        return value
    except RecursionError:
        raise BrokenJsonError("%s is not valid JSON: the values are nested deeper than %d" %
                              (what, MAX_JSON_DEPTH)) from None
    except ValueError as e:  # JSONDecodeError, UnicodeDecodeError and the duplicate key are ValueError
        raise BrokenJsonError("%s is not valid JSON: %s" % (what, e)) from None


def load_json(path):
    """A JSON file the tools did not write themselves (a delta, a file of the catalog, a report): loads_json"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise ToolError("cannot read %s: %s" % (path, e)) from None
    return loads_json(data, path)


def load_json_gz(path):
    """A snapshot: missing or broken - ToolError (exit code 2, not a traceback with exit code 1 = conflicts)"""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, EOFError, zlib.error, ValueError) as e:
        raise ToolError("cannot read the snapshot %s: %s" % (path, e)) from None


def dump_json_gz(obj, path, indent=None):
    """Deterministic gzip (mtime 0) of a sorted-keys json. Returns the uncompressed size in bytes."""
    data = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=indent,
                      separators=(",", ":") if indent is None else (",", ": ")).encode("utf-8")
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as f:
            f.write(data)
    return len(data)


def utf8_console():
    """Windows console defaults to cp1252/cp866 here: print UTF-8 instead of crashing on Cyrillic."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, io.UnsupportedOperation):
                pass


def log(message):
    print(message, file=sys.stderr, flush=True)

