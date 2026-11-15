"""The calls of the tool gw_merge (xray-16/src/utils/gw_merge, the same C++ library as the engine) for the delta S9.

plans/addons_api_support/15-merge-implementation.md section 7: the merge is never reimplemented in Python - Myers,
difflib and xdiff align differently, and the forecast of CI would differ from what the game does. Python only finds the
tool, unpacks the texts of a snapshot and reads the JSON the tool writes.

Decision 63 (00-roadmap.md 5a): gw_merge ships with the game (<game>\\bin\\gw_merge.exe) and the CI of the catalog
downloads its Linux build, so the tools require it - there is no Python fallback of its decisions. require_gw_merge
finds it and checks it is new enough; missing or too old - GwMergeMissing (a ToolError: "error: ...", exit code 2)
with where it was looked for and how to get it.
"""

import os
import shutil
import subprocess
import tarfile
import tempfile

from common import ToolError, ascii_lower, load_json, load_json_gz, loads_json

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = "gw_merge.exe" if os.name == "nt" else "gw_merge"
# Where a build of the engine puts the tool (the layout of the GW folder: GlobalWar/utils/ next to xray-16/ in the root)
BUILD_DIRS = [
    os.path.join(HERE, "..", "..", "..", "xray-16", "bin", "x64", "Release"),
    os.path.join(HERE, "..", "..", "..", "xray-16", "bin", "x64", "Debug"),
    os.path.join(HERE, "..", "..", "..", "xray-16", "bin"),
]


ENV = "GW_MERGE"  # the environment variable with the path of the tool (the option --gw-merge wins over it)


def find_gw_merge(explicit=None):
    """--gw-merge, else $GW_MERGE, else PATH, else the build folders of xray-16. None - not found (an explicit path or
    $GW_MERGE that is not a file is not found either: the other places are not tried then).
    require_gw_merge - the same search, an error with the help instead of None"""
    for candidate in (explicit, os.environ.get(ENV)):
        if candidate:
            # absolute: CreateProcess of Windows does not find a relative "dir/gw_merge.exe" with '/'
            return os.path.abspath(candidate) if os.path.isfile(candidate) else None
    found = shutil.which(EXE) or shutil.which("gw_merge")
    if found:
        return found
    for folder in BUILD_DIRS:
        path = os.path.normpath(os.path.join(folder, EXE))
        if os.path.isfile(path):
            return path
    return None


# How to get the tool: the part of the errors of require_gw_merge (and of gwplan.unavailable)
HOW_TO_GET = (
    "Every merge decision of the tools comes from gw_merge (the library of the engine); there is no Python fallback. "
    "Get it:\n"
    "  - from the game: <game>\\bin\\gw_merge.exe (it ships with the release, next to the engine);\n"
    "  - Linux: the asset gw_merge-linux-x86_64.tar.gz of the xray-16 release (the CI of the catalog downloads it: "
    "catalog/index/ci/fetch_gw_merge.sh);\n"
    "  - build it: cmake -S xray-16/src/utils/gw_merge -B build-gw_merge && "
    "cmake --build build-gw_merge --config Release\n"
    "and pass it as --gw-merge <path> or $%s=<path>." % ENV)


class GwMergeMissing(ToolError):
    """gw_merge is not found or is too old for the tools (require_gw_merge)"""


def searched(explicit=None):
    """Where find_gw_merge looked, for the message"""
    if explicit:
        return ["--gw-merge %s (no such file)" % explicit]
    env = os.environ.get(ENV)
    if env:
        return ["$%s = %s (no such file)" % (ENV, env)]
    return ["--gw-merge (not given)", "$%s (not set)" % ENV, "PATH (%s)" % EXE] + \
        [os.path.normpath(os.path.join(folder, EXE)) for folder in BUILD_DIRS]


def missing_message(explicit=None):
    return "gw_merge is not found; looked in: %s.\n%s" % ("; ".join(searched(explicit)), HOW_TO_GET)


def too_old_message(exe, why):
    return "%s is too old for the tools: %s.\n%s" % (exe, why, HOW_TO_GET)


_TOO_OLD = {}

# The oldest revision of the results of the library (kResultRevision, "revision" of gw_merge version) the tools take:
# revision 3 changed what the merge gives (the gaps of LTX / XML, the adjacent edits of Merge3), so an older tool would
# decide otherwise than the engine of today. The launcher takes the same minimum (merge_plan.rs MIN_REVISION)
MIN_REVISION = 3


