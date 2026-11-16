"""ci/fetch_gw_merge.sh and ci/gw_merge_info.py of the index repo (decision of the developer 2026-11-14): gw_merge of the
latest release of the engine by default, its sha256 from the neighbour <asset>.sha256 of the same release, the variables
GW_MERGE_URL / GW_MERGE_SHA256 as optional overrides; the algo of that gw_merge in the Summary.

The script takes only https:// addresses, so the network is replaced by a fake `curl` first in PATH (it serves files of
a folder by their URL); the https check of the script itself is not weakened."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
import unittest

import util

CI = os.path.join(util.INDEX_SKELETON, "ci")
FETCH = os.path.join(CI, "fetch_gw_merge.sh")
INFO = os.path.join(CI, "gw_merge_info.py")
DEFAULT_URL = "https://github.com/ChugunovRoman/xray-16/releases/latest/download/gw_merge-linux-x86_64.tar.gz"
TAG_URL = "https://github.com/ChugunovRoman/xray-16/releases/download/1.2.3/gw_merge-linux-x86_64.tar.gz"
PINNED_URL = "https://github.com/ChugunovRoman/xray-16/releases/download/1.0.0/gw_merge-linux-x86_64.tar.gz"

# A fake curl: -I with -w '%{redirect_url}' prints $FAKE_REDIRECT (none - exit 22, as curl -f on a 404); a download
# serves $FAKE_WEB/<url without https://, / and : as _> to -o or stdout (none - exit 22). Every URL goes to
# $FAKE_WEB/requests.log with the mark HEAD / GET
FAKE_CURL = r"""#!/usr/bin/env bash
out=""; head=0; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -w|--proto|--max-filesize|--max-time) shift ;;
    https://*|http://*|file:*) url="$1" ;;
    --*) ;;
    -*) case "$1" in *I*) head=1 ;; esac ;;
  esac
  shift
done
if [ "$head" = 1 ]; then
  echo "HEAD $url" >> "$FAKE_WEB/requests.log"
  [ -n "${FAKE_REDIRECT:-}" ] || exit 22
  printf '%s' "$FAKE_REDIRECT"
  exit 0
