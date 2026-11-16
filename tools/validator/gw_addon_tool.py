#!/usr/bin/env python3
"""gw_addon_tool - the tool of an addon author (09-content-addons-vfs.md 9, C7; 14-addon-resource-scoping.md 5).
The same code and checks as the CI of the catalog (gw_catalog.py); --index defaults to the index repo this validator
lies in (tools/validator of a clone of the index, catalog/index of the GW workspace, or the files packed into
gw_addon_tool.pyz).

    check <addon folder | part1.zip ...>      everything at once: the package (validate), the repo of the addon
          [--repo <repo folder>]              (validate-repo; default: the addon folder itself when it has
                                              catalog/catalog.ltx), the addon.ltx of the repo against the package.
                                              One summary: template leftovers first, then errors, review, warnings,
                                              each with a hint how to fix it
    validate <addon folder | part1.zip ...>   the checks of the CI: manifest, layout, Lua scanner, references,
                                              namespaces; with a snapshot of the base (--snapshots, or the
                                              snapshot_url of the index): the base files it replaces, the model
                                              references, the delta of the texts
    validate-repo <repo folder>               addon.ltx + catalog/ of the repo of the addon
    pack <addon folder> --out DIR             validate, then <id>-<ver>.zip (or parts under 2 GiB)
    remap-textures <addon folder>             the textures whose names the base has: into [private] (the base keeps its
                                              own), the models of the addon name them @<id>\\<name>
                                              (--dry-run first; --all - every texture; --only MASK)

Exit code: 0 - fine, 1 - errors found (the report says which), 2 - the run itself failed. check: 0 only when the
result is READY - template leftovers of the card ({{...}}, the text of the template, empty tags / authors /
maintainers) give 1 too, even when they are only warnings.
The checks of the texts merged with the base need gw_merge (the game ships it: <game>\\bin\\gw_merge.exe; --gw-merge
<path> or $GW_MERGE): without it they are skipped with the warning merge_plan_unavailable, the rest is checked.
See catalog/template/README.md.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gw_catalog  # noqa: E402

COMMANDS = ("check", "validate", "validate-repo", "pack", "remap-textures")


def main(argv=None):
    return gw_catalog.main(argv, prog="gw_addon_tool", only=COMMANDS, description=__doc__, auto_index=True)


if __name__ == "__main__":
    sys.exit(main())
