"""The "new addon" request (11-catalog-distribution.md 5.1): the Issue form of the index -> checked fields -> the first
addon.ltx and catalog/catalog.ltx of the repo the bot creates from the template.

The labels below are the labels of index/.github/ISSUE_TEMPLATE/new-addon.yml: GitHub writes the body of a form issue
as "### <label>\n\n<value>" (an empty optional field is "_No response_"). Keep both in step (a test checks it).
"""

import os

from . import catalog_meta, ltx
from .manifest import TYPES

FIELDS = {
    "addon_id": "Addon id / id аддона",
    "type": "Type / тип",
    "name": "Name / название",
    "summary": "Summary / кратко",
    "source": "Source or page / исходники или страница",
    "license": "License / лицензия",
    "authors": "Authors / авторы",
    "permission": "Permission link / ссылка на разрешение",
    "maintainer": "Maintainer GitHub login / логин мейнтейнера",
}
REQUIRED = ("addon_id", "type", "name", "maintainer")
NO_RESPONSE = "_No response_"


def parse_body(body):
    """Issue body -> {field id: value} (by the labels of FIELDS)"""
    by_label = {v.lower(): k for k, v in FIELDS.items()}
    out = {}
    current = None
    buf = []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        if line.startswith("### "):
            if current:
                out[current] = "\n".join(buf).strip()
            current = by_label.get(line[4:].strip().lower())
            buf = []
        elif current:
            buf.append(line)
    if current:
        out[current] = "\n".join(buf).strip()
    for k, v in list(out.items()):
        if v == NO_RESPONSE:
            out[k] = ""
    return out


def check_request(fields, cfg, existing_repos):
    """-> list of problems (empty - the request can be approved)"""
    problems = []
    for key in REQUIRED:
        if not fields.get(key):
            problems.append("`%s` is empty" % FIELDS[key])
    addon_id = fields.get("addon_id", "")
    if addon_id:
        if not ltx.is_valid_id(addon_id):
            problems.append("id `%s`: only a-z 0-9 _, up to 64 characters" % addon_id)
        elif cfg.reserved.is_reserved(addon_id):
            problems.append("id `%s` is reserved (%s)" % (addon_id, cfg.reserved.why(addon_id)))
        elif addon_id in existing_repos:
            problems.append("id `%s` is taken: the organization has a repo with this name" % addon_id)
        elif _published_before(cfg, addon_id):
            # a repo of this name was deleted (or moved out): a new one would get its published versions and accepted
            # capabilities (review F6) - a moderator decides (removes state/<id>.json in a pull request or refuses)
            problems.append("id `%s` was used by a repo that is gone (the index has state/%s.json or its history): "
                            "a moderator decides whether it can be given again" % (addon_id, addon_id))
    if fields.get("type") and fields["type"].lower() not in TYPES:
        problems.append("type `%s` is not one of %s" % (fields["type"], ", ".join(TYPES)))
    login = fields.get("maintainer", "").lstrip("@")
    if login and not catalog_meta.LOGIN_RE.match(login):
        problems.append("`%s` is not a GitHub login" % fields["maintainer"])
    lic = fields.get("license") or catalog_meta.DEFAULT_LICENSE
    if not catalog_meta.LICENSE_RE.match(lic):
        problems.append("license `%s` is not an SPDX id or `custom`" % lic)
    for key in ("source", "permission"):
        v = fields.get(key, "")
        if v and not v.startswith(("https://", "http://")):
            problems.append("`%s` must be a link" % FIELDS[key])
    for key in ("name", "summary", "authors"):
        if "\n" in fields.get(key, ""):
            problems.append("`%s` must be one line" % FIELDS[key])
    return problems


def _published_before(cfg, addon_id):
    root = getattr(cfg, "root", None)
    if not root:
        return False
    if os.path.isfile(os.path.join(root, "state", addon_id + ".json")):
        return True
    history = os.path.join(root, "history")
    return os.path.isdir(history) and any(n.startswith(addon_id + ".") for n in os.listdir(history))


def _ltx_value(s):
    """A value for the strict ltx: no ';' (a comment) and no line breaks"""
    return s.replace(";", ",").replace("\n", " ").strip()


def initial_files(fields, template_files):
    """The template's addon.ltx and catalog/catalog.ltx with the fields of the request filled in.
    template_files: {path: text} -> {path: text}. The placeholders of the template: {{id}}, {{type}}, {{name}},
    {{summary}}, {{authors}}, {{maintainer}}, {{license}}, {{source}}, {{permission}}."""
    values = {
        "id": fields["addon_id"], "type": fields["type"].lower(), "name": _ltx_value(fields.get("name", "")),
        "summary": _ltx_value(fields.get("summary", "") or fields.get("name", "")),
        "authors": _ltx_value(fields.get("authors", "")), "maintainer": fields.get("maintainer", "").lstrip("@"),
        "license": fields.get("license") or catalog_meta.DEFAULT_LICENSE,
        "source": _ltx_value(fields.get("source", "")), "permission": _ltx_value(fields.get("permission", "")),
    }
    out = {}
    for path, text in template_files.items():
        for key, value in values.items():
            text = text.replace("{{%s}}" % key, value)
        out[path] = text
    return out


def comment_for(problems, fields):
    if problems:
        return ("The request cannot be approved yet:\n\n" + "\n".join("- " + p for p in problems) +
                "\n\nEdit the issue to fix it; the check runs again.")
    return ("The request looks fine: id `%s`, type `%s`, maintainer @%s. A moderator approves it with the label "
            "`approved`; then the bot creates the repo `%s` from the template and invites the maintainer." %
            (fields["addon_id"], fields["type"].lower(), fields["maintainer"].lstrip("@"), fields["addon_id"]))

