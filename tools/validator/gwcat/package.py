"""The package of a version: release assets, zip parts, layout, forbidden files, safe unpacking
(11-catalog-distribution.md 3.4, 4.2 step 3, 4.7 row 4; 09-content-addons-vfs.md 2.1; 00-roadmap.md 5a decisions 4, 25).

Assets of a release (11 section 3.4):
    <id>-<ver>.zip                     the whole package (under 2 GiB)
    <id>-<ver>.part<N>.zip, N = 1..K   a package over 2 GiB; every part is a zip of its own, all unpack into one
                                       folder, addon.ltx is in part1, the parts do not overlap
    <id>-<ver>-bin-<os>-<arch>.zip     a plugin build: only from the CI of sdk (v2) - ignored in v1
Anything else attached to the release is ignored (a warning).
"""

import os
import re
import shutil
import stat
import zipfile

from . import limits

EXEC_EXT = (".exe", ".dll", ".bat", ".cmd", ".ps1", ".sh", ".vbs", ".so", ".dylib", ".com", ".scr", ".msi")
ALLOWED_METHODS = {zipfile.ZIP_STORED: "stored", zipfile.ZIP_DEFLATED: "deflate", 93: "zstd"}
TOP_DOCS = re.compile(r"^(readme|license|licence|changelog|copying|authors)([._-].*)?$", re.I)
# The rule of the launcher (launcher-new/src-tauri/src/service/unpack.rs is_portable_path, RESERVED_NAMES): a package it
# would refuse must not pass the catalog. Control characters are Rust's char::is_control (Cc: U+0000-U+001F,
# U+007F-U+009F); a device name is compared with the part before the first dot, trailing spaces cut ("CON .txt"), in
# upper case; COM0 / LPT0, the superscript digits and CONIN$ / CONOUT$ are devices too.
WIN_BAD_CHARS = set('<>:"|?*') | {chr(c) for c in range(32)} | {chr(c) for c in range(0x7F, 0xA0)}
WIN_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | \
    {"%s%s" % (p, d) for p in ("COM", "LPT") for d in "0123456789\u00b9\u00b2\u00b3"}


def is_windows_device(component):
    """A path component Windows opens as a device (unpack.rs is_portable_path)"""
    return component.split(".")[0].rstrip(" ").upper() in WIN_DEVICES
# The marker the launcher writes into every folder it installs (addon_install.rs MARKER_FILE; the patch editor writes it
# too): never a part of a package, the launcher writes its own at install
LAUNCHER_MARKER = ".gw_addon.json"
# A working folder = the package + the things of a repo checkout and of the launcher at its root: the same lists as the
# author mode of the launcher (launcher-new/src-tauri/src/service/addon_author.rs EXCLUDED_DIRS, EXCLUDED_FILES), in
# lower case. Folder only: a zip of a release is the package itself.
EXCLUDED_DIRS = (".git", ".github", "catalog", ".gw_merge")
# ".git" as a FILE: the gitdir pointer of a git worktree or a submodule checkout
EXCLUDED_FILES = (".gitignore", ".gitattributes", ".gitmodules", LAUNCHER_MARKER, ".git")


def is_launcher_file(name):
    """A path of a package the launcher writes itself at install (.gw_merge/, the marker): not listed in <ver>.json"""
    low = name.lower()
    return low.startswith(".gw_merge/") or low == LAUNCHER_MARKER


def classify_assets(names, addon_id, version, report):
    """names of the assets of a release -> list of the package parts in order (empty - no package)"""
    whole = "%s-%s.zip" % (addon_id, version)
    part_re = re.compile(r"^%s-%s\.part([1-9][0-9]{0,3})\.zip$" % (re.escape(addon_id), re.escape(version)))
    bin_re = re.compile(r"^%s-%s-bin-[a-z0-9]+-[a-z0-9]+\.zip$" % (re.escape(addon_id), re.escape(version)))
    parts = {}
    has_whole = False
    for name in names:
        m = part_re.match(name)
        if name == whole:
            has_whole = True
        elif m:
            parts[int(m.group(1))] = name
        elif bin_re.match(name):
            report.warning("asset_plugin_build_v1", "plugin builds are accepted only from the CI of sdk (v2): ignored",
                           name)
        else:
            report.warning("asset_unknown", "not a package asset (expected %s or %s-%s.part<N>.zip): ignored" %
                           (whole, addon_id, version), name)
    if has_whole and parts:
        report.error("asset_whole_and_parts", "the release has both %s and .part<N>.zip: one or the other" % whole)
        return []
    if has_whole:
        return [whole]
    if not parts:
        report.error("asset_no_package", "no package asset: attach %s (or %s-%s.part1.zip ...)" %
                     (whole, addon_id, version))
        return []
    numbers = sorted(parts)
    if numbers != list(range(1, len(numbers) + 1)):
        report.error("asset_parts_gap", "parts must be numbered 1..N without gaps: %s" %
                     ", ".join(str(n) for n in numbers))
        return []
    if len(numbers) == 1:
        report.warning("asset_single_part", "one part: name it %s" % whole)
    return [parts[n] for n in numbers]


