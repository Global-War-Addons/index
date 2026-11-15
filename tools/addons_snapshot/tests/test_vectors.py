"""gametag.py against the shared test vectors of the release tags (game_tags.json): the same file the engine
(GlobalWar/utils/testvectors/engine_stand.py: MergeParseTag / MergeCompareTags / ReleaseTagOlder) and the validator run.

The file lives with the validator: catalog/validator/testvectors in the GW workspace, tools/validator/testvectors in
the index repo (catalog/assemble.py). Neither found - skipped."""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

import gametag  # noqa: E402

CANDIDATES = [
    os.path.join(TOOLS, "..", "..", "..", "catalog", "validator", "testvectors"),  # GW workspace: GlobalWar/utils/addons_snapshot
    os.path.join(TOOLS, "..", "validator", "testvectors"),  # the index repo: tools/addons_snapshot
]


def vectors():
    for d in CANDIDATES:
        path = os.path.join(d, "game_tags.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    return None


class GameTagVectors(unittest.TestCase):
    def setUp(self):
        self.v = vectors()
        if self.v is None:
            self.skipTest("game_tags.json not found (no validator next to tools/addons_snapshot)")

    def test_parse(self):
        kinds = {"pre": gametag.PRE, "release": gametag.RELEASE, "patch": gametag.PATCH}
        for c in self.v["parse"]:
            e = c["expect"]
            want = None if e is None else gametag.GameTag(e["numbers"], kinds[e["kind"]], e["patch"], e["pre"])
            self.assertEqual(gametag.parse(c["in"]), want, msg=repr(c["in"]))
            self.assertEqual(gametag.is_tag(c["in"]), e is not None, msg=repr(c["in"]))

    def test_compare(self):
        for c in self.v["compare"]:
            self.assertEqual(gametag.compare(c["a"], c["b"]), c["expect"], msg=str(c))
            self.assertEqual(gametag.same(c["a"], c["b"]), c["expect"] == 0, msg=str(c))

    def test_older(self):
        for c in self.v["older"]:
            self.assertEqual(gametag.is_older(c["game_version"], c["tag"]), c["expect"], msg=str(c))


if __name__ == "__main__":
    unittest.main()
