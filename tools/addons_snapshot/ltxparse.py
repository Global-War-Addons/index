"""ltx (extended ini) parser that follows the engine loader CInifile::Load (xray-16/src/xrCore/Xr_ini.cpp).

What is reproduced (read-only mode, as pSettings/pGameIni are loaded):
    - lines: IReader::r_string ends a line at CR or LF (a lone CR is a line break too); _Trim strips every char
      <= ' ' (control chars included) from both ends;
    - comments: ';' anywhere; '//' only when the FIRST '/' of the line starts it and it comes before ';';
      a comment char between the first pair of quotes does not cut the line;
    - '#include "name"': path = folder of the including file + name; a name with "*.ltx" lists the folder of the
      mask recursively (CLocatorAPI::file_list without FS_RootOnly) and matches the path relative to that folder
      against the whole name with PatternMatch ('*' also matches '\\', case-sensitive against the lowercase
      names of the FS: the mask is not lowercased; several masks separated by ','), files in name order; the files
      found by a mask get the mask folder as their include base (as in the engine). A missing file is an engine
      R_ASSERT; any extension can be included;
    - '[name]:parent1, parent2': name lowercased, parents split as _GetItemCount/_GetItem do ('[a]:' and a trailing
      ',' give no extra parent, ',,' ends the list with an empty one), parents must already be loaded (r_section
      at parse time, otherwise Fatal "Can't find section"), their keys are copied into the section; a repeated
      section is Fatal "Duplicate section"; the section is stored when the next one starts or the file ends, so a
      section that is open in the including file is not yet visible to the included one;
    - 'key = value' / 'key': key names keep their case; a value with an odd number of quotes continues on the next
      lines (multi-line string) until the quotes are balanced; a following line with '[...]' or the end of the
      file stops it and the parser goes back to the line after the key (engine message "Incorrect inifile
      format"); the 4096-byte limit of the value buffer is not reproduced;
    - lines before the first section are ignored.

What is NOT reproduced: values are not stored (the texts blob has them), DLTX (not in the engine yet; see
09-content-addons-vfs.md section 5.1 - only the header scanner for addons understands it), "-skpsct".
Engine Fatal/assert situations are recorded in 'errors' instead of stopping.
"""

import re

from common import vdir, vnorm

# _Trim (xr_trims.cpp) strips every byte <= ' ': spaces, tabs, CR/LF and the other control chars (0x1a, 0x1c...)
WS = "".join(chr(c) for c in range(0x21))


def split_lines(text):
    """[(line number, line)] as IReader::r_string reads them: a line ends at CR or LF. The number is the one an
    editor shows (counted by LF), so the parts of a line split by a lone CR share it. Empty lines are kept (the
    engine skips them, the parser ignores them)."""
    out = []
    for no, line in enumerate(text.split("\n"), 1):
        if "\r" in line:
            out.extend((no, part) for part in line.split("\r"))
        else:
            out.append((no, line))
    return out


def strip_comment(s):
    """Line (already trimmed) without its comment, as in CInifile::Load."""
    comm = s.find(";")
    slash = s.find("/")
    if slash >= 0 and s[slash + 1: slash + 2] == "/" and (comm < 0 or slash < comm):
        comm = slash
    if comm >= 0:
        in_quot = False
        q1 = s.find('"')
        if 0 <= q1 < comm:
            q2 = s.find('"', q1 + 1)
            if q2 > comm:
                in_quot = True
        if not in_quot:
            s = s[:comm]
    return s


def include_name(s):
    """'#include "x"' -> x (the second item of the line split by quotes, trimmed), None if there is none."""
    parts = s.split('"')
    if len(parts) < 2:
        return None
    name = parts[1].strip(WS)
    return name or None


def pattern_to_regex(mask):
    """PatternMatch from LocatorAPI_defs.cpp: '*' any run (also '\\'), '?' any char, the rest literal."""
    out = []
    for ch in mask:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return re.compile("^" + "".join(out) + "$", re.S)