def revision_of(info):
    """The "revision" of gw_merge version as a number, read leniently: an int, an integral float, a text that starts
    with digits ("3", "3.1-dev"); None - not there or not a number"""
    value = info.get("revision") if isinstance(info, dict) else None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and value.is_integer() else None
    if isinstance(value, str):
        digits = ""
        for c in value.strip():
            if not ("0" <= c <= "9"):
                break
            digits += c
        return int(digits) if digits else None
    return None


def too_old(exe):
    """None - gw_merge has what the tools call (version with a revision MIN_REVISION or newer, plan with --all-files:
    plan_format 1); else why not. A plan of another plan_format is found when it is read (gwplan.run:
    PlanUnavailable)"""
    if exe not in _TOO_OLD:
        why = None
        info = version(exe)
        revision = revision_of(info)
        if not info.get("algorithm"):
            why = "no command version"
        elif revision is None:
            why = "gw_merge version reports no revision (needs %d or newer)" % MIN_REVISION
        elif revision < MIN_REVISION:
            why = "revision %d of the library, needs %d or newer" % (revision, MIN_REVISION)
        else:
            # without arguments the command prints the usage: it names "gw_merge plan" and its --all-files only when
            # the tool has them
            _, _, err = run(exe, ["plan"], check=False, what="the command plan")
            if "gw_merge plan" not in err or "--all-files" not in err:
                why = "no command plan with --all-files"
        _TOO_OLD[exe] = why
    return _TOO_OLD[exe]


def require_gw_merge(explicit=None):
    """find_gw_merge + too_old: the path of the tool, else GwMergeMissing (exit code 2 of gw_snapshot.py, the message
    says where it was looked for and how to get it)"""
    exe = find_gw_merge(explicit)
    if not exe:
        raise GwMergeMissing(missing_message(explicit))
    why = too_old(exe)
    if why:
        raise GwMergeMissing(too_old_message(exe, why))
    return exe


LUA_LIBS = ("LuaJIT.dll", "lua51.dll") if os.name == "nt" else (
    "libluajit-5.1.so.2", "libluajit-5.1.so", "libluajit-5.1.2.dylib", "libluajit-5.1.dylib")


def find_luajit(exe, explicit=None):
    """LuaJIT for gw_merge --check-lua (the engine compiles a merged script: one that does not compile - the engine
    serves the upper side whole):
    explicit, else next to gw_merge, else the build folders of xray-16. None - not found"""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    for folder in [os.path.dirname(os.path.abspath(exe))] + BUILD_DIRS:
        for name in LUA_LIBS:
            path = os.path.normpath(os.path.join(folder, name))
            if os.path.isfile(path):
                return path
    return None


def texts_tar_of(snapshot_path, snapshot=None):
    """base_texts.tar.gz next to a snapshot (texts.storage.mode == "tar"), None - the snapshot has no tar."""
    snap = snapshot if snapshot is not None else load_json_gz(snapshot_path)
    storage = (snap.get("texts") or {}).get("storage") or {}
    if storage.get("mode") != "tar":
        return None
    path = os.path.join(os.path.dirname(os.path.abspath(snapshot_path)), storage.get("file", "base_texts.tar.gz"))
    return path if os.path.isfile(path) else None


def safe_join(folder, vpath):
    """folder/<vpath> (backslashes or '/'), None when vpath leaves the folder: '..', an absolute path, a drive (a tar or a
    delta from outside must not write or read beyond the folder)"""
    parts = vpath.replace("\\", "/").split("/")
    if not vpath or vpath[0] in "/\\" or any(p in ("", ".", "..") or ":" in p for p in parts):
        return None
    return os.path.join(folder, *parts)


def unpack_texts(tar_path, folder, vpaths=None):
    """The normalized texts of a snapshot into folder/<vpath with '/'> (as gw_merge --ancestor-dir reads them).
    vpaths - only these ('\\' or '/'), None - all. Returns the number of files."""
    wanted = None if vpaths is None else {ascii_lower(v.replace("\\", "/")) for v in vpaths}
    count = 0
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar:
            if not member.isfile() or (wanted is not None and member.name not in wanted):
                continue
            target = safe_join(folder, member.name)
            if target is None:
                continue  # the names are vpaths of the snapshot: no '..', no absolute paths - checked anyway
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with tar.extractfile(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            count += 1
    return count


class TextsDir:
    """A temporary folder with the unpacked texts of a snapshot (with ... as d: d.path)."""

    def __init__(self, tar_path, vpaths=None):
        self.tar_path = tar_path
        self.vpaths = vpaths
        self.path = None

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="gw_texts_")
        unpack_texts(self.tar_path, self.path, self.vpaths)
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)


