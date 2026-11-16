"""The resources of an addon as the engine mounts them (14-addon-resource-scoping.md A6; 09-content-addons-vfs.md 3, 4).

The rules are ported from the engine; keep them in step:
    xrGame/addon_content.cpp   MountContentFolder: a layer per folder (<addon>\\gamedata\\, the set of a slot
                               <addon>\\<files>\\gamedata\\, a location pack), owner = the id; the log line
                               "~ [addon:<id>] replaces N base files" counts the base entries the public files cover
    xrCore/LocatorAPI.cpp      layer_walk: ignore_name, folders "@..." skipped in a layer with an owner, .db/.xdb
                               skipped, a file matching a [private] mask (PatternMatch of the name relative to the folder,
                               lower case, '\\') is not mounted at $game_data$, only under the alias roots
                               textures\\@<id>\\, meshes\\@<id>\\
    xrGame/addon_host.cpp      [private]: one mask per line as a key
    xrGame/addon_slots.cpp     [slot.*] files = <folder> (default slots\\<slot>\\) + gamedata\\; WarnUnsupportedFiles
The base is the snapshot of a game release (tools/addons_snapshot build): {vpath: [size, sha256]}, vpath relative to
gamedata, lower case, '\\'.

Decision 63 (00-roadmap.md 5a; review 2026-11-06 section 4.2 #2) and decision 64 item 4: the text files the engine
merges (the replaced texts of check_replacements, "replaces.texts") are decided by gw_merge plan only (plan_texts:
tools/addons_snapshot gwplan.py - the layers, [private], '@' folders, hidden / '.' entries, ignore_name, the patch files
decided by the library of the engine). There is no Python fallback: without gw_merge these checks are not made (a
warning locally, an error in the CI of the index - validate.py). The rules of the file system below (pattern_match,
MaskSet, ignore_name, layer_roots, layer_files) list every file of the package for the other checks (the count of the
replaced files, textures, models, the masks): the plan names the texts only.
"""

import os
import re

from . import limits, ogf, snapshot_tools

# KindOfName of ContentMerge.cpp (.lua is a script too): only the pre-filter of texts_to_decide (a package without such
# a file of a base name, public or private, has no merged text, gw_merge is not asked); the merged texts themselves -
# plan_texts
TEXT_MERGED = (".ltx", ".xml", ".script", ".lua")
# 09 section 7: single-file bases, replaced only by a total conversion in v1
SINGLE_FILE_BASES = ("particles.xr", "shaders.xr", "lanims.xr")
SAVE_BREAKING_RE = re.compile(r"^(spawns\\all\.spawn|levels\\[^\\]+\\level\.(ai|game|spawn))$")
ROOT_CONFIGS = ("configs\\system.ltx", "configs\\game.ltx")
SLOT_CATALOG_RE = re.compile(r"^configs\\misc\\slots\\(slots\.ltx|mod_slots_.+\.ltx)$")
LIST_SHOWN = 10


# The engine lowers names byte by byte, ASCII only (MergeIndexName, mount_layer, ContentPatchLower): str.lower()
# would lower Cyrillic too and name another file (tools/addons_snapshot/common.py ascii_lower: the same rule)
_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def ascii_lower(text):
    """A-Z -> a-z, nothing else"""
    return text.translate(_ASCII_LOWER)


def pattern_match(s, mask):
    """PatternMatch of xrCore (LocatorAPI_defs.cpp): '*' - any run (separators too), '?' - one char, case-sensitive"""
    n, m = len(s), len(mask)
    i = j = 0
    while i < n and (mask[j] if j < m else "") != "*":
        c = mask[j] if j < m else ""
        if c != s[i] and c != "?":
            return False
        i += 1
        j += 1
    mp = cp = 0
    while True:
        c = mask[j] if j < m else ""
        if i >= n:
            while j < m and mask[j] == "*":
                j += 1
            return j >= m
        if c == "*":
            j += 1
            if j >= m:
                return True
            mp, cp = j, i + 1
            continue
        if c == s[i] or c == "?":
            i += 1
            j += 1
            continue
        j, i = mp, cp
        cp += 1


