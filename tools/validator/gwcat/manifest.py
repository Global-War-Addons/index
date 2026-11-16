"""addon.ltx for the catalog (11-catalog-distribution.md 4.7 rows 1-2, 6 (api_min), 3.4 (version = tag)).

The rules of the engine (addon_host.cpp ParseManifest) plus what the catalog is stricter about: version must be
MAJOR.MINOR.PATCH (the engine only warns), load_after / patches / conflicts must be valid (the engine ignores a bad
item), [lua] entry must name a script of the package, no [plugin] with required = true (v1: plugins of index addons do
not load, 11 R4).
"""

import re

from . import limits, ltx, luacaps

TYPES = ("content", "graphics", "audio", "script", "ui", "localization", "library", "location", "total_conversion")
ENGINE_RESERVED_IDS = ("core",)  # the name of the base in addons.ltx (indexcfg.SERVICE_IDS has it too)
LUA_ENTRY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SLOT_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)+$")
# [slot.<slot>] entry / panel: SlotEntryValid of addon_slots.cpp (a digit first is allowed there)
SLOT_MODULE_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
KNOWN_ADDON_KEYS = {"id", "version", "type", "depends", "conflicts", "load_after", "patches", "game_version", "api_min",
                    "game"}
KNOWN_PLUGIN_KEYS = {"name", "api_min", "required"}  # addon_host.cpp: the other keys of [plugin] are warned about
# SimpleIni IsTrueText / IsFalseText of addon_host.cpp (case-insensitive words, "1" / "0" as is)
BOOL_WORDS = {"true", "on", "yes", "1", "false", "off", "no", "0"}
KNOWN_SECTIONS = {"addon", "plugin", "lua", "private", "location", "slots_declare", "capabilities", "overrides"}


def has_code_sections(ini):
    """'[lua]' / '[plugin]' / '[lua] and [plugin]' when the manifest declares code, '' - a manifest of content only"""
    found = [("[%s]" % sec) for sec in ("lua", "plugin") if ini.find(sec) is not None]
    return " and ".join(found)


def api_min_needed_for_scripts(m, files, report, path="addon.ltx"):
    """manifest_no_api_min for a manifest without [lua] / [plugin] whose package still has scripts (.script / .lua):
    they run against the Addons API too. A package of content only gets no warning (decision 63)"""
    ini = m.get("ini")
    if ini is None or m.get("api") or has_code_sections(ini):
        return
    raw = ini.get("addon", "api_min")
    if raw is not None and ltx.parse_api_version(raw) is None:
        return  # manifest_bad_api_min said it
    scripts = sorted(n for n in files if n.lower().endswith((".script", ".lua")))
    if scripts:
        more = " (+%d more)" % (len(scripts) - 3) if len(scripts) > 3 else ""
        report.warning("manifest_no_api_min", "[addon] api_min is not set, the package has scripts (%s%s): write the "
                                              "Addons API version they are made for (11 section 6)" %
                       (", ".join(scripts[:3]), more), path)


