"""The merge delta S9 of a version (15-merge-implementation.md 7.2; 00-roadmap.md 5a decisions 48, 50 and the format
decision of 2026-11-05: the delta is ALWAYS a separate file).

    files/<id>/<ver>.json              "merge": {"file": "<ver>.merge.json", "algo": "gw-merge/N",
                                                 "sha256": "<hex of that file>", "size": N}; no key - no delta
    files/<id>/<ver>.merge.json        the delta in the format of 15 section 7.2, as gw_merge wrote it (unchanged)
    files/<id>/<ver>.merge.<tag>.json  a delta recomputed by a newer parse (algo), tag = algo with / \\ : -> -
    <ver>.json is immutable; a recomputed delta never touches it.

The delta itself is made by tools/addons_snapshot (gw_snapshot.py delta -> gw_merge delta). Here: the call, the
reference, the checks of the file.
"""

import os
import re
import shutil

from . import snapshot_tools, strictjson

DELTA_FORMAT = 1
# "algo" of a delta: the one pattern of the schemas (delta, version-file $defs/algo) and of the names of the recomputed
# deltas (indexcheck RECOMPUTED_RE: <ver>.merge.<algo_tag>.json) - review B19. gw_merge writes "gw-merge/<N>"
ALGO_PATTERN = r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$"
ALGO_RE = re.compile(ALGO_PATTERN.replace("$", r"\Z"))
ALGO_TAG_PATTERN = r"[A-Za-z0-9._-]+"  # algo_tag of an ALGO_RE algo
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def algo_tag(algo):
    """"gw-merge/2" -> "gw-merge-2" (gwmerge.algo_file_tag, ContentMerge AlgoFileTag)"""
    return algo.replace("/", "-").replace("\\", "-").replace(":", "-")


def merge_file_name(version):
    return "%s.merge.json" % version


def recomputed_file_name(version, algo):
    return "%s.merge.%s.json" % (version, algo_tag(algo))


def check_delta_object(delta):
    """The shape gw_merge reads (format, algo, game, files) -> error text or None"""
    if not isinstance(delta, dict):
        return "the delta is not a JSON object"
    # an integer, as the engine reads it (DeltaJson): 1.0 and 1e0 equal 1 in Python, not there (review B15)
    if type(delta.get("format")) is not int or delta["format"] != DELTA_FORMAT:
        return "format is not the integer %d" % DELTA_FORMAT
    if not isinstance(delta.get("algo"), str) or not delta["algo"]:
        return "no algo"
    if not ALGO_RE.match(delta["algo"]):
        return "algo %r is not <name>/<version> of letters, digits, '.', '_', '-'" % delta["algo"][:80]
    if not isinstance(delta.get("game"), str):
        return "no game"
    if not isinstance(delta.get("files"), dict):
        return "no files"
    spellings = {}
    for vpath, entry in delta["files"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("kind"), str):
            return "files[%s] is not an entry with a kind" % vpath
        parts = vpath.replace("/", "\\").split("\\")
        # ContentMerge DeltaFromJson: no leading or double separator, no '..', no drive
        if ".." in parts or "" in parts or ":" in vpath:
            return "files[%s] is not a vpath" % vpath
        # one file under two spellings (ASCII case, '/' or '\'): the engine refuses the whole delta (review B16)
        key = "\\".join(parts).translate(_ASCII_LOWER)
        if key in spellings:
            return "files[%s] and files[%s] are one file" % (spellings[key], vpath)
        spellings[key] = vpath
        for name in ("ancestor_sha256", "addon_sha256"):
            sha = entry.get(name)
            if sha is not None and not (isinstance(sha, str) and SHA256_RE.match(sha)):
                # the engine compares the lower case hex as it is: "ABC..." is stale_delta there, never a match
                return "files[%s].%s is not a sha256 of 64 lower case hex digits" % (vpath, name)
    return None


def reference(path):
    """The "merge" value of <ver>.json for a delta file"""
    with open(path, "rb") as f:
        data = f.read()
    delta = strictjson.loads_bytes(data, os.path.basename(path))
    return {"file": os.path.basename(path), "algo": delta["algo"], "sha256": strictjson.sha256_bytes(data),
            "size": len(data)}, delta


def compute(addon_dir, out_path, snapshot, report, latest=None, game=None, gw_merge=None):
    """gw_snapshot.py delta <addon> --snapshot S --out <out_path> -> the reference for <ver>.json or None (no file of
    the base is replaced: no delta, the file is removed). Errors -> RuntimeError (the caller decides: CI retries)."""
    tmp = out_path + ".tmp"
    args = ["delta", addon_dir, "--snapshot", snapshot, "--out", tmp, "--format", "json"]
    if latest:
        args += ["--latest", latest]
    if game:
        args += ["--game", game]
    if gw_merge:
        args += ["--gw-merge", gw_merge]
    try:
        rep, _ = snapshot_tools.run_json(args)
        if not os.path.isfile(tmp):
            raise RuntimeError("gw_snapshot.py delta wrote no file")
        with open(tmp, "rb") as f:
            delta = strictjson.loads_bytes(f.read(), "the delta")
        problem = check_delta_object(delta)
        if problem:
            raise RuntimeError("the delta of gw_merge is not valid: %s" % problem)
        for w in rep.get("warnings", []):
            report.warning("delta_warning", w)
        if rep.get("lines"):
            report.warning("delta_lines", "%d file(s) became one atom (kind lines: the parse could not carry the "
                                          "changes atom by atom): %s" % (rep["lines"],
                                                                         ", ".join(rep.get("lines_files", [])[:5])))
        if rep.get("same_as_ancestor"):
            report.warning("delta_same_as_base", "%d file(s) are the same as the base: remove them from the package" %
                           rep["same_as_ancestor"])
        report.data["delta_report"] = {k: rep.get(k) for k in ("algorithm", "game", "files", "atoms", "lines",
                                                                 "texts_bytes", "new_files", "not_in_ancestor",
                                                                 "same_as_ancestor")}
        if not delta["files"]:
            os.remove(tmp)
            return None
        os.replace(tmp, out_path)
        ref, _ = reference(out_path)
        return ref
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def recompute(addon_dir, files_dir, version, snapshot, current_algos, report, latest=None, game=None, gw_merge=None):
    """A delta of a newer gw_merge for a published version -> the new file name or None (the algo is not new).
    current_algos: the algos that already have a delta of this version (the one of <ver>.json and recomputed)."""
    tmp_out = os.path.join(files_dir, "%s.merge.recompute.json" % version)
    ref = compute(addon_dir, tmp_out, snapshot, report, latest, game, gw_merge)
    if ref is None:
        return None
    if ref["algo"] in current_algos:
        os.remove(tmp_out)
        return None
    name = recomputed_file_name(version, ref["algo"])
    target = os.path.join(files_dir, name)
    if os.path.exists(target):  # another algo with the same tag ("a/1" and "a:1"): a published file is never replaced
        os.remove(tmp_out)
        raise RuntimeError("%s exists (algo %s): published files are immutable" % (name, ref["algo"]))
    shutil.move(tmp_out, target)
    return name
