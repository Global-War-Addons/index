#!/usr/bin/env python3
"""Base snapshot of GlobalWar for the addon catalog (plans/addons_api_support/12-addon-breakage-without-api.md 4.2).

    python gw_snapshot.py build <gamedata> --tag <release tag> --out <folder>
    python gw_snapshot.py diff <old snapshot> <new snapshot> [--out changes.md]
    python gw_snapshot.py check <snapshot> <addon folder> [--old-snapshot <snapshot>] [--format md|json]
    python gw_snapshot.py delta <addon folder> --snapshot <snapshot of the author's tag> [--latest <snapshot>]
                                [--out FILE|- | --catalog <index>] [--force]
    python gw_snapshot.py rebase <delta.json | files/<id>/<ver>.json> --snapshot <new snapshot> [--addon <addon folder>]
    python gw_snapshot.py pairs <addon folder> ... --snapshot <latest snapshot> [--out-dir D] [--catalog <index>]

delta / rebase / pairs - S9, the deltas of the catalog (15-merge-implementation.md 7.2), through the tool gw_merge,
required: --gw-merge PATH or $GW_MERGE (else PATH, the build folders of xray-16); the game has it as
<game>\\bin\\gw_merge.exe. There is no Python fallback of its decisions (decision 63).
Each call of gw_merge is limited by --gw-merge-timeout SEC or $GW_MERGE_TIMEOUT (default 300 s, 0 - no limit).
Errors (gw_merge missing, too old, failed or timed out, a broken JSON, a file not overwritten without --force):
"error: ...", exit 2.

See README.md next to this file.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import utf8_console  # noqa: E402


def main(argv=None):
    utf8_console()
    ap = argparse.ArgumentParser(prog="gw_snapshot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="snapshot of a gamedata folder")
    b.add_argument("gamedata", help="gamedata folder of the release (unpacked)")
    b.add_argument("--tag", required=True, help="release tag of the game, e.g. v1.4.0, 0.5.5-patch4 (gametag.py)")
    b.add_argument("--out", required=True, help="output folder: base_snapshot.json.gz, base_texts.tar.gz")
    b.add_argument("--texts", choices=("tar", "inline", "none"), default="tar",
                   help="where the normalized texts go (default: tar = base_texts.tar.gz next to the json)")
    b.add_argument("--exclude", action="append", metavar="GLOB",
                   help="extra exclude pattern on the virtual path (fnmatch, '*' crosses folders); repeatable")
    b.add_argument("--no-default-excludes", action="store_true", help="do not skip *.pl, helpers\\, tools\\")
    b.add_argument("--no-hash", action="store_true", help="skip sha256 of files (fast local runs)")
    b.add_argument("--hash-cache", metavar="FILE", help="json cache {vpath: [size, mtime_ns, sha256]} for reruns")
    b.add_argument("--workers", type=int, default=min(16, (os.cpu_count() or 4)), help="hash threads")
    b.add_argument("--events-meta", metavar="FILE", help="events_meta.json (default: searched, see README)")
    b.add_argument("--engine-src", metavar="DIR", help="xray-16/src/xrGame: built-in events and their schemas")
    b.add_argument("--no-auto", action="store_true",
                   help="do not look for ../../xray-16 next to gamedata (events_meta.json, engine sources)")
    b.add_argument("--console-dump", metavar="LOG", help="log with the output of the console command 'help'")
    b.add_argument("--bindings-dump", metavar="FILE", help="ScriptBindings_N.txt from the engine -dump_bindings")

    d = sub.add_parser("diff", help="markdown 'Изменения для аддонов' between two snapshots")
    d.add_argument("old")
    d.add_argument("new")
    d.add_argument("--out", help="write here instead of stdout")
    d.add_argument("--limit", type=int, default=200, help="max items per list (0 = all)")
    d.add_argument("--format", choices=("md", "json"), default="md")

    c = sub.add_parser("check", help="references of an addon to things missing in a snapshot")
    c.add_argument("snapshot")
    c.add_argument("addon", help="addon folder (with gamedata\\ inside, or the gamedata-like root itself)")
    c.add_argument("--old-snapshot", help="snapshot of the game version the addon was made for")
    c.add_argument("--format", choices=("md", "json"), default="md")
    c.add_argument("--out", help="write here instead of stdout")
    c.add_argument("--gw-merge", help="gw_merge: the changed atoms of the replaced files (default: $GW_MERGE, PATH, "
                                      "xray-16\\bin\\x64\\Release; not found - no atoms)")

    import catalog_merge
    catalog_merge.add_timeout(c)
    catalog_merge.add_parsers(sub)

    args = ap.parse_args(argv)
    from common import ToolError
    try:
        return run(args)
    except ToolError as e:
        # gw_merge failed or timed out, a broken delta / file of the catalog, a file not overwritten without --force
        print("error: %s" % e, file=sys.stderr, flush=True)
        return 2


def run(args):
    import catalog_merge
    import gwmerge
    gwmerge.set_timeout(getattr(args, "gw_merge_timeout", None))
    if args.cmd in catalog_merge.COMMANDS:
        return catalog_merge.COMMANDS[args.cmd](args)
    if args.cmd == "build":
        import builder
        import gametag
        from common import log
        if not gametag.is_tag(args.tag):
            # the engine compares [addon] game_version with the tag as versions only for tags of the scheme
            log("warning: --tag %r is not a release tag of the game (v<N>.<N>...[-patchN|-<pre-release>], "
                "gametag.py): addons whose game_version differs from it count as made for an older release" % args.tag)
        return builder.build(args)
    if args.cmd == "diff":
        import differ
        return differ.run(args)
    import checker
    return checker.run(args)


if __name__ == "__main__":
    sys.exit(main())
