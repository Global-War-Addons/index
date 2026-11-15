"""gw_addon_tool check: the whole local check of an author in one command (review 2026-11-06 F/A2, decision 63).

    the package       validate (the working folder of the addon, or its zip parts) - the checks of the CI of the index
    the repo          validate-repo (addon.ltx + catalog/ of the repo of the addon): --repo, or the folder itself when
                      it has catalog/catalog.ltx (a working folder that is a clone of the repo)
    the two addon.ltx the copy of the repo against the one of the package (repo_manifest_differs)

The findings are the ones of the validator (the same codes); this module only adds the comparison, sorts them for a
human (template leftovers first, then errors, moderator review, warnings, notes) and adds a hint "how to fix" to each
code it knows. Exit code (decision 64, low items): 0 - ready (no errors, no template leftovers; warnings may remain),
1 - not ready: errors or template leftovers (the summary: "ok" - no errors, "ready" - no leftovers either), 2 - the run
itself failed. A leftover fails the check even when it is only a warning of the validator: the CI of the index would
publish the card with the text of the template.
"""

from . import ltx
from .report import ERROR, NOTE, REVIEW, WARNING

# Leftovers of the template repo (review F21 / F22): the card would show the text of the template. Shown first
LEFTOVER_CODES = {"repo_placeholder", "package_placeholder", "catalog_template_description", "catalog_no_tags",
                  "catalog_no_authors", "catalog_no_maintainers"}