# One call of gw_merge may take this long, seconds. A delta of a fix pack of 77 files takes ~1 s, a merge of one file
# far less (the worst cost of Myers seen, 30k different lines, ~2 s): the limit only stops a hung tool, so that CI does
# not wait for the timeout of its whole job. --gw-merge-timeout (set_timeout) or $GW_MERGE_TIMEOUT; 0 - no limit
DEFAULT_TIMEOUT = 300
TIMEOUT_ENV = "GW_MERGE_TIMEOUT"
_timeout = None  # set_timeout: the value of --gw-merge-timeout


class GwMergeError(ToolError):
    """gw_merge failed: a usage / file error (exit code 2 without a report), a timeout, a crash"""


def set_timeout(seconds):
    """--gw-merge-timeout: seconds, 0 - no limit, None - $GW_MERGE_TIMEOUT or DEFAULT_TIMEOUT"""
    global _timeout
    if seconds is not None and seconds < 0:
        raise ToolError("--gw-merge-timeout must be >= 0 (0 - no limit)")
    _timeout = seconds


def timeout():
    """The limit of one call in seconds, None - no limit"""
    value = _timeout
    if value is None:
        env = os.environ.get(TIMEOUT_ENV, "").strip()
        try:
            value = float(env) if env else DEFAULT_TIMEOUT
        except ValueError:
            raise ToolError("$%s = %r is not a number of seconds" % (TIMEOUT_ENV, env)) from None
        if value < 0:
            raise ToolError("$%s must be >= 0 (0 - no limit)" % TIMEOUT_ENV)
    return value or None


def command_line(exe, args):
    return subprocess.list2cmdline([exe] + list(args))


def _on(what):
    return (" on " + what) if what else ""


def run(exe, args, check=True, what=None):
    """gw_merge <args> -> (exit code, stdout bytes, stderr text). check: a usage / file error (2) raises.
    what - the file (vpath) or the addon of the call: named in the errors together with the command line"""
    limit = timeout()
    try:
        r = subprocess.run([exe] + list(args), capture_output=True, timeout=limit)
    except subprocess.TimeoutExpired:
        raise GwMergeError("gw_merge %s timed out after %g s%s (raise --gw-merge-timeout or $%s, 0 - no limit); "
                           "command: %s" % (args[0], limit, _on(what), TIMEOUT_ENV, command_line(exe, args))) from None
    except OSError as e:
        raise GwMergeError("cannot run %s: %s" % (exe, e)) from None
    err = r.stderr.decode("utf-8", "replace")
    if check and r.returncode == 2:
        raise GwMergeError("gw_merge %s failed%s: %s; command: %s" % (args[0], _on(what), err.strip() or "exit code 2",
                                                                      command_line(exe, args)))
    if r.returncode not in (0, 1, 2):
        # 0 - done, 1 - done with conflicts / warnings, 2 - an error; anything else is a crash (0xC0000005 ...)
        raise GwMergeError("gw_merge %s crashed%s (exit code %d): %s; command: %s" % (
            args[0], _on(what), r.returncode, err.strip(), command_line(exe, args)))
    return r.returncode, r.stdout, err


def report_of(path, exe, args, what=None):
    """The JSON report gw_merge wrote (--report): missing - an error of the call, broken - BrokenJsonError"""
    if not os.path.isfile(path):
        raise GwMergeError("gw_merge %s wrote no report%s; command: %s" % (args[0], _on(what),
                                                                          command_line(exe, args)))
    return load_json(path)


_ALGORITHMS = {}
_VERSIONS = {}


def version(exe):
    """gw_merge version: {"algorithm", "revision", "delta_format"} - "revision" changes with every change of the
    library that changes a result (kResultRevision), while "algorithm" changes only with the format of the deltas. {} -
    an older gw_merge without the command (require_gw_merge refuses it: too_old)"""
    if exe not in _VERSIONS:
        code, out, _ = run(exe, ["version"], check=False, what="the version")
        data = None
        if code == 0:
            try:
                data = loads_json(out, "the output of gw_merge version")
            except ToolError:
                data = None
        _VERSIONS[exe] = data if isinstance(data, dict) else {}
    return _VERSIONS[exe]


