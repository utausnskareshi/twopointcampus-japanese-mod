"""tools/qa.py: the checks on synthetic batches (--batch) and on translations/ja (report mode)."""
import json
import unittest

from . import helpers as h

SRC, VO = "I2LS_Test", "I2LS_VO_Tannoy"
ROWS = {
    SRC: [
        ("UI/Count", "Deposit [COUNT] Ghosts[i2p_One]Deposit Ghost", ""),
        ("UI/Param", "Hello {PLAYER.NAME}, you have {0} points", ""),
        ("UI/Style", '<style="BodyBold">Bold</style> and <sprite name="Kudosh"> money', ""),
        ("UI/Audio", "<non_vocal_audio=1.75>Music plays</non_vocal_audio>", ""),
        ("UI/Time", "Will revert in [Time] seconds", ""),
        ("UI/Literal", "Press\\nStart now", ""),
        ("UI/Lines", "First line\nSecond line", ""),
        ("UI/Plain", "Visit the Pastoral Care office", ""),
        ("UI/Markup", "Unknown <wave>markup</wave> here", ""),
        ("UI/Short_Name", "Wand", ""),
        ("UI/Official", "Already done", "済み"),
    ],
    VO: [("VO/1", '<style="Tannoy">PA System: Attention please, students</style>', "")],
}
GOOD = {
    f"{SRC}|UI/Count": "ゴーストを[COUNT]体預ける[i2p_One]ゴーストを預ける",
    f"{SRC}|UI/Param": "{PLAYER.NAME}さん、ポイントは{0}です",
    f"{SRC}|UI/Style": '<sprite name="Kudosh">のお金と<style="BodyBold">太字</style>',
    f"{SRC}|UI/Audio": "<non_vocal_audio=1.75>音楽</non_vocal_audio>",
    f"{SRC}|UI/Time": "[Time]秒後に戻ります",
    f"{SRC}|UI/Literal": "押して\\n開始",
    f"{SRC}|UI/Lines": "一行目↵二行目",
    f"{SRC}|UI/Plain": "学生支援室に行く",
    f"{SRC}|UI/Short_Name": "杖",
    f"{VO}|VO/1": '<style="Tannoy">構内放送：学生の皆さん、お知らせです</style>',
}


class QaBase(unittest.TestCase):
    def setUp(self):
        self.root = h.make_project(h.new_dir(self), ROWS, kinds={(SRC, "UI/Short_Name"): "name"},
                                   glossary_official={"Pastoral Care": "学生支援室"},
                                   glossary_new=[{"en": "Deposit Ghost", "ja": "ゴーストを預ける"}])

    def qa_batch(self, batch: dict):
        p = self.root / "work" / "batches" / "t.ja.json"
        h.write_json(p, batch)
        return h.run_tool("qa.py", "--batch", p, project=self.root)

    def one(self, ref: str, ja):
        """Run a batch where only `ref` differs from the good batch; return (result, messages for ref)."""
        b = {ref: ja}
        r = self.qa_batch(b)
        where = ref.replace("|", " | ")
        msgs = [l for l in r.out.splitlines() if where in l]
        return r, msgs


