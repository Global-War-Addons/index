"""S9: the deltas of the catalog (plans/addons_api_support/15-merge-implementation.md section 7.2), run locally.

    delta   <addon> --snapshot <snapshot of the tag of the author> [--latest <latest snapshot>]
            -> <addon>\\.gw_merge\\delta.json (or --out FILE, --out - = stdout), as the launcher puts it;
            --catalog <index> -> files/<id>/<ver>.merge.json + the reference "merge" in files/<id>/<ver>.json, or the
            delta recomputed for a newer algo -> files/<id>/<ver>.merge.<algo tag>.json; CI steps 1-4
    rebase  <delta.json | files/<id>/<ver>.json> --snapshot <new snapshot> [--addon <addon>]
            -> checked[].rebase of the index: the delta carried over onto a new base; CI step 5 (and 7)
    pairs   <addon> <addon> ... --snapshot <latest snapshot> [--out-dir D] [--catalog <index>] [--cache DIR]
            [--max-pairs-per-file N] [--changed ID]
            -> merge_conflicts.json and merge_stats.json of the index; CI step 6

The catalog (decision 00 section 5a p.51): the delta of a version is always a separate file next to the immutable
files/<id>/<ver>.json, which holds only "merge": {"file": "<ver>.merge.json", "algo", "sha256", "size"}. A delta
recomputed by a newer gw_merge (another "algo") goes to files/<id>/<ver>.merge.<algo tag>.json ("/" "\\" ":" -> "-");
<ver>.json and <ver>.merge.json are never rewritten for it.

Everything is computed by gw_merge (the library of the engine); this module finds the files, unpacks the texts of the
snapshots and writes the JSON of the index. gw_merge is required (decision 63: no Python fallback of its decisions):
missing or too old - "error: ..." with how to get it, exit code 2 (gwmerge.require_gw_merge). The CI of the catalog
(build-index.yml, 11-catalog-distribution.md 4.2) does not exist yet: these commands are what its steps will run.
"""

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

import gametag
import gwmerge
import gwplan
from common import BrokenJsonError, ToolError, ascii_lower, load_json, load_json_gz, log, vnorm
from textnorm import read_normalized, text_sha256

LARGE_DELTA = 1024 * 1024  # the validator warns above this sum of the texts of the atoms (15 section 7.2)
DELTA_FORMAT = 1  # "format" of the delta that gw_merge reads (kDeltaFormat, 15 section 7.2)


def check_delta(delta, path):
    """A delta read from a file: a dict of format DELTA_FORMAT (an integer, as the engine reads it) with "files" (a dict
    of objects), else BrokenJsonError"""
    if not isinstance(delta, dict) or type(delta.get("format")) is not int or delta["format"] != DELTA_FORMAT or \
            not isinstance(delta.get("files"), dict) or not all(isinstance(e, dict) for e in delta["files"].values()):
        raise BrokenJsonError('%s is not a delta of format %d with "files"' % (path, DELTA_FORMAT))
    return delta


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, data):
    """UTF-8, LF, indent 2, a newline at the end - the JSON the catalog and the tools write"""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---- the catalog: files/<id>/<ver>.json -> files/<id>/<ver>.merge.json (decision 00 section 5a p.51) -----------------

MERGE_SUFFIX = ".merge.json"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$")  # an id or a version as a part of a file name of the index
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def catalog_files(index, addon_id, version):
    """(files/<id>/<ver>.json, files/<id>/<ver>.merge.json) of the index folder; an id or a version that is not a plain
    name (a path, '..') - ToolError: they come from addon.ltx of a package"""
    for what, value in (("id", addon_id), ("version", version)):
        if not value or not SAFE_NAME.match(value) or ".." in value:
            raise ToolError("[addon] %s = %r cannot name a file of the catalog" % (what, value))
    folder = os.path.join(index, "files", addon_id)
    return os.path.join(folder, version + ".json"), os.path.join(folder, version + MERGE_SUFFIX)


def recomputed_path(ver_json, algo):
    """files/<id>/<ver>.merge.<algo tag>.json: the delta recomputed by gw_merge of another version of the parse"""
    return ver_json[:-len(".json")] + ".merge.%s.json" % gwmerge.algo_file_tag(algo)


def read_merge_ref(ver_json):
    """(<ver>.json as a dict, its reference "merge" or None). A reference must be {"file": "<ver>.merge.json", "algo",
    "sha256", "size"}; anything else (the old inline section, a path in "file") - BrokenJsonError"""
    ver = load_json(ver_json)
    if not isinstance(ver, dict):
        raise BrokenJsonError("%s is not a JSON object" % ver_json)
    ref = ver.get("merge")
    if ref is None:
        return ver, None
    expected = os.path.basename(ver_json)[:-len(".json")] + MERGE_SUFFIX
    if not isinstance(ref, dict) or ref.get("file") != expected or not isinstance(ref.get("algo"), str) or \
            not ref["algo"] or not isinstance(ref.get("sha256"), str) or not SHA256_HEX.match(ref["sha256"]) or \
            not isinstance(ref.get("size"), int) or isinstance(ref.get("size"), bool) or ref["size"] <= 0:
        raise BrokenJsonError('%s: "merge" must be {"file": "%s", "algo", "sha256", "size"}' % (ver_json, expected))
    return ver, ref


