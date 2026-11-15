"""catalog/ of an addon repo: catalog.ltx, description*.md, icon.png, screenshots/ (11-catalog-distribution.md 3.1-3.3,
4.7 row 3). Errors here keep the card from being published (the versions already published stay)."""

import re

from . import images, limits, ltx

LANG_RE = re.compile(r"^[a-z]{2,3}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}\Z")
LICENSE_RE = re.compile(r"^[A-Za-z0-9.+\-() ]{1,100}$")
DEFAULT_LICENSE = "CC-BY-NC-4.0"
LINK_KEYS = ("homepage", "source", "donate")
KNOWN_KEYS = {"name", "summary", "tags", "authors", "maintainers", "license", "permission"} | set(LINK_KEYS)
DESC_RE = re.compile(r"^description(?:\.([a-z]{2,3}))?\.md$")
# A file of catalog/ the index names (icon, screenshots, description): the rule of the launcher, which drops any other
# name silently (addon_catalog.rs is_catalog_path): ASCII letters, digits, '.', '-', '_', no part ending in '.', the
# whole path relative to catalog/ at most 200 bytes
CATALOG_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\Z")
CATALOG_PATH_MAX = 200
# The text of catalog/description.md of the template repo (catalog/template): left as it is - a card with a placeholder
TEMPLATE_DESCRIPTION_MARKS = ("Describe the addon here", "Опишите аддон: что добавляет")


def _localized(ini, field, report, max_chars):
    """name / name_<lang> -> {"": ..., "ru": ...}"""
    out = {}
    for key, value in ini.items("catalog"):
        k = key.lower()
        if k == field:
            lang = ""
        elif k.startswith(field + "_"):
            lang = k[len(field) + 1:]
            if not LANG_RE.match(lang):
                report.error("catalog_bad_lang", "%s: '%s' is not a language code (2-3 letters a-z)" % (key, lang),
                             "catalog/catalog.ltx")
                continue
        else:
            continue
        if not value:
            continue
        if lang in out:
            report.error("catalog_duplicate_key", "%s is given twice" % key, "catalog/catalog.ltx")
            continue
        if len(value) > max_chars:
            report.error("catalog_too_long", "%s is %d characters, at most %d" % (key, len(value), max_chars),
                         "catalog/catalog.ltx")
        out[lang] = value
    return out


def _url(value, key, report):
    if not value:
        return ""
    if value.startswith("https://"):
        return value
    if value.startswith("http://"):
        report.warning("catalog_http_link", "%s is not https: %s" % (key, value), "catalog/catalog.ltx")
        return value
    report.error("catalog_bad_link", "%s must be an http(s) address: %s" % (key, value), "catalog/catalog.ltx")
    return ""


def placeholder_keys(data):
    """The keys of [catalog] whose value still has a {{...}} placeholder of the template (comments not looked at).
    Unreadable catalog.ltx - [] (check_catalog says why)"""
    try:
        ini = ltx.parse(ltx.decode(data, "catalog.ltx"), url_safe=True)
    except ltx.LtxError:
        return []
    if ini.find("catalog") is None:
        return []
    return [key for key, value in ini.items("catalog") if value and "{{" in value]


