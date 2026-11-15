"""The hand-over between the two jobs of build-index.yml and recompute-deltas.yml (review F3).

The job that reads the untrusted packages (the validator, zipfile, gw_merge on the texts of an author) has no token
that writes anything. It exports what it changed in the index repo:

    <out>/files/...     the new and changed files, at their paths in the repo
    <out>/changes.json  {"schema": 1, "base": "<commit>", "changed": [paths], "deleted": [paths]}

The job that commits (its token writes to index only) runs no code of a package: it checks every path and every file of
the hand-over and copies them into its own checkout of the same commit (apply). What the first job can do even when a
package takes it over is limited to what these checks let through: the generated files of the index, valid JSON, no
change of a published file (files/, history/ are immutable), nothing outside them.
"""

import os
import re
import shutil
import stat
import subprocess

from . import delta as delta_mod, limits, strictjson

ROOTS = ("index.json", "state", "files", "history")
FILE_MAX_BYTES = 64 * 1024 * 1024
_ID = r"[a-z0-9_]{1,64}"
_VER = r"(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})"
ALLOWED = [
    re.compile(r"^index\.json\Z"),
    re.compile(r"^state/%s\.json\Z" % _ID),
    re.compile(r"^files/%s/%s\.json\Z" % (_ID, _VER)),
    re.compile(r"^files/%s/%s\.merge\.json\Z" % (_ID, _VER)),
    re.compile(r"^files/%s/%s\.merge\.%s\.json\Z" % (_ID, _VER, delta_mod.ALGO_TAG_PATTERN)),
    re.compile(r"^history/%s\.[0-9a-f]{8}\.json\Z" % _ID),
]
IMMUTABLE = ("files/", "history/")
DELETABLE = ("history/",)  # render removes the history files older than 14 days; nothing else is ever deleted


class ChangesError(RuntimeError):
    pass


def _git(root, *args):
    return subprocess.run(["git", "-C", root] + list(args), capture_output=True, check=True).stdout


