#!/usr/bin/env python3
"""gw_merge_info.py [<gw_merge>] --index DIR [--summary FILE] [--warn-stale]: which gw_merge this run of the CI uses.

The CI takes the gw_merge of the latest release of the engine by default (ci/fetch_gw_merge.sh), so a new release may
change the parse (algo) of the deltas without a change in this repo. This prints `gw_merge version` (algorithm,
revision, delta_format), the address it came from (gw_merge.url beside it, written by fetch_gw_merge.sh) and how many
published versions (listed / pending / yanked with a delta, as recompute-deltas counts them) have no delta of this
algorithm, to the log and to the Summary of the run (--summary, $GITHUB_STEP_SUMMARY). --warn-stale: those versions
are an ::warning:: (build-index: the launcher of the new engine needs their recomputed deltas - run recompute-deltas).
No gw_merge (an empty path): "not used in this run". Never fails the run: it only reports.
"""

import argparse
import json
import os
import re
import subprocess
import sys

TIMEOUT = 60
# files/<id>/<ver>.merge.<algo tag>.json (gwcat/delta.py recomputed_file_name)
RECOMPUTED_RE = re.compile(r"^(\d+\.\d+\.\d+)\.merge\.[A-Za-z0-9._-]+\.json\Z")
STATUSES = ("listed", "pending", "yanked")  # the versions recompute-deltas recomputes (gwcat/index_build.py)


def gw_merge_version(exe):
    """`gw_merge version` -> (dict, None) or (None, why not)"""
    try:
        r = subprocess.run([exe, "version"], capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    if r.returncode != 0:
        return None, "exit code %d: %s" % (r.returncode, (r.stderr or "").strip()[:300])
    try:
        info = json.loads(r.stdout)
    except ValueError:
        return None, "not JSON: %s" % r.stdout.strip()[:300]
    if not isinstance(info, dict) or not isinstance(info.get("algorithm"), str) or not info["algorithm"]:
        return None, "no \"algorithm\": %s" % r.stdout.strip()[:300]
    return info, None


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def versions_without(index, algorithm):
    """["<id>@<ver>", ...]: the published versions with a delta whose deltas (the one of <ver>.json and the recomputed
    ones) have no delta of `algorithm`"""
    out = []
    state_dir = os.path.join(index, "state")
    if not os.path.isdir(state_dir):
        return out
    for name in sorted(os.listdir(state_dir)):
        if not name.endswith(".json"):
            continue
        st = _load(os.path.join(state_dir, name))
        addon_id = st.get("id") or name[:-5]
        files_dir = os.path.join(index, "files", addon_id)
        listing = os.listdir(files_dir) if os.path.isdir(files_dir) else []
        for version, sv in sorted((st.get("versions") or {}).items()):
            if sv.get("status") not in STATUSES or not sv.get("merge_files"):
                continue
            algos = set()
            vpath = os.path.join(files_dir, version + ".json")
            if os.path.isfile(vpath):
                merge = _load(vpath).get("merge")
                if isinstance(merge, dict):
                    algos.add(merge.get("algo"))
            for fname in listing:
                m = RECOMPUTED_RE.match(fname)
                if m and m.group(1) == version:
                    algos.add(_load(os.path.join(files_dir, fname)).get("algo"))
            if algorithm not in algos:
                out.append("%s@%s" % (addon_id, version))
    return out


def report(exe, index, warn_stale=False):
    """-> (lines of the log, markdown of the Summary)"""
    if not exe:
        text = "gw_merge: not used in this run (versions that replace texts of the base wait)"
        return [text], "### gw_merge\n\nNot used in this run: versions that replace texts of the base wait.\n"
    url_file = os.path.join(os.path.dirname(exe), "gw_merge.url")
    url = ""
    if os.path.isfile(url_file):
        with open(url_file, encoding="utf-8") as f:
            url = f.read().strip()
    log = []
    md = ["### gw_merge", ""]
    if url:
        log.append("gw_merge: %s" % url)
        md.append("Source: `%s`" % url)
        md.append("")
    info, why = gw_merge_version(exe)
    if info is None:
        log.append("::warning::gw_merge version failed (%s): an older gw_merge without the command?" % why)
        md.append("`gw_merge version` failed: %s" % why)
        return log, "\n".join(md) + "\n"
    log.append("gw_merge version: %s" % json.dumps(info, sort_keys=True))
    md += ["```json", json.dumps(info, indent=2, sort_keys=True), "```", ""]
    try:
        stale = versions_without(index, info["algorithm"])
    except (OSError, ValueError, AttributeError) as e:
        log.append("::warning::the deltas of the index are not read: %s" % e)
        md.append("The deltas of the index are not read: %s" % e)
        return log, "\n".join(md) + "\n"
    if stale:
        text = ("%d published version(s) have no delta of %s (recompute-deltas makes them): %s" %
                (len(stale), info["algorithm"], ", ".join(stale[:20]) + (", ..." if len(stale) > 20 else "")))
        log.append(("::warning::" if warn_stale else "") + text)
        md.append(text)
    else:
        md.append("Every published version with a delta has a delta of %s." % info["algorithm"])
    return log, "\n".join(md) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gw_merge", nargs="?", default="", help="the path fetch_gw_merge.sh printed (empty: none)")
    ap.add_argument("--index", default=".")
    ap.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    ap.add_argument("--warn-stale", action="store_true")
    args = ap.parse_args(argv)
    log, md = report(args.gw_merge, args.index, args.warn_stale)
    for line in log:
        print(line, flush=True)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