def catalog_delta(ver_json, algo=None):
    """The delta of a version of the catalog, as the launcher picks it: <ver>.merge.<tag of algo>.json when it exists
    (recomputed for this version of the parse), else the file of the reference "merge" checked by its sha256 and size.
    -> (path, delta) or (None, None) - the version has no delta. Broken files - BrokenJsonError"""
    _, ref = read_merge_ref(ver_json)
    if ref is None:
        return None, None
    if algo:
        path = recomputed_path(ver_json, algo)
        if os.path.isfile(path):
            delta = check_delta(load_json(path), path)
            if delta.get("algo") != algo:
                raise BrokenJsonError('%s: "algo" is %r, the name says %s' % (path, delta.get("algo"), algo))
            return path, delta
    path = os.path.join(os.path.dirname(ver_json), ref["file"])
    if not os.path.isfile(path):
        raise ToolError("%s: the delta %s is missing" % (ver_json, ref["file"]))
    size, sha = os.path.getsize(path), file_sha256(path)
    if size != ref["size"] or sha != ref["sha256"]:
        raise BrokenJsonError("%s: %s does not match its reference (size %d, sha256 %s; expected %d, %s)" %
                              (ver_json, ref["file"], size, sha, ref["size"], ref["sha256"]))
    delta = check_delta(load_json(path), path)
    if delta.get("algo") != ref["algo"]:
        raise BrokenJsonError('%s: "algo" is %r, the reference says %r' % (path, delta.get("algo"), ref["algo"]))
    return path, delta


def resolve_delta(path, algo):
    """A delta given on the command line: a delta file (<addon>\\.gw_merge\\delta.json, <ver>.merge*.json) or the
    files/<id>/<ver>.json of the catalog (followed to its delta). -> (path of the delta, delta)"""
    data = load_json(path)
    if isinstance(data, dict) and "merge" in data:  # a delta has no "merge"; <ver>.json may have its own "files"
        found, delta = catalog_delta(path, algo)
        if found is None:
            raise ToolError("%s has no delta (no \"merge\")" % path)
        return found, delta
    return path, check_delta(data, path)


# ---- addons ----------------------------------------------------------------------------------------------------------

