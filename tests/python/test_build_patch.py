"""tools/build_patch.py: the patcher's translation data (escaping, references, filters, warnings)."""
import json
import random
import re
import shutil
import unittest

from . import helpers as h

SRC, OTHER = "I2LS_Test", "I2LS_Other"
LONG_OFFICIAL = "これはとても長い公式の日本語の文章で、三十文字以上あるはずです。確認のために使います。"
ROWS = {
    SRC: [
        ("K/Inline", "Hello world", ""),
        ("K/Esc", "Tab and back slash", "Tab and back slash"),
        ("K/Back\\slash key", "Key with backslash", ""),
        ("K/OfficialStart", "Start", "ゲーム開始"),
        ("K/Ref", "Start game", ""),
        ("K/Count", "Deposit [COUNT] Ghosts[i2p_One]Deposit Ghost", ""),
        ("K/Reviewed", "Reviewed text", ""),
        ("K/Edited", "Edited text", ""),
        ("K/Official", "Already done", "済み"),
        ("K/LongOfficial", "Long official text", LONG_OFFICIAL),
        ("K/Long", "Long text of ours", ""),
        ("K/Inline", "Hello world", ""),  # duplicate key in the game data
    ],
    OTHER: [("O/1", "Other thing", "")],
}
OURS = {
    SRC: {
        "K/Inline": h.entry("こんにちは世界"),
        "K/Esc": h.entry("タブ\tと\\バックスラッシュ\r\n改行\\nリテラル"),
        "K/Back\\slash key": h.entry("バックスラッシュのキー"),
        "K/Ref": h.entry("ゲーム開始"),
        "K/Count": h.entry("ゴーストを[COUNT]体預ける[i2p_One]ゴーストを預ける"),
        "K/Reviewed": h.entry("確認済みの訳", "reviewed"),
        "K/Edited": h.entry("修正済みの訳", "edited"),
        "K/Official": h.entry("済み（当パッチ）"),  # official translation exists: skipped with a NOTE
        "K/Long": h.entry(LONG_OFFICIAL.replace("使います", "使う")),
    },
    OTHER: {"O/1": h.entry("他のもの"), "O/Unknown": h.entry("ゲームに無い")},
}


class BuildBase(unittest.TestCase):
    def setUp(self):
        self.root = h.make_project(h.new_dir(self), ROWS, ours=OURS)
        self.out = self.root / "out" / "data.tsv"

    def build(self, *args, out=None):
        return h.run_tool("build_patch.py", "--out", out or self.out, *args, project=self.root)

    def strings(self):
        return json.loads((self.root / "work" / "extract" / "strings.json").read_text("utf-8"))

    def write_strings(self, data):
        h.write_json(self.root / "work" / "extract" / "strings.json", data)

    def data_lines(self):
        return self.out.read_text("utf-8").split("\n")


