"""The central CI of the index (11-catalog-distribution.md 4.2, 5.2, 5.3; 15-merge-implementation.md 7.2 steps 1-4).

    ingest()   repos of the organization -> new releases checked -> files/<id>/<ver>.json (+ <ver>.merge.json),
               state/<id>.json, issues (validation failed, review, asset changed after the check)
    render()   state/ + moderation.ltx -> history/<id>.<hash8>.json and index.json (offline: also after a review)
    stats()    stats.json: downloads and stars (build-stats.yml)
    review()   a moderator's decision on a pending version (review.yml)

Files are written only when their content changes (index.json ignores its generated_at for that), so a run without
changes leaves git clean and the players' ETag of index.json stays (11 section 4.2 step 5).
"""

import datetime
import json
import os
import re
import shutil
import tempfile

from . import catalog_meta, delta as delta_mod, limits, ltx, manifest as manifest_mod, package, schema, strictjson
from .indexcfg import SERVICE_IDS
from .report import Report
from .validate import TRANSIENT, UNPACK_RESERVE, validate_version, version_file

# The version as the schemas of state/ and index.json have it (no leading zeros, up to 9 digits, ASCII): another tag
# would be refused by save_state and stop the whole run
TAG_RE = re.compile(r"^v((?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8}))\Z")
HISTORY_KEEP_DAYS = 14
LIVE = ("listed", "yanked")  # versions that were published (they are in history/)
# A version whose check fails only for a reason of CI (TRANSIENT: no snapshot yet, gw_merge failed, no disk space) is
# retried at a later run, not more often than once in RETRY_MIN_MINUTES while its inputs stay the same (the cron runs
# every 15 minutes since decision 63; a retry downloads the whole package). After RETRY_NOTIFY_AFTER tries (~6 h) the
# moderators get one issue; after RETRY_MAX tries (~2 days) the version is downloaded again only when something it
# depends on changes (its assets, the exceptions of moderation.ltx, game_releases.ltx, the gw_merge of the run:
# retry_key) or once in RETRY_PAUSED_DAYS: a fix that changes none of them (the snapshot published, a larger runner) is
# picked up too
RETRY_NOTIFY_AFTER = 12
RETRY_MAX = 96
RETRY_PAUSED_DAYS = 1
RETRY_MIN_MINUTES = 25
ISSUE_BODY_MAX = 60000
# The labels the CI of the index puts on its issues (publish-issues accepts no other)
ISSUE_LABELS = ("catalog-ci", "review", "moderation", "asset-changed")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def vkey(v):
    return ltx.parse_version(v) or (0, 0, 0)


class Index:
    """The files of the index repo"""

    def __init__(self, root):
        self.root = root

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def load_state(self, addon_id, repo):
        p = self.path("state", addon_id + ".json")
        if os.path.isfile(p):
            return strictjson.load_file(p)
        return {"schema": 1, "id": addon_id, "repo": repo, "accepted_caps": [], "versions": {}}

    def save_state(self, st):
        os.makedirs(self.path("state"), exist_ok=True)
        errors = schema.validate(st, "state")
        if errors:
            raise RuntimeError("state/%s.json does not match its schema: %s" % (st["id"], "; ".join(errors[:5])))
        return strictjson.write_file(self.path("state", st["id"] + ".json"), st)

    def states(self):
        d = self.path("state")
        if not os.path.isdir(d):
            return []
        return [strictjson.load_file(os.path.join(d, n)) for n in sorted(os.listdir(d)) if n.endswith(".json")]


# ---- ingest ---------------------------------------------------------------------------------------------------------

def _report_body(report, header):
    return header + "\n\n" + report.to_markdown("Validator report") + "\n<sub>gw-catalog index CI</sub>\n"


def _asset_fingerprint(a):
    return {"id": a["id"], "name": a["name"], "size": a["size"], "updated_at": a["updated_at"]}


def _hash8(obj):
    return strictjson.sha256_bytes(json.dumps(obj, sort_keys=True).encode("utf-8"))[:8]


def exceptions_key(cfg, addon_id):
    """What a check of an addon depends on besides its package: the moderation exceptions of the addon and the game
    releases (game_version of an unknown release is an error until the release is added). A change re-checks an
    invalid version (an exception granted after the rejection works) and wakes a paused retry. "" - nothing set"""
    exc = sorted(cfg.moderation.exceptions.get(addon_id, ()))
    rel = [t for t, _ in cfg.game_releases]
    return _hash8([exc, rel]) if exc or rel else ""


# ---- issues ---------------------------------------------------------------------------------------------------------

class DirectIssues:
    """Issues opened at once through the provider (local runs, the tests). An open issue of the bot with the same
    marker is taken instead of a new one (a run that failed after opening it and is repeated)"""

    def __init__(self, provider):
        self.provider = provider

    def open(self, repo, title, body, labels, marker, ref=None):
        body = _with_marker(body, marker)
        found = self.provider.find_issue(repo, marker, labels) if hasattr(self.provider, "find_issue") else None
        return found or self.provider.create_issue(repo, title, body, labels)