fi
echo "GET $url" >> "$FAKE_WEB/requests.log"
key="$(printf '%s' "$url" | sed -e 's#^https://##' -e 's#[/:]#_#g')"
[ -f "$FAKE_WEB/$key" ] || exit 22
if [ -n "$out" ]; then cp "$FAKE_WEB/$key" "$out"; else cat "$FAKE_WEB/$key"; fi
"""


def find_bash():
    """bash of Git for Windows (not the bash.exe of WSL in System32), else bash of PATH"""
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            root = os.path.dirname(os.path.dirname(os.path.realpath(git)))
            # usr/bin/bash.exe, not the wrapper bin/bash.exe: the wrapper puts mingw64/bin (the real curl) first in PATH
            for cand in (os.path.join(root, "usr", "bin", "bash.exe"), os.path.join(root, "..", "usr", "bin", "bash.exe")):
                if os.path.isfile(cand):
                    return os.path.normpath(cand)
        return None
    return shutil.which("bash")


BASH = find_bash()


def key_of(url):
    return url[len("https://"):].replace("/", "_").replace(":", "_")


def archive(content=b"#!/bin/sh\necho gw_merge\n"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (("gw_merge", content), ("LuaJIT-COPYRIGHT", b"MIT\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@unittest.skipUnless(os.path.isfile(FETCH), "ci/fetch_gw_merge.sh is not here")
@unittest.skipUnless(BASH, "no bash (Git for Windows) here")
class FetchGwMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = util.TempDir()
        self.addCleanup(self.tmp.cleanup)
        self.web = os.path.join(self.tmp.path, "web")
        fake_bin = os.path.join(self.tmp.path, "fakebin")
        os.makedirs(self.web)
        os.makedirs(fake_bin)
        with open(os.path.join(fake_bin, "curl"), "w", encoding="utf-8", newline="\n") as f:
            f.write(FAKE_CURL)
        os.chmod(os.path.join(fake_bin, "curl"), 0o755)
        self.env = dict(os.environ, FAKE_WEB=self.web.replace("\\", "/"),
                        PATH=fake_bin + os.pathsep + os.environ.get("PATH", ""))
        self.env.pop("FAKE_REDIRECT", None)
        # never the network: the fake curl must be the one bash finds
        r = subprocess.run([BASH, "-c", "command -v curl"], env=self.env, capture_output=True, text=True, timeout=60)
        if not r.stdout.strip().endswith("fakebin/curl"):
            self.skipTest("bash does not take the fake curl first in PATH: %s" % r.stdout.strip())
        self.tgz = archive()
        self.sha = hashlib.sha256(self.tgz).hexdigest()

    def serve(self, url, data):
        with open(os.path.join(self.web, key_of(url)), "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)

    def run_fetch(self, url="", sha="", redirect=None):
        env = dict(self.env)
        if redirect:
            env["FAKE_REDIRECT"] = redirect
        # relative <dir> under the temp folder: GNU tar reads "C:/..." as host:path
        r = subprocess.run([BASH, FETCH.replace("\\", "/"), url, sha, "out"], cwd=self.tmp.path, env=env,
                           capture_output=True, text=True, timeout=60)
        return r

    def requests(self):
        p = os.path.join(self.web, "requests.log")
        if not os.path.isfile(p):
            return []
        with open(p, encoding="utf-8") as f:
            return f.read().splitlines()

    def out(self, *parts):
        return os.path.join(self.tmp.path, "out", *parts)

    def test_default_latest_with_the_sha256_of_the_release(self):
        """No variables: latest is resolved to its tag, the archive and its .sha256 come from that tag"""
        self.serve(TAG_URL, self.tgz)
        self.serve(TAG_URL + ".sha256", "%s  gw_merge-linux-x86_64.tar.gz\n" % self.sha.upper())
        r = self.run_fetch(redirect=TAG_URL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "out/gw_merge")
        self.assertTrue(os.path.isfile(self.out("gw_merge")))
        self.assertFalse(os.path.exists(self.out("dl")))
        with open(self.out("gw_merge.url"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), TAG_URL)
        self.assertEqual(self.requests(), ["HEAD " + DEFAULT_URL, "GET " + TAG_URL + ".sha256", "GET " + TAG_URL])

    def test_latest_not_resolved_uses_the_address_as_is(self):
        self.serve(DEFAULT_URL, self.tgz)
        self.serve(DEFAULT_URL + ".sha256", self.sha + "\r\n")
        r = self.run_fetch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.requests()[1:], ["GET " + DEFAULT_URL + ".sha256", "GET " + DEFAULT_URL])

    def test_no_sha256_file_and_no_variable_is_an_error(self):
        self.serve(TAG_URL, self.tgz)
        r = self.run_fetch(redirect=TAG_URL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("::error::no %s.sha256" % TAG_URL, r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertFalse(os.path.exists(self.out("gw_merge")))
        self.assertNotIn("GET " + TAG_URL, self.requests())  # the archive is not even downloaded

    def test_wrong_sha256_of_the_release_is_refused(self):
        self.serve(TAG_URL, self.tgz)
        self.serve(TAG_URL + ".sha256", "%s  gw_merge-linux-x86_64.tar.gz\n" % ("0" * 64))
        r = self.run_fetch(redirect=TAG_URL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not have the expected sha256", r.stderr)
        self.assertFalse(os.path.exists(self.out("gw_merge")))
        self.assertFalse(os.path.exists(self.out("dl")))

    def test_broken_sha256_file_is_an_error(self):
        self.serve(TAG_URL, self.tgz)
        for text in ("", "not a hash\n", self.sha[:63] + "  x\n", self.sha + "0  x\n", "<html>404</html>"):
            self.serve(TAG_URL + ".sha256", text)
            r = self.run_fetch(redirect=TAG_URL)
            self.assertEqual(r.returncode, 1, text)
            self.assertIn("does not start with a sha256", r.stderr, text)
            self.assertFalse(os.path.exists(self.out("gw_merge")), text)

    def test_variable_sha256_overrides_the_release(self):
        """vars.GW_MERGE_SHA256 is used instead of <url>.sha256 (not even requested); a wrong one is refused"""
        self.serve(PINNED_URL, self.tgz)
        self.serve(PINNED_URL + ".sha256", "0" * 64 + "  x\n")
        r = self.run_fetch(PINNED_URL, self.sha.upper())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.requests(), ["GET " + PINNED_URL])  # a pinned tag is not resolved
        shutil.rmtree(self.out())
        r = self.run_fetch(PINNED_URL, "ab" * 32)
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not have the expected sha256", r.stderr)
        self.assertFalse(os.path.exists(self.out("gw_merge")))
        r = self.run_fetch(PINNED_URL, "abc")
        self.assertEqual(r.returncode, 1)
        self.assertIn("vars.GW_MERGE_SHA256 must be the sha256", r.stderr)

    def test_pinned_url_takes_its_own_sha256(self):
        self.serve(PINNED_URL, self.tgz)
        self.serve(PINNED_URL + ".sha256", self.sha + "  gw_merge-linux-x86_64.tar.gz\n")
        r = self.run_fetch(PINNED_URL, redirect=TAG_URL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.requests(), ["GET " + PINNED_URL + ".sha256", "GET " + PINNED_URL])

    def test_sha256_without_url_warns(self):
        self.serve(TAG_URL, self.tgz)
        r = self.run_fetch("", self.sha, redirect=TAG_URL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("::warning::vars.GW_MERGE_SHA256 is set without vars.GW_MERGE_URL", r.stderr)

    def test_only_https(self):
        for url in ("http://example.com/gw_merge.tar.gz", "file:///tmp/gw_merge", "ftp://x/y"):
            r = self.run_fetch(url, self.sha)
            self.assertEqual(r.returncode, 1, url)
            self.assertIn("must be an https:// address", r.stderr)
        self.assertEqual(self.requests(), [])

    def test_archive_without_gw_merge_at_its_root(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("dist/gw_merge")
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
        data = buf.getvalue()
        self.serve(PINNED_URL, data)
        r = self.run_fetch(PINNED_URL, hashlib.sha256(data).hexdigest())
        self.assertEqual(r.returncode, 1)
        self.assertIn("has no gw_merge at its root", r.stderr)

    def test_parse_sha256(self):
        """The first 64 hex digits of the first line, any case; anything else is not a hash"""
        cases = [(self.sha.upper() + "  gw_merge-linux-x86_64.tar.gz\n", self.sha),
                 (self.sha + " *gw_merge-linux-x86_64.tar.gz\r\nsecond line\n", self.sha),
                 (self.sha, self.sha),
                 (self.sha + "\tname", self.sha),
                 (self.sha + "0", None), (self.sha[:-1], None), ("  " + self.sha, None), ("", None),
                 ("SHA256 (gw_merge-linux-x86_64.tar.gz) = " + self.sha, None)]
        for i, (text, want) in enumerate(cases):
            name = "s%d" % i
            with open(os.path.join(self.tmp.path, name), "w", encoding="utf-8", newline="") as f:
                f.write(text)
            r = subprocess.run([BASH, "-c", 'source "$1"; parse_sha256 "$2"', "x", FETCH.replace("\\", "/"), name],
                               cwd=self.tmp.path, env=self.env, capture_output=True, text=True, timeout=60)
            if want is None:
                self.assertNotEqual(r.returncode, 0, text)
                self.assertEqual(r.stdout, "", text)
            else:
                self.assertEqual((r.returncode, r.stdout.strip()), (0, want), text)

    def test_default_url(self):
        with open(FETCH, encoding="utf-8") as f:
            self.assertIn('DEFAULT_URL="%s"' % DEFAULT_URL, f.read())


def load_info():
    spec = importlib.util.spec_from_file_location("gw_merge_info", INFO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(os.path.isfile(INFO), "ci/gw_merge_info.py is not here")
class GwMergeInfoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = util.TempDir()
        self.info = load_info()
        t = self.tmp
        versions = {"1.0.0": {"status": "listed", "merge_files": ["a.ltx"]},
                    "1.1.0": {"status": "listed", "merge_files": ["a.ltx"], "recomputed": ["1.1.0.merge.gw-merge-3.json"]},
                    "1.2.0": {"status": "pending", "merge_files": ["a.ltx"]},
                    "1.3.0": {"status": "invalid", "merge_files": ["a.ltx"]},
                    "1.4.0": {"status": "listed"}}
        t.write("index/state/my_addon.json", json.dumps({"schema": 1, "id": "my_addon", "versions": versions}))
        for ver, algo in (("1.0.0", "gw-merge/2"), ("1.1.0", "gw-merge/2"), ("1.2.0", "gw-merge/3"),
                          ("1.3.0", "gw-merge/2")):
            t.write("index/files/my_addon/%s.json" % ver,
                    json.dumps({"merge": {"file": ver + ".merge.json", "algo": algo}}))
            t.write("index/files/my_addon/%s.merge.json" % ver, json.dumps({"format": 1, "algo": algo}))
        t.write("index/files/my_addon/1.1.0.merge.gw-merge-3.json", json.dumps({"format": 1, "algo": "gw-merge/3"}))
        t.write("index/files/my_addon/1.4.0.json", json.dumps({"id": "my_addon"}))
        self.index = os.path.join(t.path, "index")

    def tearDown(self):
        self.tmp.cleanup()

    def test_versions_without_the_algorithm(self):
        """As recompute-deltas counts them: listed / pending / yanked with merge files, the delta of <ver>.json and the
        recomputed ones"""
        self.assertEqual(self.info.versions_without(self.index, "gw-merge/3"), ["my_addon@1.0.0"])
        self.assertEqual(self.info.versions_without(self.index, "gw-merge/2"), ["my_addon@1.2.0"])
        self.assertEqual(self.info.versions_without(self.index, "gw-merge/4"),
                         ["my_addon@1.0.0", "my_addon@1.1.0", "my_addon@1.2.0"])
        self.assertEqual(self.info.versions_without(os.path.join(self.tmp.path, "none"), "gw-merge/3"), [])

    def test_report(self):
        exe = self.tmp.write("bin/gw_merge", "")
        self.tmp.write("bin/gw_merge.url", TAG_URL + "\n")
        version = {"algorithm": "gw-merge/3", "revision": 4, "delta_format": 1}
        orig = self.info.gw_merge_version
        self.info.gw_merge_version = lambda e: (version, None)
        try:
            log, md = self.info.report(exe, self.index, warn_stale=True)
            self.assertIn("gw_merge: " + TAG_URL, log)
            self.assertIn("gw_merge version: " + json.dumps(version, sort_keys=True), log)
            self.assertTrue(log[-1].startswith("::warning::1 published version(s) have no delta of gw-merge/3"), log)
            self.assertIn(TAG_URL, md)
            self.assertIn('"algorithm": "gw-merge/3"', md)
            log, md = self.info.report(exe, self.index)
            self.assertFalse(log[-1].startswith("::warning::"), log)
            self.info.gw_merge_version = lambda e: (None, "exit code 2")
            log, md = self.info.report(exe, self.index, warn_stale=True)
            self.assertTrue(log[-1].startswith("::warning::gw_merge version failed"), log)
        finally:
            self.info.gw_merge_version = orig
        log, md = self.info.report("", self.index)
        self.assertIn("not used in this run", md.lower())

    def test_main_writes_the_summary(self):
        summary = os.path.join(self.tmp.path, "summary.md")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.info.main(["", "--index", self.index, "--summary", summary]), 0)
        with open(summary, encoding="utf-8") as f:
            self.assertIn("### gw_merge", f.read())

    @unittest.skipUnless(os.environ.get("GW_MERGE") and os.path.isfile(os.environ.get("GW_MERGE", "")),
                         "no gw_merge ($GW_MERGE)")
    def test_real_gw_merge_version(self):
        info, why = self.info.gw_merge_version(os.environ["GW_MERGE"])
        self.assertIsNone(why)
        self.assertEqual(set(info) >= {"algorithm", "revision", "delta_format"}, True, info)


if __name__ == "__main__":
    unittest.main()