def export(root, out):
    """The changes of the generated files of the checkout root against its HEAD -> out. -> (changed, deleted)"""
    base = _git(root, "rev-parse", "HEAD").decode().strip()
    raw = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--", *ROOTS)
    changed, deleted = [], []
    for item in raw.split(b"\0"):
        if not item:
            continue
        code, path = item[:2].decode(), item[3:].decode("utf-8")
        if "D" in code:
            deleted.append(path)
        else:
            changed.append(path)
    os.makedirs(os.path.join(out, "files"), exist_ok=True)
    for path in changed:
        target = os.path.join(out, "files", *path.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(os.path.join(root, *path.split("/")), target)
    strictjson.write_file(os.path.join(out, "changes.json"),
                          {"schema": 1, "base": base, "changed": sorted(changed), "deleted": sorted(deleted)})
    return sorted(changed), sorted(deleted)


def _allowed(path):
    return isinstance(path, str) and any(r.match(path) for r in ALLOWED)


def _check_generated(src, root, changed):
    """What of index.json / state/ the configuration of the checkout decides is taken from it, not from the hand-over:
    the source and its address templates (an asset template of a taken-over job would send every download elsewhere),
    the repo of an addon (<org>/<id>: no addon downloads from the releases of another repo), the status set by
    moderation.ltx and its revoked lists (they cannot be dropped)"""
    from .indexcfg import ConfigError, IndexConfig
    try:
        cfg = IndexConfig(root)
    except ConfigError as e:
        raise ChangesError("the configuration of the checkout: %s" % e)
    org = cfg.org.lower()

    def repo_ok(addon_id, repo, empty_ok):
        if not isinstance(repo, str):
            return False
        if repo == "":
            return empty_ok
        if not org:
            return True
        owner, _, name = repo.partition("/")
        return owner.lower() == org and name == addon_id

    states = set()
    for name in os.listdir(os.path.join(root, "state")) if os.path.isdir(os.path.join(root, "state")) else ():
        if name.endswith(".json"):
            states.add(name[:-5])
    for path in changed:
        if not path.startswith("state/"):
            continue
        addon_id = path[len("state/"):-len(".json")]
        states.add(addon_id)
        st = strictjson.load_file(os.path.join(src, "files", *path.split("/")))
        if not isinstance(st, dict) or st.get("id") != addon_id or not repo_ok(addon_id, st.get("repo"), True):
            raise ChangesError("%s: its id / repo are not the ones of the path and the organization" % path)
    if "index.json" not in changed:
        return
    index = strictjson.load_file(os.path.join(src, "files", "index.json"))
    if not isinstance(index, dict) or not isinstance(index.get("addons"), list):
        raise ChangesError("index.json is not an index")
    for key, want in (("source", cfg.source), ("templates", cfg.templates), ("mirrors", cfg.mirrors)):
        if index.get(key) != want:
            raise ChangesError("index.json: %s is not the one of source.ltx" % key)
    revoked = index.get("revoked_sha256")
    if not isinstance(revoked, list) or not set(cfg.moderation.revoked_sha256) <= set(revoked):
        raise ChangesError("index.json: revoked_sha256 lacks a sha256 of moderation.ltx")
    want_ids = {i for i in cfg.moderation.revoked if i in states}
    ids = index.get("revoked_ids", [])
    if not isinstance(ids, list) or not want_ids <= set(ids):
        raise ChangesError("index.json: revoked_ids lacks an addon revoked in moderation.ltx")
    for a in index["addons"]:
        addon_id = a.get("id") if isinstance(a, dict) else None
        if not isinstance(addon_id, str) or not repo_ok(addon_id, a.get("repo"), False) or \
                a.get("status") != cfg.moderation.status(addon_id):
            raise ChangesError("index.json: the repo or the status of %r is not the one of the organization / "
                               "moderation.ltx" % (addon_id,))


# ---- the package assets the hand-over names (decision 63: publish trusts no sha256 of the job that read the packages) --

ASSET_STATUSES = ("listed", "pending")  # the versions of state/ that reach the players (pending: after a review)
DOWNLOAD_CHUNK = 1 << 20


def _asset_rows(path, obj):
    """(id, version, tag, name, sha256, size) of every package asset a generated file names. state/: only the versions
    that are or may become listed; index.json: "latest" of every addon; history/: every entry"""
    rows = set()

    def add(addon_id, entry, version_key):
        if not isinstance(entry, dict):
            return
        for a in entry.get("assets") or []:
            if isinstance(a, dict):
                rows.add((addon_id, entry.get(version_key), entry.get("tag"), a.get("name"), a.get("sha256"),
                          a.get("size")))

    if path.startswith("state/") and isinstance(obj, dict) and isinstance(obj.get("versions"), dict):
        for version, sv in obj["versions"].items():
            if isinstance(sv, dict) and sv.get("status") in ASSET_STATUSES:
                add(obj.get("id"), dict(sv, v=version), "v")
    elif path == "index.json" and isinstance(obj, dict):
        for item in obj.get("addons") or []:
            if isinstance(item, dict):
                for entry in item.get("latest") or []:
                    add(item.get("id"), entry, "v")
    elif path.startswith("history/") and isinstance(obj, list):
        addon_id = path[len("history/"):].rsplit(".", 2)[0]
        for entry in obj:
            add(addon_id, entry, "v")
    return rows


def _known_rows(root):
    """The asset rows the checkout publishes already (they were verified when they came in)"""
    rows = set()
    for path in ["index.json"] + ["%s/%s" % (d, n) for d in ("state", "history")
                                  if os.path.isdir(os.path.join(root, d))
                                  for n in sorted(os.listdir(os.path.join(root, d))) if n.endswith(".json")]:
        full = os.path.join(root, *path.split("/"))
        if not os.path.isfile(full):
            continue
        try:
            obj = strictjson.load_file(full)
        except strictjson.StrictJsonError:
            continue
        if path.startswith("state/") and isinstance(obj, dict) and isinstance(obj.get("versions"), dict):
            # every status a version reaches only through listed / pending (yanked, gone, rejected): its assets were
            # verified then. Not "invalid": the rows of a refused version never went through verify_assets
            obj = dict(obj, versions={v: dict(sv, status="listed") for v, sv in obj["versions"].items()
                                      if isinstance(sv, dict) and sv.get("status") != "invalid"})
        rows |= _asset_rows(path, obj)
    return rows


RETIRED_STATUSES = ("yanked", "gone", "rejected")  # reached only from listed / pending (index_build)


def _check_retired(path, obj, known):
    """A version of the hand-over in yanked / gone / rejected keeps the assets it had as listed / pending, so its rows
    must be the ones the checkout publishes already. Else a taken-over job could bring in an unchecked row as yanked
    (not downloaded: not in ASSET_STATUSES), and a later hand-over list it again as a "known" row: the download would be
    skipped. -> ChangesError"""
    if not isinstance(obj, dict) or not isinstance(obj.get("versions"), dict):
        return
    for version, sv in obj["versions"].items():
        if not isinstance(sv, dict) or sv.get("status") not in RETIRED_STATUSES:
            continue
        rows = _asset_rows(path, {"id": obj.get("id"), "versions": {version: dict(sv, status="listed")}})
        if not rows <= known:
            raise ChangesError("%s %s: %s with package assets that were never listed or pending (not checked)" %
                               (path, version, sv.get("status")))


def download_digest(url, max_bytes):
    """Streams url (no token: the public asset address, as the launcher downloads it) -> (bytes read, sha256 hex).
    Never reads more than max_bytes + 1: nothing of it is parsed or unpacked, only hashed"""
    import hashlib
    import urllib.request
    h = hashlib.sha256()
    size = 0
    req = urllib.request.Request(url, headers={"User-Agent": "gw-catalog-index"})
    with urllib.request.urlopen(req, timeout=600) as r:
        while size <= max_bytes:
            chunk = r.read(min(DOWNLOAD_CHUNK, max_bytes + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
    return size, h.hexdigest()


def verify_assets(src, root, changed, fetch=download_digest, log=None):
    """Every package asset the hand-over names and the checkout does not publish yet is downloaded from the address
    the launcher uses (source.ltx [templates] asset, the repo <org>/<id>) and hashed here: its size and sha256 must be
    the ones of the hand-over. The job that read the packages could be taken over by one of them; what it says of a
    package is not taken on trust (review 2026-11-06, residual risk of F3). -> the number of assets checked"""
    from .indexcfg import ConfigError, IndexConfig
    try:
        cfg = IndexConfig(root)
    except ConfigError as e:
        raise ChangesError("the configuration of the checkout: %s" % e)
    template = cfg.templates.get("asset", "")
    if not cfg.org or "{name}" not in template:
        raise ChangesError("source.ltx: [github] org and [templates] asset ({repo}, {tag}, {name}) are needed to "
                           "check the package assets")
    known = _known_rows(root)
    new = set()
    for path in changed:
        if path == "index.json" or path.startswith(("state/", "history/")):
            obj = strictjson.load_file(os.path.join(src, "files", *path.split("/")))
            new |= _asset_rows(path, obj)
            if path.startswith("state/"):
                _check_retired(path, obj, known)
    new -= known
    seen = {}
    for addon_id, version, tag, name, sha, size in sorted(new, key=lambda r: tuple(str(x) for x in r)):
        label = "%s %s %s" % (addon_id, version, name)
        if not (isinstance(addon_id, str) and re.match(r"^%s\Z" % _ID, addon_id) and isinstance(version, str) and
                re.match(r"^%s\Z" % _VER, version)):
            raise ChangesError("%s: not an addon id / version" % label)
        if tag != "v" + version:
            raise ChangesError("%s: the tag %r is not v<version>" % (label, tag))
        if not isinstance(name, str) or not re.match(r"^%s-%s(?:\.part[1-9][0-9]{0,3})?\.zip\Z" %
                                                     (re.escape(addon_id), re.escape(version)), name):
            raise ChangesError("%s: not a package asset name of this version" % label)
        if not (isinstance(sha, str) and re.match(r"^[0-9a-f]{64}\Z", sha)) or isinstance(size, bool) or \
                not isinstance(size, int) or not 0 < size <= limits.PART_MAX_BYTES:
            raise ChangesError("%s: no valid sha256 / size" % label)
        import urllib.parse
        url = template.replace("{repo}", "%s/%s" % (cfg.org, addon_id)).replace("{tag}", tag) \
            .replace("{name}", urllib.parse.quote(name))
        if url not in seen:
            try:
                seen[url] = fetch(url, size)
            except OSError as e:
                raise ChangesError("%s: cannot download %s: %s" % (label, url, e))
        got_size, got_sha = seen[url]
        if (got_size, got_sha) != (size, sha):
            raise ChangesError("%s: %s is %d bytes, sha256 %s; the hand-over says %d bytes, %s" %
                               (label, url, got_size, got_sha, size, sha))
        if log:
            log("verified %s (%d bytes)" % (label, size))
    return len(seen)


def apply(src, root, expect_base=None, fetch=None, log=None):
    """Checks the hand-over src and copies it into the checkout root (the commit of the export). -> (changed, deleted)
    fetch: url, size -> (size, sha256) of a package asset (download_digest); given - every new asset the hand-over
    names is downloaded and hashed (verify_assets). Any problem - ChangesError, nothing is copied"""
    data = strictjson.load_file(os.path.join(src, "changes.json"))
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("changed"), list) or \
            not isinstance(data.get("deleted"), list):
        raise ChangesError("changes.json is not a hand-over of the index CI")
    base = _git(root, "rev-parse", "HEAD").decode().strip()
    if data.get("base") != (expect_base or base):
        raise ChangesError("the hand-over is made on %r, the checkout is %s" % (data.get("base"), base))
    seen = set()
    for path in data["changed"] + data["deleted"]:
        if not _allowed(path) or path in seen:
            raise ChangesError("not a generated file of the index (or listed twice): %r" % (path,))
        seen.add(path)
    for path in data["deleted"]:
        if not path.startswith(DELETABLE):
            raise ChangesError("%s: only old history/ files are deleted" % path)
    files = os.path.join(src, "files")
    # nothing else in the hand-over: every regular file listed, no links, no folders of their own
    present = set()
    for dirpath, dirnames, filenames in os.walk(files):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            mode = os.lstat(full).st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ChangesError("%s is not a regular file" % os.path.relpath(full, files))
        for name in filenames:
            present.add(os.path.relpath(os.path.join(dirpath, name), files).replace(os.sep, "/"))
    if present != set(data["changed"]):
        raise ChangesError("the files of the hand-over are not the ones changes.json lists")
    for path in data["changed"]:
        full = os.path.join(files, *path.split("/"))
        if os.path.getsize(full) > FILE_MAX_BYTES:
            raise ChangesError("%s is larger than %d bytes" % (path, FILE_MAX_BYTES))
        with open(full, "rb") as f:
            new = f.read()
        try:
            strictjson.loads_bytes(new, path)
        except strictjson.StrictJsonError as e:
            raise ChangesError(str(e))
        target = os.path.join(root, *path.split("/"))
        if path.startswith(IMMUTABLE) and os.path.lexists(target):
            with open(target, "rb") as f:
                if f.read() != new:
                    raise ChangesError("%s is published: files/ and history/ are immutable" % path)
    for path in data["deleted"]:
        if not os.path.isfile(os.path.join(root, *path.split("/"))):
            raise ChangesError("%s is to be deleted, the checkout has no such file" % path)
    _check_generated(src, root, data["changed"])
    if fetch is not None:
        verify_assets(src, root, data["changed"], fetch, log)
    for path in data["changed"]:
        target = os.path.join(root, *path.split("/"))
        os.makedirs(os.path.dirname(target) or root, exist_ok=True)
        shutil.copyfile(os.path.join(files, *path.split("/")), target)
    for path in data["deleted"]:
        os.remove(os.path.join(root, *path.split("/")))
    return data["changed"], data["deleted"]