def _manifest_sections(addon):
    """{section in lower case: [(key, value)]} of <addon>/addon.ltx ({} - no file), comments ';' and '//' cut"""
    path = os.path.join(addon, "addon.ltx")
    out = {}
    if not os.path.isfile(path):
        return out
    section = None
    with open(path, "rb") as f:
        text = f.read().decode("utf-8", "replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    for line in text.splitlines():
        for mark in (";", "//"):
            line = line.split(mark, 1)[0]
        line = line.strip()
        if not line:
            continue
        if line.startswith("["):
            section = ascii_lower(line[1:line.find("]")].strip()) if "]" in line else None
            if section is not None:
                out.setdefault(section, [])
            continue
        if section is not None:
            key, eq, value = line.partition("=")
            out[section].append((key.strip(), value.strip() if eq else ""))
    return out


def read_manifest(addon):
    """[addon] id, version, game_version ... of <addon>/addon.ltx (the keys in ASCII lower case, the values as written;
    {} - no file): id and version name the addon (labels, the files of the catalog), game_version - the base of the
    delta. What the engine takes of the addon (the layers, [private], the patches) comes from gw_merge plan (Planner)"""
    out = {}
    for key, value in _manifest_sections(addon).get("addon", []):
        out[ascii_lower(key)] = value
    return out


def addon_layer_texts(addon, base_names=None, planner=None):
    """The text files of the addon as the merge of the engine takes them (addon_merge.cpp PlanContentMergeFiles), by
    gw_merge plan (Planner, decision 63): the public files of every content layer of the addon - <addon>\\gamedata\\
    and the set of each [slot.*] (<files>\\gamedata\\, review B3) - that are not a DLTX / XML patch.
    A vpath has one entry in a delta, the engine takes the topmost layer of the addon that has it, and the set of a
    slot is mounted only while its provider is chosen: a file of gamedata\\ wins over a set (warned: with the provider
    chosen the delta of it is stale), a file of one set only is taken from that set, a file of two sets is left out
    (warned). -> ({vpath: path}, [warnings]).
    planner - a Planner; None - one over base_names with the gw_merge of require_gw_merge ($GW_MERGE ...: missing or too
    old - GwMergeMissing). A folder without addon.ltx is no addon for the engine: no texts, a warning"""
    if planner is None:
        planner = Planner(gwmerge.require_gw_merge(), base_names)
    common, sets, warnings = planner.layers(addon)
    out, more = _combine_layers(common, sets)
    return out, warnings + more


def _combine_layers(common, sets):
    """The texts of gamedata\\ (common: {vpath: path}) and of the sets ({slot: {vpath: path}}), the patch files left out,
    as one view for the delta (addon_layer_texts) -> ({vpath: path}, [warnings])"""
    out, warnings = dict(common), []
    by_vpath = {}
    for slot, texts in sorted(sets.items()):
        for vpath, path in texts.items():
            by_vpath.setdefault(vpath, []).append((slot, path))
    for vpath, found in sorted(by_vpath.items()):
        slots = ", ".join("[slot.%s]" % slot for slot, _ in found)
        if vpath in out:
            warnings.append("%s is in gamedata\\ and in the set of %s: the delta carries over the file of gamedata\\; "
                            "while the provider is chosen the engine serves the file of the set and its delta is stale "
                            "(write the changes of the set as DLTX / XmlOverride)" % (vpath, slots))
        elif len(found) > 1:
            warnings.append("%s is in the sets of %s: one delta cannot carry both, it has none of them (write the "
                            "changes as DLTX / XmlOverride)" % (vpath, slots))
        else:
            out[vpath] = found[0][1]
    return out, warnings


def addon_texts(addon, base_names=None, planner=None):
    """vpath -> path of the text files of the addon the engine merges (addon_layer_texts without the warnings)"""
    return addon_layer_texts(addon, base_names, planner)[0]


class Planner:
    """The merge decisions of the engine for the tools (decision 63): the layers of an addon and their texts
    (gwplan.addon_texts), the patches of an addon, what a compatibility patch decides (gwplan.patch_check) - by gw_merge
    plan. exe - gw_merge (missing or too old - GwMergeMissing: there is no Python fallback), base_names - the files of
    the base (the patch files need them), tag - the release tag of the base"""

    def __init__(self, exe, base_names=(), tag=""):
        why = gwplan.unavailable(exe)
        if why:
            raise gwmerge.GwMergeMissing(why)
        self.exe = exe
        self.base_names = set(base_names or ())
        self.tag = tag
        self._addons = {}

    def _texts(self, addon):
        key = os.path.normcase(os.path.abspath(addon))
        if key not in self._addons:
            self._addons[key] = gwplan.addon_texts(self.exe, addon, self.base_names, self.tag)
        return self._addons[key]

    def layers(self, addon):
        """(gamedata texts {vpath: path}, {slot: {vpath: path}}, warnings): the patch files left out, vpath in ASCII
        lower case (as the deltas spell it)"""
        found = self._texts(addon)

        def texts(files):
            return {vnorm(name): path for name, (path, note) in sorted(files.items()) if note != "patch_file"}
        sets = {label[len("slot."):]: texts(files) for label, files in found.layers.items() if label.startswith("slot.")}
        return texts(found.layers.get("gamedata", {})), sets, list(found.warnings)

    def patches(self, addon):
        """[addon] patches of the addon: the ids of the plan (ASCII lower case, a version condition dropped)"""
        info = self._texts(addon).info
        return list(info.get("patches", [])) if info else []


def make_view(texts, folder):
    """The texts as one folder <folder>\\gamedata\\<vpath> for gw_merge delta --addon (it reads only gamedata\\ of
    an addon): the files of the sets and without the private and the patch files. -> folder"""
    for vpath, path in texts.items():
        target = gwmerge.safe_join(os.path.join(folder, "gamedata"), vpath)
        if target is None:
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(path, target)
    os.makedirs(os.path.join(folder, "gamedata"), exist_ok=True)
    return folder


def addon_label(addon):
    m = read_manifest(addon)
    return "%s@%s" % (m.get("id") or os.path.basename(os.path.normpath(addon)), m.get("version", "0.0.0"))


def snapshot_texts(snapshot_path):
    snap = load_json_gz(snapshot_path)
    tar = gwmerge.texts_tar_of(snapshot_path, snap)
    if not tar:
        raise ToolError("the snapshot %s has no base_texts.tar.gz next to it (build it with --texts tar)" %
                        snapshot_path)
    return snap, tar


def refuse_overwrite(path, force, why=""):
    """A file the tools are about to write exists: an error unless --force (never overwritten silently)"""
    if os.path.lexists(path) and not force:
        raise ToolError("%s exists%s: --force to overwrite it" % (path, why))


# ---- delta (CI steps 1-4) ----------------------------------------------------------------------------------------------

def make_delta(exe, addon, snapshot_path, out, latest_path=None, game=None):
    """The section "merge" of the addon against the texts of the snapshot of its version: out + the report."""
    manifest = read_manifest(addon)
    snap, tar = snapshot_texts(snapshot_path)
    tag = game or snap["game"]["tag"]
    guessed = not manifest.get("game_version")
    warnings = []
    if manifest.get("game_version") and not gametag.same(manifest["game_version"], tag):
        warnings.append("[addon] game_version = %s, the snapshot is %s: the delta is made against another base" %
                        (manifest["game_version"], tag))
    base_names = set((snap.get("files") or {}).keys())
    latest = None
    if latest_path:
        latest = snapshot_texts(latest_path)
        base_names |= set((latest[0].get("files") or {}).keys())
    texts, layer_warnings = addon_layer_texts(addon, base_names, Planner(exe, base_names, tag))
    warnings += layer_warnings
    vpaths = list(texts)
    work = tempfile.mkdtemp(prefix="gw_delta_")
    try:
        ancestor_dir = os.path.join(work, "ancestor")
        os.makedirs(ancestor_dir)
        gwmerge.unpack_texts(tar, ancestor_dir, vpaths)
        latest_dir = None
        if latest:
            latest_dir = os.path.join(work, "latest")
            os.makedirs(latest_dir)
            gwmerge.unpack_texts(latest[1], latest_dir, vpaths)
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        # gw_merge reads <addon>\\gamedata\\: the texts the engine merges are put there (the sets of the slots too)
        view = make_view(texts, os.path.join(work, "addon"))
        report = gwmerge.delta(exe, ancestor_dir, view, tag, out, latest_dir, guessed)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not isinstance(report, dict):
        raise BrokenJsonError("the report of gw_merge delta on %s is not a JSON object" % addon)
    report["warnings"] = warnings + list(report.get("warnings", []))
    report["out"] = out
    report["size"] = os.path.getsize(out)
    return report


def catalog_targets(exe, addon, index, force):
    """Where the delta of the addon goes in the catalog index: (target, ver_json or None - <ver>.json is not touched).
    A new version - <ver>.merge.json and the reference in <ver>.json; <ver>.json already refers to a delta of another
    algo - the recomputation <ver>.merge.<algo tag>.json, <ver>.json and its delta stay as they are (immutable)"""
    manifest = read_manifest(addon)
    ver_json, merge_json = catalog_files(index, manifest.get("id"), manifest.get("version"))
    algo = gwmerge.algorithm(exe)
    ref = read_merge_ref(ver_json)[1] if os.path.isfile(ver_json) else None
    if ref is None:
        refuse_overwrite(merge_json, force, " (no reference to it in %s)" % os.path.basename(ver_json))
        return merge_json, ver_json
    if ref["algo"] != algo:
        target = recomputed_path(ver_json, algo)
        refuse_overwrite(target, force, " (the delta recomputed for %s)" % algo)
        return target, None
    refuse_overwrite(merge_json, force, " and %s refers to it: the files of the catalog are immutable" %
                     os.path.basename(ver_json))
    return merge_json, ver_json


def merge_ref(merge_json, algo):
    """The reference "merge" of <ver>.json to its delta. <ver>.json itself is written only by the validator of the
    index (version_file, by its schema and canonical form): this tool never writes it (review F19 - a <ver>.json of
    {"merge": ...} only made the next build-index fail with "exists and differs")"""
    return {"file": os.path.basename(merge_json), "algo": algo, "sha256": file_sha256(merge_json),
            "size": os.path.getsize(merge_json)}


def cmd_delta(args):
    exe = gwmerge.require_gw_merge(args.gw_merge)
    addon = os.path.abspath(args.addon)
    to_stdout = args.out == "-"
    ver_json = None
    if args.catalog:
        target, ver_json = catalog_targets(exe, addon, os.path.abspath(args.catalog), args.force)
    elif to_stdout:
        target = None
    else:
        target = os.path.abspath(args.out or os.path.join(addon, ".gw_merge", "delta.json"))
        refuse_overwrite(target, args.force)
    # gw_merge writes into a temporary folder: a failed run leaves the old file, --out - streams it
    work = tempfile.mkdtemp(prefix="gw_delta_out_")
    try:
        tmp = os.path.join(work, "delta.json")
        report = make_delta(exe, addon, args.snapshot, tmp, args.latest, args.game)
        delta = check_delta(load_json(tmp), "the delta gw_merge wrote")
        written = []
        if to_stdout:
            report["out"] = "-"
            with open(tmp, "rb") as f:
                sys.stdout.flush()
                sys.stdout.buffer.write(f.read())
                sys.stdout.buffer.flush()
        elif args.catalog and not delta["files"]:
            # no replaced texts: the version has no delta, <ver>.json gets no reference (the launcher writes nothing)
            report["out"] = None
            report["warnings"].append("no replaced .ltx / .xml / .script: nothing is written to the catalog")
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(tmp, target)
            report["out"] = target
            written.append(target)
            if ver_json:
                report["merge"] = merge_ref(target, delta["algo"])
                report["warnings"].append("%s is not written: the validator of the index writes it, with \"merge\" "
                                          "= %s" % (ver_json, json.dumps(report["merge"], sort_keys=True)))
        report["algo"] = delta["algo"]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    # with --out - the stdout is the delta: the report goes to stderr
    stream = sys.stderr if to_stdout else sys.stdout
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2), file=stream)
    else:
        print("delta %s (%s): %d file(s) (%d same as the ancestor, %d not_in_ancestor, %d new), %d atom(s), %d as "
              "lines, texts %.1f KB, file %.1f KB" % (report["out"] or "(not written)", report["algo"], report["files"],
                                                     report["same_as_ancestor"], report["not_in_ancestor"],
                                                     report["new_files"], report["atoms"], report["lines"],
                                                     report["texts_bytes"] / 1024.0, report["size"] / 1024.0),
              file=stream)
        for path in written[1:]:
            print("  reference: %s" % path, file=stream)
        for w in report["warnings"]:
            print("  warning: " + w, file=stream)
    return 0