# code -> how to fix it. A code that is not here is shown without a hint (its message says what is wrong)
HINTS = {
    # the template
    "repo_placeholder": "replace every {{...}} with the real value (the bot fills them when it creates the repo; a "
                        "repo made by hand from the template keeps them)",
    "package_placeholder": "the addon.ltx of the package still has {{...}} of the template: write the real id and type",
    "catalog_template_description": "write your own text in catalog/description*.md: the card in the launcher shows it",
    "catalog_no_tags": "add tags = ... in catalog/catalog.ltx (keys of tags.ltx of the index): players filter by them",
    "catalog_no_authors": "write authors = ... in catalog/catalog.ltx (who made the addon, free text)",
    "catalog_no_maintainers": "write maintainers = <your GitHub login> in catalog/catalog.ltx",
    "catalog_bad_maintainer": "maintainers takes GitHub logins only (letters, digits, '-'), comma separated",
    # the manifest
    "manifest_syntax": "addon.ltx is the strict ltx of the engine: [section], key = value, comments ';' or '//'",
    "manifest_no_id": "write [addon] id = <the name of the repo>",
    "manifest_bad_id": "the id is a-z 0-9 _ (up to 64) and equals the name of the repo",
    "manifest_id_mismatch": "[addon] id must be the name of the repo of the addon",
    "manifest_reserved_id": "this id is taken by the game or the catalog: pick another one (and another repo name)",
    "manifest_no_version": "write [addon] version = MAJOR.MINOR.PATCH (1.0.0)",
    "manifest_bad_version": "three numbers without leading zeros or suffixes: 1.2.0",
    "manifest_version_mismatch": "the tag of the release is v<version>: fix version in addon.ltx and pack again, or "
                                 "tag the release v<version>",
    "manifest_no_type": "write [addon] type = content (or graphics, audio, script, ui, localization, library, "
                        "location)",
    "manifest_unknown_type": "type is one of: content, graphics, audio, script, ui, localization, library, location",
    "manifest_no_api_min": "the addon has code: write [addon] api_min = <the Addons API version you tested it on> "
                           "(template README, section 6). An addon of content only does not need it",
    "manifest_bad_api_min": "api_min is MAJOR.MINOR.PATCH, for example 0.1.0",
    "manifest_bad_depends": "depends is a comma list: other_id, other_id >=1.2.0, other_id >=1.0 <2",
    "manifest_bad_lua_entry": "[lua] entry is the name of a script without .script: <id>_main",
    "manifest_lua_entry_prefix": "name the entry script <id>_main.script: script names are global for all addons",
    "manifest_plugin_required": "remove required = true: plugins of catalog addons are not loaded in v1",
    "manifest_plugin_v1": "the catalog does not load plugins in v1: the addon must work without its [plugin]",
    "manifest_plugin_no_api_min": "write [plugin] api_min = <the Plugin API version the library is built for>",
    "manifest_unknown_key": "a key the engine does not read: check its spelling (wiki doc/plugins/manifest.md)",
    "manifest_unknown_section": "a section the engine does not read: check its spelling (wiki doc/plugins/manifest.md)",
    "manifest_unknown_capability": "[capabilities] knows fs_write, ffi, os_exec",
    "manifest_bom": "save addon.ltx as UTF-8 without BOM",
    "manifest_game_version_form": "game_version is a release tag of the game: v1.4.0, 0.5.5-patch4, 0.5.6-Beta",
    "manifest_no_save_compat": "a location pack: write [location] save_compat = 1 (raise it when saves break)",
    "game_version_missing": "write [addon] game_version = <the game tag you made the replaced files on>, or move "
                            "the edits to DLTX mod_*.ltx / hooks so nothing is replaced",
    # the package
    "package_nested": "the root of the zip is the root of the addon folder: pack with gw_addon_tool.py pack",
    "package_unknown_folder": "everything of the game goes into gamedata/; remove other folders from the addon folder",
    "package_unknown_file": "only addon.ltx, README*, LICENSE*, CHANGELOG* lie next to gamedata/",
    "package_catalog": "pack the working folder of the addon, not the repo (catalog/ stays in the repo)",
    "package_executable": "remove .exe, .dll, .bat, .sh ... from the addon: the catalog does not take them",
    "package_bin": "remove bin/: plugins are not taken by the catalog in v1",
    "package_db_v1": "unpack the .db into gamedata/ (v1 takes folders only)",
    "package_name_encoding": "pack with gw_addon_tool.py pack or 7-Zip (UTF-8 names)",
    "package_no_manifest": "put addon.ltx into the root of the addon folder",
    "package_symlink": "replace the link by the file itself",
    "package_case_collision": "two files differ only by case: Windows keeps one of them; rename or remove one",
    "package_windows_name": "rename the file: Windows does not allow this name",
    "package_alias_folder": "folders @... are not mounted: use [private] (template README, section 3a)",
    "package_gw_merge": ".gw_merge/ is written by the launcher: delete it from the addon folder",
    "package_launcher_marker": ".gw_addon.json is written by the launcher: it is not part of the package",
    "package_excluded": "nothing to do: these files of the folder do not go into the package",
    "asset_no_package": "attach <id>-<version>.zip (or .part1.zip ...) with the exact name pack gives it",
    "asset_whole_and_parts": "attach either <id>-<version>.zip or its .part<N>.zip, not both",
    "asset_parts_gap": "attach every part .part1.zip .. .partN.zip",
    "asset_too_large": "a part is under 2 GiB: pack splits a large addon by itself",
    "lua_entry_missing": "put gamedata/scripts/<entry>.script into the package or fix [lua] entry",
    "slot_entry_missing": "put <entry>.script into gamedata/scripts/ or into the set of the slot (slots/<slot>/"
                          "gamedata/scripts/) of the package, or fix [slot.<slot>] entry",
    "slot_panel_missing": "put <panel>.script into the common gamedata/scripts/ of the package (not into the set of "
                          "the slot), or fix [slot.<slot>] panel",
    "manifest_bad_slot_module": "[slot.<slot>] entry / panel is the name of a script without .script: <id>_panel",
    "caps_undeclared": "add the capability to [capabilities] of addon.ltx, or remove the call",
    "caps_declared_unused": "nothing found by the scanner: remove the key from [capabilities] if it is not needed",
    "ref_without_depends": "add the other addon to [addon] depends, or stop referring to its @<id>\\ files",
    "package_foreign_namespace": "do not write into the folders of another addon",
    "namespace_section": "name new sections <id>_...: names are global for all addons",
    "namespace_script": "name new scripts <id>_...: names are global for all addons",
    "namespace_string": "name new string ids <id>_...: ids are global for all addons",
    "replaces_base": "the addon replaces files of the base for everyone: fine if meant; otherwise [private] or "
                     "remap-textures (template README, section 3a)",
    "replaces_texture_own_model": "the new models of the addon use a base texture name: [private] or remap-textures",
    "replaces_root_config": "patch the config with DLTX mod_<name>_<id>.ltx instead of replacing it",
    "replaces_save_breaking": "a new world is type = location; otherwise do not replace all.spawn / level files",
    "replaces_unsupported": "the game reads this file before the addons are mounted: the replacement has no effect",
    "replaces_single_file_base": "do not replace particles.xr / shaders.xr / lanims.xr whole",
    "private_mask_unused": "the [private] mask matches no file: fix or remove it",
    "private_texture_no_thm": "add the .thm of the private bump texture",
    "model_texture_missing": "add the texture to the addon (or depend on the addon that has it)",
    "model_motion_missing": "add the animation to the addon (or depend on the addon that has it)",
    "model_lod_missing": "add the LOD model to the addon (or depend on the addon that has it)",
    "delta_unknown_game_version": "game_version must be a game release of game_releases.ltx of the index",
    "delta_unavailable": "the delta needs a snapshot of the base: --snapshots <folder> --latest <tag>, or wait for the "
                         "snapshot of that release",
    "delta_failed": "gw_merge failed on the texts: report it (the CI of the index retries it)",
    # locally (a warning): only the checks of the merged texts are skipped; in the CI of the index (an error): it
    # retries the version
    "merge_plan_unavailable": "take gw_merge from the game: --gw-merge <game>\\bin\\gw_merge.exe (or $GW_MERGE); "
                              "Linux and a build of your own - the message above",
    "catalog_missing": "add catalog/catalog.ltx to the repo",
    "catalog_no_name": "write name = ... (or name_ru / name_en) in catalog/catalog.ltx",
    "catalog_no_summary": "write summary = ... (one line) in catalog/catalog.ltx",
    "catalog_no_description": "add catalog/description.md (or description.ru.md / description.en.md)",
    "catalog_unknown_tag": "use only tags of tags.ltx of the index",
    "catalog_bad_license": "license is an SPDX id (MIT, CC-BY-4.0 ...) or custom",
    "catalog_bad_link": "links start with https://",
    "catalog_http_link": "use https:// links",
    "catalog_icon_size": "save the icon as 256x256 PNG",
    "catalog_icon_bytes": "the icon is at most 256 KB: save it with stronger PNG compression",
    "catalog_icon_format": "the icon is a PNG file named icon.png",
    "catalog_screenshot_name": "rename the screenshot: ASCII letters, digits, '.', '-', '_' only",
    "catalog_screenshot_size": "scale the screenshot to at most 1920 px on the long side",
    "catalog_screenshot_bytes": "a screenshot is at most 1 MB: save it as .jpg or .webp",
    "catalog_too_many_screenshots": "keep at most 10 screenshots",
    "catalog_unknown_file": "the catalog does not read this file: remove it or rename it",
    "repo_no_manifest": "copy addon.ltx of the package into the repo (the CI reads the package one, this one is "
                        "for people reading the repo)",
    "repo_manifest_differs": "copy addon.ltx of the package into the repo after you change id / version",
    "repo_manifest_keys_differ": "copy addon.ltx of the package into the repo",
    "no_snapshot": "the base replacements and the delta are not checked: --snapshots <folder> --latest <tag> (or the "
                   "snapshot_url of the index, once the game publishes snapshots)",
    "no_index_config": "run the tool from a clone of the index repo (or the .pyz) or give --index <folder>",
}

