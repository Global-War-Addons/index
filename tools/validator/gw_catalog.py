#!/usr/bin/env python3
"""gw_catalog - the validator and the index tools of the GlobalWar addon catalog (stdlib only, Python 3.10+;
zstd zip parts need 3.14+).

Authors and the CI of an addon repo:
    validate <addon folder | part1.zip ...> [--id ID --version V] [--assets-dir DIR]   one version of an addon
    validate-repo <repo folder>                                                          addon.ltx + catalog/ of a repo
    check <addon folder | part1.zip ...> [--repo REPO]   validate + validate-repo + the two addon.ltx, with hints
                                    (exit code 1 also for template leftovers of the card: 0 only when ready)
    pack <addon folder> --out DIR                                                        validate, then <id>-<ver>.zip
    remap-textures <addon folder> [--snapshots DIR --game TAG | --base-snapshot F] [--all]
                                    textures of the base names -> [private] + @<id>/ in the models (09 4.1 B)
The same commands for authors: gw_addon_tool.py (check, validate, validate-repo, pack, remap-textures; --index found
by itself; also as one file gw_addon_tool.pyz, build_pyz.py).

The CI of the index:
    build-index --index DIR (--github-org ORG | --local-org DIR)   new releases -> files/, state/, history/, index.json
                [--issues-out F]                                   issues into an outbox instead of opening them
    publish-issues --index DIR --outbox F --bot-login L            the outbox -> issues, their numbers -> state/
    export-changes --index DIR --out DIR                           the generated files changed -> a hand-over
    apply-changes --index DIR --from DIR                           a hand-over checked and copied into the index
    render --index DIR                                             state/ -> history/ + index.json (offline)
    stats --index DIR (--github-org ORG | --local-org DIR)         stats.json
    review --index DIR --id ID --version V (--approve | --reject)  a moderator's decision on a pending version
    recompute-deltas --index DIR (...)                             deltas of a new gw_merge -> <ver>.merge.<algo>.json
    check-index DIR [--base REF]                                   the index repo is consistent (pull requests)
    new-addon-check --index DIR --body-file F [--existing a,b]     the "new addon" issue form -> JSON

Exit code: 0 - fine, 1 - errors found (the report says which; check: or template leftovers), 2 - the run itself
failed. gw_merge (--gw-merge, $GW_MERGE, PATH, the xray-16 build folders; the game ships it as <game>\\bin\\gw_merge.exe)
decides the merged texts and makes the delta: without it a local run skips those checks with a warning
(merge_plan_unavailable), the CI of the index (--require-delta, build-index) keeps the version waiting (the error
merge_plan_unavailable is TRANSIENT: retried), recompute-deltas stops.
See catalog/README.md.
"""

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gwcat import (catalog_meta, content, index_build, indexcheck, ltx, manifest as manifest_mod,  # noqa: E402
                   newaddon, package, remap, snapshot_tools, strictjson)
from gwcat.indexcfg import ConfigError, IndexConfig  # noqa: E402
from gwcat.report import Report  # noqa: E402
from gwcat.validate import SnapshotStore, validate_version, version_file  # noqa: E402


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def utf8_console():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except ValueError:
                pass


def emit(report, fmt, out=None, extra=None):
    if fmt == "json":
        data = report.to_json()
        if extra:
            data.update(extra)
        text = json.dumps(data, ensure_ascii=False, indent=2)
    elif fmt == "md":
        text = report.to_markdown()
    else:
        text = report.to_text()
    if out:
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
    else:
        print(text)


def load_cfg(args):
    if getattr(args, "index", None):
        return IndexConfig(args.index)
    return None


def snapshot_store(args, cfg):
    folder = getattr(args, "snapshots", None)
    url = cfg.snapshot_url if cfg else None
    if not folder and not url:
        return None
    latest = getattr(args, "latest", None) or (cfg.latest_game_release() if cfg else None)
    store = SnapshotStore(folder or os.path.join(tempfile.gettempdir(), "gw_snapshots"), url, latest,
                          [t for t, _ in cfg.game_releases] if cfg else (),
                          cfg.snapshot_digests if cfg else None)
    if not latest and folder and os.path.isdir(folder):
        # the folders that hold a snapshot (not an empty or a half-written one)
        tags = sorted((t for t in os.listdir(folder) if store._local(t)),
                      key=ltx.game_tag_sort_key)  # 0.5.5 < 0.5.5-patch1 < 0.5.6-Beta < 0.5.6, not as strings
        store.latest = tags[-1] if tags else None
    return store