# ---- rebase (CI step 5) -------------------------------------------------------------------------------------------------

def rebase_delta(exe, delta_path, snapshot_path, addon=None, out_dir=None):
    """Each file of the delta carried over onto the texts of a snapshot: {game, rebase: {clean, conflicts}, files}.
    delta_path - a delta file or files/<id>/<ver>.json of the catalog (its delta for the algo of gw_merge)"""
    snap, tar = snapshot_texts(snapshot_path)
    delta_path, delta = resolve_delta(delta_path, gwmerge.algorithm(exe))
    files = delta["files"]
    result = {"game": snap["game"]["tag"], "delta": delta_path, "delta_game": delta.get("game"),
              "algo": delta.get("algo"), "rebase": {"clean": 0, "conflicts": 0}, "files": []}
    base_names = set((snap.get("files") or {}).keys())
    texts = addon_texts(addon, base_names, Planner(exe, base_names, snap["game"]["tag"])) if addon else {}
    with gwmerge.TextsDir(tar, list(files)) as base:
        for vpath, entry in files.items():
            item = {"file": vpath}
            base_file = gwmerge.safe_join(base.path, ascii_lower(vpath))
            if base_file is None:
                item["status"] = "bad_path"  # '..', an absolute path: not a vpath of gamedata
                result["rebase"]["conflicts"] += 1
            elif entry.get("not_in_ancestor") or entry.get("same_as_ancestor"):
                item["status"] = "not_in_ancestor" if entry.get("not_in_ancestor") else "same_as_ancestor"
            elif not os.path.isfile(base_file):
                item["status"] = "base_removed"  # the new base has no such file: the replacement is a new file now
            else:
                text, report = gwmerge.apply(exe, delta_path, vpath, base_file, texts.get(vpath))
                item["status"] = report["status"]
                item["rebase_conflicts"] = report.get("rebase_conflicts", [])
                if report["status"] == "rebased":
                    result["rebase"]["conflicts" if item["rebase_conflicts"] else "clean"] += 1
                    if out_dir and text is not None:
                        target = gwmerge.safe_join(out_dir, vpath)
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        with open(target, "wb") as f:
                            f.write(text)
                else:
                    result["rebase"]["conflicts"] += 1
                    item["note"] = report.get("note", "")
            result["files"].append(item)
    return result


def cmd_rebase(args):
    exe = gwmerge.require_gw_merge(args.gw_merge)
    result = rebase_delta(exe, args.delta, args.snapshot, args.addon, args.out_dir)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("rebase of %s (%s, %s) onto %s: %d clean, %d with conflicts" % (
            result["delta"], result["delta_game"], result["algo"], result["game"], result["rebase"]["clean"],
            result["rebase"]["conflicts"]))
        for item in result["files"]:
            confl = ", ".join("%s (%s)" % (c["atom"], c.get("reason", "")) for c in item.get("rebase_conflicts", []))
            note = item.get("note")
            print("  %s: %s%s%s" % (item["file"], item["status"], (": " + confl) if confl else "",
                                    (" (%s)" % note) if note else ""))
    return 1 if result["rebase"]["conflicts"] else 0


