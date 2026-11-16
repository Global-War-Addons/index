"""The strict simplified LTX of addon.ltx / catalog.ltx / the ltx files of the index, ported from the engine.

Source of the rules: xray-16/src/xrGame/addon_host.cpp - ParseSimpleIni, IsValidId, ReadAddonVersion,
ParseAddonVersion, ParseVersion, ParseDepends (11-catalog-distribution.md 4.7: "the same strict parser as the engine").
Keep this file in step with them: an addon.ltx the engine refuses must not pass the catalog, and back.

    [section]            case-insensitive name; text after ']' (inheritance) - error
    key = value          a line without '=' is a key with an empty value; "quoted value" loses the quotes
    ; comment, // comment  to the end of the line (also inside quotes, as the engine does)
    #include ...         error
    a repeated section continues the first one
"""

import re

ID_RE = re.compile(r"^[a-z0-9_]{1,64}\Z")  # \Z: "$" would also take a trailing line break
ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


class LtxError(ValueError):
    def __init__(self, message, line=None):
        ValueError.__init__(self, message + ((" (line %d)" % line) if line else ""))
        self.line = line


def _trim(s):
    return s.strip(" \t\r\n")


class Ini:
    """sections: list of (name lower, [(key, value, line)]) in file order; a repeated section is merged."""

    def __init__(self):
        self.sections = []

    def find(self, name):
        name = name.lower()
        for sec_name, lines in self.sections:
            if sec_name == name:
                return lines
        return None

    def names(self):
        return [n for n, _ in self.sections]

    def get(self, section, key):
        """The first non-empty value of key (case-insensitive), None when absent or empty - as SimpleIni::get"""
        lines = self.find(section)
        if lines is None:
            return None
        for k, v, _ in lines:
            if k.lower() == key.lower() and v != "":
                return v
        return None

    def has_key(self, section, key):
        lines = self.find(section)
        return lines is not None and any(k.lower() == key.lower() for k, _, _ in lines)

    def keys(self, section):
        lines = self.find(section)
        return [] if lines is None else [k for k, _, _ in lines]

    def items(self, section):
        lines = self.find(section)
        return [] if lines is None else [(k, v) for k, v, _ in lines]

    def get_bool(self, section, key, default=False):
        v = self.get(section, key)
        if v is None:
            return default
        return v.lower() in ("true", "on", "yes") or v == "1"


def _comment_at(line, url_safe):
    cut = line.find(";")
    slashes = line.find("//")
    if url_safe:
        # catalog.ltx / the ltx of the index (not read by the engine): "//" opens a comment only at the start of the
        # line or after a space, so "https://..." survives. The engine (addon.ltx) cuts every "//".
        while slashes > 0 and line[slashes - 1] not in " \t":
            slashes = line.find("//", slashes + 2)
    if slashes >= 0 and (cut < 0 or slashes < cut):
        cut = slashes
    return cut


def parse(text, url_safe=False):
    """text (str, already decoded) -> Ini; LtxError on a syntax error, as ParseSimpleIni (merge_repeated = true).
    url_safe: the dialect of catalog.ltx and of the index files - see _comment_at"""
    ini = Ini()
    current = None
    if text.startswith("﻿"):
        text = text[1:]
    for line_no, raw in enumerate(text.split("\n"), 1):
        line = raw
        cut = _comment_at(line, url_safe)
        if cut >= 0:
            line = line[:cut]
        line = _trim(line)
        if not line:
            continue
        if line[0] == "[":
            close = line.find("]")
            if close < 0:
                raise LtxError("section header without ']'", line_no)
            if _trim(line[close + 1:]):
                raise LtxError("unexpected text after section header (inheritance is not supported)", line_no)
            name = _trim(line[1:close]).lower()
            if not name:
                raise LtxError("empty section name", line_no)
            current = None
            for sec_name, lines in ini.sections:
                if sec_name == name:
                    current = lines
            if current is None:
                current = []
                ini.sections.append((name, current))
            continue
        if line[0] == "#":
            raise LtxError("directives such as #include are not supported", line_no)
        if current is None:
            raise LtxError("value outside of any section", line_no)
        eq = line.find("=")
        if eq < 0:
            key, value = line, ""
        else:
            key, value = _trim(line[:eq]), _trim(line[eq + 1:])
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]
        if not key:
            raise LtxError("empty key", line_no)
        current.append((key, value, line_no))
    return ini


