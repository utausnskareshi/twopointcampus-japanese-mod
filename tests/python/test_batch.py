"""tools/batch.py export / import on a small synthetic project (temp folder, tpcloc.paths patched)."""
import json
import re
import unittest

from . import helpers as h

SRC, OTHER = "I2LS_Test", "I2LS_Other"
ROWS = {
    SRC: [
        ("UI/Plain", "Hello world", ""),
        ("UI/Lines", "First line\nSecond line\n", ""),
        ("UI/Literal", "Press\\nStart now", ""),
        ("UI/Count", "Deposit [COUNT] Ghosts[i2p_One]Deposit Ghost", ""),
        ("UI/Reviewed", "Reviewed text here", ""),
        ("UI/Edited", "Edited text here", ""),
        ("UI/Draft", "Draft text here", ""),
        ("UI/Official", "Done already", "済み"),
        ("UI/Wand_Name", "Magic Wand", ""),
        ("UI/Wand_Description", "A wand of\nmagic power", "魔法の\n杖"),
        ("Other/Pastoral", "Visit Pastoral Care today", ""),
    ],
    OTHER: [("O/1", "Other thing", ""), ("O/2", "Another thing", "")],
}
CATS = {(SRC, "UI/Wand_Name"): "Items", (SRC, "Other/Pastoral"): "Rooms"}
KINDS = {(SRC, "UI/Wand_Name"): "name", (SRC, "UI/Lines"): "sentence"}


def ours():
    return {SRC: {
        "UI/Reviewed": h.entry("確認済み", "reviewed", "n-r", review="memo R"),
        "UI/Edited": h.entry("修正済み", "edited", review="memo E"),
        "UI/Draft": h.entry("下書き", "draft", "old note", review="memo D"),
    }}


class BatchBase(unittest.TestCase):
    def setUp(self):
        self.root = h.make_project(
            h.new_dir(self), ROWS, ours=ours(), categories=CATS, kinds=KINDS,
            glossary_official={"Pastoral Care": "学生支援室", "Magic": "魔法", "Wand": "杖", "here": "ここ"},
            glossary_new=[{"en": "Ghosts", "ja": "ゴースト"}])

    def run_batch(self, *args):
        r = h.run_tool("batch.py", *args, project=self.root)
        return r

    def write_batch(self, name: str, obj) -> str:
        p = self.root / "work" / "batches" / name
        h.write_json(p, obj)
        return str(p)

    def saved(self, source=SRC):
        return json.loads((self.root / "translations" / "ja" / f"{source}.json").read_text("utf-8"))


class Export(BatchBase):
    def listing(self, name="b"):
        return (self.root / "work" / "batches" / f"{name}.txt").read_text("utf-8")

    def test_export_all(self):
        r = self.run_batch("export", "b")
        self.assertEqual(r.code, 0, r)
        n = len(json.loads((self.root / "work" / "worklist.json").read_text("utf-8")))
        self.assertEqual(n, 11)  # everything except UI/Official and UI/Wand_Description
        self.assertIn(f"exported {n} entries", r.out)
        t = self.listing()
        self.assertTrue(t.endswith("\n"))
        self.assertEqual(len(re.findall(r"^### ", t, re.M)), n)
        self.assertIn(f"### {SRC}|UI/Plain  [Other/label]\nEN: Hello world\n", t)
        self.assertIn(f"### {SRC}|UI/Lines  [Other/sentence]\nEN: First line↵Second line↵\n", t)
        self.assertIn("EN: Press\\nStart now\n", t)  # a literal backslash-n stays as typed
        self.assertNotIn("UI/Official", t)
        self.assertNotIn("ZH:", t)
        self.assertNotIn("CTX", t)

    def test_word_count(self):
        r = self.run_batch("export", "b", "--source", OTHER)
        self.assertIn("exported 2 entries (4 words)", r.out)

    def test_filters(self):
        cases = [
            (("--source", OTHER), [f"{OTHER}|O/1", f"{OTHER}|O/2"]),
            (("--source", f"{OTHER},{SRC}", "--prefix", "O/"), [f"{OTHER}|O/1", f"{OTHER}|O/2"]),
            (("--category", "Items,Rooms"), [f"{SRC}|UI/Wand_Name", f"{SRC}|Other/Pastoral"]),
            (("--kind", "name"), [f"{SRC}|UI/Wand_Name"]),
            (("--prefix", "UI/W"), [f"{SRC}|UI/Wand_Name"]),
            (("--source", SRC, "--offset", "1", "--limit", "2"), [f"{SRC}|UI/Lines", f"{SRC}|UI/Literal"]),
            (("--source", SRC, "--untranslated", "--prefix", "UI/"),
             [f"{SRC}|UI/{k}" for k in ("Plain", "Lines", "Literal", "Count", "Wand_Name")]),
        ]
        for args, want in cases:
            with self.subTest(args=args):
                r = self.run_batch("export", "f", *args)
                self.assertEqual(r.code, 0, r)
                got = re.findall(r"^### (\S+)", self.listing("f"), re.M)
                self.assertEqual(got, want)

    def test_refs_and_context(self):
        r = self.run_batch("export", "c", "--prefix", "UI/Wand", "--refs", "--context")
        self.assertEqual(r.code, 0, r)
        self.assertEqual(self.listing("c"),
                         f"### {SRC}|UI/Wand_Name  [Items/name]\n"
                         "EN: Magic Wand\n"
                         "ZH: [zh] Magic Wand\n"
                         "CTX: A wand of↵magic power\n"
                         "CTX-JA: 魔法の↵杖\n"
                         "GL: Magic=魔法; Wand=杖\n")

    def test_glossary_hits_rules(self):
        self.run_batch("export", "g", "--source", SRC)
        t = self.listing("g")
        block = lambda key: t.split(f"|{key} ")[1].split("###")[0]
        self.assertIn("GL: Pastoral Care=学生支援室", block("Other/Pastoral"))
        self.assertIn("GL: Ghosts=ゴースト", block("UI/Count"))  # glossary_new always counts
        self.assertNotIn("GL:", block("UI/Reviewed"))  # "here": lower case single word, not a term
        self.assertNotIn("GL:", block("UI/Plain"))

    def test_empty_selection_writes_an_empty_listing(self):
        r = self.run_batch("export", "e", "--source", "I2LS_None")
        self.assertEqual(r.code, 0, r)
        self.assertIn("exported 0 entries (0 words)", r.out)
        self.assertEqual(self.listing("e"), "\n")

    def test_export_does_not_touch_translations(self):
        before = h.snapshot_files(self.root / "translations")
        self.run_batch("export", "b", "--refs", "--context")
        self.assertEqual(h.snapshot_files(self.root / "translations"), before)