class BatchChecks(QaBase):
    def test_good_batch_is_clean(self):
        r = self.qa_batch(GOOD)
        self.assertEqual(r.code, 0, r)
        self.assertIn(f"t.ja.json: {len(GOOD)} entries  errors 0  warnings 0", r.out)
        self.assertEqual([l for l in r.out.splitlines() if l.startswith(("ERROR", "WARNING"))], [])

    def test_broken_tokens(self):
        cases = {
            f"{SRC}|UI/Param": "{PLAYER_NAME}さん、ポイントは{0}です",            # renamed parameter
            f"{SRC}|UI/Style": '<style="Bold">太字</style>と<sprite name="Kudosh">',  # changed style
            f"{SRC}|UI/Audio": "音楽",                                            # tags dropped
            f"{SRC}|UI/Time": "[time]秒後に戻ります",                              # case changed
            f"{SRC}|UI/Count": "ゴーストを{COUNT}体預ける[i2p_One]ゴーストを預ける",  # [COUNT] -> {COUNT}
        }
        for ref, ja in cases.items():
            with self.subTest(ref=ref):
                r, msgs = self.one(ref, ja)
                self.assertEqual(r.code, 1, r)
                self.assertTrue(any(m.startswith("ERROR") and "tokens differ" in m for m in msgs), r)

    def test_token_fix_note_turns_the_error_into_a_warning(self):
        r, msgs = self.one(f"{SRC}|UI/Time", {"ja": "まもなく戻ります", "note": "[token-fix] 英語の誤り"})
        self.assertEqual(r.code, 0, r)
        self.assertTrue(any(m.startswith("WARNING") and "tokens differ" in m for m in msgs), r)

    def test_tokens_may_move(self):
        r, _ = self.one(f"{SRC}|UI/Param", "ポイント{0}、{PLAYER.NAME}さん")
        self.assertEqual(r.code, 0, r)

    def test_plural_tags(self):
        for ja in ("ゴーストを[COUNT]体預ける", "[COUNT][i2p_One]a[i2p_One]b"):
            with self.subTest(ja=ja):
                r, msgs = self.one(f"{SRC}|UI/Count", ja)
                self.assertEqual(r.code, 1)
                self.assertTrue(any("plural tag count differs" in m for m in msgs), r)

    def test_literal_backslash_n_count(self):
        r, msgs = self.one(f"{SRC}|UI/Literal", "押して開始")
        self.assertEqual(r.code, 1)
        self.assertIn(f"ERROR {SRC} | UI/Literal: literal \\n count differs (1->0)", msgs)
        r, msgs = self.one(f"{SRC}|UI/Literal", "押して\\n\\n開始")
        self.assertIn(f"ERROR {SRC} | UI/Literal: literal \\n count differs (1->2)", msgs)
        # a real line break is no replacement for the literal one
        r, msgs = self.one(f"{SRC}|UI/Literal", "押して↵開始")
        self.assertEqual(r.code, 1)
        self.assertTrue(any("literal \\n count differs" in m for m in msgs))

    def test_line_break_marker_and_real_breaks(self):
        r, msgs = self.one(f"{SRC}|UI/Lines", "一行目二行目")
        self.assertEqual(r.code, 0)
        self.assertIn(f"WARNING {SRC} | UI/Lines: line breaks differ (LF 1->0, <br> 0->0)", msgs)
        r, msgs = self.one(f"{SRC}|UI/Lines", "一行目\n二行目")  # a JSON line break counts like ↵
        self.assertEqual(msgs, [])

    def test_half_width_punctuation(self):
        bad = ["学生支援室に行く!", "学生支援室に行く?", "学生支援室に行く...", "学生支援室に行く.", "学生支援室, 行く"]
        good = ["学生支援室に行く！", "学生支援室に行く…", "学生支援室ver1.5に行く", "学生支援室に行く。"]
        for ja in bad + good:
            with self.subTest(ja=ja):
                r, msgs = self.one(f"{SRC}|UI/Plain", ja)
                hit = any("half-width punctuation" in m for m in msgs)
                self.assertEqual(hit, ja in bad, r)
                self.assertEqual(r.code, 0)  # a warning only

    def test_empty_unknown_and_not_in_worklist(self):
        r = self.qa_batch({f"{SRC}|UI/Plain": "  ", f"{SRC}|UI/Nope": "x", "I2LS_Nope|K": "y",
                           f"{SRC}|UI/Official": "済み2"})
        self.assertEqual(r.code, 1)
        self.assertIn(f"ERROR {SRC} | UI/Plain: empty translation", r.out)
        self.assertIn(f"ERROR {SRC} | UI/Nope: key not in game data", r.out)
        self.assertIn("ERROR I2LS_Nope: unknown source file", r.out)
        self.assertIn(f"WARNING {SRC} | UI/Official: not in worklist", r.out)
        self.assertIn("t.ja.json: 1 entries  errors 3", r.out)

    def test_style_warnings(self):
        cases = [
            (f"{SRC}|UI/Plain", "Pastoral Careに行く", "English left in JA: ['Pastoral', 'Care']"),
            (f"{SRC}|UI/Plain", "支援室に行く", "glossary 'Pastoral Care' -> '学生支援室' not used"),
            (f"{VO}|VO/1", '<style="Tannoy">お知らせ：学生の皆さん</style>', "Tannoy line should start with 構内放送："),
            (f"{SRC}|UI/Markup", "不明な<wave>マークアップ</wave>", "markup in EN not covered by the token check"),
            (f"{SRC}|UI/Count", "ゴーストを[COUNT]体送る[i2p_One]送る", "glossary 'Deposit Ghost'"),
        ]
        for ref, ja, want in cases:
            with self.subTest(want=want):
                r, msgs = self.one(ref, ja)
                self.assertEqual(r.code, 0, r)
                self.assertTrue(any(m.startswith("WARNING") and want in m for m in msgs), r)

    def test_term_separator_is_an_error(self):
        """[i2t] would split the game's term table; the patcher rejects such data (FZ-07 fix)."""
        for ja in ("学生支援室[i2t]に行く", "[i2t]学生支援室に行く", "学生支援室に行く[i2t]UI/Other=注入"):
            with self.subTest(ja=ja):
                r, msgs = self.one(f"{SRC}|UI/Plain", ja)
                self.assertEqual(r.code, 1, r)
                self.assertIn(f"ERROR {SRC} | UI/Plain: contains the term separator [i2t]", msgs)
        r, msgs = self.one(f"{SRC}|UI/Plain", "学生支援室[i2p_One]に行く[I2T]")  # other tags are not the separator
        self.assertFalse(any("term separator" in m for m in msgs), r)

    def test_nul_is_an_error(self):
        """U+0000 (e.g. from a review sheet's _x0000_) is refused by the patcher and build_patch.py (fuzz finding
        nul_translation_blamed_on_game_file)."""
        for ja in ("学生支援室\0に行く", "\0学生支援室に行く", "学生支援室に行く\0", "\0"):
            with self.subTest(ja=ja):
                r, msgs = self.one(f"{SRC}|UI/Plain", ja)
                self.assertEqual(r.code, 1, r)
                self.assertIn(f"ERROR {SRC} | UI/Plain: contains U+0000 (NUL)", msgs)
        # only the character itself: escaped forms and other control characters are not reported as NUL
        for ja in ("学生支援室\\0に行く", "学生支援室\\u0000に行く", "学生支援室\x01に行く", "学生支援室\tに行く"):
            with self.subTest(ja=ja):
                r, msgs = self.one(f"{SRC}|UI/Plain", ja)
                self.assertFalse(any("U+0000" in m for m in msgs), r)

    def test_excel_escape_is_an_error(self):
        """A _x000D_ left by an Excel round trip would be shipped into the game text (RS2 follow-up in qa.py)."""
        for ja in ("学生支援室_x000D_\nに行く", "学生支援室に行く_x0009_", "学生支援室_x00e9_に行く"):
            with self.subTest(ja=ja):
                r, msgs = self.one(f"{SRC}|UI/Plain", ja)
                self.assertEqual(r.code, 1, r)
                self.assertTrue(any(m.startswith("ERROR") and "contains an Excel escape like _x000D_" in m for m in msgs), r)
        for ja in ("学生支援室_x00G0_に行く", "学生支援室x000D_に行く", "学生支援室_x000D に行く"):
            with self.subTest(ja=ja):
                r, msgs = self.one(f"{SRC}|UI/Plain", ja)
                self.assertFalse(any("Excel escape" in m for m in msgs), r)

    def test_excel_escape_in_the_english_too_is_not_an_error(self):
        root = h.make_project(h.new_dir(self, "qa_escape_en"),
                              {SRC: [("UI/Esc", "Literal _x000D_ in the source", "")]})
        p = root / "work" / "batches" / "t.ja.json"
        h.write_json(p, {f"{SRC}|UI/Esc": "原文にも _x000D_ がある"})
        r = h.run_tool("qa.py", "--batch", p, project=root)
        self.assertFalse(any("Excel escape" in l for l in r.out.splitlines()), r)
        self.assertNotIn("errors 1", r.out)

    def test_allowed_ascii_is_not_english(self):
        r, msgs = self.one(f"{SRC}|UI/Plain", "学生支援室でDLCとMODを使う")
        self.assertEqual(msgs, [], r)

    def test_ui_fit_info(self):
        r, msgs = self.one(f"{SRC}|UI/Short_Name", "とてもとても長い魔法の杖の名前です")
        self.assertEqual(r.code, 0)
        self.assertTrue(any(m.startswith("INFO") and "check UI fit" in m for m in msgs), r)

    def test_batch_does_not_write_the_report(self):
        self.qa_batch(GOOD)
        self.assertFalse((self.root / "work" / "qa_report.txt").exists())


