"""gw_merge plan for the tools (00-roadmap.md 5a, decision 63; review 2026-11-06 section 4.2 #2): which text files of an
addon the engine merges and what a compatibility patch decides, computed by the library of the engine (xrCore
ContentMerge/Plan - the code addon_merge.cpp and addon_content.cpp call).

What the plan decides here (each of these was ported by hand to Python before and drifted - review B1-B4, B9, B10; the
ports are removed, gw_merge is required - gwmerge.require_gw_merge):
    the layers of an addon      <addon>\\gamedata\\ and the set of each [slot.*] (its files folder, default
                                slots\\<slot>\\), the state of the addon (depends / patches present)
    the files of a layer        not hidden (Windows: the attribute, POSIX: a name '.*'), not ignore_name, not under a
                                folder '@...', not under a [private] mask, KindOfName (.ltx .xml .script .lua)
    the patch files             mod_<stem>_*.<ext> under configs\\ with a target in the base or in a layer (IsPatchFile)
    a compatibility patch       applied / stale (merge\\targets.ltx) / not_a_side (a target does not replace the file)
                                and the conflicts it decides (plan --merge)

The plan lists the files several addons replace (or one addon with a delta); with --all-files it lists every text file a
layer serves, so the texts of ONE addon are a plan of that addon alone: a record per file with the side of the addon
(the layer that serves it) and the note of the file (patch_file ...). The depends / patches the addon needs are added
as empty stubs (the catalog checks one package, the engine would not activate it without them).
"""

import os
import shutil
import tempfile

import gwmerge
from common import ToolError, ascii_lower, load_json

PLAN_FORMAT = 1  # main.cpp kPlanFormat: a new member keeps it, a changed meaning of a member is 2
ORDER_RESOLVED = ("order", "load_after", "pair", "slot")  # a conflict the order of the stack decided: unresolved


class PlanUnavailable(ToolError):
    """gw_merge has no plan of PLAN_FORMAT"""


def unavailable(exe):
    """None - gw_merge plan works; else why not: no gw_merge (exe None), a gw_merge too old (gwmerge.too_old) - the
    message of gwmerge.require_gw_merge, with how to get the tool. A plan of another plan_format is found by run()
    (PlanUnavailable). The validator of the catalog calls it (catalog/validator gwcat/resources.py plan_texts)"""
    if not exe:
        return gwmerge.missing_message()
    why = gwmerge.too_old(exe)
    return gwmerge.too_old_message(exe, why) if why else None


def write_base_list(path, names, tag=""):
    """The base as --base-list reads it (base_source.h ReadBaseList): one vpath per line, '# game_tag <tag>'"""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# gw_base_files 1\n")
        if tag:
            f.write("# game_tag %s\n" % tag)
        for name in sorted(set(names)):
            if name and "\n" not in name and "\r" not in name:
                f.write(name + "\n")
    return path


def run(exe, addons, base_list=None, snapshot=None, texts=None, merge=False, slots=None, lua=None, work=None,
        what=None, all_files=False):
    """gw_merge plan -> its JSON (dict). The base: base_list (a file of write_base_list) or snapshot (+ texts, the tar of
    its texts; the snapshot names it next to itself by default). slots - {slot: the id of the chosen provider} ([slots]
    of a list). all_files - --all-files: a record of every text file the layers serve. The case of the names is the one
    of the OS the tool runs on (the engine of that OS)"""
    own = work is None
    work = work or tempfile.mkdtemp(prefix="gw_plan_")
    try:
        args = ["plan"]
        args += ["--base-list", base_list] if base_list else ["--base-snapshot", snapshot]
        if texts and not base_list:
            args += ["--base-texts", texts]
        for addon in addons:
            args += ["--addon", addon]
        if slots:
            fd, listed = tempfile.mkstemp(prefix="list_", suffix=".ltx", dir=work)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write("[slots]\n" + "".join("%s = %s\n" % kv for kv in sorted(slots.items())))
            args += ["--list", listed]
        if merge:
            args.append("--merge")
            if lua:
                args.append("--check-lua=" + lua)
        if all_files:
            args.append("--all-files")
        fd, out = tempfile.mkstemp(prefix="plan_", suffix=".json", dir=work)
        os.close(fd)
        args += ["-o", out]
        gwmerge.run(exe, args, what=what)
        plan = load_json(out)
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)
    if not isinstance(plan, dict) or plan.get("plan_format") != PLAN_FORMAT or not isinstance(plan.get("files"), list):
        raise PlanUnavailable("gw_merge plan wrote plan_format %r, the tools read %d" %
                              (plan.get("plan_format") if isinstance(plan, dict) else None, PLAN_FORMAT))
    if all_files and plan.get("all_files") is not True:
        raise PlanUnavailable("gw_merge plan did not take --all-files (no \"all_files\": true in its plan)")
    broken = _broken_member(plan)
    if broken:
        raise PlanUnavailable("the plan of gw_merge (plan_format %d) has no valid %s" % (PLAN_FORMAT, broken))
    return plan