# ---- pairs (CI step 6): merge_conflicts.json, merge_stats.json ---------------------------------------------------------

LINES_ATOM = "@lines"  # a delta of kind lines: the file as one atom - it may meet any atom of another addon
GAP_PREFIX = "@gap "  # a changed gap in a delta; gw_merge atoms and the conflicts of a merge name it without it


def find_delta(addon, algo, index=None):
    """The delta of an addon as the engine picks it (addon_merge.cpp MergeReadDelta): <addon>\\.gw_merge\\delta.json
    only (a delta.<algo>.json beside it is never read; review 2026-11-06 section 4.1 item 1). -> (path, delta) or
    (None, None); a broken file (not JSON, a duplicate key, not a delta) - a warning and no delta, as in the engine.
    index - the catalog (CI): only the delta of files/<id>/<ver>.json, never the .gw_merge\\ of the package (15 section
    2.3, review B6): the launcher installs the delta of the catalog and removes the one of the package when the
    catalog has none (merge_delta.rs), so the player has no delta then either"""
    if index:
        manifest = read_manifest(addon)
        try:
            ver_json = catalog_files(index, manifest.get("id"), manifest.get("version"))[0]
            if os.path.isfile(ver_json):
                return catalog_delta(ver_json, algo)
        except ToolError as e:
            log("warning: %s: the delta of the catalog is not used (%s): the addon is merged without it" % (addon, e))
        return None, None
    path = os.path.join(addon, ".gw_merge", "delta.json")
    if not os.path.isfile(path):
        return None, None
    try:
        return path, check_delta(load_json(path), path)
    except ToolError as e:  # BrokenJsonError: not JSON, a duplicate key, not a delta of DELTA_FORMAT
        log("warning: %s (the addon is merged without the delta)" % e)
        return None, None


def addon_atoms(exe, addon, delta, base_dir, algo, texts=None):
    """file -> set of the atoms the addon changes: from its delta, else gw_merge atoms against the latest base (S8).
    The delta is used for a file as the engine uses it (15 section 2.4): the same algo and the file of the addon not
    edited after the publication (addon_sha256), else stale_delta - the changes against the latest base"""
    out = {}
    entries = (delta or {}).get("files", {})
    fresh = bool(delta) and delta.get("algo") == algo
    for vpath, path in (texts if texts is not None else addon_texts(addon)).items():
        entry = entries.get(vpath)
        if entry and not entry.get("not_in_ancestor") and fresh and \
                entry.get("addon_sha256") == text_sha256(read_normalized(path)[0]):
            changed = set()
            for a in entry.get("atoms", []):
                if a.get("format"):
                    continue
                atom = a["atom"]
                changed.add(atom[len(GAP_PREFIX):] if atom.startswith(GAP_PREFIX) else atom)
            if changed:
                out[vpath] = changed
            continue
        base_file = gwmerge.safe_join(base_dir, vpath)
        if base_file and os.path.isfile(base_file):
            # a modified container (a section, an xml node) is listed with its changed children: the children only,
            # as in a delta
            changed = {a["atom"] for a in gwmerge.atoms(exe, gwmerge.kind_of(vpath), base_file, path, vpath)
                       if not (a.get("container") and a["op"] == "modify")}
            if changed:
                out[vpath] = changed
    return out


DEFAULT_MAX_PAIRS_PER_FILE = 200  # gw_merge merges of one file per run (a merge of a popular file ~25 ms, n^2 pairs)
# what an entry of --cache holds; bump when it (or how find_pairs computes it) changes. 2: "patch_stale" in the result
# (decided with the merge of the patch: gw_merge plan), merge\targets.ltx in the key
PAIR_CACHE_FORMAT = 2


class PairCache:
    """The results of the pairs (gw_merge merge and the check of the patch of the pair) by the sha256 of everything
    they depend on: the base text, the files and deltas of both sides, the patch of the pair, gw_merge (its algo and
    its binary), --check-lua. A rerun of the CI merges only the pairs whose inputs changed. folder None - no cache
    (the counters are kept all the same). <folder>/<2 hex>/<key>.json; a broken entry - a warning, merged again"""

    def __init__(self, folder=None):
        self.folder = os.path.abspath(folder) if folder else None
        self.hits = 0  # pairs taken from the cache
        self.misses = 0  # pairs merged by gw_merge in this run
        self._sha = {}

    def file_sha(self, path):
        """sha256 of a file (memoized: the texts of an addon meet many pairs), None - no such file"""
        if not path:
            return None
        if path not in self._sha:
            self._sha[path] = file_sha256(path) if os.path.isfile(path) else None
        return self._sha[path]

    def _path(self, key):
        return os.path.join(self.folder, key[:2], key + ".json")

    def get(self, key):
        if not self.folder:
            return None
        path = self._path(key)
        if not os.path.isfile(path):
            return None
        try:
            data = load_json(path)
        except ToolError as e:
            log("warning: the cache of pairs: %s (merged again)" % e)
            return None
        if not isinstance(data, dict) or data.get("format") != PAIR_CACHE_FORMAT or data.get("key") != key or \
                not isinstance(data.get("result"), dict) or not isinstance(data["result"].get("atoms"), list):
            log("warning: the cache of pairs: %s is not an entry of format %d (merged again)" % (path, PAIR_CACHE_FORMAT))
            return None
        return data["result"]

    def put(self, key, result):
        if not self.folder:
            return
        path = self._path(key)
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        # a temporary file and a rename: a run killed halfway leaves no half-written entry
        fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"format": PAIR_CACHE_FORMAT, "key": key, "result": result}, f, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise


