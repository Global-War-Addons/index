"""The hand-written ltx files of the index repo (11-catalog-distribution.md 4.1): source.ltx, moderation.ltx, tags.ltx,
reserved_ids.ltx, game_releases.ltx. All are read by the strict parser (ltx.py)."""

import os
import re

from . import ltx

SERVICE_IDS = ("index", "template", "sdk", "core")  # 11 R7 (+ "core": the winner name of the rules of addons.ltx)
SOURCE_ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ConfigError(ValueError):
    pass


def _read(path, required=True):
    if not os.path.isfile(path):
        if required:
            raise ConfigError("%s is missing" % path)
        return ltx.Ini()
    with open(path, "rb") as f:
        data = f.read()
    try:
        return ltx.parse(ltx.decode(data, path), url_safe=True)
    except ltx.LtxError as e:
        raise ConfigError("%s: %s" % (path, e))


class Reserved:
    def __init__(self, ids=(), prefixes=()):
        self.ids = set(SERVICE_IDS) | set(ids)
        self.prefixes = tuple(prefixes)

    def is_reserved(self, addon_id):
        return addon_id in self.ids or addon_id.startswith(self.prefixes)

    def why(self, addon_id):
        if addon_id in SERVICE_IDS:
            return "a service repo of the organization"
        if addon_id in self.ids:
            return "reserved_ids.ltx [reserved]"
        return "reserved prefix (reserved_ids.ltx [prefixes]: %s)" % ", ".join(self.prefixes)


class Moderation:
    def __init__(self):
        self.blocked = {}      # id -> reason
        self.revoked = {}      # id -> reason
        self.yanked = {}       # id -> set of versions
        self.exceptions = {}   # id -> set of codes
        self.revoked_sha256 = {}  # sha -> comment
        self.breaks_saves = {}  # id -> set of versions

    def status(self, addon_id):
        if addon_id in self.revoked:
            return "revoked"
        if addon_id in self.blocked:
            return "blocked"
        return "listed"


class IndexConfig:
    def __init__(self, root):
        self.root = root
        src = _read(os.path.join(root, "source.ltx"))
        sid = src.get("source", "id")
        if not sid or not SOURCE_ID_RE.match(sid):
            raise ConfigError("source.ltx [source] id must be a-z 0-9 -, up to 64 chars")
        self.source = {"id": sid, "name": {}, "homepage": src.get("source", "homepage") or ""}
        for key, value in src.items("source"):
            k = key.lower()
            if k == "name":
                self.source["name"][""] = value
            elif k.startswith("name_"):
                self.source["name"][k[5:]] = value
        self.org = src.get("github", "org") or ""
        self.index_repo = src.get("github", "index_repo") or "index"
        self.templates = {}
        for key in ("asset", "catalog", "index", "repo"):
            value = src.get("templates", key)
            if not value or not value.startswith("https://"):
                raise ConfigError("source.ltx [templates] %s must be an https:// template" % key)
            self.templates[key] = value
        self.mirrors = [k for k in src.keys("mirrors")]
        for mirror in self.mirrors:
            if not mirror.startswith("https://"):
                raise ConfigError("source.ltx [mirrors] %s is not https://" % mirror)
        self.snapshot_url = src.get("game", "snapshot_url") or ""
        self.official = src.get_bool("source", "official", False)

        rid = _read(os.path.join(root, "reserved_ids.ltx"))
        self.reserved = Reserved(rid.keys("reserved"), rid.keys("prefixes"))

        tags = _read(os.path.join(root, "tags.ltx"))
        self.tags = {k.lower() for k in tags.keys("tags")}

        self.moderation = read_moderation(os.path.join(root, "moderation.ltx"))

        rel = _read(os.path.join(root, "game_releases.ltx"), required=False)
        self.game_releases = []  # [(tag, date)] by date
        # tag -> (sha256 of base_snapshot.json.gz, of base_texts.tar.gz): what CI accepts as the snapshot of the tag
        # (review F13; make_base_snapshot.py prints the line). A line without them - the snapshot is taken as it is
        self.snapshot_digests = {}
        for tag, value in rel.items("releases"):
            parts = ltx.split_list(value)
            date = parts[0] if parts else ""
            digests = [p.lower() for p in parts[1:]]
            if ltx.parse_game_tag(tag) is None or not DATE_RE.match(date) or len(digests) not in (0, 2) or \
                    not all(SHA_RE.match(d) for d in digests):
                raise ConfigError("game_releases.ltx [releases] %s = %s: expected <release tag of the game> = "
                                  "YYYY-MM-DD[, <sha256 of base_snapshot.json.gz>, <sha256 of base_texts.tar.gz>] "
                                  "(v1.4.0, 0.5.5-patch4, 0.5.6-Beta)" % (tag, value))
            self.game_releases.append((tag, date))
            if digests:
                self.snapshot_digests[tag] = tuple(digests)
        self.game_releases.sort(key=lambda t: (t[1], ltx.parse_game_tag(t[0])))

    def game_release_for(self, published_date):
        """The last game release on the date of the publication (the guessed base, 15 section 7.2 step 1) or None"""
        best = None
        for tag, date in self.game_releases:
            if date <= published_date:
                best = tag
        return best

    def latest_game_release(self):
        return self.game_releases[-1][0] if self.game_releases else None


def read_moderation(path):
    ini = _read(path, required=False)
    m = Moderation()
    for key, value in ini.items("blocked"):
        m.blocked[key] = value
    for key, value in ini.items("revoked"):
        m.revoked[key] = value
    for key, value in ini.items("yanked"):
        m.yanked.setdefault(key, set()).update(ltx.split_list(value))
    for key, value in ini.items("breaks_saves"):
        m.breaks_saves.setdefault(key, set()).update(ltx.split_list(value))
    for key, value in ini.items("exceptions"):
        m.exceptions.setdefault(key, set()).update(ltx.split_list(value))
    for key, value in ini.items("revoked_sha256"):
        sha = key.lower()
        if not SHA_RE.match(sha):
            raise ConfigError("moderation.ltx [revoked_sha256] '%s' is not a sha256" % key)
        m.revoked_sha256[sha] = value
    return m