class IssueOutbox:
    """build-index in two jobs (11 section 4.2, review F3): the job that reads the untrusted packages has no token that
    writes anything; the issues it would open go to a file, the job that commits opens them (publish_issues) and puts
    their numbers into state/ (ref: the addon and the path of the number in its state). The number is 0 until then."""

    def __init__(self):
        self.items = []

    def open(self, repo, title, body, labels, marker, ref=None):
        self.items.append({"repo": repo, "title": title, "body": _with_marker(body, marker), "labels": list(labels),
                           "marker": marker, "ref": ref})
        return 0

    def write(self, path):
        with open(path, "wb") as f:
            f.write(strictjson.dumps({"schema": 1, "issues": self.items}))


def _with_marker(body, marker):
    tag = "<!-- %s -->" % marker
    return body if tag in body else tag + "\n" + body


def publish_issues(idx, provider, outbox_path, cfg, creator, log):
    """The publish job of build-index: the issues of the outbox -> opened (or the open issue of the bot with the same
    marker taken: a run repeated after a lost push opens no second one), their numbers -> state/. The outbox comes from
    the job that read the packages: every field is checked before it is used. -> number of issues opened"""
    data = strictjson.load_file(outbox_path)
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("issues"), list):
        raise RuntimeError("%s is not an outbox of build-index" % outbox_path)
    opened = 0
    for item in data["issues"]:
        if not isinstance(item, dict):
            raise RuntimeError("an outbox item is not an object")
        repo, title, body, labels, marker = (item.get(k) for k in ("repo", "title", "body", "labels", "marker"))
        if not isinstance(repo, str) or not (repo == cfg.index_repo or ltx.is_valid_id(repo)):
            raise RuntimeError("outbox: %r is not the index repo or an addon repo" % (repo,))
        if not (isinstance(title, str) and 0 < len(title) <= 256 and isinstance(body, str) and
                isinstance(marker, str) and re.match(r"^gw-[a-z-]+ [ -~]{1,200}\Z", marker) and
                isinstance(labels, list) and labels and all(lb in ISSUE_LABELS for lb in labels)):
            raise RuntimeError("outbox: a bad item for %s" % repo)
        number = provider.find_issue(repo, marker, labels, creator)
        if not number:
            number = provider.create_issue(repo, title, body[:ISSUE_BODY_MAX], labels)
            opened += 1
        ref = item.get("ref")
        if ref is not None:
            if not (isinstance(ref, dict) and isinstance(ref.get("id"), str) and ltx.is_valid_id(ref["id"]) and
                    isinstance(ref.get("path"), list) and ref["path"] in _issue_refs(ref["path"])):
                raise RuntimeError("outbox: a bad reference %r" % (ref,))
            st = idx.load_state(ref["id"], "")
            node = st
            for key in ref["path"][:-1]:
                node = node.get(key) if isinstance(node, dict) else None
            if not isinstance(node, dict):
                log("outbox: state/%s.json has no %s: issue #%d not recorded" % (ref["id"], "/".join(ref["path"]),
                                                                                number))
                continue
            node[ref["path"][-1]] = number
            idx.save_state(st)
        log("issue %s#%d: %s" % (repo, number, title))
    return opened


def _issue_refs(path):
    """The places of state/<id>.json an issue number goes to (a reference of the outbox names one of them)"""
    if len(path) == 1:
        return [["catalog_issue"]]
    if len(path) == 3 and isinstance(path[1], str) and TAG_RE.match("v" + path[1]):
        return [["versions", path[1], "issue"], ["retry", path[1], "issue"]]
    return []


# ---- ingest ---------------------------------------------------------------------------------------------------------

def ingest(idx, provider, cfg, snapshots, log, gw_merge=None, only=None, now=None, work_root=None, issues=None):
    """One pass over the organization. issues: where issues go (DirectIssues of the provider by default; IssueOutbox in
    the CI of the index). -> summary dict"""
    now = now or utc_now()
    issues = issues or DirectIssues(provider)
    summary = {"new": [], "pending": [], "invalid": [], "yanked": [], "gone": [], "retry": [], "retry_later": [],
               "paused": [], "replaced": [], "catalog_errors": [], "failed": [], "issues": 0}
    merge_key = strictjson.sha256_file(gw_merge)[:8] if gw_merge and os.path.isfile(gw_merge) else ""
    work_root = work_root or tempfile.mkdtemp(prefix="gwcat_ingest_")
    try:
        for repo in sorted(provider.list_repos(), key=lambda r: r["name"]):
            name = repo["name"]
            if only and name not in only:
                continue
            if name in SERVICE_IDS or name.startswith(".") or repo.get("private"):
                continue
            if not ltx.is_valid_id(name):
                log("skip %s: not an addon id" % name)
                continue
            if cfg.reserved.is_reserved(name):
                log("skip %s: reserved id" % name)
                continue
            # One addon must not stop the catalog: a broken package, an API error or a hung tool of one repo is
            # logged, its state is not saved (the next run repeats it) and the other addons go on
            try:
                st = idx.load_state(name, repo["full_name"])
                _check_repo_identity(cfg, st, repo, name, summary, log, issues)
                _ingest_catalog(idx, provider, cfg, st, name, now, summary, log, issues)
                if cfg.moderation.status(name) == "listed":
                    _ingest_releases(idx, provider, cfg, snapshots, st, name, now, summary, log, gw_merge,
                                     os.path.join(work_root, name), issues, merge_key)
                idx.save_state(st)
            except Exception as e:  # noqa: BLE001 - isolation of one addon, reported below
                summary["failed"].append(name)
                log("::error::%s: %s: %s - not saved, repeated at the next run" % (name, type(e).__name__, e))
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
    return summary