def check_manifest(data, report, path="addon.ltx", expect_id=None, expect_version=None, reserved=None):
    """data: bytes of addon.ltx. Returns the manifest dict (or None when it is unusable)."""
    if len(data) > limits.MANIFEST_MAX_BYTES:
        report.error("manifest_too_large", "%d bytes, at most %d" % (len(data), limits.MANIFEST_MAX_BYTES), path)
        return None
    if data.startswith(b"\xef\xbb\xbf"):
        report.warning("manifest_bom", "addon.ltx starts with a BOM: the engine takes it, write UTF-8 without BOM",
                       path)
    try:
        ini = ltx.parse(ltx.decode(data, "addon.ltx"))
    except ltx.LtxError as e:
        report.error("manifest_syntax", "manifest syntax error: %s" % e, path)
        return None

    m = {"ini": ini}
    addon_id = ini.get("addon", "id")
    if addon_id is None:
        report.error("manifest_no_id", "manifest has no [addon] id", path)
        return None
    if not ltx.is_valid_id(addon_id):
        report.error("manifest_bad_id", "invalid [addon] id '%s' (allowed: a-z 0-9 _ , up to 64 chars)" % addon_id,
                     path)
        return None
    m["id"] = addon_id
    if expect_id is not None and addon_id != expect_id:
        report.error("manifest_id_mismatch", "[addon] id = %s, the repo is %s (id = repo name, 11 R7)" %
                     (addon_id, expect_id), path)
    if addon_id in ENGINE_RESERVED_IDS:
        # not only a name of the catalog: the rules file: / atom: of appdata\addons.ltx and the slots name the base
        # with it (addon_host.cpp kCoreWinner), an addon of this id can never win a rule - refused without --index too
        report.error("manifest_reserved_id", "id '%s' is reserved: the rules of addons.ltx and the slots name the base "
                                             "with it" % addon_id, path)
    elif reserved is not None and reserved.is_reserved(addon_id):
        report.error("manifest_reserved_id", "id '%s' is reserved (%s)" % (addon_id, reserved.why(addon_id)), path)

    version = ini.get("addon", "version")
    if version is None:
        report.error("manifest_no_version", "[addon] version is required by the catalog (MAJOR.MINOR.PATCH)", path)
    elif ltx.parse_version(version) is None:
        report.error("manifest_bad_version", "[addon] version '%s' is not MAJOR.MINOR.PATCH (numbers)" % version,
                     path)
    elif ltx.version_str(ltx.parse_version(version)) != version:
        # the engine reads 01.2.3 as 1.2.3; the catalog names files and the tag after the version: one spelling
        report.error("manifest_bad_version", "[addon] version '%s': write it without leading zeros (%s)" %
                     (version, ltx.version_str(ltx.parse_version(version))), path)
    elif expect_version is not None and version != expect_version:
        report.error("manifest_version_mismatch", "[addon] version = %s, the release is v%s (the tag is "
                                                  "v<version>, 11 section 3.4)" % (version, expect_version), path)
    m["version"] = version or ""

    type_name = ini.get("addon", "type")
    if type_name is None:
        report.error("manifest_no_type", "manifest has no [addon] type (allowed: %s)" % ", ".join(TYPES), path)
        type_name = ""
    elif type_name.lower() not in TYPES:
        report.error("manifest_unknown_type", "unknown [addon] type '%s' (known: %s)" % (type_name, ", ".join(TYPES)),
                     path)
    elif type_name != type_name.lower():
        report.warning("manifest_type_case", "write [addon] type in lower case: %s" % type_name.lower(), path)
    m["type"] = type_name.lower()
    if m["type"] == "total_conversion":
        report.warning("manifest_total_conversion", "type total_conversion is reserved for v2+ (00 section 2.1)", path)

    deps_text = ini.get("addon", "depends") or ""
    try:
        deps = ltx.parse_depends(deps_text)
    except ltx.LtxError as e:
        report.error("manifest_bad_depends", "invalid [addon] depends: %s" % e, path)
        deps = []
    if any(d["id"] == addon_id for d in deps):
        report.error("manifest_self_depends", "[addon] depends names the addon itself", path)
    m["depends"] = ", ".join(d["text"] for d in deps)
    m["depends_ids"] = [d["id"] for d in deps]

    conf_text = ini.get("addon", "conflicts") or ""
    try:
        conflicts = ltx.parse_depends(conf_text)
    except ltx.LtxError as e:
        report.error("manifest_bad_conflicts", "invalid [addon] conflicts (the syntax of depends): %s" % e, path)
        conflicts = []
    m["conflicts"] = ", ".join(d["text"] for d in conflicts)

    for key in ("load_after", "patches"):
        ids, bad = ltx.parse_id_list(ini.get("addon", key))
        for item in bad:
            report.error("manifest_bad_" + key, "[addon] %s '%s' is not an addon id" % (key, item), path)
        if addon_id in ids:
            report.error("manifest_self_" + key, "[addon] %s names the addon itself" % key, path)
            ids.remove(addon_id)
        m[key] = ids

    gv = ini.get("addon", "game_version")
    if ini.has_key("addon", "game_version") and gv is None:
        report.warning("manifest_empty_game_version", "[addon] game_version is empty: not set", path)
    if gv is not None and ltx.parse_game_tag(gv) is None:
        report.warning("manifest_game_version_form", "[addon] game_version '%s' is not a release tag of the game "
                                                     "(v1.4.0, 0.5.5-patch4, 0.5.6-Beta): the engine takes any "
                                                     "difference as older" % gv, path)
    m["game_version"] = gv or ""

    api = ini.get("addon", "api_min")
    # api_min matters only for code (decision 63): the Addons API is what scripts and plugins call. A pure content
    # addon (textures, sounds, DLTX) without it loads on every API major; with it, it would be "incompatible" after
    # each breaking MINOR before 1.0 for no reason. [lua] / [plugin] are known here; the .script files of the package
    # are looked at by validate.validate_version (api_min_needed_for_scripts)
    code = has_code_sections(ini)
    if api is None:
        if code:
            report.warning("manifest_no_api_min", "[addon] api_min is not set: the addon has %s, write the Addons API "
                                                  "version it is made for (11 section 6)" % code, path)
        m["api"] = ""
    elif ltx.parse_api_version(api) is None:
        report.error("manifest_bad_api_min", "[addon] api_min '%s' is not MAJOR.MINOR.PATCH (MAJOR, MINOR <= 1023, "
                                             "PATCH <= 4095)" % api, path)
        m["api"] = ""
    elif not ltx.api_is_set(ltx.parse_api_version(api)):
        # the engine reads 0.0.0 as 0: no requirement - not a group of API major 0.0 (11 section 6)
        if code:
            report.warning("manifest_no_api_min", "[addon] api_min = %s is the same as not set: the addon has %s, "
                                                  "write the Addons API version it is made for (11 section 6)" %
                           (api, code), path)
        m["api"] = ""
    else:
        m["api"] = api
    if ini.get("addon", "game") is not None:
        report.note("manifest_game_reserved", "[addon] game is reserved and not used for compatibility (11 section 6)",
                    path)
    for key in ini.keys("addon"):
        if key.lower() not in KNOWN_ADDON_KEYS:
            report.warning("manifest_unknown_key", "[addon] %s is not a key the engine reads" % key, path)

    # [plugin]
    m["plugin_api"] = ""
    m["plugin"] = None
    if ini.find("plugin") is not None:
        name = ini.get("plugin", "name")
        if name is None:
            report.error("manifest_plugin_no_name", "[plugin] section has no name", path)
        elif not ltx.is_valid_id(name):
            report.error("manifest_plugin_bad_name", "invalid [plugin] name '%s'" % name, path)
        papi = ini.get("plugin", "api_min")
        if papi is not None:
            if ltx.parse_api_version(papi) is None:
                report.error("manifest_plugin_bad_api_min", "invalid [plugin] api_min '%s' (expected "
                                                            "MAJOR.MINOR.PATCH)" % papi, path)
            else:
                m["plugin_api"] = ltx.api_value(papi)  # 0.0.0 - no requirement, as in the engine
        if papi is None or (ltx.parse_api_version(papi) is not None and not m["plugin_api"]):
            # addon_host.cpp: a library without the export gwp_plugin_api_built (a build older than GWP_PLUGIN_INIT
            # exporting it) is refused unless [plugin] api_min says what it needs
            report.warning("manifest_plugin_no_api_min", "[plugin] api_min is not set: the engine loads only a library "
                                                         "that exports gwp_plugin_api_built (built with the current "
                                                         "gwp.hpp); write the Plugin API version it is built for", path)
        for key in ini.keys("plugin"):
            if key.lower() not in KNOWN_PLUGIN_KEYS:
                report.warning("manifest_plugin_unknown_key", "[plugin] %s is not a key the engine reads (known: %s)" %
                               (key, ", ".join(sorted(KNOWN_PLUGIN_KEYS))), path)
        raw_required = ini.get("plugin", "required")
        if raw_required is not None and raw_required.lower() not in BOOL_WORDS:
            report.warning("manifest_plugin_bad_required", "[plugin] required = %s is not a boolean (true / false): "
                                                           "the engine reads it as false" % raw_required, path)
        required = ini.get_bool("plugin", "required", False)
        m["plugin"] = {"name": name or "", "required": required}
        if required:
            report.error("manifest_plugin_required", "[plugin] required = true: plugins of catalog addons are not "
                                                     "loaded in v1 (11 R4), the addon would fail", path)
        else:
            report.warning("manifest_plugin_v1", "[plugin]: plugins of catalog addons are not loaded in v1 (11 R4); "
                                                 "the addon must work without it", path)

    # [lua]
    m["lua_entry"] = ""
    if ini.find("lua") is not None:
        entry = ini.get("lua", "entry") or ""
        if not LUA_ENTRY_RE.match(entry):
            report.error("manifest_bad_lua_entry", "[lua] entry '%s' is not a Lua module name (A-Z a-z 0-9 _, not a "
                                                   "digit first, up to 64 chars)" % entry, path)
        else:
            m["lua_entry"] = entry
            if entry != addon_id and not entry.startswith(addon_id + "_"):
                report.warning("manifest_lua_entry_prefix", "[lua] entry '%s' does not start with '%s_'" %
                               (entry, addon_id), path)

    # [capabilities]
    caps, unknown = luacaps.declared_caps(ini)
    for key in unknown:
        report.warning("manifest_unknown_capability", "[capabilities] %s is not known (known: %s)" %
                       (key, ", ".join(luacaps.KNOWN_CAPS)), path)
    m["caps"] = sorted(caps)

    # [location]
    m["save_compat"] = 0
    if m["type"] == "location":
        sc = ini.get("location", "save_compat")
        if sc is None:
            report.warning("manifest_no_save_compat", "[location] save_compat is not set: the engine uses 1", path)
            m["save_compat"] = 1
        elif not (sc.isascii() and sc.isdigit()) or not 0 < int(sc) <= 0xFFFFFFFF:
            # ParseSaveCompat (level_pack.cpp): 1..u32 max in ASCII digits; "²".isdigit() is true, int("²") fails
            report.error("manifest_bad_save_compat", "[location] save_compat '%s' is not an integer from 1 to "
                                                     "4294967295" % sc, path)
        else:
            m["save_compat"] = int(sc)

    # [slot.<slot>] - the folders of the sets
    slots = []
    for sec in ini.names():
        if not sec.startswith("slot."):
            continue
        slot = sec[len("slot."):]
        if not SLOT_RE.match(slot):
            report.error("manifest_bad_slot", "[%s]: '%s' is not a slot id (a-z 0-9 _ parts through dots)" %
                         (sec, slot), path)
            continue
        files = (ini.get(sec, "files") or ("slots\\%s\\" % slot)).replace("/", "\\")
        parts = [p for p in files.split("\\") if p]
        if files.startswith("\\") or ":" in files or any(p in (".", "..") for p in parts) or not parts:
            report.error("manifest_bad_slot_files", "[%s] files '%s': no drive, no leading '\\', no '..'" %
                         (sec, files), path)
            continue
        # entry / panel: Lua modules; a bad name the engine ignores with a warning (addon_slots.cpp AddProvider)
        modules = {}
        for key in ("entry", "panel"):
            value = ini.get(sec, key) or ""
            if value and not SLOT_MODULE_RE.match(value):
                report.warning("manifest_bad_slot_module", "[%s] %s = %s: not a Lua module name (A-Z a-z 0-9 _, "
                               "up to 64 chars), the engine ignores it" % (sec, key, value), path)
                value = ""
            modules[key] = value
        slots.append({"slot": slot, "files": "/".join(parts), "entry": modules["entry"], "panel": modules["panel"]})
    m["slots"] = slots
    for sec in ini.names():
        if sec not in KNOWN_SECTIONS and not sec.startswith("slot."):
            report.warning("manifest_unknown_section", "[%s] is not a section the engine reads" % sec, path)
    m["private"] = [k for k in ini.keys("private")]
    return m


def strip(m):
    """The manifest dict without the parsed ini (for JSON)"""
    return {k: v for k, v in m.items() if k != "ini"}