def _is_dicts(value, *keys):
    """A list of objects, each with these members as strings"""
    return isinstance(value, list) and all(isinstance(x, dict) and all(isinstance(x.get(k), str) for k in keys)
                                           for x in value)


def _broken_member(plan):
    """The member of the plan the tools read that is missing or of another type (None - all there): a plan of
    plan_format 1 always has them, a broken one must give PlanUnavailable (a ToolError), not a KeyError / TypeError"""
    if not _is_dicts(plan.get("addons", []), "dir", "id"):
        return "addons[]"
    if not _is_dicts(plan.get("layers", []), "id", "folder"):
        return "layers[]"
    for f in plan["files"]:
        if not isinstance(f, dict) or not isinstance(f.get("file"), str) or not isinstance(f.get("note"), str) or \
                not isinstance(f.get("status"), str) or not _is_dicts(f.get("sides"), "addon", "layer") or \
                not _is_dicts(f.get("patches", []), "id", "status", "note"):
            return "files[] record"
        result = f.get("result")
        if result is not None and (not isinstance(result, dict) or not isinstance(result.get("conflicts", []), list)
                                   or not all(isinstance(c, dict) for c in result.get("conflicts", []))):
            return "files[].result of %s" % f["file"]
    return None


# ---- the texts of one addon ------------------------------------------------------------------------------------------

def _read_ini(path):
    """[(section, [(key, value)])] as gw_merge plan reads addon.ltx (main.cpp PlanReadIni): ';' comments, a BOM"""
    out = []
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return out
    if text.startswith("\ufeff"):
        text = text[1:]
    for line in text.split("\n"):
        line = line.split(";", 1)[0].strip(" \t\r")
        if not line:
            continue
        if line.startswith("["):
            close = line.find("]")
            out.append((line[1:close if close >= 0 else None].strip(" \t\r"), []))
        elif out:
            key, eq, value = line.partition("=")
            out[-1][1].append((key.strip(" \t\r"), value.strip(" \t\r") if eq else ""))
    return out


def addon_id_and_slots(addon):
    """(the id gw_merge plan gives the addon, the names of its [slot.*]): only to name it the provider of its slots in
    [slots] of the plan - the folders of the sets, whether the engine takes a provider (SlotFilesValid: files = a folder
    inside the addon), the masks, the state come from the plan"""
    ini = _read_ini(os.path.join(addon, "addon.ltx"))
    addon_id = ""
    slots = []
    for section, lines in ini:
        name = ascii_lower(section)
        if name == "addon" and not addon_id:
            addon_id = next((ascii_lower(v) for k, v in lines if ascii_lower(k) == "id"), "")
        elif name.startswith("slot.") and len(name) > 5 and name[5:] not in slots:
            slots.append(name[5:])
    return addon_id or ascii_lower(os.path.basename(os.path.normpath(addon))), slots


# Windows: a longer path the game (and gw_merge plan, std::filesystem) may not open without the long paths of the OS -
# the files under it are not seen. A name longer than the index takes (WalkNameTooLong) the plan skips with a warning
MAX_PATH = 260


def _longest(addon):
    """The longest path of a file of the addon (its .git and .gw_merge left out)"""
    longest = 0
    for dirpath, dirnames, filenames in os.walk(addon):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".gw_merge")]
        for name in filenames:
            longest = max(longest, len(os.path.join(dirpath, name)))
    return longest