def provider_of(args, cfg):
    from gwcat import providers
    if getattr(args, "local_org", None):
        return providers.LocalProvider(args.local_org, (cfg.org if cfg and cfg.org else "Local-Addons"))
    org = args.github_org or (cfg.org if cfg else None)
    if not org:
        raise SystemExit("--github-org or source.ltx [github] org is needed (or --local-org DIR)")
    # taken out of the environment: the tools this process runs on the packages (gw_merge, gw_snapshot.py) do not
    # inherit the token
    token = os.environ.pop(args.token_env or "GITHUB_TOKEN", None)
    if not token:
        raise SystemExit("no token in $%s" % (args.token_env or "GITHUB_TOKEN"))
    return providers.GitHubProvider(org, token, cfg.index_repo if cfg else "index",
                                    http_cache=getattr(args, "http_cache", None))


# ---- commands --------------------------------------------------------------------------------------------------------

def cmd_validate(args):
    cfg = load_cfg(args)
    sources = args.inputs
    if args.assets_dir:
        if not (args.id and args.version):
            raise SystemExit("--assets-dir needs --id and --version")
        pre = Report("assets")
        names = package.classify_assets(sorted(os.listdir(args.assets_dir)), args.id, args.version, pre)
        if not pre.ok:
            emit(pre, args.format, args.out)
            return 1
        sources = [os.path.join(args.assets_dir, n) for n in names]
    if not sources:
        raise SystemExit("nothing to validate: a folder, zip parts or --assets-dir")
    if len(sources) == 1 and os.path.isdir(sources[0]):
        source = {"kind": "folder", "path": os.path.abspath(sources[0])}
    else:
        source = {"kind": "zips", "paths": [os.path.abspath(p) for p in sources]}
    snaps = snapshot_store(args, cfg)
    files_out = os.path.abspath(args.files_out) if args.files_out else None
    if files_out:
        os.makedirs(files_out, exist_ok=True)
    report, result = validate_version(source, expect_id=args.id, expect_version=args.version, cfg=cfg,
                                      snapshots=snaps, guessed_tag=args.game, files_dir=files_out,
                                      gw_merge=args.gw_merge, require_delta=args.require_delta,
                                      snapshot_check=not args.no_snapshot_check)
    if cfg and result and result.get("manifest"):
        report.apply_exceptions(cfg.moderation.exceptions.get(result["manifest"]["id"]))
    if files_out and result and report.ok:
        m = result["manifest"]
        strictjson.write_file(os.path.join(files_out, m["version"] + ".json"),
                              version_file(m["id"], m["version"], "v" + m["version"], result))
    emit(report, args.format, args.out)
    return 0 if report.ok else 1


def read_tree(folder):
    tree = {}
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, folder).replace(os.sep, "/")
            if rel == "addon.ltx" or rel.startswith("catalog/"):
                with open(full, "rb") as f:
                    tree[rel] = f.read()
    return tree


def has_placeholder_values(data):
    """{{...}} of the template left in a value of addon.ltx. Comments (";", and "//" which the engine also cuts
    in addon.ltx) are not looked at: the template explains its placeholders in a comment."""
    for line in data.decode("utf-8", "replace").splitlines():
        for mark in (";", "//"):
            cut = line.find(mark)
            if cut >= 0:
                line = line[:cut]
        if "{{" in line:
            return True
    return False


