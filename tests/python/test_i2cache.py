"""tpcloc.i2cache: the I2 language-cache text format."""
import collections
import unittest

from . import helpers as h
from tpcloc import bundles, i2cache


@unittest.skipUnless(h.OFFICIAL_BUNDLE.exists(), "official bundle copy missing")
class OfficialBundleIdentity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.texts = bundles.read_texts(h.OFFICIAL_BUNDLE)

    def test_every_text_asset_round_trips_byte_for_byte(self):
        self.assertGreaterEqual(len(self.texts), 20)
        for name, t in self.texts.items():
            with self.subTest(name=name):
                pairs = i2cache.parse(t)
                self.assertGreater(len(pairs), 0)
                out = i2cache.dumps(pairs)
                self.assertEqual(out, t)
                self.assertEqual(out.encode("utf-8"), t.encode("utf-8"))

    def test_entry_count_matches_separators(self):
        for name, t in self.texts.items():
            with self.subTest(name=name):
                self.assertEqual(len(i2cache.parse(t)), t.count(i2cache.SEP) + 1)

    def test_known_duplicates_are_kept_in_order(self):
        pairs = i2cache.parse(self.texts["I2LS_Characters"])
        counts = collections.Counter(k for k, _ in pairs)
        dups = {k for k, n in counts.items() if n > 1}
        self.assertIn("Characters/DLC_Hospital/Archetypes/Doctor_M_Name", dups)
        d = i2cache.to_dict(self.texts["I2LS_Characters"])
        self.assertEqual(len(d), len(pairs) - sum(n - 1 for n in counts.values()))

    def test_values_with_equals_and_line_breaks_exist_and_survive(self):
        eq = lf = 0
        for t in self.texts.values():
            for k, v in i2cache.parse(t):
                self.assertNotIn("=", k)
                eq += "=" in v
                lf += "\n" in v
        self.assertGreater(eq, 100)  # e.g. every I2LS_VO_Tannoy line has <style="Tannoy">
        self.assertGreater(lf, 100)


class Synthetic(unittest.TestCase):
    def test_empty_text(self):
        self.assertEqual(i2cache.parse(""), [])
        self.assertEqual(i2cache.dumps([]), "")
        self.assertEqual(i2cache.to_dict(""), {})

    def test_split_at_first_equals_only(self):
        t = 'A=x=y[i2t]B=<style="Tannoy">a=b</style>[i2t]C='
        self.assertEqual(i2cache.parse(t), [("A", "x=y"), ("B", '<style="Tannoy">a=b</style>'), ("C", "")])
        self.assertEqual(i2cache.dumps(i2cache.parse(t)), t)

    def test_line_breaks_and_literal_backslash_n(self):
        t = "A=line1\nline2[i2t]B=lit\\nlit[i2t]C=\r\nCRLF\n"
        pairs = i2cache.parse(t)
        self.assertEqual(pairs[0], ("A", "line1\nline2"))
        self.assertEqual(pairs[1], ("B", "lit\\nlit"))
        self.assertEqual(pairs[2], ("C", "\r\nCRLF\n"))
        self.assertEqual(i2cache.dumps(pairs), t)

    def test_duplicates_last_wins_in_dict_but_all_kept_in_parse(self):
        t = "K=1[i2t]X=2[i2t]K=3"
        self.assertEqual(i2cache.parse(t), [("K", "1"), ("X", "2"), ("K", "3")])
        self.assertEqual(i2cache.to_dict(t), {"K": "3", "X": "2"})
        self.assertEqual(i2cache.dumps(i2cache.parse(t)), t)

    def test_empty_values_and_empty_keys(self):
        for t in ("A=", "A=[i2t]B=", "A=1[i2t]=orphan", "="):
            with self.subTest(t=t):
                self.assertEqual(i2cache.dumps(i2cache.parse(t)), t)

    def test_unicode_survives(self):
        t = "A=日本語　全角スペース[i2t]B=😀 emoji[i2t]C=ｶﾀｶﾅ"
        self.assertEqual(i2cache.dumps(i2cache.parse(t)), t)

    def test_chunk_without_equals_reads_as_empty_value(self):
        # Not present in the game data. parse() gives (key, ""), so dumps() writes "key=" (same meaning
        # for the game; the C# I2Cache keeps untouched chunks raw).
        t = "A=1[i2t]NoValue[i2t]B=2"
        self.assertEqual(i2cache.parse(t), [("A", "1"), ("NoValue", ""), ("B", "2")])
        self.assertEqual(i2cache.dumps(i2cache.parse(t)), "A=1[i2t]NoValue=[i2t]B=2")
        self.assertEqual(i2cache.parse(i2cache.dumps(i2cache.parse(t))), i2cache.parse(t))


if __name__ == "__main__":
    unittest.main()