def _check_repo_identity(cfg, st, repo, name, summary, log, issues):
    """The GitHub id of the repo (11 section 5.1, review F6): a repo deleted (or moved out of the organization) and made
    again under the same name is another addon. Its state of the old repo must not carry over: the published versions
    of the old author go (gone), the accepted capabilities and type are forgotten - the first release of the new repo
    is a first publication and goes to review. The moderators get an issue."""
    repo_id = repo.get("repo_id")
    if repo_id is None:
        return
    old = st.get("repo_id")
    st["repo_id"] = repo_id
    if old is None or old == repo_id:
        return
    for sv in st["versions"].values():
        if sv["status"] in ("listed", "pending", "yanked"):
            sv["status"] = "gone"
            sv["reason"] = "the repo was replaced by another one of the same name (GitHub id %s -> %s)" % (old, repo_id)
    st["accepted_caps"] = []
    st.pop("accepted_type", None)
    st.pop("catalog", None)
    st.pop("catalog_checked", None)
    summary["replaced"].append(name)
    used = sorted(st["versions"], key=vkey)
    issues.open(cfg.index_repo, "Repo replaced: %s" % name,
                "The repo `%s` has another GitHub id now (%s, was %s): it was deleted or moved out of the organization "
                "and made again. The versions of the old repo are taken out of the index (gone); the first release of "
                "the new repo goes to review. The version numbers of the old repo stay taken (files/ is immutable): a "
                "release of the new repo with one of them is not checked at all - tell the author to start after "
                "them: %s." % (name, repo_id, old, ", ".join(used) or "-"), ["moderation"],
                "gw-ci replaced %s %s" % (name, repo_id))
    summary["issues"] += 1
    log("::warning::%s: the repo was replaced (GitHub id %s -> %s): its old versions are gone" % (name, old, repo_id))


def _catalog_check_key(cfg, rev, name):
    """The catalog card is checked again only when this changes: the commit of the repo, tags.ltx, the exceptions"""
    return "%s:%s" % (rev, _hash8([sorted(cfg.tags), sorted(cfg.moderation.exceptions.get(name, ()))]))


def _ingest_catalog(idx, provider, cfg, st, name, now, summary, log, issues):
    # The same commit, the same tags and exceptions: nothing to read again (11 section 4.2; review F8 - at 1000 addons
    # the files of catalog/ were downloaded every 30 min)
    head = provider.head_rev(name) if hasattr(provider, "head_rev") else None
    if head and st.get("catalog_checked") == _catalog_check_key(cfg, head, name):
        if st.get("catalog_errors"):
            summary["catalog_errors"].append(name)
        return
    rev, tree = provider.read_tree(name, rev=head) if head else provider.read_tree(name)
    st["catalog_checked"] = _catalog_check_key(cfg, rev, name)
    rep = Report("%s catalog/ @ %s" % (name, rev[:7]))
    card = catalog_meta.check_catalog(tree, rep, cfg.tags)
    if "addon.ltx" in tree:
        mrep = Report("addon.ltx")
        manifest_mod.check_manifest(tree["addon.ltx"], mrep, "addon.ltx", expect_id=name, reserved=cfg.reserved)
        for item in mrep.errors:  # the repo copy of addon.ltx is informative (the package has the real one)
            rep.warning("repo_manifest", item["message"], "addon.ltx")
    else:
        rep.warning("repo_no_manifest", "the repo has no addon.ltx (11 section 3.1)", "addon.ltx")
    rep.apply_exceptions(cfg.moderation.exceptions.get(name))
    if rep.ok and card is not None:
        card["rev"] = rev
        if st.get("catalog") != card:
            st["catalog"] = card
        st.pop("catalog_errors", None)
        st.pop("catalog_issue", None)
        return
    errors = sorted("%s: %s" % (i["code"], i["message"]) for i in rep.errors)
    summary["catalog_errors"].append(name)
    if st.get("catalog_errors") != errors:
        st["catalog_errors"] = errors
        st["catalog_issue"] = issues.open(
            name, "Catalog card is not valid (%s)" % rev[:7],
            _report_body(rep, "The card in `catalog/` was not published; the catalog keeps the previous one. Fix the "
                              "errors below and push."), ["catalog-ci"],
            "gw-ci catalog %s %s" % (name, _hash8(errors)), {"id": name, "path": ["catalog_issue"]})
        summary["issues"] += 1
    log("%s: catalog/ has errors, the previous card is kept" % name)


