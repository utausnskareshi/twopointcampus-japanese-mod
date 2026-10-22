"""tools/extract.py and tools/worklist.py on a sandbox game made of read-only copies of the installed
game's bundles; outputs must equal work/extract/strings.json, work/worklist.json, work/keep_english.json,
work/glossary_official.tsv, work/notosans_jp_static_charset.txt and the official bundle copy in work/backup/official.
Plus worklist rules on synthetic data, and
extract.py's game version (round 3c, S18) on a sandbox game made of copies of the official Japanese bundle.
"""
import contextlib
import io
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from . import helpers as h
from tpcloc import official, paths


def real_game_dir() -> Path | None:
    with mock.patch.dict(os.environ):
        os.environ.pop("TPC_GAME_DIR", None)
        try:
            g = paths.find_game_dir()
        except FileNotFoundError:
            return None
    return g if (paths.bundles_dir(g) / h.JA_BUNDLE_NAME).exists() else None


REAL_GAME = real_game_dir()
REAL_FILES = ["worklist.json", "keep_english.json", "glossary_official.tsv"]


def copy_game(dst: Path, *, japanese: str = "installed") -> Path:
    """Copy (read only) every language bundle, the fonts bundle and version.txt of the installed game.

    japanese: "installed" = the installed bundle with its .tpcjp-original/.tpcjp-state as they are,
              "official"  = only the official copy from work/backup/official (no backup, no state).
    """
    src = paths.bundles_dir(REAL_GAME)
    h.make_game(dst, japanese=None)
    out = dst / h.BUNDLE_REL
    shutil.copyfile(REAL_GAME / "version.txt", dst / "version.txt")
    names = [f"localisation_assets_localisation{s}.bundle" for l, s in paths.BUNDLE_LANGS.items() if l != "Japanese"]
    for name in names + ["fonts_assets_all.bundle"]:
        shutil.copyfile(src / name, out / name)
    ja = src / h.JA_BUNDLE_NAME
    if japanese == "installed":
        for suffix in ("", official.BACKUP_SUFFIX, official.STATE_SUFFIX):
            if (src / (ja.name + suffix)).exists():
                shutil.copyfile(src / (ja.name + suffix), out / (ja.name + suffix))
    else:
        shutil.copyfile(h.OFFICIAL_BUNDLE, out / ja.name)
    return dst


@unittest.skipUnless(REAL_GAME and h.REAL_STRINGS.exists() and h.OFFICIAL_BUNDLE.exists(),
                     "installed game / work/extract/strings.json missing")
