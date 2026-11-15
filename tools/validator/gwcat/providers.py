"""Where the index builder reads the repos of the organization and writes issues.

GitHubProvider - the REST API of GitHub (the token of the GitHub App of the catalog, 11 section 2). Package assets are
downloaded by their public browser_download_url (no token: a redirect to the storage of GitHub must not carry it).
LocalProvider - a folder that mimics an organization, for local runs and the tests:

    <root>/repos.json                     [{"name": "my_addon", "stars": 3, "rev": "<40 hex>"?, "archived": false,
                                            "repo_id": 123?}]
    <root>/<name>/head/...                the tree of the default branch (addon.ltx, catalog/ ...)
    <root>/<name>/releases.json           [{"id", "tag", "body", "draft", "prerelease", "published_at",
                                            "assets": [{"id", "name", "updated_at", "download_count"?, "uploader"?}]}]
    <root>/<name>/assets/<tag>/<asset>    the files of the assets (their size is the file size)
Issues of LocalProvider go to <root>/issues.json (a list), so the tests see them.
"""

import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

from . import catalog_meta, limits

API = "https://api.github.com"
UA = "gw-catalog-index"
# At most this many files of catalog/ are looked at (by name): a repo with thousands of files there costs nothing more
CATALOG_FILES_MAX = 200


class ProviderError(RuntimeError):
    pass


def _tree_files(prefixes, paths):
    return [p for p in paths if p == "addon.ltx" or any(p.startswith(x) for x in prefixes)]


def _catalog_limit(path):
    """The bytes a file of the repo the catalog reads may have (limits.py), None - a name catalog_meta does not read:
    only its name is passed on (catalog_unknown_file), it is never downloaded"""
    if path in ("addon.ltx", "catalog/catalog.ltx"):
        return limits.MANIFEST_MAX_BYTES
    if path == "catalog/icon.png":
        return limits.ICON_MAX_BYTES
    rel = path[len("catalog/"):]
    if catalog_meta.DESC_RE.match(rel):
        return limits.DESCRIPTION_MAX_BYTES
    if rel.startswith("screenshots/") and re.search(r"\.(png|jpe?g|webp)$", rel, re.I):
        return limits.SCREENSHOT_MAX_BYTES
    return None


def plan_tree(entries, prefixes):
    """[(path, size)] of the tree of a repo -> [(path, what)]: what = "read" (download it), "large" (over its limit:
    not downloaded, passed on as limit + 1 bytes - catalog_meta reports the size), "name" (not read by the catalog:
    passed on empty). review F8: the files of catalog/ are not all downloaded into memory any more"""
    out = []
    for path, size in sorted(entries):
        if path not in _tree_files(prefixes, [path]):
            continue
        if len(out) >= CATALOG_FILES_MAX:
            break
        limit = _catalog_limit(path)
        if limit is None:
            out.append((path, "name", 0))
        elif size > limit:
            out.append((path, "large", limit))
        else:
            out.append((path, "read", limit))
    return out


def placeholder(what, limit):
    """The bytes passed on for a file that is not downloaded (plan_tree)"""
    return b"\0" * (limit + 1) if what == "large" else b""