def _check_name(name, report, where):
    """A path inside the package (posix). False - unusable (an error was added)."""
    if "\\" in name:
        report.error("package_backslash", "a zip entry with '\\' in its name: repack with a tool that writes '/'",
                     where + ": " + name)
        return False
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        report.error("package_absolute_path", "an absolute path", where + ": " + name)
        return False
    parts = name.rstrip("/").split("/")
    for p in parts:
        if p in ("", ".", ".."):
            report.error("package_bad_path", "empty, '.' or '..' component", where + ": " + name)
            return False
        if any(c in WIN_BAD_CHARS for c in p) or p[-1] in " ." or is_windows_device(p):
            report.error("package_windows_name", "not a valid Windows file name (characters <>:\"|?*, control "
                                                 "characters, a trailing dot / space, a device name)",
                         where + ": " + name)
            return False
    if len(name) > 200:
        report.warning("package_long_path", "the path is %d characters: may exceed MAX_PATH inside the game folder" %
                       len(name), where + ": " + name)
    return True


def inspect_zip(path, label, report):
    """-> {posix name: size} of the files of a zip part; errors into report"""
    files = {}
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        report.error("package_bad_zip", "not a zip: %s" % e, label)
        return files
    with zf:
        seen = set()
        for info in zf.infolist():
            name = info.filename
            if "\\" in info.orig_filename:  # zipfile on Windows turns a backslash into '/': check the raw name
                _check_name(info.orig_filename, report, label)
                continue
            if name in seen:
                report.error("package_duplicate_entry", "the zip has this name twice", label + ": " + name)
                continue
            seen.add(name)
            if not info.flag_bits & 0x800 and not name.isascii():
                report.error("package_name_encoding", "a non-ASCII name without the UTF-8 flag of zip: repack with a "
                                                      "tool that writes UTF-8 names (7-Zip, the launcher)",
                             label + ": " + name)
                continue
            if info.flag_bits & 0x1:
                report.error("package_encrypted", "an encrypted entry", label + ": " + name)
                continue
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                report.error("package_symlink", "a symbolic link", label + ": " + name)
                continue
            if not _check_name(name, report, label):
                continue
            if info.is_dir():
                continue
            if info.compress_type not in ALLOWED_METHODS:
                report.error("package_compression", "compression method %d: only stored, deflate, zstd (11 section "
                                                    "3.4)" % info.compress_type, label + ": " + name)
                continue
            files[name] = info.file_size
    return files


def check_layout(files, manifest, report, label="package"):
    """files: {posix path: size} of the whole package (all parts). The layout of 11 section 3.4 / 09 section 2.1."""
    lower = {}
    for name in files:
        key = name.lower()
        if key in lower:
            report.error("package_case_collision", "'%s' and '%s' are one file on Windows" % (lower[key], name), label)
        lower[key] = name
    if "addon.ltx" not in files:
        tops = {n.split("/")[0] for n in files if "/" in n}
        nested = [t for t in tops if t + "/addon.ltx" in files]
        if nested:
            report.error("package_nested", "addon.ltx is in '%s/': the root of the zip must be the root of the addon "
                                           "folder (addon.ltx, gamedata/ ...)" % nested[0], label)
        else:
            report.error("package_no_manifest", "no addon.ltx at the root of the package", label)
    slot_tops = {s["files"].split("/")[0].lower() for s in (manifest or {}).get("slots", [])}
    patches = bool((manifest or {}).get("patches"))
    allowed_dirs = {"gamedata", "merge", "slots", ".gw_merge"} | slot_tops
    addon_id = (manifest or {}).get("id", "")
    warned = set()
    for name in sorted(files):
        top, _, rest = name.partition("/")
        low = top.lower()
        lname = name.lower()
        if lname.endswith(EXEC_EXT):
            report.error("package_executable", "executables, libraries and OS scripts are not allowed (11 section 3.4)",
                         name)
        if not rest:
            if low == "addon.ltx":
                continue
            if low == LAUNCHER_MARKER:
                report.warning("package_launcher_marker", "%s is written by the launcher into the folder it installs "
                                                          "(its own one replaces it): leave it out of the package" %
                               LAUNCHER_MARKER, name)
                continue
            if low.endswith(".db"):
                report.error("package_db_v1", "<id>.db is not accepted in v1: only folders (00 section 5a decision 4)",
                             name)
            elif not TOP_DOCS.match(top):
                report.error("package_unknown_file", "a file at the root: only addon.ltx, README*, LICENSE*, "
                                                     "CHANGELOG*", name)
            continue
        if low in warned:
            continue
        if low == "bin":
            report.error("package_bin", "bin/ is not allowed: plugin builds come only from the CI of sdk (v2)", top + "/")
            warned.add(low)
        elif low == "catalog":
            report.error("package_catalog", "catalog/ belongs to the repo, not to the package", top + "/")
            warned.add(low)
        elif low == ".gw_merge":
            report.warning("package_gw_merge", ".gw_merge/ is written by the launcher from the catalog (15 section "
                                               "2.3): the folder of the package is replaced at install", top + "/")
            warned.add(low)
        elif low == "merge" and not patches:
            report.warning("package_merge_unused", "merge/ is read only for a compatibility patch ([addon] patches)",
                           top + "/")
            warned.add(low)
        elif low not in allowed_dirs:
            report.error("package_unknown_folder", "folder '%s' is not used by the engine (allowed: gamedata/, "
                                                   "merge/ of a patch, the folders of [slot.*] files)" % top, top + "/")
            warned.add(low)
        elif low == "gamedata":
            # <root>\addons\<id>\... (meshes, textures, sounds, configs ...): the namespace of an addon (09 section 1)
            parts = rest.lower().split("/")
            other = parts[2] if len(parts) > 3 and parts[1] == "addons" else ""
            if other and other != addon_id:
                report.error("package_foreign_namespace", "a path in the namespace of another addon '%s' "
                                                          "(09 section 3)" % other, name)
    for s in (manifest or {}).get("slots", []):
        prefix = s["files"].lower().rstrip("/") + "/gamedata/"
        if not any(n.lower().startswith(prefix) for n in files):
            report.warning("package_slot_empty", "[slot.%s] files = %s: no gamedata/ in it" % (s["slot"], s["files"]),
                           s["files"])