class Output(BuildBase):
    def test_header_and_lines(self):
        r = self.build("--version", "1.2.3")
        self.assertEqual(r.code, 0, r)
        raw = self.out.read_bytes()
        self.assertNotIn(b"\r\n", raw)
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        lines = raw.decode("utf-8").split("\n")
        self.assertEqual(lines[-1], "")  # ends with a newline
        self.assertEqual(lines[0], "#TPCJP-PATCH\t1")
        self.assertEqual(lines[1], "#patch_version\t1.2.3")
        self.assertEqual(lines[2], f"#game_version\t{h.GAME_VERSION}")
        self.assertEqual(lines[3], "#bundle_sha256\t" + "ab" * 32)
        self.assertRegex(lines[4], r"^#built\t\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        body = lines[6:-1]
        self.assertEqual(lines[5], f"#entries\t{len(body)}")
        self.assertEqual(len(body), 9)
        self.assertIn("9 entries (1 references to official text), patch version 1.2.3", r.out)
        self.assertIn("by status (written/translated): draft 7/8, reviewed 1/1, edited 1/1", r.out)

    def test_inline_and_reference_lines(self):
        self.build()
        lines = self.data_lines()
        self.assertIn(f"{SRC}\tK/Inline\t{h.base_hash('')}\tこんにちは世界", lines)
        self.assertIn(f"&{SRC}\tK/Ref\t{h.base_hash('')}\t{SRC}\tK/OfficialStart", lines)
        self.assertIn(f"{SRC}\tK/Esc\t{h.base_hash('Tab and back slash')}\t"
                      "タブ\\tと\\\\バックスラッシュ\\r\\n改行\\\\nリテラル", lines)
        self.assertIn(f"{SRC}\tK/Back\\\\slash key\t{h.base_hash('')}\tバックスラッシュのキー", lines)
        self.assertEqual(sum(1 for l in lines if "\tK/Inline\t" in l), 1)  # duplicate key written once
        # game order: sources and keys as in strings.json
        keys = [l.split("\t")[1] for l in lines if l and not l.startswith("#")]
        self.assertEqual(keys, ["K/Inline", "K/Esc", "K/Back\\\\slash key", "K/Ref", "K/Count", "K/Reviewed",
                                "K/Edited", "K/Long", "O/1"])

    def test_escaping_round_trip_through_the_patcher_parser(self):
        self.build()
        header, entries = h.cs_parse_patch_data(self.out.read_text("utf-8"))
        self.assertEqual(header["patch_version"], "1.0.0")
        self.assertEqual(int(header["entries"]), len(entries))
        ours = h.load_ours(self.root)
        strings = self.strings()
        for e in entries:
            with self.subTest(key=e["key"]):
                ja_official = strings["sources"][e["source"]]["langs"]["Japanese"][e["key"]]
                self.assertEqual(e["base"], h.base_hash(ja_official))
                if e["ja"] is None:
                    ref = strings["sources"][e["ref_source"]]["langs"]["Japanese"][e["ref_key"]]
                    self.assertEqual(ref, ours[e["source"]][e["key"]]["ja"])
                else:
                    self.assertEqual(e["ja"], ours[e["source"]][e["key"]]["ja"])

    def test_random_strings_escape_round_trip(self):
        bp = h.load_tool("build_patch")
        rnd = random.Random(1234)
        alphabet = ["\\", "\t", "\r", "\n", "n", "t", "r", "a", "日", "\\n", "\\\\", " ", "😀"]
        for _ in range(2000):
            s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 12)))
            e = bp.esc(s)
            self.assertNotIn("\t", e)
            self.assertNotIn("\n", e)
            self.assertNotIn("\r", e)
            self.assertEqual(h.cs_unescape(e), s, repr(s))
        self.assertEqual(bp.base_hash("abc"), h.base_hash("abc"))

    def test_deterministic_except_built(self):
        self.build()
        first = [l for l in self.data_lines() if not l.startswith("#built")]
        second_out = self.root / "out" / "again.tsv"
        self.build(out=second_out)
        second = [l for l in second_out.read_text("utf-8").split("\n") if not l.startswith("#built")]
        self.assertEqual(first, second)

    def test_notes_and_near_copy_warning(self):
        r = self.build()
        self.assertIn(f"NOTE {SRC}|K/Official: not in worklist (official translation exists); skipped", r.out)
        self.assertRegex(r.out, rf"WARNING {SRC}\|K/Long: nearly identical \(0\.9\d\) to official {SRC}\|K/LongOfficial")
        self.assertNotIn("O/Unknown", r.out)  # terms the game does not have are not written
        self.assertNotIn("O/Unknown", self.out.read_text("utf-8"))
        self.assertNotIn("worklist.json does not match", r.out)

    def test_status_filter(self):
        r = self.build("--status", "reviewed, edited")
        self.assertEqual(r.code, 0, r)
        keys = [l.split("\t")[1] for l in self.data_lines() if l and not l.startswith("#")]
        self.assertEqual(keys, ["K/Reviewed", "K/Edited"])
        self.assertIn("by status (written/translated): draft 0/8, reviewed 1/1, edited 1/1", r.out)
        self.assertIn("#entries\t2", self.data_lines())