def pair_key(cache, common, vpath, base_file, sides, deltas, patch):
    """The key of a pair in the PairCache: sha256 of the hashes of its inputs. common - algo, gw_merge, check_lua;
    patch - None or {id, patches, fragment (sha256), targets (sha256 of merge\\targets.ltx)} of the patch of the pair"""
    parts = dict(common)
    parts.update({
        "file": vpath,
        "base": cache.file_sha(base_file),
        # the owner id names the side in gw_merge; the whole delta file, as gw_merge reads it (--delta-of)
        "sides": [[owner, cache.file_sha(path), cache.file_sha(deltas.get(n + 1))]
                  for n, (owner, path) in enumerate(sides)],
        "patch": patch,
    })
    data = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def is_changed(it, changed):
    """The addon is named in --changed: by its id, id@version or the name of its folder (case ignored)"""
    names = {it["label"].lower(), os.path.basename(os.path.normpath(it["dir"])).lower()}
    if it["manifest"].get("id"):
        names.add(it["manifest"]["id"].lower())
    return bool(names & changed)


def merge_pair(exe, vpath, base_file, sides, deltas, patch, lua, snapshot):
    """One pair of one file merged by gw_merge -> the result kept in the cache: {"atoms": [conflicting atoms],
    "fallback": error (the merged text does not pass the check, the engine serves the upper side whole),
    "patch_resolves": bool, "patch_stale": [ids] (only with a patch and a conflict)}. patch - (the patch addon, its
    fragment). What the patch decides: gw_merge plan --merge (gwplan.patch_check over snapshot: (path, texts tar) -
    decision 63)"""
    report = gwmerge.merge(exe, vpath, base_file, sides, deltas, lua)
    result = {"atoms": sorted({c["atom"] for c in report.get("conflicts", [])})}
    if report.get("status") == "fallback":
        result["fallback"] = report.get("error", "")
    if patch and (result["atoms"] or "fallback" in result):
        addon, fragment = patch
        pid = addon["manifest"].get("id", addon["label"])
        resolves, stale, _ = gwplan.patch_check(
            exe, snapshot[0], snapshot[1], vpath, sides, deltas, pid, addon["patches"], fragment,
            os.path.join(addon["dir"], "merge", "targets.ltx"), lua)
        result["patch_resolves"] = bool(resolves)
        result["patch_stale"] = list(stale)
    return result


