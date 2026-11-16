"""Checks of the unpacked package content: Lua capabilities (11 section 4.7 row 6), references @<id>\\ (14 section 8.3,
00 section 5a decision 7), namespaces (09 section 10.2, decision 3: warnings only), replacements of the base and the
references a snapshot lacks (12 V3, decisions 19-20: warnings only; through gw_snapshot.py check)."""

import gzip
import json
import os
import re

from . import luacaps, ogf, snapshot_tools

TEXT_EXT = (".ltx", ".xml", ".script", ".lua", ".txt", ".json")
# .omf: motions have no references to other files. .ogf: the names the engine reads (ogf.read_refs), not the bytes of
# the geometry - they hold "@<digits>\" by chance
REF_SCAN_EXT = TEXT_EXT + (".ogf", ".thm")
REF_RE = re.compile(rb"@([a-z0-9_]{1,64})[\\/]")
# A binary .thm: a name (bump, detail) is a zero-terminated string after a non-printable byte
REF_BIN_RE = re.compile(rb"(?:^|[\x00-\x1f])@([a-z0-9_]{1,64})[\\/][\x20-\x7e]*\x00")
OGF_REF_RE = re.compile(r"^@([a-z0-9_]{1,64})[\\/]")
LTX_SECTION_RE = re.compile(r"^\s*\[([^\]!][^\]]*)\]", re.M)
STRING_ID_RE = re.compile(r"<string\s+id\s*=\s*\"([^\"]+)\"")
SCAN_LIMIT = 64 * 1024 * 1024  # a file above this is not scanned for references (a level geometry ...)


def _read_text(path):
    with open(path, "rb") as f:
        data = f.read()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1251", "replace")


def check_lua(root, files, manifest, report):
    """Lua scan of every script of the package. -> (found caps sorted, review flags count)"""
    result = {}
    for name in sorted(files):
        if name.lower().endswith((".script", ".lua")):
            luacaps.scan_text(_read_text(os.path.join(root, *name.split("/"))), name, result)
    found = result.get("caps", {})
    declared = set(manifest.get("caps", []))
    for cap in sorted(found):
        where = found[cap]
        more = " (+%d more)" % (len(where) - 3) if len(where) > 3 else ""
        if cap not in declared:
            report.error("caps_undeclared", "the scripts use %s, [capabilities] does not declare it: %s%s" %
                         (cap, "; ".join(where[:3]), more))
        else:
            report.note("caps_used", "%s: %s%s" % (cap, "; ".join(where[:3]), more))
    for cap in sorted(declared - set(found)):
        report.note("caps_declared_unused", "[capabilities] %s is declared, the scanner did not find it" % cap)
    seen = {}
    for code, where, what in result.get("review", []):
        seen.setdefault(code, []).append("%s %s" % (where, what))
    for code, items in sorted(seen.items()):
        more = " (+%d more)" % (len(items) - 5) if len(items) > 5 else ""
        report.review(code, "%s%s" % ("; ".join(items[:5]), more))
    report.data["caps_found"] = sorted(found)
    report.data["scanner"] = sorted(seen)
    return sorted(found), len(seen)


def check_entry(files, manifest, report):
    """[lua] entry, [slot.<slot>] entry and panel: the module must be a script of the package itself. The engine runs
    [slot.*] entry / panel under the provider only when the layer of the addon serves the script (Lua
    gw_slots.check_provider_module, addon_lua.module_owner): a module of the base or of another addon is not used"""
    names = {n.lower() for n in files}
    entry = manifest.get("lua_entry")
    if entry and "gamedata/scripts/%s.script" % entry.lower() not in names:
        report.error("lua_entry_missing", "[lua] entry = %s: no gamedata/scripts/%s.script in the package" %
                     (entry, entry), "addon.ltx")
    for s in manifest.get("slots", []):
        set_scripts = "%s/gamedata/scripts/" % s["files"].lower()
        # entry: the common content or the set of the provider (both are layers of the addon while it is chosen)
        module = s.get("entry") or ""
        if module and ("gamedata/scripts/%s.script" % module.lower() not in names and
                       set_scripts + "%s.script" % module.lower() not in names):
            report.error("slot_entry_missing", "[slot.%s] entry = %s: neither gamedata/scripts/%s.script nor "
                         "%s%s.script is in the package" % (s["slot"], module, module, set_scripts, module),
                         "addon.ltx")
        # panel: the common content only - the appearance dialog loads it while the provider is not chosen (its set is
        # not mounted then)
        module = s.get("panel") or ""
        if module and "gamedata/scripts/%s.script" % module.lower() not in names:
            in_set = set_scripts + "%s.script" % module.lower() in names
            report.error("slot_panel_missing", "[slot.%s] panel = %s: no gamedata/scripts/%s.script in the package%s" %
                         (s["slot"], module, module,
                          " (it lies in the set %s: the panel module must be in the common gamedata/scripts/, the "
                          "dialog loads it while the provider is not chosen)" % set_scripts if in_set else ""),
                         "addon.ltx")