def engine_items(src, sep=","):
    """Items of a list as _GetItemCount + _GetItem (trimmed) give them (xr_trims.cpp): the count is the number of
    separators up to the first doubled one, plus one if anything follows the last counted separator. So 'b,' and
    'b' -> ['b'], '' -> [], 'b,,c' -> ['b', ''], ' ' -> ['']."""
    if not src:
        return []
    cnt = 0
    pos = 0
    while True:
        p = src.find(sep, pos)
        if p < 0:
            break
        pos = p + 1
        cnt += 1
        if src[pos: pos + 1] == sep:
            break
    if src[pos:]:
        cnt += 1
    parts = src.split(sep)
    return [parts[k].strip(WS) for k in range(cnt)]


def split_parents(text):
    return engine_items(text, ",")


class LtxTree:
    """One root (system.ltx, game.ltx, ...) loaded with its includes.

    read_text(vpath) -> normalized text or None; all_files: sorted list of all virtual paths (for '*.ltx' masks).
    """

    def __init__(self, root, read_text, all_files):
        self.root = root
        self.read_text = read_text
        self.all_files = all_files
        self.files = []
        self.file_index = {}
        self.sections = {}
        self.errors = []
        self._stack = []

    # -- public ---------------------------------------------------------------------------------------------

    def load(self):
        text = self.read_text(self.root)
        if text is None:
            self.errors.append({"kind": "root_missing", "file": self.root})
            return self
        self._load(self.root, text, vdir(self.root))
        return self

    def to_json(self):
        sections = {}
        for name, sec in self.sections.items():
            item = {"file": sec["file"], "line": sec["line"], "keys": sorted(sec["keys"])}
            if sec["parents"]:
                item["parents"] = sec["parents"]
            sections[name] = item
        return {"root": self.root, "files": self.files, "sections": sections, "errors": self.errors}

    # -- loader ---------------------------------------------------------------------------------------------

    def _error(self, kind, fidx, line, **extra):
        item = {"kind": kind, "file": self.files[fidx], "line": line}
        item.update(extra)
        self.errors.append(item)

    def _file_idx(self, vpath):
        idx = self.file_index.get(vpath)
        if idx is None:
            idx = len(self.files)
            self.files.append(vpath)
            self.file_index[vpath] = idx
        else:
            # The engine loads such a file again: its sections become duplicates (reported there).
            self.errors.append({"kind": "loaded_twice", "file": vpath, "line": 0})
        return idx

    def _store(self, cur):
        if cur is None:
            return
        if cur["name"] in self.sections:
            first = self.sections[cur["name"]]
            self._error("duplicate_section", cur["file"], cur["line"], section=cur["name"],
                        first=self.files[first["file"]] + ":" + str(first["line"]))
            return
        self.sections[cur["name"]] = cur

    def _load(self, vpath, text, path):
        if vpath in self._stack:
            self.errors.append({"kind": "include_cycle", "file": vpath, "line": 0})
            return
        self._stack.append(vpath)
        fidx = self._file_idx(vpath)
        numbered = split_lines(text)
        lines = [line for _no, line in numbered]
        n = len(lines)
        i = 0
        cur = None
        while i < n:
            lineno = numbered[i][0]
            s = strip_comment(lines[i].strip(WS))
            i += 1
            if s.startswith("#") and "#include" in s:
                inc = include_name(s)
                if inc is not None:
                    self._include(fidx, lineno, path, inc)
                else:
                    # no name in quotes: the engine opens the folder itself and fails R_ASSERT "Can't find include"
                    self._error("include_missing", fidx, lineno, include="")
                continue
            if s.startswith("["):
                self._store(cur)
                cur = None
                close = s.find("]")
                if close < 0:
                    self._error("bad_section", fidx, lineno, text=s)
                    continue
                name = s[1:close].lower()
                parents = []
                inh = s.find("]:")
                if inh >= 0:
                    for parent in split_parents(s[inh + 2:]):
                        parent_l = parent.lower()
                        parents.append(parent_l)
                        if parent_l not in self.sections:
                            self._error("missing_parent", fidx, lineno, section=name, parent=parent_l)
                cur = {"name": name, "file": fidx, "line": lineno, "parents": parents, "keys": set()}
                continue
            if cur is None:
                continue
            eq = s.find("=")
            if eq >= 0:
                key = s[:eq].strip(WS)
                value_raw = s[eq + 1:]
                if value_raw.count('"') % 2 == 1:
                    back = i
                    while value_raw.count('"') % 2 == 1:
                        # At the end of the file the engine keeps reading empty strings until the value buffer
                        # overflows - the same "Incorrect inifile format" and the same seek back as for '[...]'.
                        nxt = lines[i] if i < n else None
                        b = nxt.find("[") if nxt is not None else -1
                        if nxt is None or (b >= 0 and nxt.find("]", b) >= 0):
                            self._error("unclosed_quote", fidx, lineno, section=cur["name"], key=key)
                            i = back
                            break
                        value_raw += "\n" + nxt
                        i += 1
            else:
                key = s.strip(WS)
            if key:
                cur["keys"].add(key)
        self._store(cur)
        self._stack.pop()

    def _include(self, fidx, lineno, path, inc):
        full = vnorm(path + inc)
        if "*.ltx" in inc:
            inc_dir = vdir(full)
            # file_list: the whole name is the mask (_SequenceToList: ',' separates masks), matched as written
            # (PatternMatch is case-sensitive, the FS names are lowercase) against the path relative to inc_dir
            masks = [pattern_to_regex(m) for m in (x.strip(WS) for x in engine_items(inc, ",")) if m]
            found = []
            for f in self.all_files:
                if f.startswith(inc_dir):
                    rel = f[len(inc_dir):]
                    if any(rx.match(rel) for rx in masks):
                        found.append(f)
            for f in sorted(found):
                text = self.read_text(f)
                if text is not None:
                    self._load(f, text, inc_dir)
            return
        text = self.read_text(full)
        if text is None:
            self._error("include_missing", fidx, lineno, include=full)
            return
        self._load(full, text, vdir(full))