class Import(BatchBase):
    def test_marker_and_literal_backslash_n(self):
        p = self.write_batch("t.ja.json", {
            f"{SRC}|UI/Lines": "一行目↵二行目↵",
            f"{SRC}|UI/Literal": "押して\\n開始",
            f"{SRC}|UI/Plain": {"ja": "こんにちは\n世界", "note": "直接の改行"},
        })
        r = self.run_batch("import", p)
        self.assertEqual(r.code, 0, r)
        self.assertIn("imported 3 ({'I2LS_Test': 3}), skipped reviewed 0", r.out)
        self.assertNotIn("PROBLEM", r.out)
        s = self.saved()
        self.assertEqual(s["UI/Lines"], {"ja": "一行目\n二行目\n", "status": "draft", "note": ""})
        self.assertEqual(s["UI/Literal"]["ja"], "押して\\n開始")
        self.assertEqual(s["UI/Plain"], {"ja": "こんにちは\n世界", "status": "draft", "note": "直接の改行"})
        raw = (self.root / "translations" / "ja" / f"{SRC}.json").read_text("utf-8")
        self.assertIn('"押して\\\\n開始"', raw)
        self.assertIn('"一行目\\n二行目\\n"', raw)

    def test_saved_in_game_key_order(self):
        p = self.write_batch("t.ja.json", {f"{SRC}|Other/Pastoral": "学生支援室へ", f"{SRC}|UI/Plain": "やあ",
                                           f"{SRC}|UI/Wand_Name": "魔法の杖"})
        self.run_batch("import", p)
        self.assertEqual(list(self.saved()), ["UI/Plain", "UI/Reviewed", "UI/Edited", "UI/Draft", "UI/Wand_Name",
                                              "Other/Pastoral"])

    def test_reviewed_and_edited_are_kept_without_overwrite(self):
        p = self.write_batch("t.ja.json", {f"{SRC}|UI/Reviewed": "新1", f"{SRC}|UI/Edited": "新2",
                                           f"{SRC}|UI/Draft": "新3"})
        before = self.saved()
        r = self.run_batch("import", p)
        self.assertIn("imported 1 ({'I2LS_Test': 1}), skipped reviewed 2", r.out)
        s = self.saved()
        self.assertEqual(s["UI/Reviewed"], before["UI/Reviewed"])
        self.assertEqual(s["UI/Edited"], before["UI/Edited"])
        # a replaced draft keeps its reviewer memo and (no new note) its old note
        self.assertEqual(s["UI/Draft"], {"ja": "新3", "status": "draft", "note": "old note", "review": "memo D"})

    def test_overwrite_replaces_reviewed_and_keeps_review_memo(self):
        p = self.write_batch("t.ja.json", {f"{SRC}|UI/Reviewed": {"ja": "新1", "note": "新メモ"},
                                           f"{SRC}|UI/Edited": "新2"})
        r = self.run_batch("import", p, "--overwrite")
        self.assertIn("imported 2", r.out)
        self.assertIn("skipped reviewed 0", r.out)
        s = self.saved()
        self.assertEqual(s["UI/Reviewed"], {"ja": "新1", "status": "draft", "note": "新メモ", "review": "memo R"})
        self.assertEqual(s["UI/Edited"], {"ja": "新2", "status": "draft", "note": "", "review": "memo E"})

    def test_unknown_terms_are_reported_and_not_saved(self):
        p = self.write_batch("t.ja.json", {f"{SRC}|UI/Nope": "x", "I2LS_Nope|K": "y", "NoBar": "z",
                                           f"{OTHER}|O/1": "他"})
        before_src = (self.root / "translations" / "ja" / f"{SRC}.json").read_bytes()
        r = self.run_batch("import", p)
        self.assertEqual(r.lines("PROBLEM"), [f"PROBLEM unknown term {SRC}|UI/Nope", "PROBLEM unknown term I2LS_Nope|K",
                                              "PROBLEM unknown term NoBar"])
        self.assertIn("imported 1 ({'I2LS_Other': 1})", r.out)
        self.assertEqual((self.root / "translations" / "ja" / f"{SRC}.json").read_bytes(), before_src)
        self.assertEqual(self.saved(OTHER), {"O/1": h.entry("他")})
        self.assertFalse((self.root / "translations" / "ja" / "I2LS_Nope.json").exists())

    def test_token_problems_are_reported(self):
        p = self.write_batch("t.ja.json", {f"{SRC}|UI/Count": "ゴーストを預ける"})
        r = self.run_batch("import", p)
        self.assertEqual(len(r.lines("PROBLEM tokens differ")), 1, r)
        self.assertIn("['[COUNT]', '[i2p_One]']", r.out)

    def test_import_only_rewrites_changed_sources(self):
        p = self.write_batch("t.ja.json", {f"{OTHER}|O/2": "別のもの"})
        before = (self.root / "translations" / "ja" / f"{SRC}.json").read_bytes()
        self.run_batch("import", p)
        self.assertEqual((self.root / "translations" / "ja" / f"{SRC}.json").read_bytes(), before)
        self.assertEqual(self.saved(OTHER), {"O/2": h.entry("別のもの")})

    def test_import_of_same_batch_twice_is_stable(self):
        p = self.write_batch("t.ja.json", {f"{SRC}|UI/Lines": "一↵二↵", f"{SRC}|UI/Plain": "やあ"})
        self.run_batch("import", p)
        first = h.snapshot_files(self.root / "translations")
        self.run_batch("import", p)
        self.assertEqual(h.snapshot_files(self.root / "translations"), first)

    def test_bad_value_crashes_before_anything_is_saved(self):
        p = self.write_batch("t.ja.json", {f"{SRC}|UI/Plain": "やあ", f"{SRC}|UI/Lines": {"note": "no ja"}})
        before = h.snapshot_files(self.root / "translations")
        r = self.run_batch("import", p)
        self.assertNotEqual(r.code, 0)
        self.assertEqual(h.snapshot_files(self.root / "translations"), before)