def _wildcard(mask):
    """A matcher of mask equal to pattern_match: the parts between the '*' have fixed lengths, so the first one is the
    head, the last one the tail and the ones between are found leftmost in order (what the backtracking of PatternMatch
    finds), each by a regex in C - O(len) per name, no backtracking on hostile masks"""
    parts = mask.split("*")
    regs = [re.compile("".join("." if c == "?" else re.escape(c) for c in p), re.S) if p else None for p in parts]
    if len(parts) == 1:
        return lambda s: regs[0].fullmatch(s) is not None
    head, tail = len(parts[0]), len(parts[-1])
    middle = [r for r in regs[1:-1] if r is not None]

    def match(s):
        lo, hi = head, len(s) - tail
        if hi < lo or (regs[0] and not regs[0].match(s, 0, lo)) or (regs[-1] and not regs[-1].match(s, hi)):
            return False
        for r in middle:
            m = r.search(s, lo, hi)
            if not m:
                return False
            lo = m.end()
        return True
    return match


class MaskSet:
    """The [private] masks of a layer (normalized): a name without '*' / '?' is looked up in a set, the others are
    matched as PatternMatch does. Thousands of plain names (remap-textures writes one per file) cost nothing."""

    def __init__(self, masks):
        self.plain = set(m for m in masks if "*" not in m and "?" not in m)
        self.wild = [(m, _wildcard(m)) for m in masks if "*" in m or "?" in m]

    def hits(self, name):
        """the masks that take name"""
        out = [name] if name in self.plain else []
        return out + [m for m, match in self.wild if match(name)]

    def match(self, name):
        return name in self.plain or any(match(name) for _, match in self.wild)


def _ends_with(name, tail):
    # ENDS_WITH of ignore_name: len > sizeof(tail), sizeof counts the terminating zero
    return len(name) > len(tail) + 1 and name.endswith(tail)


def ignore_name(name):
    """ignore_name of LocatorAPI.cpp: names the file system never mounts (case-sensitive, as there)"""
    if name in ("Thumbs.db", ".svn", ".vs"):
        return True
    return any(_ends_with(name, t) for t in ("Thumbs.db", ".VC.db", ".VC.opendb", ".sln", ".pdb", ".ipdb", ".iobj"))


def normalize_mask(mask):
    """A [private] mask as mount_layer keeps it: '\\', lower case, no leading separator. None - empty"""
    m = ascii_lower(mask.replace("/", "\\")).lstrip("\\")
    return m or None


def layer_roots(manifest):
    """[(label, package prefix)] of the content layers of the addon: gamedata/ and the set of each [slot.*]"""
    roots = [("gamedata", "gamedata/")]
    for s in manifest.get("slots", []):
        prefix = s["files"].rstrip("/") + "/gamedata/"
        if prefix.lower() != "gamedata/" and all(prefix.lower() != p.lower() for _, p in roots):
            roots.append(("slot." + s["slot"], prefix))
    return roots


def layer_files(files, prefix, masks):
    """The files of one layer folder as layer_walk mounts them.
    -> {"public": {vpath: package path}, "private": {vpath: package path}, "skipped": [package paths],
        "matched": set of the masks that took a file}"""
    out = {"public": {}, "private": {}, "skipped": [], "matched": set()}
    masks = masks if isinstance(masks, MaskSet) else MaskSet(masks)
    low_prefix = prefix.lower()
    for name in sorted(files):
        if not name.lower().startswith(low_prefix):
            continue
        rel = name[len(prefix):]
        parts = rel.split("/")
        if not rel or any(ignore_name(p) for p in parts) or any(p.startswith("@") for p in parts[:-1]):
            out["skipped"].append(name)
            continue
        vpath = ascii_lower(rel).replace("/", "\\")
        dot = vpath.rfind(".")
        if dot >= 0 and (vpath[dot:dot + 3] == ".db" or vpath[dot:dot + 4] == ".xdb"):
            out["skipped"].append(name)
            continue
        hit = masks.hits(vpath)
        if hit:
            out["matched"].update(hit)
            out["private"][vpath] = name
        else:
            out["public"][vpath] = name
    return out