class LocalProvider:
    def __init__(self, root, org="Local-Addons"):
        self.root = root
        self.org = org
        self.issues_path = os.path.join(root, "issues.json")

    def _repos(self):
        with open(os.path.join(self.root, "repos.json"), encoding="utf-8") as f:
            return json.load(f)

    def list_repos(self):
        return [{"name": r["name"], "full_name": "%s/%s" % (self.org, r["name"]), "stars": r.get("stars", 0),
                 "archived": r.get("archived", False), "repo_id": r.get("repo_id")} for r in self._repos()]

    def _head_files(self, name):
        head = os.path.join(self.root, name, "head")
        files = {}
        for dirpath, _, filenames in os.walk(head):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                files[os.path.relpath(full, head).replace(os.sep, "/")] = full
        return files

    def head_rev(self, name):
        """The commit of the default branch: "rev" of repos.json, else a hash of the files of head/"""
        rev = next((r.get("rev") for r in self._repos() if r["name"] == name and r.get("rev")), None)
        if rev:
            return rev
        files = self._head_files(name)
        h = hashlib.sha1()
        for rel in sorted(files):
            with open(files[rel], "rb") as f:
                h.update(rel.encode("utf-8") + b"\0" + f.read())
        return h.hexdigest()

    def read_tree(self, name, prefixes=("catalog/",), rev=None):
        files = self._head_files(name)
        out = {}
        for rel, what, limit in plan_tree([(r, os.path.getsize(p)) for r, p in files.items()], prefixes):
            if what == "read":
                with open(files[rel], "rb") as f:
                    out[rel] = f.read(limit + 1)
            else:
                out[rel] = placeholder(what, limit)
        return rev or self.head_rev(name), out

    def list_releases(self, name):
        path = os.path.join(self.root, name, "releases.json")
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as f:
            rels = json.load(f)
        out = []
        for r in rels:
            assets = []
            for a in r.get("assets", []):
                fp = os.path.join(self.root, name, "assets", r["tag"], a["name"])
                assets.append({"id": a.get("id", a["name"]), "name": a["name"],
                               "size": os.path.getsize(fp) if os.path.isfile(fp) else a.get("size", 0),
                               "updated_at": a.get("updated_at", r.get("published_at", "")),
                               "download_count": a.get("download_count", 0), "uploader": a.get("uploader", ""),
                               "local_path": fp})
            out.append({"id": r.get("id", r["tag"]), "tag": r["tag"], "body": r.get("body", ""),
                        "draft": r.get("draft", False), "prerelease": r.get("prerelease", False),
                        "published_at": r.get("published_at", "2026-01-01T00:00:00Z"), "assets": assets})
        return out

    def download_asset(self, name, release, asset, dest):
        shutil.copyfile(asset["local_path"], dest)
        return dest

    def _issues(self):
        if os.path.isfile(self.issues_path):
            with open(self.issues_path, encoding="utf-8") as f:
                return json.load(f)
        return []

    def create_issue(self, repo, title, body, labels=()):
        issues = self._issues()
        issues.append({"number": len(issues) + 1, "repo": repo, "title": title, "body": body, "labels": list(labels),
                       "state": "open", "user": "bot"})
        with open(self.issues_path, "w", encoding="utf-8") as f:
            json.dump(issues, f, ensure_ascii=False, indent=2)
        return len(issues)

    def find_issue(self, repo, marker, labels=(), creator="bot"):
        """The number of an open issue of the bot in repo with <!-- marker --> in its body, None - none"""
        tag = "<!-- %s -->" % marker
        for issue in self._issues():
            if issue["repo"] == repo and issue.get("state", "open") == "open" and \
                    issue.get("user", "bot") == (creator or "bot") and tag in issue["body"]:
                return issue["number"]
        return None


# An ETag of GitHub as it is sent back in If-None-Match: a quoted string, never "*" (which matches any version of the
# resource: a cache file written by a run taken over by a package must not freeze the view of a repo)
ETAG_RE = re.compile(r'^(?:W/)?"[^"*\\]{1,200}"\Z')
HTTP_CACHE_SCHEMA = 1


def _repo_fields(r):
    return {k: r.get(k) for k in ("name", "full_name", "stargazers_count", "archived", "private", "default_branch",
                                  "id")}


def _release_fields(r):
    out = {k: r.get(k) for k in ("id", "tag_name", "body", "draft", "prerelease", "published_at")}
    out["assets"] = [{"id": a.get("id"), "name": a.get("name"), "size": a.get("size"),
                      "updated_at": a.get("updated_at"), "download_count": a.get("download_count", 0),
                      "uploader": {"login": (a.get("uploader") or {}).get("login", "")},
                      "browser_download_url": a.get("browser_download_url")} for a in r.get("assets") or []]
    return out