def check_repo(folder, expect_id, cfg):
    """addon.ltx + catalog/ of the repo of an addon (validate-repo, check) -> (Report, card or None, tree)"""
    tree = read_tree(folder)
    report = Report("repo " + folder)
    if "addon.ltx" not in tree:
        report.error("repo_no_manifest", "the repo has no addon.ltx (11 section 3.1)", "addon.ltx")
    else:
        m = manifest_mod.check_manifest(tree["addon.ltx"], report, "addon.ltx", expect_id=expect_id,
                                        reserved=cfg.reserved if cfg else None)
        if m and m.get("id") and has_placeholder_values(tree["addon.ltx"]):
            report.error("repo_placeholder", "addon.ltx still has {{...}} placeholders of the template", "addon.ltx")
    card = catalog_meta.check_catalog(tree, report, cfg.tags if cfg else None)
    keys = catalog_meta.placeholder_keys(tree["catalog/catalog.ltx"]) if "catalog/catalog.ltx" in tree else []
    if keys:
        report.error("repo_placeholder", "catalog.ltx still has {{...}} placeholders of the template: %s" %
                     ", ".join(keys), "catalog/catalog.ltx")
    if cfg is None:
        report.note("no_index_config", "--index not given: tags and reserved ids are not checked")
    return report, card, tree


def cmd_validate_repo(args):
    report, card, _ = check_repo(os.path.abspath(args.repo), args.id, load_cfg(args))
    emit(report, args.format, args.out, {"card": card} if args.format == "json" else None)
    return 0 if report.ok else 1


def cmd_check(args):
    """validate + validate-repo + the addon.ltx of the repo against the package, one summary with hints"""
    from gwcat import authorcheck
    from gwcat.validate import _manifest_bytes
    cfg = load_cfg(args)
    inputs = args.inputs
    if len(inputs) == 1 and os.path.isdir(inputs[0]):
        source = {"kind": "folder", "path": os.path.abspath(inputs[0])}
    else:
        source = {"kind": "zips", "paths": [os.path.abspath(p) for p in inputs]}
    report, result = validate_version(source, expect_id=args.id, cfg=cfg, snapshots=snapshot_store(args, cfg),
                                      guessed_tag=args.game, gw_merge=args.gw_merge,
                                      snapshot_check=not args.no_snapshot_check)
    m = result.get("manifest") if result else None
    if cfg and m:
        report.apply_exceptions(cfg.moderation.exceptions.get(m["id"]))
    pkg_manifest = _manifest_bytes(source, Report("addon.ltx"))
    if not isinstance(pkg_manifest, bytes):
        pkg_manifest = None
    if pkg_manifest is not None and has_placeholder_values(pkg_manifest):
        report.error("package_placeholder", "the addon.ltx of the package still has {{...}} placeholders of the "
                                            "template", "addon.ltx")
    # the repo: --repo, or the addon folder itself when it is a clone of the repo (catalog/ next to addon.ltx)
    repo = args.repo
    if not repo and source["kind"] == "folder" and \
            os.path.isfile(os.path.join(source["path"], "catalog", "catalog.ltx")):
        repo = source["path"]
    not_checked = []
    repo_report = None
    if repo:
        repo = os.path.abspath(repo)
        repo_report, _, tree = check_repo(repo, args.id or (m["id"] if m else None), cfg)
        if cfg and (args.id or m):
            repo_report.apply_exceptions(cfg.moderation.exceptions.get(args.id or m["id"]))
        if "addon.ltx" in tree and pkg_manifest is not None:
            authorcheck.compare_manifests(tree["addon.ltx"], pkg_manifest, repo_report)
    else:
        not_checked.append("the repo of the addon (addon.ltx + catalog/): give --repo <the clone of the repo>")
    if "merge_plan_unavailable" in report.codes():
        not_checked.append("the text files of the base the addon replaces (which of them the game merges), "
                           "game_version and the delta of the texts: no gw_merge (<game>\\bin\\gw_merge.exe; "
                           "--gw-merge <path> or $GW_MERGE)")
    if "no_snapshot" in report.codes():
        not_checked.append("the files of the base the addon replaces, the model references to the base and the delta "
                           "of the texts: no snapshot of the base (--snapshots <folder> --latest <tag>)")
    if cfg is None:
        not_checked.append("the tags and the reserved ids: no index config (--index <the index repo>)")
    summary = authorcheck.summarize(report, repo_report, m, source.get("path") or ", ".join(source.get("paths", [])),
                                    repo or "", not_checked)
    text = json.dumps(summary, ensure_ascii=False, indent=2) if args.format == "json" else \
        authorcheck.render_text(summary)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
    else:
        print(text)
    # ready: no errors and no template leftovers (a leftover is often only a warning of the validator, but the card
    # would show the text of the template - decision 64, low items)
    return 0 if summary["ready"] else 1