def private_masks(manifest):
    """-> (the [private] masks normalized, without repeats; the masks with '*' / '?' over PRIVATE_WILDCARD_MAX - they
    are left out of the checks: each one costs a match per file of the package)"""
    masks, seen, wild = [], set(), 0
    dropped = []
    for raw in manifest.get("private", []):
        m = normalize_mask(raw)
        if not m or m in seen:
            continue
        seen.add(m)
        if "*" in m or "?" in m:
            wild += 1
            if wild > limits.PRIVATE_WILDCARD_MAX:
                dropped.append(m)
                continue
        masks.append(m)
    return masks, dropped


def scan_layers(files, manifest):
    """-> (layers [{label, prefix, public, private, skipped}], masks, unused masks)"""
    masks, _ = private_masks(manifest)
    mask_set = MaskSet(masks)
    layers, matched = [], set()
    for label, prefix in layer_roots(manifest):
        lf = layer_files(files, prefix, mask_set)
        matched |= lf.pop("matched")
        lf.update({"label": label, "prefix": prefix})
        layers.append(lf)
    return layers, masks, [m for m in masks if m not in matched]


def replaced_by(layers, base_files):
    """The package paths of the public files that cover base files (the engine: layer covered entries, under == 0)"""
    return sorted(pkg for lf in layers for vpath, pkg in lf["public"].items() if vpath in base_files)


def texts_to_decide(layers, base_files):
    """The files of the layers - public and [private] - with a name of the base and the extension of a text
    (TEXT_MERGED): a superset of the files gw_merge plan can name as merged texts of the package (the plan leaves out
    the private files, the patch files mod_*, the hidden entries ...). The private ones are kept in: which file a
    [private] mask takes is decided by the plan, so a disagreement of MaskSet with the engine cannot keep a merged text
    from gw_merge. Empty - no merged text, gw_merge is not needed. -> sorted vpaths"""
    return sorted({v for lf in layers for part in ("public", "private") for v in lf[part]
                   if v in base_files and v.endswith(TEXT_MERGED)})


def merged_texts(layers, plan):
    """{(layer label, vpath)} of the text files of the layers the engine merges with the base. plan - plan_texts (gw_merge
    plan)"""
    return {(lf["label"], vpath) for lf in layers for vpath, note in plan.get(lf["label"], {}).items()
            if note != "patch_file"}


def replaces_record(layers, base_files, game_tag, plan):
    """The "replaces" object of files/<id>/<ver>.json (schemas/version-file.schema.json $defs/replaces). texts - the
    replaced files the engine merges by the delta, by plan (plan_texts: gw_merge plan)"""
    items = []
    for lf in layers:
        items += [(pkg, vpath, lf["label"]) for vpath, pkg in lf["public"].items() if vpath in base_files]
    items.sort()
    shown = [pkg for pkg, _, _ in items[:limits.REPLACES_LIST_MAX]]
    merged = merged_texts(layers, plan)
    return {"game": game_tag, "count": len(items), "texts": sum(1 for _, v, label in items if (label, v) in merged),
            "files": shown, "truncated": len(items) > len(shown)}


def plan_texts(root, base_names, tag="", gw_merge=None):
    """The text files of the layers of the package the engine merges, by gw_merge plan (decisions 63, 64 item 4):
    -> ({layer label ("gamedata", "slot.<slot>"): {vpath: note}}, None) or (None, why not: gw_merge is not found or is
    too old - the text says how to get it -, it failed). note: "" / "no_base_text" / "no_base" (merged when it replaces a
    base file), "patch_file" (a DLTX / XML patch: not merged); a file the layer does not serve (private, hidden ...) is
    not listed"""
    try:
        gwmerge = snapshot_tools.import_module("gwmerge")
        gwplan = snapshot_tools.import_module("gwplan")
        common = snapshot_tools.import_module("common")
    except (RuntimeError, ImportError) as e:
        return None, str(e)
    exe = gwmerge.find_gw_merge(gw_merge)
    if not exe:
        return None, snapshot_tools.gw_merge_missing(gw_merge)
    try:
        why = gwplan.unavailable(exe)
        if why:
            # an older gw_merge (no command plan with --all-files): the first line says why, the how-to is ours
            return None, "%s; %s" % (why.splitlines()[0].rstrip(" ."), snapshot_tools.GW_MERGE_HOW_TO)
        found = gwplan.addon_texts(exe, root, base_names, tag)
    except (common.ToolError, OSError) as e:
        return None, "gw_merge plan failed: %s" % e
    return {label: {ascii_lower(name): note for name, (_, note) in files.items()}
            for label, files in found.layers.items()}, None


