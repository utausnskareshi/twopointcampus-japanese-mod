"""tpcloc.text: Japanese detection, the protected markup tokens and the needs-translation decision."""
import unittest

from tpcloc import text


class Tokens(unittest.TestCase):
    TABLE = [
        # (string, expected sorted tokens)
        ("<non_vocal_audio=1.75>Music</non_vocal_audio>", ["</non_vocal_audio>", "<non_vocal_audio=1.75>"]),
        ('<style="Jingle"><non_vocal_audio=2></non_vocal_audio></style>',
         ["</non_vocal_audio>", "</style>", '<non_vocal_audio=2>', '<style="Jingle">']),
        ("Deposit [COUNT] Ghosts[i2p_One]Deposit Ghost", ["[COUNT]", "[i2p_One]"]),
        ("Will revert in [Time]...", ["[Time]"]),
        ("[i2p_Zero]a[i2p_One]b[i2p_Two]c[i2p_Few]d[i2p_Many]e", sorted(
            ["[i2p_Zero]", "[i2p_One]", "[i2p_Two]", "[i2p_Few]", "[i2p_Many]"])),
        ("[i2s_Male]x", ["[i2s_Male]"]),
        ('<sprite name="CoursePoints"> {POINTS} Course points', ['<sprite name="CoursePoints">', "{POINTS}"]),
        ("<sprite=3> and <sprite index=1>", ["<sprite index=1>", "<sprite=3>"]),
        ("{PLAYER.NAME} {0} {Num_2}", ["{0}", "{Num_2}", "{PLAYER.NAME}"]),
        ('<style="BodyBold">x</style><color=#FF0000>y</color><size=120%>z</size>',
         sorted(['<style="BodyBold">', "</style>", "<color=#FF0000>", "</color>", "<size=120%>", "</size>"])),
        ("<b>1</b><i>2</i><u>3</u>a<br>b", sorted(["<b>", "</b>", "<i>", "</i>", "<u>", "</u>", "<br>"])),
        ('<link="url">here</link>', ["</link>", '<link="url">']),
        # not tokens
        ("[HOLD] to build", []),  # translated officially
        ("[count] [time] [COUNTS]", []),
        ("{ COUNT } {a-b} {}", []),
        ("<Sprite name=x> <BR> <non_vocal_audio = 2>", []),
        ("line\\nbreak and real\nbreak", []),  # line breaks are checked separately (qa.py)
        ("plain text", []),
    ]

    def test_table(self):
        for s, want in self.TABLE:
            with self.subTest(s=s):
                self.assertEqual(text.tokens(s), want)

    def test_order_does_not_matter_but_count_does(self):
        self.assertEqual(text.tokens("{A} then {B}"), text.tokens("{B}は{A}"))
        self.assertNotEqual(text.tokens("{A} {A}"), text.tokens("{A}"))
        self.assertNotEqual(text.tokens("a[i2p_One]b"), text.tokens("ab"))

    def test_tokens_returns_sorted_list(self):
        r = text.tokens("{Z}{A}<b></b>")
        self.assertEqual(r, sorted(r))


class HasJapanese(unittest.TestCase):
    def test_table(self):
        for s, want in [("ひらがな", True), ("カタカナ", True), ("漢字", True), ("ｶﾀｶﾅ", True), ("ー", True),
                        ("Hello", False), ("", False), ("、。！？「」", False), ("↓ BGM LV{LEVEL}", False),
                        ("한국어", False), ("abc日def", True)]:
            with self.subTest(s=s):
                self.assertIs(text.has_japanese(s), want)


class NeedsTranslation(unittest.TestCase):
    TABLE = [
        # (english, official japanese, needs translation?)
        ("", None, False),
        ("   ", "", False),
        ("123 456", None, False),          # no word
        ("a", None, False),                # a single letter is not a word
        ("Hello world", None, True),       # missing
        ("Hello world", "", True),         # empty
        ("Hello world", "  ", True),       # blank
        ("Hello world", "こんにちは", False),  # official Japanese
        ("Hello world", "Hello world", True),  # official copy of the English
        ("Unused text", None, False),      # dev placeholders without any official value
        ("This is a placeholder", "", False),
        ("TODO: fix later", None, False),
        ("(PH) Title", None, False),
        ("SHOULDN'T SEE THIS", None, False),
        ("DOES THIS EXIST", None, False),
        ("Do the TODO list", None, True),  # TODO only at the start
        ("Unused text", "Unused text", True),  # placeholder words only matter when there is no value
        ("Down", "↓", False),              # symbol differs from English: translated
        ("Level {LEVEL}", "LV{LEVEL}", False),
        ("Now Loading...", "Now Loading...", True),
        ("Loading", "Now Loading...", False),  # differs, < 3 words: accepted as the official choice
        ("Hello there", "This is still English", True),  # >= 3 English words
        ("Track", "<non_vocal_audio=1.75></non_vocal_audio>", False),  # only tokens
        ("Track", "<non_vocal_audio=1.75>crowd cheering loudly</non_vocal_audio>", True),  # English inside tags
        ("Pts", '<sprite name="Very Long Sprite Name Here">', False),  # words inside a token do not count
        ("Wait", "Will revert in [Time]", True),
        ("Count", "[COUNT] items[i2p_One]item", False),
        ("Hi", "{PLAYER.NAME} {SCORE} {LEVEL}", False),
        ("Hello\\nWorld", "Hello\\nWorld", True),  # literal backslash-n, copy of English
        ("One\\nTwo", "Un\\nDeux", False),         # differs, "Un", "nDeux": 2 words
        ("One\\nTwo", "Un mot\\nde plus", True),  # "Un", "mot", "nde", "plus"
        ("Hello", "ｶﾀｶﾅ", False),
    ]

    def test_table(self):
        for en, ja, want in self.TABLE:
            with self.subTest(en=en, ja=ja):
                self.assertIs(text.needs_translation(en, ja), want)


class Patterns(unittest.TestCase):
    def test_word_re(self):
        self.assertEqual(text.WORD_RE.findall("It's a {COUNT} x yz"), ["It's", "COUNT", "yz"])

    def test_unused_re(self):
        for s in ("unused", "UNUSED thing", "(PH)", "placeholder", "TODO", "SHOULDN'T", "SHOULDN_T", "DOES IT EXIST"):
            with self.subTest(s=s):
                self.assertTrue(text.UNUSED_RE.search(s))
        for s in ("Used", "To do", "exist"):
            with self.subTest(s=s):
                self.assertFalse(text.UNUSED_RE.search(s))


if __name__ == "__main__":
    unittest.main()
