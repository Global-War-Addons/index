"""One version of an addon from end to end: assets / folder -> manifest -> layout -> content -> delta -> the data of
files/<id>/<ver>.json. Used by the CLI (gw_catalog.py validate) and by the index builder (build_index.py)."""

import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
import zlib

from . import content, delta, limits, ltx, manifest as manifest_mod, package, resources, strictjson
from .report import Report

# CI retries these at the next run (index_build: counted, an issue for the moderators, paused after RETRY_MAX runs):
# a reason of CI, not of the package. A game_version that is no release of game_releases.ltx is no reason to wait
# (delta_unknown_game_version): only a known release gets its snapshot
TRANSIENT = {"delta_unavailable", "delta_failed", "package_unpack_failed", "ci_disk_space", "merge_plan_unavailable"}
# What a broken zip raises while it is read (a bad CRC, a broken deflate stream, a cut file, an encrypted entry,
# an unknown compression): an error of the package, not of the run
ZIP_READ_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError)
# A game tag as a folder name and a part of the snapshot URL: [addon] game_version comes from the package
SAFE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
UNPACK_RESERVE = 1 * limits.GIB  # free space left on the disk of the runner after unpacking


class SnapshotStore:
    """Snapshots of the game releases: <folder>/<tag>/base_snapshot.json.gz + base_texts.tar.gz
    (tools/addons_snapshot build). url_template (source.ltx [game] snapshot_url, {tag} and {name}): downloaded on
    demand by CI - the game release pipeline publishes them as assets (12 section 4.2)."""

    NAMES = ("base_snapshot.json.gz", "base_texts.tar.gz")

    def __init__(self, folder, url_template=None, latest=None, releases=(), digests=None):
        self.folder = folder
        self.url_template = url_template
        self.latest = latest
        # the tags of game_releases.ltx: the snapshots are published under them, a tag written another way
        # (game_version 1.4 for the release v1.4.0) is downloaded under the tag of the release
        self.releases = list(releases)
        # tag -> (sha256 of base_snapshot.json.gz, of base_texts.tar.gz) of game_releases.ltx (review F13): a snapshot
        # uploaded again under the same tag or an old copy of the CI cache is not used
        self.digests = dict(digests or {})
        self._verified = set()

    def _matches(self, name):
        """The files of the folder <name> are the ones game_releases.ltx names (no sha256 there - taken as they are)"""
        want = self.digests.get(name)
        if not want or name in self._verified:
            return True
        d = os.path.join(self.folder, name)
        if all(strictjson.sha256_file(os.path.join(d, n)) == w for n, w in zip(self.NAMES, want)):
            self._verified.add(name)
            return True
        return False

    def _local(self, name):
        d = os.path.join(self.folder, name)
        path = os.path.join(d, self.NAMES[0])
        if not (os.path.isfile(path) and os.path.isfile(os.path.join(d, self.NAMES[1]))):
            return None
        if not self._matches(name):
            # another snapshot under this tag (the cache of CI keeps an old one): dropped, downloaded again
            for n in self.NAMES:
                os.remove(os.path.join(d, n))
            return None
        return path

    def get(self, tag):
        if not tag or not self.folder or not SAFE_TAG_RE.match(tag) or ".." in tag:
            return None
        d = os.path.join(self.folder, tag)
        path = self._local(tag)
        if path:
            return path
        # the same release written another way (game_version 1.4 or V1.4.0, the folder v1.4.0): the engine takes them
        # as one version (gametag.py)
        if os.path.isdir(self.folder):
            for name in sorted(os.listdir(self.folder)):
                if name != tag and SAFE_TAG_RE.match(name) and ltx.same_game_tag(name, tag) and self._local(name):
                    return self._local(name)
        path = os.path.join(d, self.NAMES[0])
        if not self.url_template:
            return None
        if tag not in self.releases:
            release = next((r for r in self.releases if SAFE_TAG_RE.match(r) and ltx.same_game_tag(r, tag)), None)
            if release:
                return self.get(release)
        made = not os.path.isdir(d)
        try:
            os.makedirs(d, exist_ok=True)
            for name in self.NAMES:
                url = self.url_template.replace("{tag}", tag).replace("{name}", name)
                with urllib.request.urlopen(url, timeout=120) as r, open(os.path.join(d, name) + ".part", "wb") as f:
                    shutil.copyfileobj(r, f, 1 << 20)
                os.replace(os.path.join(d, name) + ".part", os.path.join(d, name))
            if not self._matches(tag):
                raise OSError("the snapshot of %s does not match its sha256 in game_releases.ltx" % tag)
        except OSError:
            # no half of a snapshot left: a folder of one file is not a snapshot, an empty one is not a release
            for name in self.NAMES:
                for leftover in (os.path.join(d, name) + ".part", os.path.join(d, name)):
                    if leftover and os.path.isfile(leftover) and (made or leftover.endswith(".part") or
                                                                   tag in self.digests):
                        os.remove(leftover)
            if made and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
            return None
        return path