def cmd_pack(args):
    folder = os.path.abspath(args.folder)
    report, result = validate_version({"kind": "folder", "path": folder}, cfg=load_cfg(args), snapshot_check=False)
    if not report.ok and not args.force:
        emit(report, args.format)
        log("not packed: fix the errors (or --force)")
        return 1
    files = package.folder_files(folder, Report("pack"))
    files = {k: v for k, v in files.items() if not package.is_launcher_file(k)}
    m = result["manifest"] if result else None
    if not m or not m.get("version"):
        raise SystemExit("addon.ltx has no usable id / version")
    part = int(args.part_size) if args.part_size else package.limits.PART_MAX_BYTES
    out = package.make_zips(folder, files, os.path.abspath(args.out), m["id"], m["version"], part)
    emit(report, args.format)
    for p in out:
        print(p)
    return 0


def cmd_remap_textures(args):
    cfg = load_cfg(args)
    folder = os.path.abspath(args.folder)
    base_files = None
    if args.base_snapshot:
        base_files = content.load_snapshot_files(args.base_snapshot)
    elif not args.all:
        snaps = snapshot_store(args, cfg)
        tag = args.game
        if not tag:
            p = os.path.join(folder, "addon.ltx")
            if os.path.isfile(p):
                with open(p, "rb") as f:
                    m = manifest_mod.check_manifest(f.read(), Report("addon.ltx"), "addon.ltx")
                tag = m.get("game_version") if m else None
        tag = tag or (snaps.latest if snaps else None)
        path = snaps.get(tag) if snaps and tag else None
        if path:
            base_files = content.load_snapshot_files(path)
            log("the base: snapshot %s" % tag)
    report = remap.remap_textures(folder, base_files=base_files, all_textures=args.all, only=args.only,
                                  make_private=not args.no_private, rewrite_models=not args.no_models,
                                  dry_run=args.dry_run)
    emit(report, args.format, args.out)
    return 0 if report.ok else 1


def cmd_build_index(args):
    cfg = IndexConfig(args.index)
    idx = index_build.Index(args.index)
    provider = provider_of(args, cfg)
    only = set(args.only.split(",")) if args.only else None
    outbox = index_build.IssueOutbox() if args.issues_out else None
    try:
        summary = index_build.ingest(idx, provider, cfg, snapshot_store(args, cfg), log, gw_merge=args.gw_merge,
                                     only=only, issues=outbox)
    finally:
        if hasattr(provider, "save_http_cache"):
            provider.save_http_cache()
    if hasattr(provider, "http_stats"):
        summary["http"] = dict(provider.http_stats)  # requests to the API / of them answered 304 (no rate limit)
    if outbox is not None:
        outbox.write(args.issues_out)
    summary["index_changed"] = index_build.render(idx, cfg, log)
    check = indexcheck.check(args.index)
    summary["check_errors"] = [i["message"] for i in check.errors]
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.summary:
        with open(args.summary, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)
    return 1 if check.errors else 0


def cmd_render(args):
    cfg = IndexConfig(args.index)
    changed = index_build.render(index_build.Index(args.index), cfg, log)
    print("index.json %s" % ("changed" if changed else "unchanged"))
    return 0


def cmd_stats(args):
    cfg = IndexConfig(args.index)
    changed = index_build.stats(index_build.Index(args.index), provider_of(args, cfg), log)
    print("stats.json %s" % ("changed" if changed else "unchanged"))
    return 0


def cmd_review(args):
    cfg = IndexConfig(args.index)
    idx = index_build.Index(args.index)
    try:
        print(index_build.review(idx, args.id, args.version, "approve" if args.approve else "reject",
                                 issue=args.issue))
    except RuntimeError as e:
        log("::error::%s" % e)
        return 1
    index_build.render(idx, cfg, log)
    return 0