def algorithm(exe):
    """The version of the parse of the tool (kAlgorithmVersion, e.g. "gw-merge/2") as gw_merge itself reports it (the
    field "algorithm" of its JSON): the "algo" of the deltas it makes and accepts. Never hardcoded here - the engine
    bumps it together with the parse"""
    if exe not in _ALGORITHMS:
        fd, empty = tempfile.mkstemp(prefix="gw_algo_", suffix=".ltx")
        os.close(fd)
        try:
            args = ["atoms", "ltx", empty, empty]
            _, out, _ = run(exe, args, what="an empty file (the version of the parse)")
            algo = loads_json(out, "the output of " + command_line(exe, args))
            algo = algo.get("algorithm") if isinstance(algo, dict) else None
            if not isinstance(algo, str) or not algo:
                raise GwMergeError('gw_merge atoms reports no "algorithm": %s' % command_line(exe, args))
            _ALGORITHMS[exe] = algo
        finally:
            os.remove(empty)
    return _ALGORITHMS[exe]


def delta(exe, ancestor_dir, addon, game, out, latest_dir=None, guessed=False):
    """gw_merge delta: the section "merge" of the addon -> out; returns the report of the validator (dict)."""
    report = out + ".report.json"
    args = ["delta", "--ancestor-dir", ancestor_dir, "--addon", addon, "--game", game, "-o", out, "--report", report]
    if latest_dir:
        args += ["--latest-dir", latest_dir]
    if guessed:
        args.append("--guessed")
    try:
        run(exe, args, what=addon)
        return report_of(report, exe, args, addon)
    finally:
        if os.path.exists(report):
            os.remove(report)


def atoms(exe, kind, ancestor, file, name):
    """gw_merge atoms: [{atom, op, line, container?}] of a file against its ancestor."""
    args = ["atoms", kind, ancestor, file, "--name", name]
    _, out, _ = run(exe, args, what=name)
    data = loads_json(out, "the output of " + command_line(exe, args))
    found = data.get("atoms") if isinstance(data, dict) else None
    if not isinstance(found, list):
        raise GwMergeError('gw_merge atoms reports no "atoms" on %s; command: %s' % (name, command_line(exe, args)))
    return found


def apply(exe, delta_path, vpath, new_base, addon_file=None, owner="addon"):
    """gw_merge apply: (the text carried over or None, the report dict)."""
    fd, out = tempfile.mkstemp(prefix="gw_apply_")
    os.close(fd)
    report = out + ".json"
    args = ["apply", "auto", "--delta", delta_path, "--vpath", vpath, new_base, "-o", out, "--report", report,
            "--id", owner]
    if addon_file:
        args += ["--addon-file", addon_file]
    try:
        # exit code 2 with a report - status "failed" (the new base is not understood by the parser): a result of the
        # file, not an error of the run; without a report - a usage / file error
        code, _, err = run(exe, args, check=False, what=vpath)
        if code == 2 and not os.path.isfile(report):
            raise GwMergeError("gw_merge apply failed on %s: %s; command: %s" % (vpath, err.strip() or "exit code 2",
                                                                                command_line(exe, args)))
        data = report_of(report, exe, args, vpath)
        if data.get("status") == "rebased":
            with open(out, "rb") as f:
                text = f.read()
        else:
            text = None
        return text, data
    finally:
        for path in (out, report):
            if os.path.exists(path):
                os.remove(path)


def merge(exe, vpath, base, sides, deltas, lua=None):
    """gw_merge merge of the sides [(owner, file)] over base with the deltas {side number (1-based): delta file}
    (--delta-of): the report dict (status, conflicts, rebase_conflicts, ancestors ...). lua - LuaJIT for --check-lua"""
    fd, out = tempfile.mkstemp(prefix="gw_merge_")
    os.close(fd)
    report = out + ".json"
    args = ["merge", "auto", "--base", base, "--name", vpath, "-o", out, "--report", report]
    for owner, path in sides:
        args += ["--side", "%s=%s" % (owner, path)]
    for n, path in sorted(deltas.items()):
        args += ["--delta-of", str(n), path]
    if lua:
        args.append("--check-lua=" + lua)
    try:
        run(exe, args, what=vpath)
        return report_of(report, exe, args, vpath)
    finally:
        for path in (out, report):
            if os.path.exists(path):
                os.remove(path)


def kind_of(vpath):
    """ContentMerge KindOfName: the extension in any ASCII case; .script and .lua are Lua (review B21)"""
    v = ascii_lower(vpath)
    if v.endswith(".ltx"):
        return "ltx"
    if v.endswith(".xml"):
        return "xml"
    if v.endswith((".script", ".lua")):
        return "lua"
    return None


def algo_file_tag(algo):
    """"gw-merge/2" -> "gw-merge-2": files/<id>/<ver>.merge.<tag>.json of the catalog (the player has only
    <addon>/.gw_merge/delta.json)"""
    return algo.replace("/", "-").replace("\\", "-").replace(":", "-")