def decode(data, what):
    """bytes of a catalog ltx -> str; the catalog wants UTF-8 (the engine also takes other bytes, the catalog does not:
    the id, names and the card are shown by the launcher). Raises LtxError."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise LtxError("%s is not UTF-8 (%s)" % (what, e))


# ---- ids and versions -------------------------------------------------------------------------------------------------

def is_valid_id(s):
    return bool(ID_RE.match(s or ""))


def read_version(text, pos, min_parts):
    """ReadAddonVersion: min_parts..3 dot-separated numbers of up to 9 digits at text[pos] -> ((M, m, p), pos) or None"""
    parts = []
    while len(parts) < 3:
        start = pos
        while pos < len(text) and "0" <= text[pos] <= "9":
            pos += 1
        digits = pos - start
        if digits == 0 or digits > 9:
            return None
        parts.append(int(text[start:pos]))
        if len(parts) == 3 or pos >= len(text) or text[pos] != ".":
            break
        pos += 1
    if len(parts) < min_parts:
        return None
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts), pos


def parse_version(text):
    """[addon] version: exactly MAJOR.MINOR.PATCH (ParseAddonVersion) -> tuple or None"""
    r = read_version(text or "", 0, 3)
    return r[0] if r and r[1] == len(text) else None


def parse_api_version(text):
    """[plugin] api_min / [addon] api_min for the catalog: strict MAJOR.MINOR.PATCH within the limits of the engine
    (GWP_MAKE_VERSION: MAJOR, MINOR <= 1023, PATCH <= 4095) -> tuple or None. The engine also takes "1", "1.2" with
    a warning (ParseVersion); the catalog does not."""
    v = parse_version(text)
    if v is None or v[0] > 0x3FF or v[1] > 0x3FF or v[2] > 0xFFF:
        return None
    return v


def api_is_set(v):
    """api_min as the engine takes it (addon_host.cpp: 0 - no requirement): None and 0.0.0 are "not set" (11 section 6:
    the compatibility is not specified)"""
    return bool(v) and tuple(v) != (0, 0, 0)


def api_value(text):
    """api_min of a manifest as the index keeps it: '' when not set, invalid or 0.0.0 (the same as not set)"""
    return text if text and api_is_set(parse_api_version(text)) else ""


def api_major(v):
    """The "major" of 11 section 6: "1" for 1.x.y and higher, "0.x" for 0.x.y (a breaking change before 1.0 is the
    second number). v - tuple or None -> "" (unknown); 0.0.0 - not set, "" as well"""
    if not api_is_set(v):
        return ""
    return str(v[0]) if v[0] >= 1 else "0.%d" % v[1]


def version_str(v):
    return "%d.%d.%d" % tuple(v)


VERSION_OPS = [(">=", "ge"), ("<=", "le"), ("==", "eq"), (">", "gt"), ("<", "lt"), ("=", "eq")]


def parse_depends(value):
    """ParseDepends: 'a, b>=1.2.0, c >=1.0 <2.0' -> [{"id", "conditions": [(op, (M, m, p))], "text"}].
    Raises LtxError with the message of the engine."""
    out = []
    for raw in value.split(","):
        entry = _trim(raw)
        if not entry:
            continue
        pos = 0
        while pos < len(entry) and entry[pos] in ID_CHARS:
            pos += 1
        dep_id = entry[:pos]
        if not is_valid_id(dep_id):
            raise LtxError("'%s': expected an addon id (a-z 0-9 _ , up to 64 chars) first" % entry)
        conditions = []
        while True:
            while pos < len(entry) and entry[pos] in " \t":
                pos += 1
            if pos >= len(entry):
                break
            op = next((name for sym, name in VERSION_OPS if entry.startswith(sym, pos)), None)
            if op is None:
                raise LtxError("'%s': expected a version condition such as >=1.2.0 after the id" % entry)
            pos += len(next(sym for sym, name in VERSION_OPS if entry.startswith(sym, pos)))
            while pos < len(entry) and entry[pos] in " \t":
                pos += 1
            r = read_version(entry, pos, 1)
            if r is None:
                raise LtxError("'%s': expected a version (MAJOR[.MINOR[.PATCH]], numbers) after the operator" % entry)
            conditions.append((op, r[0]))
            pos = r[1]
        out.append({"id": dep_id, "conditions": conditions, "text": entry})
    return out


def condition_holds(op, want, have):
    c = (have > want) - (have < want)
    return {"lt": c < 0, "le": c <= 0, "eq": c == 0, "ge": c >= 0, "gt": c > 0}[op]


def parse_id_list(value):
    """load_after / patches: ids through commas -> (ids, bad items)"""
    ids, bad = [], []
    for raw in (value or "").split(","):
        item = _trim(raw)
        if not item:
            continue
        if is_valid_id(item):
            if item not in ids:
                ids.append(item)
        else:
            bad.append(item)
    return ids, bad


def split_list(value):
    """A free comma list (tags, authors, maintainers): trimmed, empty items dropped"""
    return [x for x in (_trim(p) for p in (value or "").split(",")) if x]


def game_tags():
    """tools/addons_snapshot/gametag.py: the release tags of the game parsed and compared as the engine does
    (MergeParseTag). One implementation for the tools and the catalog: imported, not copied (snapshot_tools.py)"""
    from . import snapshot_tools
    return snapshot_tools.import_module("gametag")


def parse_game_tag(tag):
    """[addon] game_version / the tags of the game releases (15 section 2.2): v1.4.0, 0.5.5-patch4, 0.5.6-Beta ...
    -> a comparable gametag.GameTag (v1.4 == 1.4.0) or None"""
    return game_tags().parse(_trim(tag or ""))  # the ltx trim (spaces, tabs), as the engine: not str.strip()


def game_tag_sort_key(tag):
    """sorted(tags, key=...): the release tags of the game in their order, other strings before them"""
    return game_tags().sort_key(tag)


def same_game_tag(a, b):
    """The same release of the game: equal strings or equal tags (v1.4 and 1.4.0, -Beta and -beta)"""
    return game_tags().same(a, b)