def listing(items, shown=LIST_SHOWN):
    """the first items, comma separated, and how many more"""
    items = list(items)
    more = " (+%d more)" % (len(items) - shown) if len(items) > shown else ""
    return ", ".join(items[:shown]) + more


def check_risky(layers, manifest, report, base_files=None):
    """Replacements that hurt more than one file: saves, single-file bases, the root configs, files the engine reads
    before the content is mounted. Warnings (09 sections 3, 7; addon_slots.cpp WarnUnsupportedFiles)."""
    kind = manifest.get("type", "")
    for lf in layers:
        public = lf["public"]

        def of_base(v):
            return base_files is None or v in base_files
        saves = [public[v] for v in sorted(public) if SAVE_BREAKING_RE.match(v) and of_base(v)]
        if saves and kind != "location":
            report.warning("replaces_save_breaking", "replaces %s: the saves made without the addon will not load with "
                                                     "it, and back (09 section 3); a new world is a location pack "
                                                     "(type = location)" % listing(saves), saves[0])
        single = [public[v] for v in SINGLE_FILE_BASES if v in public and of_base(v)]
        if single and kind != "total_conversion":
            report.warning("replaces_single_file_base", "replaces the whole %s: any other addon that does the same "
                                                        "wins or loses all of it (09 section 7: only type = "
                                                        "total_conversion in v1)" % listing(single), single[0])
        roots = [public[v] for v in ROOT_CONFIGS if v in public and of_base(v)]
        if roots:
            report.warning("replaces_root_config", "replaces %s whole: the game merges it with the base by the delta, "
                                                   "but a DLTX file mod_system_<id>.ltx / mod_game_<id>.ltx changes only "
                                                   "what the addon needs (13 section 4.6)" % listing(roots), roots[0])
        dead = [public[v] for v in sorted(public) if v == "gamemtl.xr" or SLOT_CATALOG_RE.match(v)]
        if dead:
            report.warning("replaces_unsupported", "%s: read before the content of the addons is mounted, the file has "
                                                   "no effect (gamemtl.xr - not supported; the slots - declare them "
                                                   "with [slots_declare])" % listing(dead), dead[0])


# ---- models ---------------------------------------------------------------------------------------------------------

def _texture_key(name):
    key = ascii_lower(name).replace("/", "\\").lstrip("\\")
    return key[:-4] if key.endswith(".dds") else key