def find_pairs(exe, addons, snapshot_path, lua=None, index=None, cache=None,
               max_pairs_per_file=DEFAULT_MAX_PAIRS_PER_FILE, changed=(), planner=None):
    """-> (merge_conflicts.json, merge_stats.json). cache - PairCache (None - none); max_pairs_per_file - the limit
    of the gw_merge merges of one file in this run (0 / None - no limit; the pairs found in the cache are not counted):
    above it the pairs with the addons of changed (ids, id@version or folder names) go first, then the order of the
    addons on the command line; the rest are skipped and counted in merge_stats.json "skipped".
    planner - a Planner (default: one over the files of the snapshot): the texts of the addons and their patches come
    from gw_merge plan (decision 63), as what the patch of a pair decides (merge_pair)"""
    snap, tar = snapshot_texts(snapshot_path)
    algo = gwmerge.algorithm(exe)
    cache = cache if cache is not None else PairCache(None)
    base_names = set((snap.get("files") or {}).keys())
    planner = planner if planner is not None else Planner(exe, base_names, snap["game"]["tag"])
    # the revision of the library (gw_merge version) too: a fix that changes a result without a new algo must not be
    # served from the cache of a build with the same algo
    common = {"format": PAIR_CACHE_FORMAT, "algo": algo, "gw_merge": cache.file_sha(exe), "check_lua": bool(lua),
              "revision": gwmerge.version(exe).get("revision")}
    changed = {c.lower() for c in changed}
    info = []
    vpaths = set()
    for addon in addons:
        delta_path, delta = find_delta(addon, algo, index)
        manifest = read_manifest(addon)
        info.append({"dir": addon, "label": addon_label(addon), "manifest": manifest,
                     "patches": planner.patches(addon), "delta_path": delta_path, "delta": delta,
                     "texts": addon_texts(addon, base_names, planner)})
        info[-1]["changed"] = is_changed(info[-1], changed)
        vpaths.update(info[-1]["texts"])
    pairs = []
    stats = {}  # (file, atom) -> {addons, conflict_pairs, patched}
    skipped = []  # {file, pairs, checked, skipped}: the files over max_pairs_per_file
    with gwmerge.TextsDir(tar, vpaths) as base:
        for it in info:
            it["atoms"] = addon_atoms(exe, it["dir"], it["delta"], base.path, algo, it["texts"])
            for vpath, atoms in it["atoms"].items():
                for atom in atoms:
                    stats.setdefault((vpath, atom), {"addons": 0, "conflict_pairs": 0, "patched": False})
                    stats[(vpath, atom)]["addons"] += 1
        # The pairs with a common atom are merged (13 section 4.7); a script both change is merged even when their atoms
        # differ: separate edits may still not compile together (more than 200 locals of one function, a name defined
        # twice), and the engine then serves the upper side whole (fallback). Without LuaJIT nothing is checked there
        candidates = {}  # vpath -> [(i, j)]
        for i in range(len(info)):
            for j in range(i + 1, len(info)):
                a, b = info[i], info[j]
                for vpath in sorted(set(a["atoms"]) & set(b["atoms"])):
                    sa, sb = a["atoms"][vpath], b["atoms"][vpath]
                    shared = sa & sb or LINES_ATOM in sa or LINES_ATOM in sb
                    if shared or (lua and gwmerge.kind_of(vpath) == "lua"):
                        candidates.setdefault(vpath, []).append((i, j))
        done = {}  # (i, j, vpath) -> (sides, patch addon or None, stale, result)
        for vpath in sorted(candidates):
            base_file = gwmerge.safe_join(base.path, vpath)
            todo = []
            for i, j in candidates[vpath]:
                a, b = info[i], info[j]
                sides = [(a["manifest"].get("id", a["label"]), a["texts"][vpath]),
                         (b["manifest"].get("id", b["label"]), b["texts"][vpath])]
                deltas = {n + 1: x["delta_path"] for n, x in enumerate((a, b)) if x["delta_path"]}
                # the patch made for the pair (its targets include both); whether the engine applies it - all its
                # targets sides of the file (not_a_side, review B4), stale - is decided with its merge (merge_pair)
                ids = {ascii_lower(sides[0][0]), ascii_lower(sides[1][0])}
                patch = next((p for p in info if {ascii_lower(t) for t in p["patches"]} >= ids), None)
                fragment, patch_part = None, None
                if patch:
                    fragment = gwmerge.safe_join(os.path.join(patch["dir"], "merge", "gamedata"), vpath)
                    patch_part = {"id": patch["manifest"].get("id", patch["label"]), "patches": patch["patches"],
                                  "fragment": cache.file_sha(fragment),
                                  "targets": cache.file_sha(os.path.join(patch["dir"], "merge", "targets.ltx"))}
                key = pair_key(cache, common, vpath, base_file, sides, deltas, patch_part)
                job = (i, j, sides, deltas, patch, fragment, key)
                result = cache.get(key)
                if result is None:
                    todo.append(job)
                else:
                    cache.hits += 1
                    done[(i, j, vpath)] = (patch, result)
            if max_pairs_per_file and len(todo) > max_pairs_per_file:
                # the pairs with the addons changed since the last run first (both changed, then one), then the order
                # of the addons on the command line: a deterministic choice
                todo.sort(key=lambda job: (-(info[job[0]]["changed"] + info[job[1]]["changed"]), job[0], job[1]))
                # pairs - all the candidates of the file, checked - taken from the cache or merged now
                skipped.append({"file": vpath, "pairs": len(candidates[vpath]),
                                "checked": len(candidates[vpath]) - len(todo) + max_pairs_per_file,
                                "skipped": len(todo) - max_pairs_per_file})
                todo = todo[:max_pairs_per_file]
            for i, j, sides, deltas, patch, fragment, key in todo:
                result = merge_pair(exe, vpath, base_file, sides, deltas, (patch, fragment) if patch else None, lua,
                                    (snapshot_path, tar))
                cache.misses += 1
                cache.put(key, result)
                done[(i, j, vpath)] = (patch, result)
        # the records in the order of the pairs of addons (as on the command line), the files of a pair sorted
        for (i, j, vpath), (patch, result) in sorted(done.items(), key=lambda kv: kv[0]):
            stale = result.get("patch_stale", [])
            atoms = list(result["atoms"])
            if not atoms and "fallback" not in result:
                continue
            record = {"a": info[i]["label"], "b": info[j]["label"], "file": vpath, "atoms": atoms,
                      "patch": patch["label"] if patch else None}
            if "fallback" in result:
                record["fallback"] = result["fallback"]
            if patch:
                if stale:
                    record["patch_stale"] = stale
                record["patch_resolves"] = bool(result.get("patch_resolves"))
            pairs.append(record)
            for atom in atoms:
                key = (vpath, atom)
                stats.setdefault(key, {"addons": 0, "conflict_pairs": 0, "patched": False})
                stats[key]["conflict_pairs"] += 1
                stats[key]["patched"] = stats[key]["patched"] or bool(record.get("patch_resolves"))
    conflicts = {"schema": 1, "game": snap["game"]["tag"], "pairs": pairs}
    stat_list = [{"file": f, "atom": a, **v} for (f, a), v in stats.items()]
    stat_list.sort(key=lambda s: (-s["addons"], -s["conflict_pairs"], s["file"], s["atom"]))
    return conflicts, {"schema": 1, "game": snap["game"]["tag"], "atoms": stat_list,
                       "max_pairs_per_file": max_pairs_per_file or 0,
                       "skipped_pairs": sum(s["skipped"] for s in skipped), "skipped": skipped}


def cmd_pairs(args):
    exe = gwmerge.require_gw_merge(args.gw_merge)
    lua = gwmerge.find_luajit(exe, args.check_lua)
    if args.check_lua and not lua:
        raise ToolError("--check-lua: %s is not found" % args.check_lua)
    if not lua:
        log("warning: LuaJIT is not found next to gw_merge (--check-lua LIB): a merged script that does not compile "
            "(the engine serves the upper side whole, fallback) is not seen, and the pairs that change different "
            "atoms of one script are not merged")
    if args.max_pairs_per_file < 0:
        raise ToolError("--max-pairs-per-file %d: 0 (no limit) or more" % args.max_pairs_per_file)
    cache = PairCache(args.cache)
    changed = [c.strip() for value in args.changed or [] for c in value.split(",") if c.strip()]
    conflicts, stats = find_pairs(exe, [os.path.abspath(a) for a in args.addons], args.snapshot, lua,
                                  os.path.abspath(args.catalog) if args.catalog else None, cache,
                                  args.max_pairs_per_file, changed)
    out_dir = args.out_dir or "."
    os.makedirs(out_dir, exist_ok=True)
    for name, data in (("merge_conflicts.json", conflicts), ("merge_stats.json", stats)):
        write_json(os.path.join(out_dir, name), data)  # mutable files of the index (like stats.json): rewritten
    log("pairs: %d addon(s), %d conflicting pair(s) of files, %d atom(s) changed by them -> %s" %
        (len(args.addons), len(conflicts["pairs"]), len(stats["atoms"]), os.path.abspath(out_dir)))
    log("pairs: %d pair(s) of files merged by gw_merge, %d taken from the cache%s" %
        (cache.misses, cache.hits, (" " + cache.folder) if cache.folder else " (no --cache)"))
    if stats["skipped_pairs"]:
        log("warning: pairs: %d pair(s) of %d file(s) are NOT checked (--max-pairs-per-file %d; merge_stats.json "
            "\"skipped\"): their conflicts are not in merge_conflicts.json; with --cache the next runs check them" %
            (stats["skipped_pairs"], len(stats["skipped"]), stats["max_pairs_per_file"]))
        for s in stats["skipped"]:
            log("  %s: %d of %d pair(s) skipped" % (s["file"], s["skipped"], s["pairs"]))
    for p in conflicts["pairs"]:
        what = ", ".join(p["atoms"])
        if "fallback" in p:
            what += ("; " if what else "") + "fallback: " + p["fallback"]
        state = ""
        if p["patch"] and not p.get("patch_resolves"):
            state = ", stale: " + ", ".join(p["patch_stale"]) if p.get("patch_stale") else ", does not resolve"
        print("%s x %s %s: %s%s" % (p["a"], p["b"], p["file"], what,
                                    (" (patch %s%s)" % (p["patch"], state)) if p["patch"] else ""))
    return 1 if any(not p.get("patch_resolves") for p in conflicts["pairs"]) else 0


