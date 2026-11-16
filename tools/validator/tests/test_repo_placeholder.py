"""validate-repo: {{...}} placeholders are looked for in addon.ltx values only, not in comments."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import gw_catalog  # noqa: E402

TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(HERE)), "template", "addon.ltx")


class RepoPlaceholderTest(unittest.TestCase):
    # The template is next to the validator only in the GW workspace (catalog/template); the assembled index repo
    # (tools/validator, assemble.py --check, validate-pr.yml) has no template: skipped there (review F5)
    @unittest.skipUnless(os.path.isfile(TEMPLATE), "catalog/template is not next to the validator")
    def test_template_comment_is_not_a_placeholder(self):
        with open(TEMPLATE, "rb") as f:
            data = f.read()
        # The unfilled template still has {{id}} / {{type}} in values
        self.assertTrue(gw_catalog.has_placeholder_values(data))
        # The bot fills the values; the comment explaining {{...}} stays and must not fail the repo
        filled = data.replace(b"{{id}}", b"my_mod").replace(b"{{type}}", b"content")
        self.assertIn(b"{{", filled)
        self.assertFalse(gw_catalog.has_placeholder_values(filled))

    def test_comment_marks(self):
        self.assertFalse(gw_catalog.has_placeholder_values(b"[addon]\nid = a ; {{x}}\nname = b // {{y}}\n"))
        self.assertTrue(gw_catalog.has_placeholder_values(b"[addon]\nid = {{id}}\n"))


if __name__ == "__main__":
    unittest.main()