class AgainstRealExtract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = h.TEMP_ROOT / "t" / "extract_cls"
        shutil.rmtree(cls.base, ignore_errors=True)
        cls.base.mkdir(parents=True)
        if not h.KEEP:
            cls.addClassCleanup(shutil.rmtree, cls.base, True)
        cls.game = copy_game(cls.base / "game")
        cls.project = cls.base / "proj"
        (cls.project / "work").mkdir(parents=True)
        # translations are needed by the content check of tpcloc.official (is_patched)
        shutil.copytree(h.PROJECT / "translations" / "ja", cls.project / "translations" / "ja")
        cls.before_game = h.snapshot_files(cls.game)
        cls.extract = h.run_tool("extract.py", project=cls.project, game=cls.game)
        cls.worklist = h.run_tool("worklist.py", project=cls.project)

    def real_strings_text(self):
        return h.REAL_STRINGS.read_text("utf-8")

    def test_extract_succeeds_and_game_is_untouched(self):
        self.assertEqual(self.extract.code, 0, self.extract)
        self.assertIn("installed bundle is our patched output", self.extract.out)
        self.assertNotIn("WARNING", self.extract.out)
        self.assertEqual(h.snapshot_files(self.game), self.before_game)

    def test_strings_json_identical_except_game_dir(self):
        new = (self.project / "work" / "extract" / "strings.json").read_text("utf-8")
        real = self.real_strings_text()
        new_dir = json.loads(new)["game_dir"]
        real_dir = json.loads(real)["game_dir"]
        self.assertEqual(new_dir, str(self.game))
        swapped = new.replace(json.dumps(new_dir)[1:-1], json.dumps(real_dir)[1:-1], 1)
        self.assertEqual(swapped, real)
        # byte level too (same line endings: text-mode write_text)
        raw_new = (self.project / "work" / "extract" / "strings.json").read_bytes()
        raw_new = raw_new.replace(json.dumps(new_dir)[1:-1].encode("utf-8"), json.dumps(real_dir)[1:-1].encode("utf-8"), 1)
        self.assertEqual(raw_new, h.REAL_STRINGS.read_bytes())

    def test_official_bundle_copy_saved(self):
        # the backup in the sandbox game is the official file: the tests and test tools start from this copy
        copy = self.project / "work" / "backup" / "official" / h.JA_BUNDLE_NAME
        self.assertEqual(h.sha256_file(copy), h.sha256_file(h.OFFICIAL_BUNDLE))
        self.assertFalse(copy.with_name(copy.name + ".tmp").exists())
        self.assertIn(f"official Japanese bundle -> {copy}", self.extract.out)

    def test_static_charset_identical(self):
        real = h.PROJECT / "work" / "notosans_jp_static_charset.txt"
        if not real.exists():
            self.skipTest("no charset file")
        self.assertEqual((self.project / "work" / "notosans_jp_static_charset.txt").read_bytes(), real.read_bytes())

    def test_worklist_outputs_identical(self):
        self.assertEqual(self.worklist.code, 0, self.worklist)
        for name in REAL_FILES:
            with self.subTest(name=name):
                self.assertEqual((self.project / "work" / name).read_bytes(), (h.REAL_WORK / name).read_bytes())
        n = len(json.loads((h.REAL_WORK / "worklist.json").read_text("utf-8")))
        self.assertIn(f"worklist: {n} entries", self.worklist.out)


@unittest.skipUnless(REAL_GAME and h.REAL_STRINGS.exists() and h.OFFICIAL_BUNDLE.exists(),
                     "installed game / work/extract/strings.json missing")
class SandboxVariants(unittest.TestCase):
    def project(self, root, translations=True):
        (root / "work").mkdir(parents=True)
        if translations:
            shutil.copytree(h.PROJECT / "translations" / "ja", root / "translations" / "ja")
        return root

    def test_official_bundle_without_backup(self):
        root = h.new_dir(self)
        game = copy_game(root / "game", japanese="official")
        proj = self.project(root / "proj")
        r = h.run_tool("extract.py", project=proj, game=game)
        self.assertEqual(r.code, 0, r)
        self.assertIn("no backup; the installed bundle is the official one", r.out)
        new = json.loads((proj / "work" / "extract" / "strings.json").read_text("utf-8"))
        real = json.loads(h.REAL_STRINGS.read_text("utf-8"))
        self.assertEqual(new["bundles"]["Japanese"], {"file": h.JA_BUNDLE_NAME, "sha256": h.sha256_file(h.OFFICIAL_BUNDLE)})
        self.assertEqual(new["sources"], real["sources"])
        self.assertEqual({k: v for k, v in new["bundles"].items() if k != "Japanese"},
                         {k: v for k, v in real["bundles"].items() if k != "Japanese"})
        copy = proj / "work" / "backup" / "official" / h.JA_BUNDLE_NAME
        self.assertEqual(h.sha256_file(copy), h.sha256_file(h.OFFICIAL_BUNDLE))

    def test_patched_bundle_without_backup_stops(self):
        root = h.new_dir(self)
        game = copy_game(root / "game")
        b = h.ja_bundle(game)
        official.backup_path(b).unlink()
        official.state_path(b).unlink(missing_ok=True)
        proj = self.project(root / "proj")
        r = h.run_tool("extract.py", project=proj, game=game)
        self.assertEqual(r.code, 1, r)
        self.assertIn("error: ", r.out)
        self.assertIn("of our translations but", r.out)
        self.assertFalse((proj / "work" / "extract" / "strings.json").exists())
        self.assertFalse((proj / "work" / "backup").exists())

    def test_no_game_found(self):
        root = h.new_dir(self)
        proj = self.project(root / "proj", translations=False)
        r = h.run_tool("extract.py", project=proj)  # TPC_GAME_DIR -> a folder that does not exist
        self.assertNotEqual(r.code, 0)
        self.assertFalse((proj / "work" / "extract" / "strings.json").exists())