def _stub(work, n, addon_id, patches=()):
    """An addon without content (a dependency of the checked addon, the target of a patch)"""
    root = os.path.join(work, "stub%d" % n)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "addon.ltx"), "w", encoding="utf-8", newline="\n") as f:
        f.write("[addon]\nid = %s\ntype = content\n" % addon_id + ("patches = %s\n" % ", ".join(patches)
                                                                    if patches else ""))
    return root


def _same_dir(a, b):
    return os.path.normcase(os.path.normpath(os.path.abspath(a))) == os.path.normcase(os.path.normpath(os.path.abspath(b)))


def find_addon(plan, folder):
    """The entry of addons[] of the plan of this folder (None - not an addon: no addon.ltx)"""
    return next((a for a in plan.get("addons", []) if _same_dir(a.get("dir", ""), folder)), None)


def _path_of(folder, name):
    return os.path.join(folder, *name.split("\\")) if os.sep != "\\" else folder + name


# The note of a file of the addon alone (plan --all-files) -> the note of AddonTexts: one_addon - the file of the base
# the addon replaces (no text of the base in a --base-list, as a file of two addons would note it)
_NOTE_OF = {"one_addon": "no_base_text"}


class AddonTexts:
    """The text files of one addon as the engine takes them for the merge.
    info - its entry of addons[] of the plan (id, type, active, reason, patches, depends, private ...) or None;
    layers - {"gamedata" | "slot.<slot>": {name: (path, note)}}: the files each layer serves (the name as the plan
    spells it: '\\', ASCII lower case on Windows); note - "no_base_text" (in the base: merged when another addon
    replaces it too), "no_base", "patch_file", "read_error"; warnings - why a layer gives nothing"""

    def __init__(self):
        self.info = None
        self.layers = {}
        self.warnings = []


def addon_texts(exe, addon, base_names, tag=""):
    """The texts of the addon by gw_merge plan --all-files over the base names (vpaths of gamedata, '\\') ->
    AddonTexts. One run per layer configuration of the addon alone (its depends / patches as stubs): no slot chosen -
    the files of gamedata\\, each slot alone - the files of its set; and one with all its slots chosen - the patch files
    (their targets in the base or in any layer of the addon)"""
    addon = os.path.abspath(addon)
    out = AddonTexts()
    addon_id, slots = addon_id_and_slots(addon)
    work = tempfile.mkdtemp(prefix="gw_plan_")
    try:
        base = write_base_list(os.path.join(work, "base.txt"), base_names, tag)
        longest = _longest(addon)
        if os.name == "nt" and longest >= MAX_PATH:
            out.warnings.append("a path of %s is %d chars long (MAX_PATH %d): the game and gw_merge plan may not open "
                                "the files under it - shorten the folders" % (addon, longest, MAX_PATH))
        stubs = []

        def plan_of(chosen):
            return run(exe, stubs + [addon], base_list=base, slots=chosen, work=work, what=addon, all_files=True)

        plan = plan_of({})
        me = find_addon(plan, addon)
        if me is not None and not me.get("active") and me.get("reason", "").startswith(("depends:", "patches:")):
            # the catalog checks the package alone: what it depends on is assumed installed
            needed = [i for i in list(me.get("depends", [])) + list(me.get("patches", [])) if i != me.get("id")]
            stubs = [_stub(work, n, i) for n, i in enumerate(dict.fromkeys(needed))]
            plan = plan_of({})
            me = find_addon(plan, addon)
        out.info = me
        if me is None:
            out.warnings.append("%s has no addon.ltx: gw_merge plan takes no file of it" % addon)
            return out
        if not me.get("active"):
            out.warnings.append("gw_merge plan does not activate %s (%s): no file of it is merged" %
                                (me.get("id"), me.get("reason")))
            return out
        addon_id = me["id"]
        if me.get("type") == "location" and not any(x.get("owner") == addon_id for x in plan.get("layers", [])):
            out.warnings.append("a location addon: its gamedata\\ is a location pack, the engine serves its files "
                                "whole and merges none of them")
        # A patch file of the addon is one with all its sets mounted (the catalog takes the targets from every layer
        # of the package): a target in the set of a slot makes a file of gamedata\\ a patch file too
        patch_files = set()
        if slots:
            patch_files = {f["file"] for f in plan_of({s: addon_id for s in slots})["files"]
                           if f["note"] == "patch_file"}
        layers = {}
        for label, chosen, layer_id in [("gamedata", {}, "addon:" + addon_id)] + \
                [("slot." + s, {s: addon_id}, "slot:%s:%s" % (s, addon_id)) for s in slots]:
            if chosen:
                plan = plan_of(chosen)
            for warning in plan.get("warnings", []):
                if warning.startswith("FS: layer '") and warning not in out.warnings:
                    out.warnings.append(warning)  # a name too long for the index: not mounted
            folder = next((x["folder"] for x in plan.get("layers", []) if x["id"] == layer_id), None)
            if folder is None:
                continue  # no such folder: the layer is not mounted
            files = {}
            for f in plan["files"]:
                side = next((s for s in f["sides"] if s["addon"] == addon_id), None)
                if side is not None and side["layer"] == layer_id:
                    note = "patch_file" if f["file"] in patch_files else _NOTE_OF.get(f["note"], f["note"])
                    files[f["file"]] = (_path_of(folder, f["file"]), note)
            layers[label] = files
        if any(layers.values()):
            out.layers = layers  # no file in any layer: none listed
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---- the compatibility patch of a pair (plan --merge) ----------------------------------------------------------------

