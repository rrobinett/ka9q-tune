"""CPU lists are sets. Spelling must not change the answer."""

import unittest

from ka9q_tune import cpuset


class ParsingTest(unittest.TestCase):
    def test_range_and_list_spell_the_same_set(self):
        # The bug this exists to prevent: a drop-in renders 12-13 and a
        # hand-written command line carries 12,13, a string comparison calls
        # them different, and the station reboots forever chasing it.
        self.assertEqual(cpuset.parse("12-13"), cpuset.parse("12,13"))
        self.assertEqual(cpuset.parse("8-9"), frozenset({8, 9}))

    def test_mixed_and_whitespace(self):
        self.assertEqual(cpuset.parse(" 0-3, 8 ,12-13 "),
                         frozenset({0, 1, 2, 3, 8, 12, 13}))

    def test_empty_forms(self):
        for text in ("", None, "(null)", "   ", "none"):
            self.assertEqual(cpuset.parse(text), frozenset())

    def test_garbage_is_skipped_not_raised(self):
        self.assertEqual(cpuset.parse("8,banana,9"), frozenset({8, 9}))

    def test_format_collapses_runs(self):
        self.assertEqual(cpuset.format({8, 9}), "8,9")
        self.assertEqual(cpuset.format({8, 9, 10}), "8-10")
        self.assertEqual(cpuset.format({0, 1, 2, 8}), "0-2,8")
        self.assertEqual(cpuset.format(set()), "")

    def test_round_trip(self):
        for text in ("8-9", "12,13", "0-3,8,12-15"):
            self.assertEqual(cpuset.parse(cpuset.format(cpuset.parse(text))),
                             cpuset.parse(text))

    def test_equal_ignores_spelling(self):
        self.assertTrue(cpuset.equal("12-13", "12,13"))
        self.assertFalse(cpuset.equal("12-13", "12"))

    def test_mask(self):
        self.assertEqual(cpuset.to_mask({0, 1}), "00000003")
        self.assertEqual(cpuset.to_mask({8, 9}), "00000300")


if __name__ == "__main__":
    unittest.main()