def _ingest_releases(idx, provider, cfg, snapshots, st, name, now, summary, log, gw_merge, work, issues,
                     merge_key=""):
    releases = [r for r in provider.list_releases(name) if not r["draft"]]
    seen = set()
    exc_key = exceptions_key(cfg, name)
    for rel in sorted(releases, key=lambda r: (r["published_at"], str(r["id"]))):
        m = TAG_RE.match(rel["tag"])
        if not m:
            log("%s: release %s: the tag is not v<MAJOR.MINOR.PATCH>, skipped" % (name, rel["tag"]))
            continue
        version = m.group(1)
        seen.add(version)
        sv = st["versions"].get(version)
        # an invalid version is checked again after a new upload, or when an exception or a game release is added
        # (review F14: an exception granted after the rejection did nothing until the author uploaded the same file)
        if sv is None or (sv["status"] == "invalid" and (
                _assets_changed(sv, rel, package_names=_package_names(rel, name, version)) or
                sv.get("exceptions", "") != exc_key)):
            key = _retry_key(rel, exc_key, merge_key)
            retry = st.get("retry", {}).get(version)
            if retry and retry["count"] >= RETRY_MAX and retry.get("key") == key and \
                    _days_between(retry.get("last") or retry["since"], now) < RETRY_PAUSED_DAYS:
                summary["paused"].append("%s@%s" % (name, version))
                continue  # gave up until an input changes or a day passes: no download every 15 min
            if retry and retry.get("key") == key and \
                    _minutes_between(retry.get("last") or retry["since"], now) < RETRY_MIN_MINUTES:
                summary["retry_later"].append("%s@%s" % (name, version))
                continue  # tried a moment ago with the same inputs: the next run after RETRY_MIN_MINUTES
            _new_version(idx, provider, cfg, snapshots, st, name, version, rel, now, summary, log, gw_merge, work,
                         issues, exc_key, key)
        elif sv["status"] in ("listed", "pending") and _assets_changed(sv, rel):
            sv["status"] = "yanked"
            sv["reason"] = "a package asset changed after the check"
            sv["issue"] = issues.open(
                cfg.index_repo, "Asset changed after the check: %s %s" % (name, rel["tag"]),
                "The package assets of `%s` %s differ from the checked ones (id, size or updated_at). The version is "
                "yanked. Checked:\n\n```\n%s\n```\nNow:\n\n```\n%s\n```\n" %
                (name, rel["tag"], "\n".join("%s %s %s" % (a["name"], a["size"], a["sha256"]) for a in sv["assets"]),
                 "\n".join("%s %s %s" % (a["name"], a["size"], a["updated_at"]) for a in rel["assets"])),
                ["moderation", "asset-changed"], "gw-ci asset-changed %s %s" % (name, version),
                {"id": name, "path": ["versions", version, "issue"]})
            summary["yanked"].append("%s@%s" % (name, version))
            summary["issues"] += 1
    for version in list(st.get("retry", {})):
        if version not in seen or st["versions"].get(version, {}).get("status") not in (None, "invalid"):
            st["retry"].pop(version)  # the release is gone or the version got its result
    if not st.get("retry"):
        st.pop("retry", None)
    for version, sv in st["versions"].items():
        if version not in seen and sv["status"] in ("listed", "pending"):
            sv["status"] = "gone"
            sv["reason"] = "the release is deleted or made a draft"
            summary["gone"].append("%s@%s" % (name, version))


def _package_names(rel, addon_id, version):
    """The names of the release assets that make the package (what _new_version checked); other assets do not count"""
    return set(package.classify_assets([a["name"] for a in rel["assets"]], addon_id, version, Report("assets")))


def _assets_changed(sv, rel, package_names=None):
    now = {a["name"]: a for a in rel["assets"]}
    for a in sv["assets"]:
        b = now.get(a["name"])
        if b is None or _asset_fingerprint(b) != {"id": a["id"], "name": a["name"], "size": a["size"],
                                                  "updated_at": a["updated_at"]}:
            return True
    if package_names is not None:  # an invalid version: a new upload with other package names is a new attempt too
        return {a["name"] for a in sv["assets"]} != package_names
    return False


def _retry_key(rel, exc_key, merge_key):
    """What a paused retry waits for: the assets of the release, the exceptions and game releases, the gw_merge"""
    return _hash8([sorted((a["name"], a["id"], a["size"], a["updated_at"]) for a in rel["assets"]), exc_key,
                   merge_key])


def _new_version(idx, provider, cfg, snapshots, st, name, version, rel, now, summary, log, gw_merge, work, issues,
                 exc_key="", retry_key=""):
    # the downloaded and unpacked package goes right after the version: a run over many new versions (the first run of
    # the catalog) must not keep all of them on the disk of the runner
    vwork = os.path.join(work, version)
    try:
        _check_new_version(idx, provider, cfg, snapshots, st, name, version, rel, now, summary, log, gw_merge, vwork,
                           issues, exc_key, retry_key)
    finally:
        shutil.rmtree(vwork, ignore_errors=True)