class ReportMode(QaBase):
    def save(self, entries: dict):
        with h.project_paths(self.root):
            from tpcloc import translations
            keys = [k for k, _, _ in ROWS[SRC]]
            translations.save(SRC, entries, keys)

    def test_clean_report(self):
        self.save({"UI/Plain": h.entry("学生支援室に行く", "reviewed"), "UI/Time": h.entry("[Time]秒後に戻ります")})
        r = h.run_tool("qa.py", project=self.root)
        self.assertEqual(r.code, 0, r)
        self.assertIn("translated 2 / 11  errors 0  warnings 0", r.out)
        rep = (self.root / "work" / "qa_report.txt").read_text("utf-8")
        self.assertTrue(rep.startswith("translated 2 / 11; missing by category: {'Other': 9}"))
        self.assertIn("== ERRORS (0)", rep)

    def test_errors_and_bad_status(self):
        self.save({"UI/Plain": h.entry("学生支援室に行く", "approved"), "UI/Time": h.entry("すぐ戻ります")})
        r = h.run_tool("qa.py", project=self.root)
        self.assertEqual(r.code, 1, r)
        self.assertIn(f"ERROR {SRC} | UI/Plain: bad status 'approved'", r.out)
        self.assertIn(f"ERROR {SRC} | UI/Time: tokens differ", r.out)

    def test_separator_and_excel_escape_in_report_mode(self):
        self.save({"UI/Plain": h.entry("学生支援室[i2t]に行く"), "UI/Short_Name": h.entry("杖_x000D_\n")})
        r = h.run_tool("qa.py", project=self.root)
        self.assertEqual(r.code, 1, r)
        self.assertIn(f"ERROR {SRC} | UI/Plain: contains the term separator [i2t]", r.out)
        self.assertIn(f"ERROR {SRC} | UI/Short_Name: contains an Excel escape like _x000D_ (re-import the review sheet)", r.out)
        rep = (self.root / "work" / "qa_report.txt").read_text("utf-8")
        self.assertIn("contains the term separator [i2t]", rep)

    def test_nul_in_report_mode(self):
        self.save({"UI/Plain": h.entry("学生支援室\0に行く", "reviewed"), "UI/Time": h.entry("[Time]秒後に戻ります")})
        r = h.run_tool("qa.py", project=self.root)
        self.assertEqual(r.code, 1, r)
        self.assertIn("translated 2 / 11  errors 1  warnings 0", r.out)
        self.assertIn(f"ERROR {SRC} | UI/Plain: contains U+0000 (NUL)", r.out)
        rep = (self.root / "work" / "qa_report.txt").read_text("utf-8")
        self.assertIn(f"{SRC} | UI/Plain: contains U+0000 (NUL)", rep)

    def test_strict_fails_on_warnings(self):
        self.save({"UI/Plain": h.entry("学生支援室に行く!")})
        self.assertEqual(h.run_tool("qa.py", project=self.root).code, 0)
        r = h.run_tool("qa.py", "--strict", project=self.root)
        self.assertEqual(r.code, 1, r)

    def test_static_charset_info(self):
        (self.root / "work" / "notosans_jp_static_charset.txt").write_text("学生支援室に行く", "utf-8")
        self.save({"UI/Plain": h.entry("学生支援室に行く"), "UI/Short_Name": h.entry("杖")})
        r = h.run_tool("qa.py", project=self.root)
        rep = (self.root / "work" / "qa_report.txt").read_text("utf-8")
        self.assertIn("1 characters outside the static NotoSans_JP atlas (dynamic fallback needed): 杖", rep)


if __name__ == "__main__":
    unittest.main()