def check_refs(root, files, manifest, report):
    """@<other_id>\\ in texts and models needs <other_id> in depends (00 section 5a decision 7)"""
    own = manifest.get("id")
    deps = set(manifest.get("depends_ids", []))
    bad = {}
    for name in sorted(files):
        if not name.lower().endswith(REF_SCAN_EXT) or files[name] > SCAN_LIMIT:
            continue
        with open(os.path.join(root, *name.split("/")), "rb") as f:
            data = f.read()
        if name.lower().endswith(".ogf"):
            try:
                refs = ogf.read_refs(data)
            except ogf.OgfError:
                continue  # resources.read_models reports it
            found = [OGF_REF_RE.match(r.lower()) for k in ("textures", "motions", "lods") for r in refs[k]]
            found = [m.group(1) for m in found if m]
        else:
            pattern = REF_BIN_RE if name.lower().endswith(".thm") else REF_RE
            found = [m.group(1).decode("ascii") for m in pattern.finditer(data)]
        for ref in found:
            if ref != own and ref not in deps:
                bad.setdefault(ref, []).append(name)
    for ref, where in sorted(bad.items()):
        report.error("ref_without_depends", "@%s\\ is referenced, but '%s' is not in [addon] depends: %s" %
                     (ref, ref, ", ".join(sorted(set(where))[:5])))


def check_namespaces(root, files, manifest, report, base_files=None):
    """09 section 10.2 (decision 3): recommendations, warnings only. base_files - vpaths of the snapshot (or None)"""
    own = manifest.get("id", "")
    prefix = own + "_"
    new_sections = []
    for name in sorted(files):
        low = name.lower()
        if not low.startswith("gamedata/"):
            continue
        vpath = low[len("gamedata/"):].replace("/", "\\")
        base_name = os.path.basename(low)
        if low.endswith(".ltx") and base_name.startswith("mod_"):
            for m in LTX_SECTION_RE.finditer(_read_text(os.path.join(root, *name.split("/")))):
                sec = m.group(1).strip()
                if sec and not sec.lower().startswith(prefix) and not sec.startswith("!"):
                    new_sections.append("%s [%s]" % (name, sec))
        if base_files is None or vpath in base_files:
            continue
        if low.endswith(".script") and low.startswith("gamedata/scripts/"):
            rel = low[len("gamedata/scripts/"):]
            if not (rel.startswith(prefix) or rel.startswith(own + "/") or rel == own + ".script"):
                report.warning("namespace_script", "a new script outside the namespace: name it %s<name>.script or put "
                                                   "it into scripts/%s/" % (prefix, own), name)
        if low.endswith(".xml") and low.startswith("gamedata/configs/text/") and not base_name.startswith("mod_"):
            # st_<id>_... (the convention of the game) passes too: the id of the addon is in the string id
            ids = [i for i in STRING_ID_RE.findall(_read_text(os.path.join(root, *name.split("/"))))
                   if own not in i.lower()]
            if ids:
                report.warning("namespace_string", "%d new string id(s) without the id of the addon (%s...): %s" %
                               (len(ids), prefix, ", ".join(ids[:5])), name)
    if new_sections:
        more = " (+%d more)" % (len(new_sections) - 5) if len(new_sections) > 5 else ""
        report.warning("namespace_section", "new sections of DLTX files without the prefix %s: %s%s" %
                       (prefix, "; ".join(new_sections[:5]), more))


def load_snapshot_files(snapshot_path):
    """vpath -> [size, sha256] of a base_snapshot.json.gz"""
    with gzip.open(snapshot_path, "rt", encoding="utf-8") as f:
        return json.load(f)["files"]


def check_replacements(manifest, report, base_files, plan):
    """Text files of the package that replace files of the base whole and that the engine merges with the base by atoms
    (addon_merge.cpp PlanContentMergeFiles: the public files of every content layer of the addon - gamedata\\ and the
    sets of [slot.*]; not [private], not hidden, not a DLTX / XML patch mod_*). -> text vpaths replaced (for the delta).
    plan - resources.plan_texts (gw_merge plan, decisions 63, 64 item 4: no Python fallback). The list of all the
    replaced files (the rules of the engine) - resources.check_resources."""
    # every text a layer serves (not only the files of the package listing: the plan reads the folder itself)
    texts = sorted({v for layer in plan.values() for v, note in layer.items()
                    if note != "patch_file" and v in base_files})
    if texts and not manifest.get("game_version"):
        report.warning("game_version_missing", "%d text file(s) of the base are replaced whole, [addon] game_version "
                                               "is not set: the base version of the delta is guessed (15 section 7.2)"
                       % len(texts), "addon.ltx")
    report.data["replaced_texts"] = texts
    return texts


def check_snapshot_refs(snapshot_path, addon_dir, report, old_snapshot=None):
    """gw_snapshot.py check: references of the addon that the snapshot lacks - warnings (decision 20)"""
    args = ["check", snapshot_path, addon_dir, "--format", "json"]
    if old_snapshot:
        args += ["--old-snapshot", old_snapshot]
    try:
        data, _ = snapshot_tools.run_json(args)
    except (RuntimeError, OSError) as e:
        report.warning("snapshot_check_failed", "gw_snapshot.py check did not run: %s" % e)
        return
    for p in data.get("problems", []):
        report.warning("base_ref_" + p.get("kind", "unknown"), "%s: %s" % (p.get("ref", ""), p.get("why", "")),
                       "gamedata/" + p.get("file", "").replace("\\", "/") + ":%s" % p.get("line", ""))