def _retry(cfg, st, name, version, rel, errors, now, summary, log, issues, retry_key):
    """A check that failed for a reason of CI (TRANSIENT): counted in state/ retry; one issue for the moderators after
    RETRY_NOTIFY_AFTER runs, no download after RETRY_MAX runs until retry_key changes (review F7, F16)"""
    label = "%s@%s" % (name, version)
    retries = st.setdefault("retry", {})
    item = retries.get(version)
    if item is None or item.get("key") != retry_key:
        item = {"count": 0, "since": now, "key": retry_key}
    item["count"] += 1
    item["last"] = now
    item["errors"] =["%s: %s" % (e["code"], e["message"]) for e in errors][:10]
    retries[version] = item
    summary["retry"].append(label)
    log("%s: %s - retried at the next run (%d)" % (label, "; ".join(e["message"] for e in errors), item["count"]))
    if item["count"] == RETRY_NOTIFY_AFTER and "issue" not in item:
        item["issue"] = issues.open(
            cfg.index_repo, "CI cannot check %s %s" % (name, rel["tag"]),
            "The check of `%s` %s fails for a reason of CI, not of the package, since %s (%d runs):\n\n%s\n\nAfter "
            "%d runs the version is checked again only when its assets, moderation.ltx [exceptions], "
            "game_releases.ltx or gw_merge change, or once a day. Typical fixes: add the game release to "
            "game_releases.ltx and publish its snapshot; a larger runner for a package that does not fit its disk; a "
            "fixed gw_merge." %
            (name, rel["tag"], item["since"], item["count"], "\n".join("- " + e for e in item["errors"]), RETRY_MAX),
            ["moderation", "catalog-ci"], "gw-ci retry %s %s" % (name, version),
            {"id": name, "path": ["retry", version, "issue"]})
        summary["issues"] += 1
    if item["count"] >= RETRY_MAX:
        log("::warning::%s: %d failed runs: paused until an input changes" % (label, item["count"]))