class Validation(BuildBase):
    def test_bad_status_values(self):
        for value in ("bogus", "reviewed,bogus", "", "reviewed,", "Reviewed"):
            with self.subTest(value=value):
                r = self.build("--status", value)
                self.assertEqual(r.code, 1, r)
                self.assertIn("unknown --status value(s)", r.out)
                self.assertFalse(self.out.exists())

    def test_option_without_value(self):
        for args in (("--status",), ("--status", "--version", "1.0.0"), ("--version",)):
            with self.subTest(args=args):
                r = self.build(*args)
                self.assertEqual(r.code, 1, r)
                self.assertIn("needs a value", r.out)
                self.assertFalse(self.out.exists())

    def test_zero_entries(self):
        with h.project_paths(self.root):
            from tpcloc import translations
            for source in OURS:
                translations.save(source, {k: {**e, "status": "draft"} for k, e in OURS[source].items()})
        r = self.build("--status", "reviewed")
        self.assertEqual(r.code, 1, r)
        self.assertIn("0 entries selected with --status reviewed; nothing written", r.out)
        self.assertFalse(self.out.exists())

    def test_zero_entries_keeps_an_existing_output(self):
        self.build()
        before = self.out.read_bytes()
        shutil.rmtree(self.root / "translations" / "ja")
        (self.root / "translations" / "ja").mkdir()
        r = self.build()
        self.assertEqual(r.code, 1, r)
        self.assertEqual(self.out.read_bytes(), before)

    def test_tokens_differ_stops_the_build(self):
        with h.project_paths(self.root):
            from tpcloc import translations
            translations.save(OTHER, {"O/1": h.entry("他")})
            translations.save(SRC, {"K/Count": h.entry("ゴーストを預ける")})
        r = self.build()
        self.assertEqual(r.code, 1, r)
        self.assertIn(f"tokens differ in {SRC}|K/Count; run tools/qa.py", r.out)
        self.assertFalse(self.out.exists())
        with h.project_paths(self.root):
            translations.save(SRC, {"K/Count": h.entry("ゴーストを預ける", note="[token-fix] 英語の誤り")})
        r = self.build()
        self.assertEqual(r.code, 0, r)
        self.assertIn(f"{SRC}\tK/Count\t{h.base_hash('')}\tゴーストを預ける", self.data_lines())

    def test_term_separator_stops_the_build(self):
        """The patcher rejects data whose text contains [i2t] (FZ-07 fix), so build_patch.py stops before writing."""
        self.build()
        before = self.out.read_bytes()
        with h.project_paths(self.root):
            from tpcloc import translations
            translations.save(OTHER, {"O/1": h.entry("他の[i2t]もの")})
        r = self.build()
        self.assertEqual(r.code, 1, r)
        self.assertIn(f"{OTHER}|O/1 contains the term separator [i2t]; run tools/qa.py", r.out)
        self.assertEqual(self.out.read_bytes(), before)  # the previous output is kept
        fresh = self.root / "out" / "fresh.tsv"
        self.assertEqual(self.build(out=fresh).code, 1)
        self.assertFalse(fresh.exists())
        # the --status filter decides first: a draft with [i2t] does not stop a reviewed-only build
        r = self.build("--status", "reviewed", out=fresh)
        self.assertEqual(r.code, 0, r)
        self.assertNotIn("[i2t]", fresh.read_text("utf-8"))

    def test_nul_stops_the_build(self):
        """The patcher rejects data containing U+0000 (fuzz finding nul_translation_blamed_on_game_file: the bundle it
        wrote failed its own check and the game's file was blamed), so build_patch.py stops before writing, like [i2t]."""
        self.build()
        before = self.out.read_bytes()
        fresh = self.root / "out" / "fresh.tsv"
        with h.project_paths(self.root):
            from tpcloc import translations
            for ja in ("他の\0もの", "\0他のもの", "他のもの\0", "\0"):
                with self.subTest(ja=ja):
                    translations.save(OTHER, {"O/1": h.entry(ja)})
                    r = self.build()
                    self.assertEqual(r.code, 1, r)
                    self.assertIn(f"{OTHER}|O/1 contains U+0000 (NUL); run tools/qa.py", r.out)
                    self.assertEqual(self.out.read_bytes(), before)  # the previous output is kept
                    self.assertEqual(self.build(out=fresh).code, 1)
                    self.assertFalse(fresh.exists())
        # the --status filter decides first: a draft with U+0000 does not stop a reviewed-only build
        r = self.build("--status", "reviewed", out=fresh)
        self.assertEqual(r.code, 0, r)
        self.assertNotIn(b"\0", fresh.read_bytes())

    def test_nul_from_the_extract_stops_the_build(self):
        """Keys and the game version come from work/extract/strings.json: U+0000 there is refused too, before writing."""
        data = self.strings()
        data["game_version"] = "10.3\0"
        self.write_strings(data)
        r = self.build()
        self.assertEqual(r.code, 1, r)
        self.assertIn("U+0000 (NUL) in the data to write: '#game_version\\t10.3\\x00'; work/extract/strings.json looks "
                      "damaged: re-run tools/extract.py", r.out)
        self.assertFalse(self.out.exists())
        root = h.make_project(h.new_dir(self, "nul_key"), {SRC: [("K/Nul\0Key", "Hello world", "")]},
                              ours={SRC: {"K/Nul\0Key": h.entry("こんにちは世界")}})
        out = root / "out" / "data.tsv"
        r = h.run_tool("build_patch.py", "--out", out, project=root)
        self.assertEqual(r.code, 1, r)
        self.assertIn(f"U+0000 (NUL) in the data to write: '{SRC}\\tK/Nul\\x00Key\\t", r.out)
        self.assertFalse(out.exists())

    def test_version_value_is_validated(self):
        """R18: header values are unescaped by the patcher and recorded in its state file (key=value lines), so an
        escaped or real line break could forge a key there, and a backslash made the data unloadable
        (「翻訳データのエスケープが正しくありません」). --version takes letters, digits and . + _ - only."""
        for version in ("1.0\\ngame_version=FORGED", "1.0\\", "1.0\n", "1.0\t1", "1 0", "1/0", "版1", "1.0;x", ""):
            with self.subTest(version=version):
                r = self.build("--version", version)
                self.assertEqual(r.code, 1, r)
                self.assertIn(f"--version {version!r} is not valid: use letters, digits and . + _ - only", r.out)
                self.assertFalse(self.out.exists())
        for version in ("1.0.1", "2.0.0-beta.1+build_7", "v3"):
            with self.subTest(version=version):
                r = self.build("--version", version)
                self.assertEqual(r.code, 0, r)
                header, _ = h.cs_load_patch_data(self.out.read_bytes())
                self.assertEqual(header["patch_version"], version)

    def test_game_version_with_control_characters_stops_the_build(self):
        """The game version comes from work/extract/strings.json (version.txt) and is recorded by the patcher too."""
        self.build()
        before = self.out.read_bytes()
        for gv in ("10.3\ngame_version=FORGED", "10.3\r", "10.3\t1", "10.3\x85", "10.3 ", "10.3 ", "10.3\x7f",
                   "\x1b10.3"):
            with self.subTest(game_version=gv):
                data = self.strings()
                data["game_version"] = gv
                self.write_strings(data)
                r = self.build()
                self.assertEqual(r.code, 1, r)
                self.assertIn(f"control character in the game version {gv!r} of work/extract/strings.json; it looks "
                              "damaged: re-run tools/extract.py", r.out)
                self.assertEqual(self.out.read_bytes(), before)  # the previous output is kept

    def test_game_version_with_a_backslash_is_escaped(self):
        """The patcher unescapes header values: a raw backslash used to make the data unloadable."""
        data = self.strings()
        data["game_version"] = "10.3\\n+x\\"
        self.write_strings(data)
        r = self.build()
        self.assertEqual(r.code, 0, r)
        self.assertIn("#game_version\t10.3\\\\n+x\\\\", self.data_lines())
        header, _ = h.cs_load_patch_data(self.out.read_bytes())
        self.assertEqual(header["game_version"], "10.3\\n+x\\")

    def test_bundle_sha256_must_be_a_sha256(self):
        self.build()
        before = self.out.read_bytes()
        for sha in ("AB" * 32, "ab" * 31, "ab" * 32 + "\n", "ab" * 32 + "a", "", "xy" * 32):
            with self.subTest(sha=sha):
                data = self.strings()
                data["bundles"]["Japanese"]["sha256"] = sha
                self.write_strings(data)
                r = self.build()
                self.assertEqual(r.code, 1, r)
                self.assertIn(f"Japanese bundle sha256 {sha!r} in work/extract/strings.json is not 64 lower-case hex "
                              "digits; it looks damaged: re-run tools/extract.py", r.out)
                self.assertEqual(self.out.read_bytes(), before)

    def test_output_passes_the_patchers_strict_loader(self):
        """What build_patch.py writes is accepted by the patcher's stricter loader (UTF-8, #entries, keys, base hashes)."""
        r = self.build("--version", "2.0.0")
        self.assertEqual(r.code, 0, r)
        header, entries = h.cs_load_patch_data(self.out.read_bytes())
        self.assertEqual(int(header["entries"]), len(entries))
        self.assertEqual(len(entries), 9)
        for e in entries:
            self.assertRegex(e["base"], r"^[0-9a-f]{16}$")
            self.assertNotIn("[i2t]", e["key"] + (e["ja"] or ""))
        for args in (("--status", "reviewed"), ("--status", "edited,reviewed")):
            with self.subTest(args=args):
                self.assertEqual(self.build(*args).code, 0)
                header, entries = h.cs_load_patch_data(self.out.read_bytes())
                self.assertEqual(int(header["entries"]), len(entries))

    def test_stale_official_value_is_skipped(self):
        data = self.strings()
        data["sources"][SRC]["langs"]["Japanese"]["K/Inline"] = "公式のこんにちは"  # a game update translated it
        data["sources"][SRC]["langs"]["Japanese"]["K/Esc"] = "Tab and back slash (changed English copy)"
        self.write_strings(data)
        r = self.build()
        self.assertEqual(r.code, 0, r)
        self.assertIn(f"WARNING {SRC}|K/Inline: official value no longer needs translation; skipped", r.out)
        self.assertIn("WARNING work/worklist.json does not match work/extract/strings.json (2 terms differ); "
                      "run tools/worklist.py", r.out)
        lines = self.data_lines()
        self.assertFalse(any("\tK/Inline\t" in l for l in lines))
        # still needs translation (official value is English): written with the hash of the current value
        esc_line = next(l for l in lines if "\tK/Esc\t" in l)
        self.assertEqual(esc_line.split("\t")[2], h.base_hash("Tab and back slash (changed English copy)"))

    def test_missing_japanese_key_stops_the_build(self):
        data = self.strings()
        del data["sources"][SRC]["langs"]["Japanese"]["K/Reviewed"]
        self.write_strings(data)
        r = self.build()
        self.assertEqual(r.code, 1, r)
        self.assertIn(f"{SRC}|K/Reviewed is missing from the Japanese data in work/extract/strings.json", r.out)
        self.assertIn("re-run tools/extract.py", r.out)
        self.assertFalse(self.out.exists())

    def test_near_copy_thresholds(self):
        bp = h.load_tool("build_patch")
        official_index = {LONG_OFFICIAL: (SRC, "K/LongOfficial"), "短い公式": (SRC, "K/Short"),
                          "話者：" + LONG_OFFICIAL[:-1] + "★": (SRC, "K/Speaker")}
        found = bp.near_copies([(SRC, "A", LONG_OFFICIAL.replace("使います", "使う")),
                                (SRC, "B", "短い公式だ"),  # shorter than NEAR_COPY_MIN_LEN
                                (SRC, "C", "まったく関係のない、とても長い日本語の文章をここに書いておきます。おしまい。"),
                                (SRC, "D", '<style="X">' + LONG_OFFICIAL + "</style>")], official_index)
        self.assertEqual(len(found), 2, found)
        self.assertTrue(found[0].startswith(f"{SRC}|A: nearly identical"))
        self.assertTrue(found[1].startswith(f"{SRC}|D: nearly identical (1.00)"), found)
        self.assertEqual(bp.plain('話者：<b>一</b>\\n二 三'), "一二三")


