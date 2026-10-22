"""tools/lookup.py: search by key / English / Japanese over a synthetic project."""
import unittest

from . import helpers as h

SRC, OTHER = "I2LS_Test", "I2LS_Other"
ROWS = {
    SRC: [
        ("UI/Hello", "Hello\nworld", ""),
        ("UI/Official", "Done already", "済み\n完了"),
        ("UI/Copy", "Same as English", "Same as English"),
        ("UI/Hello", "Hello\nworld", ""),  # duplicate key: shown once
        ("UI/Mine", "Mine text", ""),
    ],
    OTHER: [("O/Hello", "Other hello", "")],
}


class Lookup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = h.TEMP_ROOT / "t" / "lookup_cls"
        import shutil
        shutil.rmtree(cls.root, ignore_errors=True)
        cls.root.mkdir(parents=True)
        if not h.KEEP:
            cls.addClassCleanup(shutil.rmtree, cls.root, True)
        h.make_project(cls.root, ROWS, ours={SRC: {"UI/Mine": h.entry("私の\n訳", "reviewed"),
                                                   "UI/Hello": h.entry("こんにちは")}})

    def look(self, *args):
        r = h.run_tool("lookup.py", *args, project=self.root)
        self.assertEqual(r.code, 0, r)
        return r.out

    def test_key_search_default_case_insensitive(self):
        out = self.look("ui/HELLO")
        self.assertEqual(out, f"### {SRC}|UI/Hello\nEN: Hello↵world\nJA*(draft): こんにちは\n")

    def test_official_and_ours_lines(self):
        out = self.look("Official|Mine|Copy")
        self.assertEqual(out,
                         f"### {SRC}|UI/Official\nEN: Done already\nJA: 済み↵完了\n"
                         f"### {SRC}|UI/Copy\nEN: Same as English\n"  # an English copy is not shown as JA
                         f"### {SRC}|UI/Mine\nEN: Mine text\nJA*(reviewed): 私の↵訳\n")

    def test_english_search(self):
        out = self.look("hello", "--en")
        self.assertEqual([l for l in out.splitlines() if l.startswith("###")],
                         [f"### {SRC}|UI/Hello", f"### {OTHER}|O/Hello"])
        self.assertEqual(self.look("^world", "--en"), "")  # no multiline flag: ^ is the start of the text

    def test_japanese_search_official_and_ours(self):
        self.assertIn(f"### {SRC}|UI/Official", self.look("完了", "--ja"))
        self.assertIn(f"### {SRC}|UI/Mine", self.look("私の", "--ja"))
        self.assertEqual(self.look("Same as", "--ja"), "")  # English copies are not Japanese

    def test_source_filter_and_limit(self):
        out = self.look("hello", "--en", "--source", OTHER)
        self.assertEqual([l for l in out.splitlines() if l.startswith("###")], [f"### {OTHER}|O/Hello"])
        out = self.look(".", "--limit", "2")
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("###")]), 2)
        self.assertTrue(out.endswith("... (limit 2; use --limit N)\n"))

    def test_usage(self):
        r = h.run_tool("lookup.py", project=self.root)
        self.assertEqual(r.code, 1)
        self.assertIn("usage: python tools/lookup.py", r.out)


if __name__ == "__main__":
    unittest.main()