def merge_parts(parts_files, report):
    """[(label, {name: size})] -> {name: size}; a name in two parts is an error (11 section 4.7 row 4)"""
    out, owner = {}, {}
    for label, files in parts_files:
        for name, size in files.items():
            if name in out:
                report.error("package_parts_overlap", "'%s' is in %s and %s" % (name, owner[name], label), label)
                continue
            out[name] = size
            owner[name] = label
    if len(parts_files) > 1 and "addon.ltx" in owner and owner["addon.ltx"] != parts_files[0][0]:
        report.error("package_manifest_not_in_part1", "addon.ltx must be in part1 (11 section 3.4)", owner["addon.ltx"])
    return out


def extract(parts, dest, names=None):
    """Unpacks the zip parts (already inspected: their names are safe) into dest. names - only these (None - all)."""
    real = os.path.realpath(dest)
    for path in parts:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir() or (names is not None and info.filename not in names):
                    continue
                target = os.path.realpath(os.path.join(dest, *info.filename.split("/")))
                if not target.startswith(real + os.sep):
                    raise RuntimeError("unsafe path %s" % info.filename)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                try:
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                except NotImplementedError as e:
                    raise RuntimeError("%s: %s (zstd needs Python 3.14+)" % (info.filename, e))


def folder_files(root, report):
    """A local addon folder (author's checkout, GlobalWar/addons/tests/...) -> {posix path: size}.
    The things of a repo checkout and of the launcher at the root (EXCLUDED_DIRS, EXCLUDED_FILES, the rules of the author
    mode of the launcher) are not a part of the package: left out with a note, as the launcher does"""
    files = {}
    excluded = []
    for dirpath, dirnames, filenames in os.walk(root):
        at_root = os.path.normcase(os.path.abspath(dirpath)) == os.path.normcase(os.path.abspath(root))
        for d in list(dirnames):
            if at_root and d.lower() in EXCLUDED_DIRS:
                dirnames.remove(d)  # a checkout of the author: not a part of the package
                excluded.append(d + "/")
                continue
            if os.path.islink(os.path.join(dirpath, d)):
                report.error("package_symlink", "a symbolic link",
                             os.path.relpath(os.path.join(dirpath, d), root).replace(os.sep, "/"))
                dirnames.remove(d)
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if at_root and fn.lower() in EXCLUDED_FILES:
                excluded.append(rel)
                continue
            if os.path.islink(full):
                report.error("package_symlink", "a symbolic link", rel)
                continue
            if not _check_name(rel, report, "folder"):
                continue
            files[rel] = os.path.getsize(full)
    if excluded:
        report.note("package_excluded", "not a part of the package (a repo checkout, the launcher): %s" %
                    ", ".join(sorted(excluded)))
    return files


def make_zips(root, files, out_dir, addon_id, version, part_max=limits.PART_MAX_BYTES):
    """Packs a folder into <id>-<ver>.zip or .part<N>.zip (addon.ltx first, in part1), deflate. -> [paths]
    The split is by compressed size estimate = file size (safe: deflate does not grow data much past it)."""
    os.makedirs(out_dir, exist_ok=True)
    names = sorted(files, key=lambda n: (n != "addon.ltx", n.lower()))
    groups, cur, cur_size = [], [], 0
    for n in names:
        size = files[n] + 200 + len(n.encode("utf-8")) * 2
        if cur and cur_size + size > part_max:
            groups.append(cur)
            cur, cur_size = [], 0
        cur.append(n)
        cur_size += size
    if cur:
        groups.append(cur)
    out = []
    for i, group in enumerate(groups, 1):
        name = "%s-%s.zip" % (addon_id, version) if len(groups) == 1 else \
            "%s-%s.part%d.zip" % (addon_id, version, i)
        path = os.path.join(out_dir, name)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for n in group:
                info = zipfile.ZipInfo(n, date_time=(2000, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                with open(os.path.join(root, *n.split("/")), "rb") as src, zf.open(info, "w", force_zip64=True) as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
        out.append(path)
    return out
