"""Strict JSON for the catalog: UTF-8 without BOM, no duplicate keys, no NaN / Infinity; canonical output.

Every JSON file of the index (index.json, stats.json, files/, history/, state/, the delta) is read through load_*:
a duplicate key is ambiguous (parsers keep the first or the last one), so the launcher and CI could see different data.

The rules are the ones of the reader of the delta in the engine (xrCore/ContentMerge/DeltaJson.cpp) and of
tools/addons_snapshot/common.py loads_json, so a file one of them takes is taken by all: strict UTF-8, no duplicate
key, no NaN / Infinity (a number token out of the range of a double, 1e400, is a number there too), no raw control
character in a string, no lone surrogate in a \\u escape, at most
MAX_DEPTH arrays / objects one in another. One difference, on purpose: a UTF-8 BOM - the engine, the launcher and the
tools take it (an editor of an author writes it), the files of the index (written only by its CI) must not have it.
"""

import hashlib
import json


class StrictJsonError(ValueError):
    pass


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise StrictJsonError("duplicate key %r" % key)
        out[key] = value
    return out


MAX_DEPTH = 64  # DeltaJson.cpp kMaxDepth


def _constant(name):
    raise StrictJsonError("%s is not JSON" % name)


def _check_tree(value):
    """Lone surrogates (json takes "\\ud800") and the depth: walked without recursion"""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list)):
            if depth > MAX_DEPTH:
                raise StrictJsonError("the values are nested deeper than %d" % MAX_DEPTH)
            children = item.items() if isinstance(item, dict) else enumerate(item)
            for key, child in children:
                if isinstance(key, str):
                    stack.append((key, depth))
                stack.append((child, depth + 1))
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                raise StrictJsonError("a lone surrogate in a \\u escape")


def loads_bytes(data, what="JSON"):
    if data.startswith(b"\xef\xbb\xbf"):
        raise StrictJsonError("%s starts with a UTF-8 BOM" % what)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise StrictJsonError("%s is not UTF-8: %s" % (what, e))
    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
        _check_tree(value)
        return value
    except RecursionError:
        raise StrictJsonError("%s: the values are nested deeper than %d" % (what, MAX_DEPTH))
    except StrictJsonError as e:
        raise StrictJsonError("%s: %s" % (what, e))
    except ValueError as e:
        raise StrictJsonError("%s: %s" % (what, e))


def load_file(path):
    with open(path, "rb") as f:
        return loads_bytes(f.read(), path)


def dumps(obj, compact=False):
    """Canonical text: sorted keys, UTF-8 characters as is, LF, a final newline. compact - index.json (size, R22)."""
    if compact:
        text = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        text = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)
    return (text + "\n").encode("utf-8")


def write_file(path, obj, compact=False):
    """Writes only when the bytes change (an untouched file keeps its mtime and git sees no change). -> changed"""
    data = dumps(obj, compact)
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    with open(path, "wb") as f:
        f.write(data)
    return True


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
