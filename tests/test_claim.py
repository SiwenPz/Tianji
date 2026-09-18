"""The claim marker: minted, carried into a description, and stripped again."""
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "tianji" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import claim  # noqa: E402


class TokenTests(unittest.TestCase):
    def test_a_token_is_128_bits_of_base64url(self):
        token = claim.new_token()
        self.assertTrue(claim.is_well_formed(token), token)
        self.assertEqual(len(token), 22)

    def test_tokens_do_not_repeat(self):
        tokens = {claim.new_token() for _ in range(200)}
        self.assertEqual(len(tokens), 200)

    def test_a_token_is_not_its_digest(self):
        token = claim.new_token()
        self.assertNotEqual(claim.token_digest(token), token)

    def test_the_digest_is_stable_and_scoped_to_the_token(self):
        token = claim.new_token()
        self.assertEqual(claim.token_digest(token), claim.token_digest(token))
        self.assertNotEqual(claim.token_digest(token), claim.token_digest(claim.new_token()))

    def test_malformed_tokens_are_refused(self):
        for bad in ("", "short", "x" * 21, "x" * 23, "!" * 22, None, 42):
            self.assertFalse(claim.is_well_formed(bad), bad)
            with self.assertRaises(ValueError):
                claim.token_digest(bad)


class MarkerTests(unittest.TestCase):
    def test_a_marker_round_trips(self):
        token = claim.new_token()
        description = claim.attach_marker("写一个模块", token)
        self.assertEqual(claim.extract_marker(description), token)

    def test_the_marker_is_anchored_at_the_end(self):
        token = claim.new_token()
        # A description that merely mentions the shape mid-sentence is not a
        # claim: no marker means no identity.
        text = f"参考 {claim.format_marker(token)} 的写法，然后继续"
        self.assertIsNone(claim.extract_marker(text))

    def test_only_the_trailing_marker_is_used(self):
        first, second = claim.new_token(), claim.new_token()
        text = f"{claim.format_marker(first)} 正文 {claim.format_marker(second)}"
        self.assertEqual(claim.extract_marker(text), second)

    def test_stripping_removes_the_capability(self):
        token = claim.new_token()
        description = claim.attach_marker("写一个模块", token)
        stripped = claim.strip_marker(description)
        self.assertEqual(stripped, "写一个模块")
        self.assertNotIn(token, stripped)
        self.assertIsNone(claim.extract_marker(stripped))

    def test_attaching_replaces_an_existing_marker(self):
        first, second = claim.new_token(), claim.new_token()
        description = claim.attach_marker("任务", first)
        replaced = claim.attach_marker(description, second)
        self.assertEqual(claim.extract_marker(replaced), second)
        self.assertNotIn(first, replaced)

    def test_attaching_to_an_empty_description_still_works(self):
        token = claim.new_token()
        self.assertEqual(claim.extract_marker(claim.attach_marker("", token)), token)
        self.assertEqual(claim.extract_marker(claim.attach_marker(None, token)), token)

    def test_a_description_without_a_marker_is_untouched(self):
        self.assertEqual(claim.strip_marker("普通描述"), "普通描述")
        self.assertIsNone(claim.extract_marker("普通描述"))

    def test_marker_regex_rejects_partial_tokens(self):
        self.assertIsNone(claim.extract_marker("[TJ:short]"))
        self.assertIsNone(claim.extract_marker("[TJ:" + "x" * 22))
        self.assertIsNone(claim.extract_marker("[TJ:" + "x" * 21 + "]"))
        self.assertTrue(re.match(claim.MARKER_RE, claim.format_marker(claim.new_token())))


if __name__ == "__main__":
    unittest.main()