MANIFEST_REFUSED = object()  # addon.ltx is over the limit: reported, not read


def _manifest_bytes(source, report):
    if source["kind"] == "folder":
        p = os.path.join(source["path"], "addon.ltx")
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read(limits.MANIFEST_MAX_BYTES + 1)
        return None
    for path in source["paths"]:
        try:
            with zipfile.ZipFile(path) as zf:
                try:
                    info = zf.getinfo("addon.ltx")
                except KeyError:
                    continue
                # Never more than the limit in memory: the declared size of a deflate entry costs nothing to fake (a
                # 0.6 MB zip "of 600 MB" took 1.5 GB of RAM), and a stream may unpack past its declared size, so the
                # read itself is bounded too. One byte over the limit is enough for manifest_too_large.
                if info.file_size > limits.MANIFEST_MAX_BYTES:
                    report.error("manifest_too_large", "%d bytes, at most %d" %
                                 (info.file_size, limits.MANIFEST_MAX_BYTES), "addon.ltx")
                    return MANIFEST_REFUSED
                with zf.open(info) as f:
                    return f.read(limits.MANIFEST_MAX_BYTES + 1)
        except ZIP_READ_ERRORS + (OSError,):
            continue  # reported by inspect_zip (encrypted, compression) or the package has no readable addon.ltx
    return None