class GitHubProvider:
    """http_cache: a JSON file of the answers of the API with their ETags (build-index every 15 minutes, decision 63).
    A request repeated with If-None-Match that GitHub answers 304 costs no rate limit: a run over an organization that
    did not change reads its repos and releases nearly for free. Only the lists of repos, releases and the commit of a
    branch are cached, and an entry is used only when GitHub answers 304 to its ETag. The cache comes from an earlier
    run of the job that reads the packages: the workflow drops it once an hour (a run without it)."""

    def __init__(self, org, token, index_repo="index", http_cache=None):
        self.org = org
        self.token = token
        self.index_repo = index_repo
        self.http_cache = http_cache
        self.http_stats = {"requests": 0, "not_modified": 0}
        self._cache = {}
        self._used = {}
        if http_cache and os.path.isfile(http_cache):
            try:
                with open(http_cache, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get("schema") == HTTP_CACHE_SCHEMA and \
                        isinstance(data.get("entries"), dict):
                    self._cache = data["entries"]
            except (OSError, ValueError):
                self._cache = {}

    def save_http_cache(self):
        """Writes the entries used by this run (the ones of repos that are gone drop out)"""
        if not self.http_cache:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.http_cache)), exist_ok=True)
        tmp = self.http_cache + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"schema": HTTP_CACHE_SCHEMA, "entries": self._used}, f, ensure_ascii=False)
        os.replace(tmp, self.http_cache)

    def _get_cached(self, url, extract):
        """GET url (JSON of the API) -> (extract(data), the Link header). Conditional when the cache has its ETag"""
        entry = self._cache.get(url) if self.http_cache else None
        headers = {}
        if isinstance(entry, dict) and isinstance(entry.get("etag"), str) and ETAG_RE.match(entry["etag"]) and \
                "data" in entry:
            headers["If-None-Match"] = entry["etag"]
        else:
            entry = None
        self.http_stats["requests"] += 1
        data, resp_headers = self._request("GET", url, headers=headers)
        if data is None and entry is not None:  # 304: the cached answer is the current one
            self.http_stats["not_modified"] += 1
            self._used[url] = entry
            return entry["data"], entry.get("link") or ""
        value = extract(data)
        link = resp_headers.get("Link") or ""
        etag = resp_headers.get("ETag") or ""
        if self.http_cache and ETAG_RE.match(etag):
            self._used[url] = {"etag": etag, "data": value, "link": link}
        return value, link

    def _request(self, method, url, body=None, accept="application/vnd.github+json", auth=True, retries=3, raw=False,
                 limit=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Accept", accept)
            req.add_header("User-Agent", UA)
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
            for key, value in (headers or {}).items():
                req.add_header(key, value)
            if auth and self.token:  # not carried over a redirect to another host
                req.add_unredirected_header("Authorization", "Bearer " + self.token)
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    # limit: a raw file is never read past it (the tree said its size, the file may differ)
                    payload = r.read(limit) if limit is not None else r.read()
                    if raw or not payload:
                        return payload, r.headers
                    return json.loads(payload), r.headers
            except urllib.error.HTTPError as e:
                if e.code == 304 and headers and "If-None-Match" in headers:
                    return None, e.headers  # not modified: the caller takes its cached answer
                if e.code in (500, 502, 503, 504) or (e.code == 403 and e.headers.get("Retry-After")):
                    time.sleep(int(e.headers.get("Retry-After") or 5 * (attempt + 1)))
                    continue
                raise ProviderError("%s %s: HTTP %d %s" % (method, url, e.code, e.read()[:300]))
            except urllib.error.URLError as e:
                if attempt + 1 < retries:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise ProviderError("%s %s: %s" % (method, url, e))
        raise ProviderError("%s %s: retries exhausted" % (method, url))

    def _paged(self, url, item=None):
        """All pages of a list of the API. item: the fields of one element to keep (the pages are then read through
        the HTTP cache, conditional requests)"""
        out = []
        while url:
            if item is not None:
                data, link = self._get_cached(url, lambda page: [item(x) for x in page])
            else:
                data, headers = self._request("GET", url)
                link = headers.get("Link") or ""
            out.extend(data)
            url = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part[part.find("<") + 1:part.find(">")]
        return out

    def list_repos(self):
        repos = self._paged("%s/orgs/%s/repos?per_page=100&type=all" % (API, self.org), _repo_fields)
        # repo_id: a repo deleted and made again under the same name is another repo (index_build, review F6)
        return [{"name": r["name"], "full_name": r["full_name"], "stars": r.get("stargazers_count", 0),
                 "archived": r.get("archived", False), "private": r.get("private", False),
                 "default_branch": r.get("default_branch", "master"), "repo_id": r.get("id")} for r in repos]

    def head_rev(self, name):
        """The commit of the default branch: one API call (the catalog is read again only when it changes)"""
        commit, _ = self._get_cached("%s/repos/%s/%s/commits/HEAD" % (API, self.org, name),
                                     lambda c: {"sha": c["sha"]})
        return commit["sha"]

    def read_tree(self, name, prefixes=("catalog/",), rev=None):
        repo = "%s/%s" % (self.org, name)
        rev = rev or self.head_rev(name)
        tree, _ = self._request("GET", "%s/repos/%s/git/trees/%s?recursive=1" % (API, repo, rev))
        if tree.get("truncated"):
            raise ProviderError("%s: the tree is too large for the API" % repo)
        entries = [(t["path"], int(t.get("size") or 0)) for t in tree.get("tree", []) if t["type"] == "blob"]
        out = {}
        for p, what, limit in plan_tree(entries, prefixes):
            if what != "read":
                out[p] = placeholder(what, limit)
                continue
            url = "https://raw.githubusercontent.com/%s/%s/%s" % (repo, rev, urllib.parse.quote(p))
            data, _ = self._request("GET", url, accept="*/*", raw=True, limit=limit + 1)
            out[p] = data
        return rev, out

    def list_releases(self, name):
        rels = self._paged("%s/repos/%s/%s/releases?per_page=100" % (API, self.org, name), _release_fields)
        out = []
        for r in rels:
            out.append({"id": r["id"], "tag": r["tag_name"], "body": r.get("body") or "", "draft": r["draft"],
                        "prerelease": r["prerelease"], "published_at": r.get("published_at") or "",
                        "assets": [{"id": a["id"], "name": a["name"], "size": a["size"], "updated_at": a["updated_at"],
                                    "download_count": a.get("download_count", 0),
                                    "uploader": (a.get("uploader") or {}).get("login", ""),
                                    "url": a["browser_download_url"]} for a in r.get("assets", [])]})
        return out

    def download_asset(self, name, release, asset, dest):
        req = urllib.request.Request(asset["url"], headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=600) as r, open(dest + ".part", "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        os.replace(dest + ".part", dest)
        return dest

    def create_issue(self, repo, title, body, labels=()):
        data, _ = self._request("POST", "%s/repos/%s/%s/issues" % (API, self.org, repo),
                                {"title": title, "body": body[:60000], "labels": list(labels)})
        return data["number"]

    def find_issue(self, repo, marker, labels=(), creator=None):
        """An open issue of creator (the bot of the catalog) in repo with <!-- marker --> in its body -> its number,
        None - none. Only the issues of the bot count: anybody can open an issue with the marker"""
        if not creator:
            return None
        tag = "<!-- %s -->" % marker
        query = {"state": "open", "creator": creator, "per_page": "100"}
        if labels:
            query["labels"] = labels[0]
        url = "%s/repos/%s/%s/issues?%s" % (API, self.org, repo, urllib.parse.urlencode(query))
        for issue in self._paged(url):
            if "pull_request" not in issue and tag in (issue.get("body") or ""):
                return issue["number"]
        return None