def add_timeout(parser):
    parser.add_argument("--gw-merge-timeout", type=float, metavar="SEC",
                        help="the limit of one call of gw_merge, seconds (default: $%s, else %d; 0 - no limit)" %
                             (gwmerge.TIMEOUT_ENV, gwmerge.DEFAULT_TIMEOUT))


def add_gw_merge(parser):
    parser.add_argument("--gw-merge", metavar="PATH",
                        help="gw_merge, required (default: $%s, PATH, the build folders of xray-16; the game has it "
                             "as <game>\\bin\\gw_merge.exe)" % gwmerge.ENV)


def add_parsers(sub):
    """The subcommands of gw_snapshot.py"""
    d = sub.add_parser("delta", help="the delta S9 of an addon against the snapshot of the version of its author")
    d.add_argument("addon", help="addon folder (addon.ltx and gamedata\\ inside)")
    d.add_argument("--snapshot", required=True, help="base_snapshot.json.gz of the tag the addon is made on "
                                                     "(base_texts.tar.gz next to it)")
    d.add_argument("--latest", help="the latest snapshot: the files its base has and the base of the author has not "
                                    "become not_in_ancestor")
    d.add_argument("--game", help="the tag written into the delta (default: game.tag of --snapshot)")
    where = d.add_mutually_exclusive_group()
    where.add_argument("--out", help="the delta file (default: <addon>\\.gw_merge\\delta.json); '-' - stdout (the "
                                     "report then goes to stderr)")
    where.add_argument("--catalog", metavar="INDEX", help="the folder of the catalog index: files/<id>/<ver>.merge.json "
                                                          "and its reference in files/<id>/<ver>.json; when <ver>.json "
                                                          "refers to a delta of another algo - "
                                                          "files/<id>/<ver>.merge.<algo tag>.json (id, version - "
                                                          "addon.ltx)")
    d.add_argument("--force", action="store_true", help="overwrite an existing delta file (never done silently)")
    add_gw_merge(d)
    add_timeout(d)
    d.add_argument("--format", choices=("text", "json"), default="text")

    r = sub.add_parser("rebase", help="CI step 5: the delta carried over onto a new snapshot (checked[].rebase)")
    r.add_argument("delta", help="a delta file (delta.json, <ver>.merge.json) or files/<id>/<ver>.json of the "
                                 "catalog (its delta for the algo of gw_merge: <ver>.merge.<algo tag>.json, else the "
                                 "reference \"merge\")")
    r.add_argument("--snapshot", required=True, help="the new snapshot")
    r.add_argument("--addon", help="the addon folder: its files are checked against addon_sha256 (stale_delta)")
    r.add_argument("--out-dir", help="write the texts carried over here (for a look)")
    add_gw_merge(r)
    add_timeout(r)
    r.add_argument("--format", choices=("text", "json"), default="text")

    p = sub.add_parser("pairs", help="CI step 6: the pairs of addons with a common atom or one changed script "
                                     "(compiled with --check-lua) -> merge_conflicts.json, merge_stats.json")
    p.add_argument("addons", nargs="+", help="addon folders (their .gw_merge\\delta.json are used when there)")
    p.add_argument("--snapshot", required=True, help="the latest snapshot")
    p.add_argument("--out-dir", help="where the two json go (default: here)")
    p.add_argument("--catalog", metavar="INDEX", help="the folder of the catalog index: the delta of an addon is "
                                                      "taken from files/<id>/<ver>.json first, as the launcher installs it")
    add_gw_merge(p)
    add_timeout(p)
    p.add_argument("--check-lua", metavar="LIB", help="LuaJIT for the check of the merged scripts, as in the engine "
                                                      "(default: next to gw_merge, xray-16\\bin\\x64\\Release)")
    p.add_argument("--cache", metavar="DIR", help="the cache of the results of the pairs (by sha256 of the base text, "
                                                  "the files and deltas of both addons, the patch of the pair, "
                                                  "gw_merge and --check-lua): a rerun merges only the new pairs. "
                                                  "Default: none (not inside --out-dir: that is the index repo)")
    p.add_argument("--max-pairs-per-file", type=int, default=DEFAULT_MAX_PAIRS_PER_FILE, metavar="N",
                   help="at most N gw_merge merges of one file per run (the pairs from --cache are not counted; "
                        "default %d, 0 - no limit): the rest are skipped and counted in merge_stats.json "
                        "\"skipped\"" % DEFAULT_MAX_PAIRS_PER_FILE)
    p.add_argument("--changed", action="append", metavar="ID[,ID...]",
                   help="addons new or updated since the last run (id, id@version or folder name): over "
                        "--max-pairs-per-file their pairs are merged first (else the order of the addons here)")


COMMANDS = {"delta": cmd_delta, "rebase": cmd_rebase, "pairs": cmd_pairs}