@unittest.skipUnless(h.REAL_DATA.exists() and h.REAL_STRINGS.exists(), "real project data missing")
class RealData(unittest.TestCase):
    def test_rebuild_matches_dist_data_except_built(self):
        root = h.new_dir(self)
        (root / "work" / "extract").mkdir(parents=True)
        shutil.copyfile(h.REAL_STRINGS, root / "work" / "extract" / "strings.json")
        shutil.copyfile(h.PROJECT / "work" / "worklist.json", root / "work" / "worklist.json")
        shutil.copytree(h.PROJECT / "translations" / "ja", root / "translations" / "ja")
        real = h.REAL_DATA.read_text("utf-8")
        version = re.search(r"^#patch_version\t(.*)$", real, re.M).group(1)
        out = root / "out.tsv"
        r = h.run_tool("build_patch.py", "--version", version, "--out", out, project=root)
        self.assertEqual(r.code, 0, r)
        strip = lambda t: [l for l in t.split("\n") if not l.startswith("#built\t")]
        self.assertEqual(strip(out.read_text("utf-8")), strip(real))
        # the shipped data passes the patcher's stricter loader (C1 fixes: UTF-8, #entries, keys, [i2t], base hashes)
        header, entries = h.cs_load_patch_data(h.REAL_DATA.read_bytes())
        self.assertEqual(int(header["entries"]), len(entries))
        self.assertFalse(any("[i2t]" in (e["ja"] or "") for e in entries))
        self.assertNotIn("\0", real)  # the patcher refuses U+0000 anywhere in the data (round 3)


if __name__ == "__main__":
    unittest.main()
