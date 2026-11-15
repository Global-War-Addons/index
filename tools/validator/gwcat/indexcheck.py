"""check-index: the whole index repo is consistent (validate-pr.yml runs it on every pull request, build-index.yml
before its commit).

    - every JSON is strict (UTF-8 without BOM, no duplicate keys, no NaN) and matches its schema
    - files/<id>/<ver>.json: id / version match the path; its "merge" names <ver>.merge.json, which exists, has that
      sha256 and size and is a delta of that algo; <ver>.merge.<tag>.json: tag = the tag of its own algo, an algo other
      than the one of <ver>.json; no stray files
    - history/<id>.<hash8>.json: hash8 = the first 8 hex of sha256 of the file
    - index.json: history files and files/<id>/<ver>.json of every latest version exist; revoked_sha256 sorted
    - the ltx files (source, tags, reserved_ids, moderation, game_releases) parse
    - with --base <git ref>: published files (files/, history/) are not modified or deleted; generated files
      (index.json, stats.json, state/, files/, history/) are not changed by a pull request at all (only CI pushes them)
"""

import os
import re
import subprocess

from . import delta as delta_mod, schema, strictjson
from .indexcfg import ConfigError, IndexConfig
from .report import Report

VERSION_FILE_RE = re.compile(r"^(\d+\.\d+\.\d+)\.json$")
MERGE_FILE_RE = re.compile(r"^(\d+\.\d+\.\d+)\.merge\.json$")
RECOMPUTED_RE = re.compile(r"^(\d+\.\d+\.\d+)\.merge\.(%s)\.json$" % delta_mod.ALGO_TAG_PATTERN)
HISTORY_RE = re.compile(r"^([a-z0-9_]{1,64})\.([0-9a-f]{8})\.json$")
GENERATED = ("index.json", "stats.json", "state/", "files/", "history/")
IMMUTABLE = ("files/", "history/")


def _load(path, report, rel):
    try:
        return strictjson.load_file(path)
    except (strictjson.StrictJsonError, OSError) as e:
        report.error("json_invalid", str(e), rel)
        return None


def _schema(obj, name, report, rel):
    for err in schema.validate(obj, name)[:10]:
        report.error("schema", "%s: %s" % (name, err), rel)


def check(root, base=None):
    report = Report("index repo " + root)
    try:
        IndexConfig(root)
    except ConfigError as e:
        report.error("config", str(e))
    _check_files(root, report)
    _check_history(root, report)
    _check_state(root, report)
    _check_index(root, report)
    stats_path = os.path.join(root, "stats.json")
    if os.path.isfile(stats_path):
        obj = _load(stats_path, report, "stats.json")
        if obj is not None:
            _schema(obj, "stats", report, "stats.json")
    if base:
        _check_git(root, base, report)
    return report


def _check_files(root, report):
    files_root = os.path.join(root, "files")
    if not os.path.isdir(files_root):
        return
    for addon_id in sorted(os.listdir(files_root)):
        d = os.path.join(files_root, addon_id)
        if not os.path.isdir(d):
            report.error("files_layout", "a file directly in files/", "files/" + addon_id)
            continue
        names = sorted(os.listdir(d))
        refs = {}
        for name in names:
            rel = "files/%s/%s" % (addon_id, name)
            m = VERSION_FILE_RE.match(name)
            if not m:
                continue
            obj = _load(os.path.join(d, name), report, rel)
            if obj is None:
                continue
            _schema(obj, "version-file", report, rel)
            if obj.get("id") != addon_id or obj.get("version") != m.group(1):
                report.error("files_mismatch", "id / version do not match the path", rel)
            ref = obj.get("merge")
            if ref:
                refs[m.group(1)] = ref["algo"]
                if ref.get("file") != delta_mod.merge_file_name(m.group(1)):
                    report.error("merge_ref_name", "merge.file must be %s" % delta_mod.merge_file_name(m.group(1)), rel)
                    continue
                mp = os.path.join(d, ref["file"])
                if not os.path.isfile(mp):
                    report.error("merge_ref_missing", "%s does not exist" % ref["file"], rel)
                    continue
                with open(mp, "rb") as f:
                    data = f.read()
                if strictjson.sha256_bytes(data) != ref.get("sha256") or len(data) != ref.get("size"):
                    report.error("merge_ref_hash", "sha256 / size of %s do not match the reference" % ref["file"], rel)
        for name in names:
            rel = "files/%s/%s" % (addon_id, name)
            if VERSION_FILE_RE.match(name):
                continue
            mm = MERGE_FILE_RE.match(name)
            rm = RECOMPUTED_RE.match(name)
            if not (mm or rm):
                report.error("files_stray", "not <ver>.json, <ver>.merge.json or <ver>.merge.<algo>.json", rel)
                continue
            ver = (mm or rm).group(1)
            obj = _load(os.path.join(d, name), report, rel)
            if obj is None:
                continue
            _schema(obj, "delta", report, rel)
            problem = delta_mod.check_delta_object(obj)
            if problem:
                report.error("delta_invalid", problem, rel)
                continue
            if mm:
                if ver not in refs:
                    report.error("merge_orphan", "no files/%s/%s.json refers to it" % (addon_id, ver), rel)
                elif refs[ver] != obj["algo"]:
                    report.error("merge_algo", "algo %s, the reference says %s" % (obj["algo"], refs[ver]), rel)
            else:
                if not os.path.isfile(os.path.join(d, ver + ".json")):
                    report.error("merge_orphan", "no %s.json for it" % ver, rel)
                if rm.group(2) != delta_mod.algo_tag(obj["algo"]):
                    report.error("merge_recomputed_name", "the name must be %s (algo %s)" %
                                 (delta_mod.recomputed_file_name(ver, obj["algo"]), obj["algo"]), rel)
                if refs.get(ver) == obj["algo"]:
                    report.error("merge_recomputed_same", "the same algo as the delta of %s.json" % ver, rel)