@unittest.skipUnless(h.OFFICIAL_BUNDLE.exists(), "official bundle copy missing")
class ExtractGameVersion(unittest.TestCase):
    """S18 (round 3c): extract.py reads version.txt like TpcJpPatch.exe (tpcloc.paths.game_version): no traceback when
    it cannot be read, no BOM in strings.json (and so in the patch data's #game_version). In-process on a sandbox
    game whose every language bundle is a copy of the official Japanese one (the fonts step is skipped)."""

    def setUp(self):
        root = h.new_dir(self)
        self.project = h.make_project(root / "proj", {})
        self.strings = self.project / "work" / "extract" / "strings.json"
        self.strings.unlink()
        self.game = h.make_game(root / "game", japanese=None)
        for lang in paths.BUNDLE_LANGS:
            shutil.copyfile(h.OFFICIAL_BUNDLE, paths.loc_bundle(self.game, lang))
        self.version_txt = self.game / "version.txt"
        ctx = h.project_paths(self.project)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.ex = h.load_tool("extract")
        self.ex.write_static_charset = lambda game: None

    def run_extract(self) -> tuple[int, str]:
        out, code, msg = io.StringIO(), 0, ""
        with mock.patch.object(paths, "find_game_dir", lambda: h.assert_under_temp(self.game)), \
                contextlib.redirect_stdout(out):
            try:
                self.ex.main()
            except SystemExit as e:
                code, msg = (1, e.code) if isinstance(e.code, str) else (e.code or 0, "")
        return code, out.getvalue() + msg

    def test_game_version_is_read_like_the_patcher(self):
        for raw, want in ((h.GAME_VERSION.encode("utf-8"), h.GAME_VERSION),
                          (b"\xef\xbb\xbf" + h.GAME_VERSION.encode("utf-8") + b"\r\n", h.GAME_VERSION),
                          (b" 10.4.0\r\nbuild 1234\r\n", "10.4.0  build 1234")):
            with self.subTest(raw=raw):
                self.version_txt.write_bytes(raw)
                code, out = self.run_extract()
                self.assertEqual(code, 0, out)
                self.assertIn("no backup; the installed bundle is the official one", out)
                self.assertEqual(json.loads(self.strings.read_text("utf-8"))["game_version"], want)
                self.assertIn(f"game {want})", out)

    def test_folder_at_the_state_path_is_no_state_file(self):
        """S19: extract.py used to stop with a PermissionError traceback."""
        official.state_path(paths.loc_bundle(self.game, "Japanese")).mkdir()
        code, out = self.run_extract()
        self.assertEqual(code, 0, out)
        self.assertIn("no backup; the installed bundle is the official one", out)

    def test_unreadable_or_empty_version_txt_stops_before_writing(self):
        """It used to stop with a FileNotFoundError / PermissionError traceback (or record an empty version)."""
        for how in ("missing", "folder", "empty", "access denied"):
            with self.subTest(how=how):
                if self.version_txt.is_dir():
                    self.version_txt.rmdir()
                self.version_txt.unlink(missing_ok=True)
                if how == "folder":
                    self.version_txt.mkdir()
                elif how != "missing":
                    self.version_txt.write_bytes(b" \r\n" if how == "empty" else h.GAME_VERSION.encode("utf-8"))
                real = Path.read_bytes

                def denied(p, *args, **kwargs):
                    if p.name == "version.txt" and how == "access denied":
                        raise PermissionError(13, "Access is denied", str(p))
                    return real(p, *args, **kwargs)
                with mock.patch.object(Path, "read_bytes", denied):
                    code, out = self.run_extract()
                self.assertEqual(code, 1, out)
                self.assertEqual(out, f"error: the game version cannot be read from {self.version_txt} (missing, empty "
                                      "or unreadable); verify the game files in Steam")
                self.assertFalse(self.strings.exists())

    def test_japanese_bundle_in_a_format_the_patcher_does_not_read_stops(self):
        """Round 3c (parity with the patcher's unsupported-format rule): UnityPy reads a UnityFS 9 file, the patcher
        refuses it; extract.py stops instead of extracting it as official data."""
        raw = h.OFFICIAL_BUNDLE.read_bytes()
        ja = paths.loc_bundle(self.game, "Japanese")
        ja.write_bytes(raw[:8] + (9).to_bytes(4, "big") + raw[12:])
        code, out = self.run_extract()
        self.assertEqual(code, 1, out)
        self.assertIn(f"error: {ja.name} is in a format TpcJpPatch.exe does not read (UnityFS version 9); verify the game "
                      "files in Steam, and if that does not help, the patch needs an update", out)
        self.assertFalse(self.strings.exists())