def validate_version(source, expect_id=None, expect_version=None, cfg=None, snapshots=None, guessed_tag=None,
                     files_dir=None, work_dir=None, gw_merge=None, require_delta=False, snapshot_check=True,
                     subject=None):
    """source: {"kind": "folder", "path"} | {"kind": "zips", "paths": [part1, ...]}.
    files_dir: where <ver>.merge.json goes (None - the delta is computed into a temp folder and only reported).
    -> (Report, result dict or None)"""
    report = Report(subject or (source.get("path") or ", ".join(os.path.basename(p) for p in source["paths"])))
    # 1. the files of the package
    if source["kind"] == "folder":
        files = package.folder_files(source["path"], report)
    else:
        parts = []
        for p in source["paths"]:
            parts.append((os.path.basename(p), package.inspect_zip(p, os.path.basename(p), report)))
        files = package.merge_parts(parts, report)
    # 2. addon.ltx
    data = _manifest_bytes(source, report)
    if data is MANIFEST_REFUSED:
        return report, None
    if data is None:
        package.check_layout(files, None, report)
        return report, None
    m = manifest_mod.check_manifest(data, report, "addon.ltx", expect_id, expect_version,
                                    cfg.reserved if cfg else None)
    if m is None:
        return report, None
    package.check_layout(files, m, report)
    manifest_mod.api_min_needed_for_scripts(m, files, report)
    if not report.ok:
        return report, {"manifest": manifest_mod.strip(m)}

    own_work = work_dir is None
    work = work_dir or tempfile.mkdtemp(prefix="gwcat_")
    os.makedirs(work, exist_ok=True)
    try:
        # 3. the content (a folder is read in place; zips are unpacked)
        if source["kind"] == "folder":
            root = source["path"]
        else:
            root = os.path.join(work, "pkg")
            # the sizes of the entries bound what zipfile writes: a zip bomb is refused before it fills the disk
            unpacked = sum(files.values())
            free = shutil.disk_usage(work).free
            if unpacked + UNPACK_RESERVE > free:
                # the disk of the runner, not the package: the CI of the index retries it (a larger runner helps)
                report.error("ci_disk_space", "the package unpacks to %d bytes, the disk of the check has %d free" %
                             (unpacked, free))
                return report, {"manifest": manifest_mod.strip(m)}
            try:
                package.extract(source["paths"], root)
            except ZIP_READ_ERRORS as e:
                report.error("package_bad_zip", "the zip cannot be unpacked: %s" % e)
                return report, {"manifest": manifest_mod.strip(m)}
            except OSError as e:
                report.error("package_unpack_failed", str(e))
                return report, {"manifest": manifest_mod.strip(m)}
        content.check_entry(files, m, report)
        content.check_lua(root, files, m, report)
        content.check_refs(root, files, m, report)

        # 4. the base: the snapshot of the author's version and the latest one
        game_tag = m["game_version"] or guessed_tag
        snap_author = snapshots.get(game_tag) if snapshots and game_tag else None
        snap_latest = snapshots.get(snapshots.latest) if snapshots and snapshots.latest else None
        base_files = None
        record_files, record_tag = None, ""
        texts = []
        for snap, tag in ((snap_author, game_tag), (snap_latest, snapshots.latest if snapshots else None)):
            if snap:
                files_of = content.load_snapshot_files(snap)
                base_files = files_of if base_files is None else {**base_files, **files_of}
                if record_files is None:
                    # "replaces": against the base of the author's version (game_version), else the latest one
                    record_files, record_tag = files_of, tag or ""
        plan, no_plan = None, None
        if base_files is not None:
            layers, _, _ = resources.scan_layers(files, m)
            candidates = resources.texts_to_decide(layers, base_files)
            if candidates:
                # the merged texts by gw_merge plan only (decisions 63, 64 item 4: no Python fallback)
                plan, no_plan = resources.plan_texts(root, base_files.keys(), record_tag, gw_merge)
            else:
                plan = {}  # no text file of a layer has a name of the base: nothing is merged, gw_merge is not needed
            if plan is not None:
                texts = content.check_replacements(m, report, base_files, plan)
            elif not require_delta:
                # a local check (an author without the game or on Linux): only the checks of the merged texts are
                # skipped, said clearly; the CI of the index (require_delta) has gw_merge - an error there, below
                report.warning("merge_plan_unavailable", "the package replaces text files of the base (%s): which of "
                                                         "them the game merges, game_version_missing and the delta are "
                                                         "not checked - %s" %
                               (resources.listing(candidates, 5), no_plan))
                no_plan = None
        else:
            report.note("no_snapshot", "no snapshot of the base: replacements and the delta are not checked")
        replaces = resources.check_resources(root, files, m, report, base_files, record_tag, record_files, plan)
        content.check_namespaces(root, files, m, report, base_files)
        if snap_latest and snapshot_check:
            content.check_snapshot_refs(snap_latest, root, report, snap_author if snap_author != snap_latest else None)

        # 5. the delta (always a separate file, 15 section 7.2)
        ref = None
        if require_delta and snapshots and snapshots.latest and not snap_latest:
            # a release of game_releases.ltx without its snapshot (not published yet, a network error): the
            # replacements cannot be checked, the version waits instead of going out without a delta
            report.error("delta_unavailable", "no snapshot of the latest game release %s: the replacements of the "
                                              "base and the delta cannot be checked" % snapshots.latest)
        if texts:
            level = report.error if require_delta else report.warning
            gv = m["game_version"]
            if gv and not snap_author and not SAFE_TAG_RE.match(gv):
                level("delta_game_version_tag", "game_version %s is not a release tag of the game (letters, digits, "
                                                "'.', '_', '-', up to 64): the delta cannot be made" % gv)
            elif gv and not snap_author and require_delta and cfg is not None and \
                    not any(ltx.same_game_tag(t, gv) for t, _ in cfg.game_releases):
                level("delta_unknown_game_version", "game_version %s is not a release of the game known to the catalog "
                                                    "(game_releases.ltx): the delta cannot be made. Fix game_version "
                                                    "(the version is checked again when the release is added)" % gv)
            elif gv and not snap_author:
                level("delta_unavailable", "no snapshot of game_version %s: the delta cannot be made" % gv)
            elif not snap_author:
                level("delta_unavailable", "game_version is not set and no game release is known for the date: the "
                                           "delta cannot be made")
            else:
                out_dir = files_dir or os.path.join(work, "files")
                os.makedirs(out_dir, exist_ok=True)
                out = os.path.join(out_dir, delta.merge_file_name(m["version"]))
                try:
                    ref = delta.compute(root, out, snap_author, report, latest=snap_latest,
                                        game=game_tag, gw_merge=gw_merge)
                except (RuntimeError, OSError) as e:
                    level("delta_failed", "the delta is not made: %s" % e)
        if no_plan:
            # an error of CI (TRANSIENT: the version is retried), told after the checks of the delta
            report.error("merge_plan_unavailable", "the package replaces text files of the base, gw_merge plan does not "
                                                   "decide them (%s): the CI of the index needs it" % no_plan)
        files_list = [{"path": n, "size": files[n]} for n in sorted(files) if not package.is_launcher_file(n)]
        result = {"manifest": manifest_mod.strip(m), "files": files_list, "merge": ref, "replaces": replaces,
                  "unpacked_size": sum(f["size"] for f in files_list), "caps_found": report.data.get("caps_found", []),
                  "scanner": report.data.get("scanner", []), "game_tag": game_tag or ""}
        report.data["result"] = {k: v for k, v in result.items() if k not in ("files", "replaces")}
        if replaces is not None:
            report.data["result"]["replaces"] = {k: v for k, v in replaces.items() if k != "files"}
        return report, result
    finally:
        if own_work:
            shutil.rmtree(work, ignore_errors=True)


def version_file(addon_id, version, tag, result):
    """The immutable files/<id>/<ver>.json (11 section 4.1 + the base files it replaces (14 A6) + the reference to the
    delta)"""
    out = {"schema": 1, "id": addon_id, "version": version, "tag": tag, "files": result["files"]}
    if result.get("replaces") is not None:
        out["replaces"] = result["replaces"]
    if result.get("merge"):
        out["merge"] = result["merge"]
    return out