def read_models(root, layers, report):
    """{package path: refs (ogf.read_refs)} of the .ogf files of the layers (public and private: the owner sees both).
    A model that cannot be read - a warning."""
    models = {}
    for lf in layers:
        for vpath, pkg in sorted(list(lf["public"].items()) + list(lf["private"].items())):
            if not (vpath.startswith("meshes\\") and vpath.endswith(".ogf")):
                continue
            path = os.path.join(root, *pkg.split("/"))
            try:
                if os.path.getsize(path) > limits.MODEL_READ_MAX_BYTES:
                    report.note("model_not_read", "the model is larger than %d MiB: its references are not checked" %
                                (limits.MODEL_READ_MAX_BYTES // limits.MIB), pkg)
                    continue
                with open(path, "rb") as f:
                    data = f.read()
                models[pkg] = (lf, vpath, ogf.read_refs(data))
            except (OSError, ogf.OgfError) as e:
                report.warning("model_unreadable", "the model is not read: %s" % e, pkg)
    return models


def check_models(models, layers, manifest, report, base_files):
    """References of the models: textures, motions, LODs found in the addon (any layer, the private files too - the
    model of the addon takes them through @<id>), in the base or under @<id>\\ of the addon (14 section 5).
    Without a dependency these are warnings, with one - notes (the dependency may bring them)."""
    own = manifest.get("id", "")
    mine = set()
    for lf in layers:
        mine.update(lf["public"])
        mine.update(lf["private"])

    def has(vpath):
        return vpath in mine or vpath in base_files

    omf_dirs = set()  # every folder that holds an .omf at any depth: "folder\*.omf" sets
    for vpath in list(mine) + list(base_files):
        if vpath.endswith(".omf"):
            parts = vpath.split("\\")[:-1]
            for i in range(1, len(parts) + 1):
                omf_dirs.add("\\".join(parts[:i]) + "\\")
    missing = {"texture": {}, "motion": {}, "lod": {}}
    for pkg, (_, _, refs) in sorted(models.items()):
        for name in refs["textures"]:
            if not name or name.startswith("$"):
                continue
            key = _texture_key(name)
            if key.startswith("@"):
                owner, _, rest = key[1:].partition("\\")
                if owner != own:
                    continue  # another addon: check_refs wants it in depends, its files are not known here
                if "textures\\%s.dds" % rest not in mine:
                    missing["texture"].setdefault(name, pkg)
                continue
            if not has("textures\\%s.dds" % key):
                missing["texture"].setdefault(name, pkg)
        for name in refs["motions"]:
            key = ascii_lower(name).replace("/", "\\").lstrip("\\")
            if not key:
                continue
            if key.startswith("@"):
                continue
            if "\\*.omf" in key:
                folder = "meshes\\" + key[:key.index("\\*.omf")] + "\\"
                if folder not in omf_dirs:
                    missing["motion"].setdefault(name, pkg)
            elif not has("meshes\\%s.omf" % key):
                missing["motion"].setdefault(name, pkg)
        for name in refs["lods"]:
            key = ascii_lower(name).replace("/", "\\").lstrip("\\")
            if key and not key.startswith("@") and not has("meshes\\" + key + ("" if key.endswith(".ogf") else ".ogf")):
                missing["lod"].setdefault(name, pkg)
    level = report.note if manifest.get("depends_ids") else report.warning
    for kind, items in missing.items():
        if items:
            level("model_%s_missing" % kind, "%d %s reference(s) of the models are neither in the addon nor in the "
                                             "base%s: %s" % (len(items), kind, " (a dependency may bring them)"
                                                             if manifest.get("depends_ids") else "",
                                                             listing("%s (%s)" % (n, p) for n, p in items.items())))


def check_scoping(models, layers, manifest, report, base_files):
    """14 section 2: the signs of an accidental replacement and of a private texture that loses its bump."""
    replaced_new = set()      # base textures a new model of the addon uses
    replaced_by_base = set()  # base textures a replaced base model of the addon uses
    for _, (lf, vpath, refs) in models.items():
        # a [private] model with a base name replaces nothing: it is a new model of the addon
        target = replaced_by_base if vpath in base_files and vpath in lf["public"] else replaced_new
        target.update(_texture_key(n) for n in refs["textures"] if n and not n.startswith(("$", "@")))
    accidental = []
    for lf in layers:
        for vpath, pkg in sorted(lf["public"].items()):
            if not (vpath.startswith("textures\\") and vpath.endswith(".dds") and vpath in base_files):
                continue
            key = vpath[len("textures\\"):-4]
            if key in replaced_new and key not in replaced_by_base:
                accidental.append(pkg)
    if accidental:
        report.warning("replaces_texture_own_model", "%d replaced base texture(s) are used by the new models of the "
                                                     "addon: the base models of these names get them too. If that is "
                                                     "not meant, list them in [private] (gw_addon_tool remap-textures "
                                                     "does it): %s" % (len(accidental), listing(accidental)),
                       accidental[0])
    kept = [pkg for lf in layers for vpath, pkg in sorted(lf["private"].items()) if vpath in base_files]
    if kept:
        report.note("private_over_base", "%d [private] file(s) have names of base files: the base keeps its own, the "
                                         "addon sees them under @%s\\: %s" % (len(kept), manifest.get("id", ""),
                                                                               listing(kept)))
    mine = set()
    for lf in layers:
        mine.update(lf["public"])
        mine.update(lf["private"])
    lost = []
    for lf in layers:
        for vpath, pkg in sorted(lf["private"].items()):
            if not (vpath.startswith("textures\\") and vpath.endswith(".dds")) or "_bump" in vpath:
                continue
            stem = vpath[:-4]
            bump = stem + "_bump.dds" in mine or stem + "_bump#.dds" in mine
            if bump and stem + ".thm" not in mine and stem + ".thm" not in base_files:
                lost.append(pkg)
    if lost:
        report.warning("private_texture_no_thm", "private texture(s) with a bump but without .thm: the engine takes the "
                                                 "bump name from the .thm (14 section 6), the bump is lost: %s" %
                       listing(lost), lost[0])


def check_resources(root, files, manifest, report, base_files=None, game_tag="", record_files=None, plan=None):
    """The A6 checks. base_files - the files of the base for the checks (the snapshots known, None - unknown);
    record_files - the snapshot of game_tag the "replaces" record is made against (default base_files); plan -
    plan_texts (the merged texts by gw_merge plan), None - not decided (no gw_merge): no record, its "texts" would be
    unknown. -> the "replaces" record or None"""
    layers, masks, unused = scan_layers(files, manifest)
    _, dropped = private_masks(manifest)
    if dropped:
        report.error("private_too_many_masks", "[private] has more than %d masks with '*' or '?' (each one is matched "
                                               "against every file): join them into folder masks; not checked: %s" %
                     (limits.PRIVATE_WILDCARD_MAX, listing(dropped)), "addon.ltx")
    for mask in unused:
        report.warning("private_mask_unused", "[private] %s: no file of the addon matches the mask (the engine logs "
                                              "'[private]: no file matches the masks')" % mask, "addon.ltx")
    private = [pkg for lf in layers for pkg in sorted(lf["private"].values())]
    if private:
        report.note("private_files", "%d private file(s): seen only under textures\\@%s\\, meshes\\@%s\\ (and by the "
                                     "models of the addon): %s" % (len(private), manifest.get("id", ""),
                                                                    manifest.get("id", ""), listing(private)))
    alias = [pkg for lf in layers for pkg in lf["skipped"] if any(p.startswith("@") for p in
                                                                    pkg[len(lf["prefix"]):].split("/")[:-1])]
    if alias:
        report.warning("package_alias_folder", "folders named '@...' are not mounted (the names of the alias roots "
                                               "of the addons): %s" % listing(alias), alias[0])
    check_risky(layers, manifest, report, base_files)
    report.data["layers"] = [{"label": lf["label"], "public": len(lf["public"]), "private": len(lf["private"])}
                             for lf in layers]
    if base_files is None:
        return None
    models = read_models(root, layers, report)
    check_models(models, layers, manifest, report, base_files)
    check_scoping(models, layers, manifest, report, base_files)
    record_base = base_files if record_files is None else record_files
    if plan is None:
        replaced = replaced_by(layers, record_base)
        if replaced:
            report.note("replaces_base", "replaces %d base file(s) of %s (the texts merged by the delta are not "
                                         "decided: no gw_merge plan; in the game: 'replaces N base files', console "
                                         "addon_files %s): %s" % (len(replaced), game_tag or "the base",
                                                                  manifest.get("id", ""), listing(replaced)))
        return None
    record = replaces_record(layers, record_base, game_tag, plan)
    if record["count"]:
        report.note("replaces_base", "replaces %d base file(s) of %s (%d text(s) merged by the delta; in the game: "
                                     "'replaces N base files', console addon_files %s): %s" %
                    (record["count"], game_tag or "the base", record["texts"], manifest.get("id", ""),
                     listing(record["files"])))
    return record