def _check_history(root, report):
    d = os.path.join(root, "history")
    if not os.path.isdir(d):
        return
    for name in sorted(os.listdir(d)):
        rel = "history/" + name
        m = HISTORY_RE.match(name)
        if not m:
            report.error("history_name", "not <id>.<hash8>.json", rel)
            continue
        with open(os.path.join(d, name), "rb") as f:
            data = f.read()
        if strictjson.sha256_bytes(data)[:8] != m.group(2):
            report.error("history_hash", "the name does not match sha256 of the content", rel)
        obj = _load(os.path.join(d, name), report, rel)
        if obj is not None:
            _schema(obj, "history", report, rel)


def _check_state(root, report):
    d = os.path.join(root, "state")
    if not os.path.isdir(d):
        return
    for name in sorted(os.listdir(d)):
        rel = "state/" + name
        obj = _load(os.path.join(d, name), report, rel)
        if obj is None:
            continue
        _schema(obj, "state", report, rel)
        if obj.get("id") + ".json" != name:
            report.error("state_name", "id does not match the file name", rel)


def _check_index(root, report):
    path = os.path.join(root, "index.json")
    if not os.path.isfile(path):
        report.note("index_missing", "no index.json yet")
        return
    obj = _load(path, report, "index.json")
    if obj is None:
        return
    _schema(obj, "index", report, "index.json")
    if obj.get("revoked_sha256") != sorted(obj.get("revoked_sha256", [])):
        report.error("index_revoked_order", "revoked_sha256 is not sorted", "index.json")
    ids = [a.get("id") for a in obj.get("addons", [])]
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        report.error("index_addons_order", "addons are not sorted by id or an id repeats", "index.json")
    for a in obj.get("addons", []):
        if not os.path.isfile(os.path.join(root, *a.get("history", "").split("/"))):
            report.error("index_history_missing", "%s: %s does not exist" % (a.get("id"), a.get("history")),
                         "index.json")
        for e in a.get("latest", []):
            if not os.path.isfile(os.path.join(root, "files", a.get("id", ""), "%s.json" % e.get("v"))):
                report.error("index_files_missing", "%s %s: files/%s/%s.json does not exist" %
                             (a.get("id"), e.get("v"), a.get("id"), e.get("v")), "index.json")


def _check_git(root, base, report):
    try:
        out = subprocess.run(["git", "-C", root, "diff", "--name-status", "--no-renames", base + "...HEAD"],
                             capture_output=True, check=True).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError) as e:
        report.error("git_diff", "git diff against %s failed: %s" % (base, e))
        return
    for line in out.splitlines():
        status, _, path = line.partition("\t")
        if path.startswith(IMMUTABLE) and status != "A":
            report.error("immutable_changed", "a published file is %s" % {"M": "modified", "D": "deleted"}.get(
                status, status), path)
        if path == "index.json" or path == "stats.json" or path.startswith(GENERATED[2:]):
            report.error("generated_by_ci", "generated files are written only by the CI of the index", path)