class CheckJapaneseKeys(unittest.TestCase):
    def test_warnings(self):
        ex = h.load_tool("extract")
        import io
        import contextlib
        sources = {
            "Same": {"keys": ["a", "b"], "langs": {"Japanese": {"a": "", "b": ""}}},
            "NoJa": {"keys": ["a"], "langs": {}},
            "Diff": {"keys": ["a", "b"], "langs": {"Japanese": {"a": "", "c": ""}}},
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ex.check_japanese_keys(sources)
        out = buf.getvalue().splitlines()
        self.assertEqual(out[0], "WARNING: NoJa: no Japanese data")
        self.assertTrue(out[1].startswith("WARNING: Diff: 1 English terms missing from Japanese, 1 extra (e.g. ['b'])"))
        self.assertEqual(len(out), 2)


class WorklistRules(unittest.TestCase):
    """worklist.py on a synthetic strings.json: selection, keep_english reasons, categories, kinds, glossary."""

    @classmethod
    def setUpClass(cls):
        cls.wl = h.load_tool("worklist")
        cls.root = h.TEMP_ROOT / "t" / "worklist_cls"
        shutil.rmtree(cls.root, ignore_errors=True)
        cls.root.mkdir(parents=True)
        if not h.KEEP:
            cls.addClassCleanup(shutil.rmtree, cls.root, True)
        rows = {
            "I2LS_Test": [
                ("UI/Mods/Title", "Mod browser heading", ""),
                ("Need/Hospital_Desc", "Treat the patients well", ""),
                ("Same/All", "Same everywhere", ""),
                ("UI/Frontend/Copyright_Text", "Copyright notice text", ""),
                ("Misc/KeyCode/Space", "Space bar", ""),
                ("OK/Button", "OK", ""),
                ("Quest/Placeholder_Title", "Quest Title", ""),
                ("Blank/Asian", "Only western languages", ""),
                ("Done/Official", "Already done", "済み"),
                ("Gloss/Name", "Magic Wand", "魔法の杖"),
                ("Gloss/Name2", "Magic Wand", "魔法のつえ"),
                ("Gloss/Name3", "Magic Wand", "魔法の杖"),
                ("Gloss/Sentence", "This is a sentence.", "文です。"),
                ("Gloss/Long_Text", "Six words are far too many here", "六語"),
                ("Need/Ghost_Name", "Spooky", ""),
                ("Need/Ghost_Name", "Spooky", ""),  # duplicate key
            ],
            "I2LS_Fonts": [("Font/Main", "NotoSans", "")],
        }
        h.make_project(cls.root, rows)
        strings = json.loads((cls.root / "work" / "extract" / "strings.json").read_text("utf-8"))
        src = strings["sources"]["I2LS_Test"]["langs"]
        for lang in h.REF_LANGS:
            src[lang]["Same/All"] = "Same everywhere"
        for lang in ("Chinese (Simplified)", "Korean"):
            src[lang]["Blank/Asian"] = ""
        h.write_json(cls.root / "work" / "extract" / "strings.json", strings)
        cls.r = h.run_tool("worklist.py", project=cls.root)
        cls.work = {w["key"]: w for w in json.loads((cls.root / "work" / "worklist.json").read_text("utf-8"))}
        cls.keep = {w["key"]: w for w in json.loads((cls.root / "work" / "keep_english.json").read_text("utf-8"))}

    def test_run(self):
        self.assertEqual(self.r.code, 0, self.r)
        self.assertIn("worklist: 3 entries", self.r.out)

    def test_selection(self):
        self.assertEqual(sorted(self.work), ["Need/Ghost_Name", "Need/Hospital_Desc", "UI/Mods/Title"])
        w = self.work["Need/Hospital_Desc"]
        self.assertEqual(w, {"source": "I2LS_Test", "key": "Need/Hospital_Desc", "en": "Treat the patients well",
                             "ja_official": "", "category": "DLC_Hospital", "kind": "text",
                             "zh": "[zh] Treat the patients well", "ko": "[ko] Treat the patients well",
                             "fr": "[fr] Treat the patients well", "de": "[de] Treat the patients well"})
        self.assertEqual(self.work["UI/Mods/Title"]["category"], "Mods")
        self.assertEqual(self.work["UI/Mods/Title"]["kind"], "title")
        self.assertEqual(self.work["Need/Ghost_Name"]["category"], "DLC_Ghost")
        self.assertEqual(self.work["Need/Ghost_Name"]["kind"], "name")

    def test_keep_english_reasons(self):
        self.assertNotIn("reason", self.keep["Same/All"])
        self.assertEqual(self.keep["UI/Frontend/Copyright_Text"]["reason"],
                         "legal notice: an unofficial translation must not reword it")
        self.assertEqual(self.keep["Misc/KeyCode/Space"]["reason"],
                         "key names / abbreviations stay in English (as in the official JA)")
        self.assertEqual(self.keep["OK/Button"]["reason"], "key names / abbreviations stay in English (as in the official JA)")
        self.assertEqual(self.keep["Quest/Placeholder_Title"]["reason"], "placeholder text")
        self.assertEqual(self.keep["Blank/Asian"]["reason"], "blank in zh/ko")
        self.assertNotIn("Font/Main", self.keep)
        self.assertNotIn("Font/Main", self.work)

    def test_glossary(self):
        lines = (self.root / "work" / "glossary_official.tsv").read_text("utf-8").splitlines()
        self.assertEqual(lines, ["en\tja\tcount\talternatives", "Already done\t済み\t1\t",
                                 "Magic Wand\t魔法の杖\t2\t魔法のつえ(1)"])

    def test_category_and_kind_functions(self):
        c, k = self.wl.category, self.wl.kind
        self.assertEqual(c("S", "UI/Mods/X"), "Mods")
        self.assertEqual(c("S", "X/UI/Mods"), "Other")
        self.assertEqual(c("S", "Rooms/Haunted_Name"), "DLC_Ghost")
        self.assertEqual(c("S", "Event/CM2_Goal"), "ChallengeMode")
        self.assertEqual(c("S", "Item/Christmas_Tree"), "Seasonal")
        self.assertEqual(c("S", "Food/Crumley_Pie"), "Cafeteria")
        self.assertEqual(c("S", "Hospital/Ghost"), "DLC_Hospital")  # first rule wins
        self.assertEqual(k("I2LS_CharacterNames", "x", "y"), "person_name")
        self.assertEqual(k("I2LS_VO_Tannoy", "x", "y"), "tannoy")
        self.assertEqual(k("I2LS_VO_Radio", "x", "y"), "radio")
        self.assertEqual(k("S", "Item/Name", "y"), "name")
        self.assertEqual(k("S", "Item/Header", "y"), "title")
        self.assertEqual(k("S", "Item/Tooltip", "y"), "text")
        self.assertEqual(k("S", "Item/Label", "x" * 61), "text")
        self.assertEqual(k("S", "Item/Label", "short"), "label")


if __name__ == "__main__":
    unittest.main()