def cmd_recompute(args):
    cfg = IndexConfig(args.index)
    exe = find_gw_merge(args.gw_merge)
    if not exe:
        # every delta is made by gw_merge: nothing to recompute without it (the workflow downloads it)
        log("::error::recompute-deltas needs gw_merge: %s" % snapshot_tools.gw_merge_missing(args.gw_merge))
        return 2
    why = gw_merge_too_old(exe)
    if why:
        # an older gw_merge would write deltas of an old revision under the name of the new algo
        log("::error::recompute-deltas needs a newer gw_merge: %s is too old (%s); %s" %
            (exe, why, snapshot_tools.GW_MERGE_HOW_TO))
        return 2
    only = set(args.only.split(",")) if args.only else None
    out = index_build.recompute_deltas(index_build.Index(args.index), provider_of(args, cfg),
                                       snapshot_store(args, cfg), log, args.gw_merge, only)
    print("\n".join(out) or "no new deltas")
    return 0


def cmd_publish_issues(args):
    cfg = IndexConfig(args.index)
    opened = index_build.publish_issues(index_build.Index(args.index), provider_of(args, cfg), args.outbox, cfg,
                                        args.bot_login, log)
    print("%d issue(s) opened" % opened)
    return 0


def cmd_export_changes(args):
    from gwcat import cichanges
    changed, deleted = cichanges.export(os.path.abspath(args.index), os.path.abspath(args.out))
    print("%d changed, %d deleted" % (len(changed), len(deleted)))
    return 0


def cmd_apply_changes(args):
    from gwcat import cichanges
    try:
        changed, deleted = cichanges.apply(os.path.abspath(getattr(args, "from")), os.path.abspath(args.index),
                                           fetch=None if args.no_verify_assets else cichanges.download_digest,
                                           log=log)
    except cichanges.ChangesError as e:
        log("::error::the hand-over is refused: %s" % e)
        return 1
    print("%d changed, %d deleted" % (len(changed), len(deleted)))
    return 0


def cmd_check_index(args):
    report = indexcheck.check(os.path.abspath(args.dir), args.base)
    emit(report, args.format, args.out)
    return 0 if report.ok else 1


def cmd_new_addon_check(args):
    cfg = IndexConfig(args.index)
    with open(args.body_file, encoding="utf-8") as f:
        fields = newaddon.parse_body(f.read())
    existing = set(x for x in (args.existing or "").split(",") if x)
    problems = newaddon.check_request(fields, cfg, existing)
    out = {"ok": not problems, "fields": fields, "problems": problems,
           "comment": newaddon.comment_for(problems, fields)}
    if not problems and args.template:
        tpl = {}
        for rel in ("addon.ltx", "catalog/catalog.ltx"):
            with open(os.path.join(args.template, *rel.split("/")), encoding="utf-8") as f:
                tpl[rel] = f.read()
        out["files"] = newaddon.initial_files(fields, tpl)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not problems else 1


def find_gw_merge(explicit=None):
    """gw_merge as the tools find it (tools/addons_snapshot gwmerge.find_gw_merge), None - not found"""
    try:
        return snapshot_tools.import_module("gwmerge").find_gw_merge(explicit)
    except (RuntimeError, ImportError):
        return None


def gw_merge_too_old(exe):
    """None - exe is new enough for the tools (gwmerge.too_old: gw_merge version with the revision gwmerge.MIN_REVISION
    or newer, gw_merge plan --all-files); else why not"""
    try:
        gwmerge = snapshot_tools.import_module("gwmerge")
        common = snapshot_tools.import_module("common")
    except (RuntimeError, ImportError) as e:
        return str(e)
    try:
        return gwmerge.too_old(exe)
    except (common.ToolError, OSError) as e:
        return str(e)


def find_index():
    """The index repo next to the validator: tools/validator of the index repo, or catalog/validator + catalog/index
    of the GW workspace. None - not found"""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, "..", ".."), os.path.join(here, "..", "index")):
        if (os.path.isfile(os.path.join(candidate, "source.ltx")) and
                os.path.isfile(os.path.join(candidate, "reserved_ids.ltx"))):
            return os.path.normpath(candidate)
    return None