# a family of codes -> the hint of the family (the first prefix that matches)
PREFIX_HINTS = (
    ("lua_", "the scanner flags a trick that hides calls: a moderator reviews the version; use plain calls if you can"),
    ("catalog_screenshot_", "screenshots: .png/.jpg/.webp, at most 1920 px and 1 MB, ASCII names, no subfolders"),
    ("manifest_bad_", "fix the value in addon.ltx (wiki doc/plugins/manifest.md)"),
    ("manifest_self_", "an addon cannot name itself here: remove its own id"),
)

LEVEL_TITLES = (
    (ERROR, "ERRORS - the version is not published until they are fixed"),
    (REVIEW, "MODERATOR REVIEW - published after a moderator approves"),
    (WARNING, "WARNINGS - published, but worth fixing"),
    (NOTE, "NOTES"),
)


def hint(code):
    if code in HINTS:
        return HINTS[code]
    for prefix, text in PREFIX_HINTS:
        if code.startswith(prefix):
            return text
    return ""


def _addon_values(data):
    """{(section, key lower): value} of an addon.ltx, None - not readable"""
    try:
        ini = ltx.parse(ltx.decode(data, "addon.ltx"))
    except ltx.LtxError:
        return None
    out = {}
    for sec in ini.names():
        for key, value in ini.items(sec):
            out[(sec, key.lower())] = value
    return out