class RoundTrip(BatchBase):
    def test_export_listing_to_batch_and_back_keeps_line_breaks(self):
        """A translator copying the EN lines verbatim (↵ and literal \\n) gets the exact English back."""
        self.run_batch("export", "rt", "--source", SRC)
        listing = (self.root / "work" / "batches" / "rt.txt").read_text("utf-8")
        batch, ref = {}, None
        for line in listing.splitlines():
            if line.startswith("### "):
                ref = line[4:].split("  [")[0]
            elif line.startswith("EN: "):
                batch[ref] = line[4:]
        p = self.write_batch("rt.ja.json", batch)
        r = self.run_batch("import", p, "--overwrite")
        self.assertEqual(r.code, 0, r)
        self.assertNotIn("PROBLEM", r.out)
        en = {k: v for k, v, _ in ROWS[SRC]}
        s = self.saved()
        for ref in batch:
            key = ref.split("|", 1)[1]
            with self.subTest(key=key):
                self.assertEqual(s[key]["ja"], en[key])


class Usage(BatchBase):
    def test_usage(self):
        for args in ((), ("export",), ("bogus", "x")):
            with self.subTest(args=args):
                r = self.run_batch(*args)
                self.assertEqual(r.code, 1)
                self.assertIn("python tools/batch.py export <name>", r.out)


if __name__ == "__main__":
    unittest.main()