def effective_keys(sections, name, cache=None, depth=0):
    """Own keys of a section plus the keys of its parents (as the engine copies them)."""
    if cache is None:
        cache = {}
    if name in cache:
        return cache[name]
    sec = sections.get(name)
    if sec is None or depth > 64:
        return set()
    keys = set(sec.get("keys", ()))
    for parent in sec.get("parents", ()):
        keys |= effective_keys(sections, parent, cache, depth + 1)
    cache[name] = keys
    return keys


# -- lenient scanner for addon files (check) --------------------------------------------------------------

DLTX_HEADER = re.compile(r"^(!!|!|@)?\[([^\]]*)\](.*)$")


def scan_headers(text):
    """Section headers and includes of one ltx file, DLTX syntax understood (IXRay/Anomaly, 09 section 5.1).

    Yields dicts: {"line", "kind": "section"|"override"|"delete"|"safe"|"include", "name", "parents"}.
    Parents of a DLTX override may start with '!' (= remove that parent); they are returned as written.
    """
    numbered = split_lines(text)
    lines = [line for _no, line in numbered]
    i = 0
    n = len(lines)
    in_section = False
    while i < n:
        lineno = numbered[i][0]
        s = strip_comment(lines[i].strip(WS)).strip(WS)
        i += 1
        if s.startswith("#") and "#include" in s:
            inc = include_name(s)
            if inc:
                yield {"line": lineno, "kind": "include", "name": inc}
            continue
        m = DLTX_HEADER.match(s)
        if m:
            prefix, name, rest = m.group(1), m.group(2).lower(), m.group(3)
            parents = rest[1:] if rest.startswith(":") else None
            kind = {"!!": "delete", "!": "override", "@": "safe"}.get(prefix, "section")
            plist = split_parents(parents) if parents is not None else []
            yield {"line": lineno, "kind": kind, "name": name, "parents": [p.lower() for p in plist]}
            in_section = True
            continue
        if in_section:
            eq = s.find("=")
            if eq >= 0 and s[eq + 1:].count('"') % 2 == 1:
                value_raw = s[eq + 1:]
                back = i
                while value_raw.count('"') % 2 == 1:
                    nxt = lines[i] if i < n else None
                    b = nxt.find("[") if nxt is not None else -1
                    if nxt is None or (b >= 0 and nxt.find("]", b) >= 0):
                        i = back  # as LtxTree: the engine goes back to the line after the key
                        break
                    value_raw += "\n" + nxt
                    i += 1