def compare_manifests(repo_bytes, package_bytes, report):
    """The addon.ltx of the repo against the one of the package (11 section 3.1: the repo keeps the addon.ltx of the
    latest version). id / version - repo_manifest_differs (warning, as the author mode of the launcher), any other value
    - repo_manifest_keys_differ (a note)"""
    if repo_bytes == package_bytes:
        return
    repo, pkg = _addon_values(repo_bytes), _addon_values(package_bytes)
    if repo is None or pkg is None:
        return  # the manifest check of that copy reports it
    for key in ("id", "version"):
        a, b = repo.get(("addon", key)), pkg.get(("addon", key))
        if a != b:
            report.warning("repo_manifest_differs", "[addon] %s: the repo has %s, the package %s" %
                           (key, a if a is not None else "-", b if b is not None else "-"), "addon.ltx (repo)")
    other = sorted("[%s] %s" % k for k in set(repo) | set(pkg)
                   if k not in (("addon", "id"), ("addon", "version")) and repo.get(k) != pkg.get(k))
    if other:
        more = " (+%d more)" % (len(other) - 8) if len(other) > 8 else ""
        report.note("repo_manifest_keys_differ", "the addon.ltx of the repo and of the package differ in %s%s" %
                    (", ".join(other[:8]), more), "addon.ltx (repo)")


def is_leftover(item):
    # any finding about a value that is still a {{...}} of the template (a bad id, license, link, login ...)
    return item["code"] in LEFTOVER_CODES or "{{" in item["message"]


def collect(package_report, repo_report):
    """The findings of both reports: [(where, item)], one finding the two reports share (the repo is the package folder:
    its addon.ltx is checked twice) once"""
    out, seen = [], set()
    for where, rep in (("package", package_report), ("repo", repo_report)):
        if rep is None:
            continue
        for item in rep.sorted_items():
            key = (item["level"], item["code"], item.get("path") or "", item["message"])
            if key in seen:
                continue
            seen.add(key)
            out.append((where, item))
    return out


def summarize(package_report, repo_report, manifest=None, package_path="", repo_path="", not_checked=()):
    """-> dict for --format json and for render_text"""
    items = collect(package_report, repo_report)
    leftovers = [(w, i) for w, i in items if is_leftover(i)]
    rest = [(w, i) for w, i in items if not is_leftover(i)]
    errors = sum(1 for _, i in items if i["level"] == ERROR)

    def row(where, item):
        out = dict(item, where=where)
        h = hint(item["code"])
        if h:
            out["hint"] = h
        return out

    return {
        "ok": errors == 0,
        "ready": errors == 0 and not leftovers,
        "package": package_path, "repo": repo_path or None,
        "addon": {k: manifest.get(k) for k in ("id", "version", "type")} if manifest else None,
        "errors": errors,
        "leftovers": [row(w, i) for w, i in leftovers],
        "findings": {level: [row(w, i) for w, i in rest if i["level"] == level] for level, _ in LEVEL_TITLES},
        "not_checked": list(not_checked),
    }


def render_text(s, prog="gw_addon_tool.py"):
    lines = ["%s check" % prog, "  package: %s" % s["package"]]
    if s["addon"]:
        lines[-1] += "  (%s %s, type %s)" % (s["addon"]["id"] or "?", s["addon"]["version"] or "?",
                                             s["addon"]["type"] or "?")
    lines.append("  repo:    %s" % (s["repo"] or "- (not checked)"))

    def block(title, rows):
        if not rows:
            return
        lines.append("")
        lines.append("%s (%d):" % (title, len(rows)))
        for r in rows:
            where = r["where"] + ((" " + r["path"]) if r.get("path") else "")
            lines.append("  %-7s %s  [%s]" % (r["level"], r["code"], where))
            lines.append("          %s" % r["message"])
            if r.get("hint"):
                lines.append("          fix: %s" % r["hint"])

    block("TEMPLATE LEFTOVERS - fill these in first", s["leftovers"])
    for level, title in LEVEL_TITLES:
        block(title, s["findings"][level])
    if s["not_checked"]:
        lines.append("")
        lines.append("NOT CHECKED:")
        lines.extend("  - %s" % n for n in s["not_checked"])
    lines.append("")
    if not s["ok"]:
        extra = (", %d template leftover(s)" % len(s["leftovers"])) if s["leftovers"] else ""
        lines.append("RESULT: NOT READY - %d error(s)%s. Fix them and run check again." % (s["errors"], extra))
    elif s["leftovers"]:
        lines.append("RESULT: NOT READY - no errors, but the card still has %d template leftover(s): the catalog "
                     "would show them. Fill them in and run check again." % len(s["leftovers"]))
    else:
        a = s["addon"] or {}
        lines.append("RESULT: READY. Next: %s pack <addon folder> --out <folder outside it>, then a release v%s with "
                     "%s-%s.zip (template README, section 4)." % (prog, a.get("version") or "<version>",
                                                                  a.get("id") or "<id>",
                                                                  a.get("version") or "<version>"))
    return "\n".join(lines)