def main(argv=None, prog="gw_catalog", only=None, description=None, auto_index=False):
    """only: the commands this CLI has (gw_addon_tool: the ones of the authors); auto_index: --index defaults to
    find_index()"""
    utf8_console()
    ap = argparse.ArgumentParser(prog=prog, description=description or __doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot-tools", help="tools/addons_snapshot (default: $GW_SNAPSHOT_TOOLS, ../addons_snapshot, "
                                             "../../GlobalWar/utils/addons_snapshot)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, **kw):
        if only is not None and name not in only:
            return argparse.ArgumentParser(add_help=False)  # not a command of this CLI: a parser nobody reaches
        return sub.add_parser(name, **kw)

    def fmt(p):
        p.add_argument("--format", choices=("text", "json", "md"), default="text")
        p.add_argument("--out", help="write the report here")

    def prov(p):
        p.add_argument("--github-org", help="default: source.ltx [github] org")
        p.add_argument("--token-env", default="GITHUB_TOKEN", help="env var with the token (default GITHUB_TOKEN)")
        p.add_argument("--local-org", help="a local folder that mimics the organization (see gwcat/providers.py)")

    def snaps(p):
        p.add_argument("--snapshots", help="folder <tag>/base_snapshot.json.gz + base_texts.tar.gz")
        p.add_argument("--latest", help="the latest game tag (default: game_releases.ltx, else the last folder)")
        p.add_argument("--gw-merge", help="gw_merge (default: $GW_MERGE, PATH, xray-16 build folders; the game has it: "
                                          "<game>\\bin\\gw_merge.exe)")

    v = add("validate", help="one version of an addon (folder or zip parts)")
    v.add_argument("inputs", nargs="*", help="an addon folder, or the zip parts in order")
    v.add_argument("--assets-dir", help="the downloaded assets of a release (needs --id and --version)")
    v.add_argument("--id", help="expected [addon] id (the repo name)")
    v.add_argument("--version", help="expected [addon] version (the tag without v)")
    v.add_argument("--index", help="the index repo: reserved ids, tags, moderation exceptions, snapshot_url")
    snaps(v)
    v.add_argument("--game", help="the game tag of the delta when game_version is not set (guessed)")
    v.add_argument("--files-out", help="write <ver>.json and <ver>.merge.json here")
    v.add_argument("--require-delta", action="store_true", help="a missing delta is an error (CI of the index)")
    v.add_argument("--no-snapshot-check", action="store_true", help="skip gw_snapshot.py check")
    fmt(v)

    r = add("validate-repo", help="addon.ltx and catalog/ of an addon repo")
    r.add_argument("repo")
    r.add_argument("--id", help="expected id (the repo name)")
    r.add_argument("--index")
    fmt(r)

    ck = add("check", help="the whole local check of an author: the package (validate), the repo (validate-repo), "
                           "the addon.ltx of the repo against the package; a summary with hints")
    ck.add_argument("inputs", nargs="+", help="the addon folder, or the zip parts in order")
    ck.add_argument("--repo", help="the clone of the repo of the addon (addon.ltx + catalog/); default: the addon "
                                   "folder itself when it has catalog/catalog.ltx")
    ck.add_argument("--id", help="expected id (the repo name; default: [addon] id of the package)")
    ck.add_argument("--index", help="the index repo: reserved ids, tags, exceptions, snapshot_url")
    snaps(ck)
    ck.add_argument("--game", help="the game tag of the delta when game_version is not set (guessed)")
    ck.add_argument("--no-snapshot-check", action="store_true", help="skip gw_snapshot.py check")
    ck.add_argument("--format", choices=("text", "json"), default="text")
    ck.add_argument("--out", help="write the summary here")

    p = add("pack", help="validate a folder and pack <id>-<ver>.zip (or parts)")
    p.add_argument("folder")
    p.add_argument("--out", required=True)
    p.add_argument("--part-size", help="bytes per part (default: under 2 GiB)")
    p.add_argument("--index")
    p.add_argument("--force", action="store_true", help="pack even with errors")
    p.add_argument("--format", choices=("text", "json", "md"), default="text")

    rt = add("remap-textures", help="the textures with base names -> [private] + @<id>\\ names in the models")
    rt.add_argument("folder", help="the addon folder (changed in place: commit or copy it first)")
    rt.add_argument("--index", help="the index repo: snapshot_url and game_releases.ltx")
    snaps(rt)
    rt.add_argument("--game", help="the game tag of the snapshot (default: [addon] game_version, else --latest)")
    rt.add_argument("--base-snapshot", help="a base_snapshot.json.gz instead of --snapshots / --index")
    rt.add_argument("--all", action="store_true", help="every texture of the addon, not only the base names")
    rt.add_argument("--only", action="append", help="a mask of the textures (as [private]: textures\\wpn\\*), "
                                                     "repeatable")
    rt.add_argument("--no-private", action="store_true", help="do not touch [private], only rename in the models")
    rt.add_argument("--no-models", action="store_true", help="do not rewrite the models, only [private]")
    rt.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    fmt(rt)

    b = add("build-index", help="CI: ingest new releases and render index.json")
    b.add_argument("--index", required=True)
    prov(b)
    snaps(b)
    b.add_argument("--only", help="comma list of addon ids")
    b.add_argument("--http-cache", help="a JSON file of the answers of the GitHub API with their ETags: read with "
                                        "If-None-Match, written back (an unchanged organization costs no rate limit)")
    b.add_argument("--summary", help="write the summary JSON here")
    b.add_argument("--issues-out", help="write the issues into this outbox (publish-issues opens them) instead of "
                                        "opening them: the job that reads the packages has no token that writes")

    pi = add("publish-issues", help="CI: open the issues of an outbox of build-index, numbers -> state/")
    pi.add_argument("--index", required=True)
    prov(pi)
    pi.add_argument("--outbox", required=True)
    pi.add_argument("--bot-login", required=True, help="the login of the App of the catalog (<slug>[bot]): only its "
                                                       "open issues are taken for a marker")

    ec = add("export-changes", help="CI: the changed generated files of the index -> a hand-over folder")
    ec.add_argument("--index", required=True)
    ec.add_argument("--out", required=True)

    ac = add("apply-changes", help="CI: check a hand-over of export-changes and copy it into the index")
    ac.add_argument("--index", required=True)
    ac.add_argument("--from", required=True)
    ac.add_argument("--no-verify-assets", action="store_true",
                    help="do not download the new package assets the hand-over names to check their sha256 (offline "
                         "runs only: the CI always checks them)")

    rd = add("render", help="state/ -> history/ + index.json")
    rd.add_argument("--index", required=True)

    s = add("stats", help="stats.json")
    s.add_argument("--index", required=True)
    prov(s)

    rv = add("review", help="approve or reject a pending version")
    rv.add_argument("--index", required=True)
    rv.add_argument("--id", required=True)
    rv.add_argument("--version", required=True)
    rv.add_argument("--issue", type=int, help="the issue the label was put on: must be the review issue of the version")
    g = rv.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true")
    g.add_argument("--reject", action="store_true")

    rc = add("recompute-deltas", help="deltas of a new gw_merge as <ver>.merge.<algo>.json")
    rc.add_argument("--index", required=True)
    prov(rc)
    snaps(rc)
    rc.add_argument("--only")

    c = add("check-index", help="consistency of an index repo")
    c.add_argument("dir")
    c.add_argument("--base", help="git ref of the base of a pull request: immutability and generated files")
    fmt(c)

    n = add("new-addon-check", help="the new addon issue form")
    n.add_argument("--index", required=True)
    n.add_argument("--body-file", required=True)
    n.add_argument("--existing", help="comma list of the repos of the organization")
    n.add_argument("--template", help="the template repo folder: print the filled addon.ltx and catalog.ltx")

    args = ap.parse_args(argv)
    if auto_index and hasattr(args, "index") and not args.index:
        args.index = find_index()
        if args.index:
            log("--index %s (found next to the validator)" % args.index)
    if args.snapshot_tools:
        snapshot_tools.set_explicit(args.snapshot_tools)
    commands = {"validate": cmd_validate, "validate-repo": cmd_validate_repo, "check": cmd_check, "pack": cmd_pack,
                "build-index": cmd_build_index, "render": cmd_render, "stats": cmd_stats, "review": cmd_review,
                "recompute-deltas": cmd_recompute, "check-index": cmd_check_index,
                "publish-issues": cmd_publish_issues, "export-changes": cmd_export_changes,
                "apply-changes": cmd_apply_changes,
                "new-addon-check": cmd_new_addon_check, "remap-textures": cmd_remap_textures}
    try:
        return commands[args.cmd](args)
    except ConfigError as e:
        log("config error: %s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
