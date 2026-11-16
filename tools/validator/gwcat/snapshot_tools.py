"""Where tools/addons_snapshot is and how it is called (the delta S9, the snapshot check, the Lua tokenizer).

The catalog does not copy their logic (15-merge-implementation.md 7.1: one implementation of the merge for the engine,
CI and the launcher). Layouts that are searched:
    explicit --snapshot-tools DIR / $GW_SNAPSHOT_TOOLS
    <validator>/../addons_snapshot            the index repo: tools/validator + tools/addons_snapshot
    <validator>/../../GlobalWar/utils/addons_snapshot   the GW workspace: catalog/validator + GlobalWar/utils/addons_snapshot
"""

import importlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # catalog/validator
CANDIDATES = [
    os.path.join(HERE, "..", "addons_snapshot"),
    os.path.join(HERE, "..", "..", "GlobalWar", "utils", "addons_snapshot"),
]

_explicit = [None]

# gw_merge (xray-16/src/utils/gw_merge, the merge library of the engine) decides the merged texts and makes the delta.
# Decision 64 item 4 (00-roadmap.md 5a): it ships with the game, the Python ports of its rules are removed - a check
# that needs it reports how to get it (local runs) or fails (the CI of the index: ci/fetch_gw_merge.sh downloads it)
GW_MERGE_HOW_TO = ("get gw_merge (the merge of the engine; the validator has no Python fallback): from the game - "
                   "<game>\\bin\\gw_merge.exe; Linux - the asset gw_merge-linux-x86_64.tar.gz of the xray-16 release "
                   "(the one ci/fetch_gw_merge.sh of the index downloads); or build it - cmake -S "
                   "xray-16/src/utils/gw_merge -B build-gw_merge && cmake --build build-gw_merge --config Release. "
                   "Then name it: --gw-merge <path> or $GW_MERGE=<path> (or put it on PATH)")


def gw_merge_missing(explicit=None):
    """The text of a gw_merge that is not found (gwmerge.find_gw_merge: --gw-merge, $GW_MERGE, PATH, the xray-16 build
    folders), with how to get it"""
    if explicit:
        where = "--gw-merge %s is not a file" % explicit
    elif os.environ.get("GW_MERGE"):
        where = "$GW_MERGE=%s is not a file" % os.environ["GW_MERGE"]
    else:
        where = "not in --gw-merge, $GW_MERGE, PATH, the xray-16 build folders"
    return "gw_merge is not found (%s); %s" % (where, GW_MERGE_HOW_TO)


def set_explicit(path):
    _explicit[0] = path


def find():
    for candidate in [_explicit[0], os.environ.get("GW_SNAPSHOT_TOOLS")] + CANDIDATES:
        if candidate and os.path.isfile(os.path.join(candidate, "gw_snapshot.py")):
            return os.path.normpath(os.path.abspath(candidate))
    return None


def require():
    path = find()
    if not path:
        raise RuntimeError("tools/addons_snapshot is not found: --snapshot-tools DIR or $GW_SNAPSHOT_TOOLS "
                           "(looked in %s)" % ", ".join(os.path.normpath(c) for c in CANDIDATES))
    return path


def import_module(name):
    """A module of tools/addons_snapshot (luascan, textnorm, ...) - imported, not copied"""
    path = require()
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module(name)


def run(args, timeout=1800):
    """python gw_snapshot.py <args> -> (exit code, stdout str, stderr str)"""
    script = os.path.join(require(), "gw_snapshot.py")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run([sys.executable, script] + list(args), capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        # a package that hangs gw_merge: an error of this version (the callers catch RuntimeError), not of the run
        raise RuntimeError("gw_snapshot.py %s did not finish in %d s" % (args[0], timeout))
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def run_json(args, timeout=1800):
    code, out, err = run(args, timeout)
    if code not in (0, 1):
        raise RuntimeError("gw_snapshot.py %s failed (exit %d): %s" % (args[0], code, (err or out).strip()[-2000:]))
    try:
        return json.loads(out), err
    except ValueError:
        raise RuntimeError("gw_snapshot.py %s did not print JSON: %s" % (args[0], (out or err).strip()[-2000:]))