def check_catalog(tree, report, tags_dict=None):
    """tree: {posix path relative to the repo root: bytes}. Returns the card data (a dict) or None."""
    path = "catalog/catalog.ltx"
    data = tree.get(path)
    if data is None:
        report.error("catalog_missing", "no catalog/catalog.ltx in the repo", path)
        return None
    if len(data) > limits.MANIFEST_MAX_BYTES:
        report.error("catalog_too_large", "catalog.ltx is over %d bytes" % limits.MANIFEST_MAX_BYTES, path)
        return None
    if data.startswith(b"\xef\xbb\xbf"):
        report.warning("catalog_bom", "catalog.ltx starts with a BOM (write UTF-8 without BOM)", path)
    try:
        ini = ltx.parse(ltx.decode(data, "catalog.ltx"), url_safe=True)
    except ltx.LtxError as e:
        report.error("catalog_syntax", str(e), path)
        return None
    if ini.find("catalog") is None:
        report.error("catalog_no_section", "catalog.ltx has no [catalog] section", path)
        return None
    for name in ini.names():
        if name != "catalog":
            report.warning("catalog_unknown_section", "[%s] is not read" % name, path)
    for key in ini.keys("catalog"):
        k = key.lower()
        base = k.split("_", 1)[0] if k.startswith(("name_", "summary_")) else k
        if base not in KNOWN_KEYS:
            report.warning("catalog_unknown_key", "%s is not a key of [catalog]" % key, path)

    card = {}
    card["name"] = _localized(ini, "name", report, limits.NAME_MAX_CHARS)
    card["summary"] = _localized(ini, "summary", report, limits.SUMMARY_MAX_CHARS)
    if not card["name"]:
        report.error("catalog_no_name", "name or name_<lang> is required", path)
    if not card["summary"]:
        report.error("catalog_no_summary", "summary or summary_<lang> is required", path)

    tags = []
    for tag in ltx.split_list(ini.get("catalog", "tags")):
        t = tag.lower()
        if tags_dict is not None and t not in tags_dict:
            report.error("catalog_unknown_tag", "tag '%s' is not in tags.ltx of the index (known: %s)" %
                         (tag, ", ".join(sorted(tags_dict)) or "-"), path)
            continue
        if t not in tags:
            tags.append(t)
    if len(tags) > limits.TAGS_MAX:
        report.error("catalog_too_many_tags", "%d tags, at most %d" % (len(tags), limits.TAGS_MAX), path)
    if not ltx.split_list(ini.get("catalog", "tags")):
        report.warning("catalog_no_tags", "tags is empty: the addon is found only by its name (the tags of tags.ltx "
                                          "of the index)", path)
    card["tags"] = tags

    authors_line = ini.get("catalog", "authors") or ""
    if len(authors_line) > limits.AUTHORS_MAX_CHARS:
        report.error("catalog_too_long", "authors is %d characters, at most %d" %
                     (len(authors_line), limits.AUTHORS_MAX_CHARS), path)
    card["authors"] = ltx.split_list(authors_line)
    if not card["authors"]:
        report.warning("catalog_no_authors", "authors is empty: write who made the addon", path)
    maintainers = []
    logins = ltx.split_list(ini.get("catalog", "maintainers"))
    for login in logins:
        if not LOGIN_RE.match(login):
            report.error("catalog_bad_maintainer", "'%s' is not a GitHub login" % login, path)
        elif login not in maintainers:
            maintainers.append(login)
    card["maintainers"] = maintainers
    if not logins:  # written but not valid: catalog_bad_maintainer above says so, not "empty"
        report.warning("catalog_no_maintainers", "maintainers is empty: write the GitHub logins of who publishes",
                       path)

    lic = ini.get("catalog", "license")
    if lic is None:
        lic = DEFAULT_LICENSE
        report.note("catalog_default_license", "license is not set: %s (R16)" % DEFAULT_LICENSE, path)
    elif not LICENSE_RE.match(lic):
        report.error("catalog_bad_license", "license '%s' is not an SPDX id / expression or 'custom'" % lic, path)
    card["license"] = lic
    permission = _url(ini.get("catalog", "permission"), "permission", report)
    if permission:
        card["permission"] = permission
    card["links"] = {k: _url(ini.get("catalog", k), k, report) for k in LINK_KEYS}

    # description*.md
    desc = {}
    for p in sorted(tree):
        if not p.startswith("catalog/") or "/" in p[len("catalog/"):]:
            continue
        m = DESC_RE.match(p[len("catalog/"):])
        if not m:
            continue
        body = tree[p]
        if len(body) > limits.DESCRIPTION_MAX_BYTES:
            report.error("catalog_description_too_large", "over %d bytes" % limits.DESCRIPTION_MAX_BYTES, p)
            continue
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            report.error("catalog_description_encoding", "not UTF-8", p)
            continue
        if any(mark in text for mark in TEMPLATE_DESCRIPTION_MARKS):
            report.warning("catalog_template_description", "the text of the template is still there: describe the "
                                                           "addon (the card shows this file)", p)
        desc[m.group(1) or ""] = p[len("catalog/"):]
    if not desc:
        report.error("catalog_no_description", "at least one catalog/description.md or description.<lang>.md is "
                                               "required", "catalog/")
    card["description"] = desc

    # icon.png
    icon = tree.get("catalog/icon.png")
    card["icon"] = ""
    if icon is not None:
        # the size first: a file over the limit is not downloaded by the CI of the index (providers.plan_tree)
        fmt, w, h = images.image_info(icon) if len(icon) <= limits.ICON_MAX_BYTES else (None, 0, 0)
        if len(icon) > limits.ICON_MAX_BYTES:
            report.error("catalog_icon_bytes", "icon.png is over %d bytes" % limits.ICON_MAX_BYTES, "catalog/icon.png")
        elif fmt != "png":
            report.error("catalog_icon_format", "icon.png is not a PNG", "catalog/icon.png")
        elif (w, h) != (limits.ICON_SIDE, limits.ICON_SIDE):
            report.error("catalog_icon_size", "icon.png is %dx%d, must be %dx%d" %
                         (w, h, limits.ICON_SIDE, limits.ICON_SIDE), "catalog/icon.png")
        else:
            card["icon"] = "icon.png"

    # screenshots/
    shots = []
    for p in sorted(tree):
        if not p.startswith("catalog/screenshots/"):
            continue
        name = p[len("catalog/screenshots/"):]
        if "/" in name:
            report.error("catalog_screenshot_folder", "screenshots/ has no subfolders", p)
            continue
        if not CATALOG_NAME_RE.match(name) or name.endswith(".") or \
                len(("screenshots/" + name).encode("utf-8")) > CATALOG_PATH_MAX:
            report.error("catalog_screenshot_name", "the launcher shows only names of ASCII letters, digits, '.', '-', "
                                                    "'_' (no blank, no other characters, not ending in '.', the path "
                                                    "up to %d characters): rename the file" % CATALOG_PATH_MAX, p)
            continue
        ext = name[name.rfind("."):].lower() if "." in name else ""
        want = images.EXT_FORMATS.get(ext)
        if not want:
            report.error("catalog_screenshot_type", "only .png, .jpg, .jpeg, .webp", p)
            continue
        if len(tree[p]) > limits.SCREENSHOT_MAX_BYTES:  # first: not downloaded by the CI (providers.plan_tree)
            report.error("catalog_screenshot_bytes", "over %d bytes" % limits.SCREENSHOT_MAX_BYTES, p)
            continue
        fmt, w, h = images.image_info(tree[p])
        if fmt != want:
            report.error("catalog_screenshot_format", "the file is not a %s image" % want, p)
            continue
        if max(w, h) > limits.SCREENSHOT_MAX_SIDE:
            report.error("catalog_screenshot_size", "%dx%d: the longer side is at most %d px" %
                         (w, h, limits.SCREENSHOT_MAX_SIDE), p)
            continue
        shots.append("screenshots/" + name)
    if len(shots) > limits.SCREENSHOTS_MAX:
        report.error("catalog_too_many_screenshots", "%d screenshots, at most %d" % (len(shots),
                                                                                     limits.SCREENSHOTS_MAX),
                     "catalog/screenshots/")
        shots = shots[:limits.SCREENSHOTS_MAX]
    card["screenshots"] = shots

    known = {"catalog/catalog.ltx", "catalog/icon.png"} | {"catalog/" + d for d in desc.values()}
    for p in sorted(tree):
        if p.startswith("catalog/") and p not in known and not p.startswith("catalog/screenshots/") and \
                not DESC_RE.match(p[len("catalog/"):]):
            report.warning("catalog_unknown_file", "not read by the catalog", p)
    return card