def _copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)


def patch_check(exe, snapshot, texts, vpath, sides, deltas, patch_id, patch_targets, fragment, targets_ltx=None,
                lua=None):
    """What the compatibility patch decides on one file the two sides replace, as the engine plans and merges it:
    sides [(owner, file)] bottom up, deltas {side number (1-based): delta file}; the patch: its id, its [addon] patches,
    its fragment merge\\gamedata\\<vpath> and merge\\targets.ltx. Each addon is a view of its file (and delta) only,
    the targets that are not sides are stubs (the patch is then not_a_side).
    -> (resolves, stale_targets, note): resolves - applied, not stale, the merged text served (no fallback) and no
    conflict left to the order of the stack"""
    work = tempfile.mkdtemp(prefix="gw_patch_plan_")
    try:
        rel = vpath.replace("\\", "/")
        views = []
        owners = set()
        for n, (owner, path) in enumerate(sides):
            view = os.path.join(work, "side%d" % n)
            _copy(path, gwmerge.safe_join(os.path.join(view, "gamedata"), rel))
            if deltas.get(n + 1):
                _copy(deltas[n + 1], os.path.join(view, ".gw_merge", "delta.json"))
            with open(os.path.join(view, "addon.ltx"), "w", encoding="utf-8", newline="\n") as f:
                f.write("[addon]\nid = %s\ntype = content\n" % owner)
            views.append(view)
            owners.add(ascii_lower(owner))
        stubs = [_stub(work, n, t) for n, t in enumerate(t for t in dict.fromkeys(ascii_lower(x) for x in patch_targets)
                                                         if t not in owners)]
        patch = _stub(work, len(stubs) + 1, patch_id, patch_targets)
        if fragment and os.path.isfile(fragment):
            _copy(fragment, gwmerge.safe_join(os.path.join(patch, "merge", "gamedata"), rel))
        if targets_ltx and os.path.isfile(targets_ltx):
            _copy(targets_ltx, os.path.join(patch, "merge", "targets.ltx"))
        plan = run(exe, views + stubs + [patch], snapshot=snapshot, texts=texts, merge=True, lua=lua, work=work,
                   what=vpath)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    entry = next((f for f in plan["files"] if ascii_lower(f["file"]) == ascii_lower(vpath.replace("/", "\\"))), None)
    record = next((p for p in (entry or {}).get("patches", []) if p["id"] == ascii_lower(patch_id)), None)
    if entry is None or record is None:
        return False, [], "the patch has no fragment of the file" if entry else "the file is not planned"
    if record["status"] == "skipped":
        return False, [], record["note"]
    if record["status"] == "stale":
        return False, list(record.get("stale_targets", [])), record["note"]
    result = entry.get("result")
    if not entry.get("merge") or result is None:
        return False, [], "not merged: %s" % (entry.get("note") or entry.get("status"))
    resolves = entry["status"] != "fallback" and \
        not any(c.get("resolved_by") in ORDER_RESOLVED for c in result.get("conflicts", []))
    return resolves, [], record["note"]