def _check_new_version(idx, provider, cfg, snapshots, st, name, version, rel, now, summary, log, gw_merge, vwork,
                       issues, exc_key="", retry_key=""):
    label = "%s@%s" % (name, version)
    rep = Report(label)
    parts = package.classify_assets([a["name"] for a in rel["assets"]], name, version, rep)
    by_name = {a["name"]: a for a in rel["assets"]}
    os.makedirs(vwork, exist_ok=True)
    paths, assets = [], []
    # The disk of the runner before the download (review F7): the zips and their unpacked files are on it at once, the
    # unpacked size is at least the size of the zips. Not enough - a problem of CI (a larger runner), not of the author
    need = 2 * sum(by_name[p]["size"] for p in parts) + UNPACK_RESERVE
    free = shutil.disk_usage(vwork).free
    if parts and need > free:
        rep.error("ci_disk_space", "the package (%d bytes in zips) needs about %d bytes of the disk of the check, it "
                                   "has %d free" % (need // 2 - UNPACK_RESERVE // 2, need, free))
        parts = []
    for part in parts:
        a = by_name[part]
        if a["size"] > package.limits.PART_MAX_BYTES:
            rep.error("asset_too_large", "%d bytes: a part is under 2 GiB" % a["size"], part)
            continue
        dest = os.path.join(vwork, part)
        provider.download_asset(name, rel, a, dest)
        if os.path.getsize(dest) != a["size"]:
            rep.error("package_unpack_failed", "downloaded %d bytes, the release says %d" %
                      (os.path.getsize(dest), a["size"]), part)
        sha = strictjson.sha256_file(dest)
        if sha in cfg.moderation.revoked_sha256:
            rep.error("asset_revoked", "this package is in revoked_sha256: %s" % cfg.moderation.revoked_sha256[sha],
                      part)
        paths.append(dest)
        assets.append({"name": part, "size": a["size"], "sha256": sha, "id": a["id"], "updated_at": a["updated_at"]})
    date = (rel["published_at"] or now)[:10]
    files_tmp = os.path.join(vwork, "files")
    result = None
    if rep.ok and paths:
        vrep, result = validate_version({"kind": "zips", "paths": paths}, expect_id=name, expect_version=version,
                                        cfg=cfg, snapshots=snapshots, guessed_tag=cfg.game_release_for(date),
                                        files_dir=files_tmp, work_dir=os.path.join(vwork, "work"), gw_merge=gw_merge,
                                        require_delta=True, subject=label)
        rep.extend(vrep)
    rep.apply_exceptions(cfg.moderation.exceptions.get(name))
    errors = rep.errors
    if errors and all(e["code"] in TRANSIENT for e in errors):
        _retry(cfg, st, name, version, rel, errors, now, summary, log, issues, retry_key)
        return
    st.get("retry", {}).pop(version, None)
    sv = {"tag": rel["tag"], "release_id": rel["id"], "checked_at": now, "pre": bool(rel["prerelease"]),
          "date": date, "notes": rel.get("body") or "", "assets": assets}
    if errors:
        sv["status"] = "invalid"
        sv["errors"] = ["%s: %s" % (e["code"], e["message"]) for e in errors][:50]
        sv["exceptions"] = exc_key
        prev = st["versions"].get(version)
        if prev is not None and prev["status"] == "invalid" and prev.get("errors") == sv["errors"] and \
                [a["sha256"] for a in prev["assets"]] == [a["sha256"] for a in assets] and "issue" in prev:
            # the same package refused for the same reasons, checked again for a new exception or game release (or
            # the first run over a state without "exceptions"): the author has the issue already, no second one
            sv["issue"] = prev["issue"]
        else:
            sv["issue"] = issues.open(name, "Release %s is not published: validation failed" % rel["tag"],
                                      _report_body(rep, "Fix the package and upload it again to the same release (or "
                                                        "make a new one)."), ["catalog-ci"],
                                      "gw-ci invalid %s %s %s" % (name, version, _hash8([assets, sv["errors"]])),
                                      {"id": name, "path": ["versions", version, "issue"]})
            summary["issues"] += 1
        st["versions"][version] = sv
        summary["invalid"].append(label)
        log("%s: invalid (%d error(s))" % (label, len(errors)))
        return

    # files/<id>/<ver>.json (+ the delta): immutable once written
    files_dir = idx.path("files", name)
    os.makedirs(files_dir, exist_ok=True)
    vf = version_file(name, version, rel["tag"], result)
    vf_path = os.path.join(files_dir, version + ".json")
    if os.path.isfile(vf_path):
        if strictjson.load_file(vf_path) != vf:
            raise RuntimeError("%s exists and differs: files/<id>/<ver>.json is immutable (11 section 4.1)" % vf_path)
    else:
        if result.get("merge"):
            shutil.copyfile(os.path.join(files_tmp, result["merge"]["file"]),
                            os.path.join(files_dir, result["merge"]["file"]))
        strictjson.write_file(vf_path, vf)
    man = result["manifest"]
    sv.update({"manifest": {k: man[k] for k in ("type", "api", "plugin_api", "depends", "conflicts", "load_after",
                                                 "patches", "caps", "save_compat", "game_version")},
               "caps_found": result["caps_found"], "scanner": result["scanner"],
               "files": "files/%s/%s.json" % (name, version), "unpacked_size": result["unpacked_size"],
               "merge_files": _merge_files(files_dir, result.get("merge")), "game": result.get("game_tag", "")})
    reasons = []
    if not any(v["status"] in LIVE for v in st["versions"].values()):
        reasons.append("first publication")
    added = sorted(set(man["caps"]) - set(st.get("accepted_caps", [])))
    if added:
        reasons.append("capabilities expanded: +" + ", ".join(added))
    if result["scanner"]:
        reasons.append("the Lua scanner fired: " + ", ".join(result["scanner"]))
    if st.get("accepted_type") and st["accepted_type"] != man["type"]:
        reasons.append("type changed: %s -> %s" % (st["accepted_type"], man["type"]))
    if reasons:
        sv["status"] = "pending"
        sv["review"] = reasons
        # review.yml finds the version by this marker; it is the marker of the issue for the outbox too
        marker = "gw-review %s %s" % (name, version)
        sv["issue"] = issues.open(
            cfg.index_repo, "Review: %s %s" % (name, rel["tag"]),
            _report_body(rep, "<!-- %s -->\n\n**Why a review:** %s.\n\nRepo: https://github.com/%s/%s, release %s. "
                              "Declared capabilities: %s; accepted before: %s.\n\nApprove: add the label `approved`; "
                              "reject: the label `rejected` (only moderators can label)." %
                         (marker, "; ".join(reasons), cfg.org or provider.org, name, rel["tag"],
                          ", ".join(man["caps"]) or "-", ", ".join(st.get("accepted_caps", [])) or "-")),
            ["review"], marker, {"id": name, "path": ["versions", version, "issue"]})
        summary["pending"].append(label)
        summary["issues"] += 1
    else:
        sv["status"] = "listed"
        st["accepted_caps"] = sorted(man["caps"])
        st["accepted_type"] = man["type"]
        summary["new"].append(label)
    st["versions"][version] = sv
    log("%s: %s" % (label, sv["status"]))


def _merge_files(files_dir, ref):
    if not ref:
        return 0
    with open(os.path.join(files_dir, ref["file"]), "rb") as f:
        d = strictjson.loads_bytes(f.read(), ref["file"])
    return len(d.get("files", {}))


# ---- review ----------------------------------------------------------------------------------------------------------

def review(idx, addon_id, version, decision, now=None, issue=None):
    """A moderator's decision on a pending version: approve -> listed (its capabilities and type accepted),
    reject -> rejected. issue: the number of the issue the label was put on - it must be the review issue the CI opened
    for this version (state/ keeps its number): a copy of the marker in another issue decides nothing. -> message"""
    st = idx.load_state(addon_id, "")
    sv = st["versions"].get(version)
    if sv is None:
        raise RuntimeError("%s %s is not in state/" % (addon_id, version))
    if issue is not None and sv.get("issue") != issue:
        raise RuntimeError("%s %s: its review issue is #%s, not #%s" % (addon_id, version, sv.get("issue"), issue))
    if sv["status"] != "pending":
        return "%s %s is %s, not pending: nothing to do" % (addon_id, version, sv["status"])
    if decision == "approve":
        sv["status"] = "listed"
        newest = max([v for v, s in st["versions"].items() if s["status"] == "listed"], key=vkey)
        if newest == version:
            st["accepted_caps"] = sorted(sv["manifest"]["caps"])
            st["accepted_type"] = sv["manifest"]["type"]
    else:
        sv["status"] = "rejected"
    sv["reason"] = "%s by a moderator at %s" % ("approved" if decision == "approve" else "rejected", now or utc_now())
    idx.save_state(st)
    return "%s %s: %s" % (addon_id, version, sv["status"])


# ---- render ----------------------------------------------------------------------------------------------------------

def _api_major(api):
    return ltx.api_major(ltx.parse_api_version(api) if api else None)


def _entry(addon_id, version, sv, cfg, prev_save_compat):
    man = sv["manifest"]
    yanked = sv["status"] == "yanked" or version in cfg.moderation.yanked.get(addon_id, set())
    breaks = False
    if man["type"] == "location":
        breaks = prev_save_compat is not None and prev_save_compat != man["save_compat"]
    first_line = (sv.get("notes") or "").strip().split("\n", 1)[0].lower()
    if "breaks-saves" in first_line or version in cfg.moderation.breaks_saves.get(addon_id, set()):
        breaks = True
    # api_value: 0.0.0 kept in a state written before it counted as not set - "" (11 section 6)
    return {"v": version, "tag": sv["tag"], "date": sv["date"], "pre": sv["pre"], "api": ltx.api_value(man["api"]),
            "plugin_api": ltx.api_value(man["plugin_api"]), "depends": man["depends"], "conflicts": man["conflicts"],
            "load_after": ", ".join(man["load_after"]), "patches": ", ".join(man["patches"]), "caps": man["caps"],
            "save_compat": man["save_compat"], "breaks_saves": breaks, "size": sv.get("unpacked_size", 0),
            "assets": [{"name": a["name"], "kind": "content", "size": a["size"], "sha256": a["sha256"]}
                       for a in sv["assets"]],
            "game": sv.get("game", ""), "merge_files": sv.get("merge_files", 0),
            "notes": sv.get("notes", ""), "yanked": yanked}


def _latest(entries):
    """11 section 4.3: the last stable version of every API major + the last pre-release of that major when it is newer
    than its stable one (review F18: a beta under API 0.1 was hidden by a stable version under 0.2, which a player on
    the engine 0.1 cannot install)"""
    live = [e for e in entries if not e["yanked"]]
    best, best_pre = {}, {}
    for e in live:
        major = _api_major(e["api"])
        group = best_pre if e["pre"] else best
        if major not in group or vkey(e["v"]) > vkey(group[major]["v"]):
            group[major] = e
    out = list(best.values())
    for major, e in best_pre.items():
        if major not in best or vkey(e["v"]) > vkey(best[major]["v"]):
            out.append(e)
    out.sort(key=lambda e: vkey(e["v"]), reverse=True)
    return [{k: v for k, v in e.items() if k not in ("notes", "yanked")} for e in out]


def render(idx, cfg, log, now=None):
    """state/ -> history/ + index.json. -> True when index.json changed"""
    now = now or utc_now()
    addons = []
    revoked = set(cfg.moderation.revoked_sha256)
    revoked_ids = []
    os.makedirs(idx.path("history"), exist_ok=True)
    for st in idx.states():
        addon_id = st["id"]
        published = sorted([(v, s) for v, s in st["versions"].items() if s["status"] in LIVE], key=lambda t: vkey(t[0]))
        if cfg.moderation.status(addon_id) == "revoked":
            # every package CI ever checked, not only the published ones (review F15): a version whose release the
            # author deleted (gone) is still installed by the players; the id itself goes to revoked_ids, for the
            # addon that has no published version left (it has no entry in "addons")
            revoked.update(a["sha256"] for s in st["versions"].values() for a in s.get("assets", []))
            revoked_ids.append(addon_id)
        if not published:
            continue
        entries, prev_sc = [], None
        for version, sv in published:
            entries.append(_entry(addon_id, version, sv, cfg, prev_sc))
            prev_sc = sv["manifest"]["save_compat"]
        history = sorted(entries, key=lambda e: vkey(e["v"]), reverse=True)
        errors = schema.validate(history, "history")
        if errors:
            raise RuntimeError("history of %s: %s" % (addon_id, "; ".join(errors[:5])))
        data = strictjson.dumps(history)
        name = "history/%s.%s.json" % (addon_id, strictjson.sha256_bytes(data)[:8])
        changed = False
        if not os.path.isfile(idx.path(*name.split("/"))):
            with open(idx.path(*name.split("/")), "wb") as f:
                f.write(data)
        if st.get("history") != name:
            if st.get("history"):
                st.setdefault("old_history", []).append({"file": st["history"], "replaced_at": now})
            st["history"] = name
            changed = True
        keep = []
        for old in st.get("old_history", []):
            age = _days_between(old["replaced_at"], now)
            if age >= HISTORY_KEEP_DAYS and old["file"] != name:
                p = idx.path(*old["file"].split("/"))
                if os.path.isfile(p):
                    os.remove(p)
                changed = True
            else:
                keep.append(old)
        if keep != st.get("old_history", []):
            st["old_history"] = keep
            if not keep:
                st.pop("old_history")
        if changed:
            idx.save_state(st)
        card = st.get("catalog")
        latest = _latest(entries)
        if card is None or not latest:
            log("%s: not in index.json (%s)" % (addon_id, "no valid card yet" if card is None else "no version"))
            continue
        newest = max(published, key=lambda t: vkey(t[0]))[1]
        item = {"id": addon_id, "repo": st["repo"], "type": newest["manifest"]["type"],
                "status": cfg.moderation.status(addon_id), "name": card["name"], "summary": card["summary"],
                "tags": card["tags"], "authors": card["authors"], "maintainers": card["maintainers"],
                "license": card["license"], "links": card["links"], "rev": card["rev"], "icon": card["icon"],
                "screenshots": card["screenshots"], "description": card["description"], "history": name,
                "latest": latest}
        if card.get("permission"):
            item["permission"] = card["permission"]
        addons.append(item)
    addons.sort(key=lambda a: a["id"])
    index = {"schema": 1, "generated_at": now, "source": cfg.source, "templates": cfg.templates,
             "mirrors": cfg.mirrors, "revoked_sha256": sorted(revoked), "addons": addons}
    if revoked_ids:  # only when there is one: an index without revoked addons stays as it was
        index["revoked_ids"] = sorted(revoked_ids)
    errors = schema.validate(index, "index")
    if errors:
        raise RuntimeError("index.json does not match its schema: %s" % "; ".join(errors[:5]))
    path = idx.path("index.json")
    if os.path.isfile(path):
        old = strictjson.load_file(path)
        old["generated_at"] = now
        if old == index:
            return False
    strictjson.write_file(path, index, compact=True)
    return True


def _days_between(a, b):
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (datetime.datetime.strptime(b, fmt) - datetime.datetime.strptime(a, fmt)).days


def _minutes_between(a, b):
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (datetime.datetime.strptime(b, fmt) - datetime.datetime.strptime(a, fmt)).total_seconds() / 60


# ---- stats -----------------------------------------------------------------------------------------------------------

def stats(idx, provider, log, now=None):
    """stats.json: [downloads of all package assets of all releases, stars] of the addons in index.json"""
    now = now or utc_now()
    index_path = idx.path("index.json")
    ids = {a["id"] for a in strictjson.load_file(index_path)["addons"]} if os.path.isfile(index_path) else set()
    repos = {r["name"]: r for r in provider.list_repos()}
    out = {}
    for addon_id in sorted(ids):
        r = repos.get(addon_id)
        if r is None:
            continue
        downloads = 0
        for rel in provider.list_releases(addon_id):
            for a in rel["assets"]:
                if a["name"].startswith(addon_id + "-") and a["name"].endswith(".zip"):
                    downloads += int(a.get("download_count") or 0)
        out[addon_id] = [downloads, int(r.get("stars") or 0)]
    data = {"schema": 1, "generated_at": now, "stats": out}
    errors = schema.validate(data, "stats")
    if errors:
        raise RuntimeError("stats.json: %s" % "; ".join(errors[:5]))
    path = idx.path("stats.json")
    if os.path.isfile(path):
        old = strictjson.load_file(path)
        if old.get("stats") == out:
            return False
    strictjson.write_file(path, data, compact=True)
    return True


# ---- recompute deltas for a new gw_merge (15 section 7.2: "a newer algo") ---------------------------------------------

def recompute_deltas(idx, provider, snapshots, log, gw_merge=None, only=None, work_root=None):
    """For every listed / pending version with a delta: the delta of this gw_merge, if its algo is new, into
    files/<id>/<ver>.merge.<algo tag>.json. <ver>.json is not touched. -> list of new files"""
    out = []
    work_root = work_root or tempfile.mkdtemp(prefix="gwcat_recompute_")
    try:
        for st in idx.states():
            addon_id = st["id"]
            if only and addon_id not in only:
                continue
            files_dir = idx.path("files", addon_id)
            for version, sv in sorted(st["versions"].items(), key=lambda t: vkey(t[0])):
                if sv["status"] not in ("listed", "pending", "yanked") or not sv.get("merge_files"):
                    continue
                vf = strictjson.load_file(os.path.join(files_dir, version + ".json"))
                algos = {vf["merge"]["algo"]} if vf.get("merge") else set()
                for name in os.listdir(files_dir):
                    if name.startswith(version + ".merge.") and name != delta_mod.merge_file_name(version):
                        algos.add(strictjson.load_file(os.path.join(files_dir, name))["algo"])
                rel = next((r for r in provider.list_releases(addon_id) if r["tag"] == sv["tag"]), None)
                if rel is None:
                    continue
                snap = snapshots.get(sv.get("game"))
                if not snap:
                    log("%s@%s: no snapshot %s, skipped" % (addon_id, version, sv.get("game")))
                    continue
                vwork = os.path.join(work_root, addon_id, version)
                os.makedirs(vwork, exist_ok=True)
                name = None
                try:
                    # a yanked version may have lost or changed its asset (that is why it is yanked): skipped, the
                    # other versions are recomputed
                    paths = []
                    for a in sv["assets"]:
                        asset = next((x for x in rel["assets"] if x["name"] == a["name"]), None)
                        if asset is None:
                            break
                        paths.append(provider.download_asset(addon_id, rel, asset, os.path.join(vwork, a["name"])))
                        if strictjson.sha256_file(paths[-1]) != a["sha256"]:
                            break
                    else:
                        root = os.path.join(vwork, "pkg")
                        package.extract(paths, root)
                        rep = Report("recompute")
                        name = delta_mod.recompute(root, files_dir, version, snap, algos, rep,
                                                   latest=snapshots.get(snapshots.latest), game=sv.get("game"),
                                                   gw_merge=gw_merge)
                        paths = None
                finally:
                    shutil.rmtree(vwork, ignore_errors=True)
                if paths is not None:
                    log("%s@%s: the package asset is missing or differs from the checked one, skipped" %
                        (addon_id, version))
                    continue
                if name:
                    sv.setdefault("recomputed", []).append(name)
                    idx.save_state(st)
                    out.append("files/%s/%s" % (addon_id, name))
                    log("%s@%s: %s" % (addon_id, version, name))
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
    return out
