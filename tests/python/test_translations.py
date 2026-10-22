"""tpcloc.translations: save() formatting, key order and atomic replacement; loaders."""
import json
import os
import shutil
import unittest
from unittest import mock

from . import helpers as h
from tpcloc import paths, translations


def expected_bytes(entries: dict) -> bytes:
    body = json.dumps(entries, ensure_ascii=False, indent=1) + "\n"
    return body.replace("\n", os.linesep).encode("utf-8")  # text mode: platform line endings


class Save(unittest.TestCase):
    def setUp(self):
        self.root = h.new_dir(self)
        self.ctx = h.project_paths(self.root)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.dir = paths.TRANSLATIONS_DIR

    def test_exact_format(self):
        entries = {"K/1": h.entry("訳\n改行", note="memo \"q\" \\ tab\t"), "K/2": h.entry("x", "edited", review="レビュー")}
        p = translations.save("I2LS_Test", entries)
        self.assertEqual(p, self.dir / "I2LS_Test.json")
        raw = p.read_bytes()
        self.assertEqual(raw, expected_bytes(entries))
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("訳".encode("utf-8"), raw)  # ensure_ascii=False
        self.assertIn(b'\\n', raw)  # a line break inside a value is a JSON escape
        self.assertEqual(json.loads(raw.decode("utf-8")), entries)
        # indent=1, one space
        self.assertTrue(raw.decode("utf-8").splitlines()[1].startswith(' "K/1": {'))

    def test_key_order_follows_game_order_unknown_last_stable(self):
        entries = {"c": h.entry("3"), "zz": h.entry("?1"), "a": h.entry("1"), "b": h.entry("2"), "yy": h.entry("?2")}
        translations.save("S", entries, ["a", "b", "c"])
        got = list(json.loads((self.dir / "S.json").read_text("utf-8")))
        self.assertEqual(got[:3], ["a", "b", "c"])
        self.assertEqual(got[3:], ["zz", "yy"])  # not in the game order: kept in their own order at the end

    def test_no_key_order_keeps_insertion_order(self):
        entries = {"b": h.entry("2"), "a": h.entry("1")}
        translations.save("S", entries)
        self.assertEqual(list(json.loads((self.dir / "S.json").read_text("utf-8"))), ["b", "a"])
        translations.save("S", entries, [])
        self.assertEqual(list(json.loads((self.dir / "S.json").read_text("utf-8"))), ["b", "a"])

    def test_creates_folder(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        translations.save("S", {"a": h.entry("1")})
        self.assertTrue((self.dir / "S.json").exists())

    def test_load_all_round_trip_is_byte_identical(self):
        entries = {"a": h.entry("一", note="n"), "b": h.entry("二", "reviewed", review="r")}
        translations.save("S", entries, ["a", "b"])
        before = (self.dir / "S.json").read_bytes()
        loaded = translations.load_all()
        self.assertEqual(loaded, {"S": entries})
        translations.save("S", loaded["S"], ["a", "b"])
        self.assertEqual((self.dir / "S.json").read_bytes(), before)

    def _fail_and_check(self, target: str, exc=OSError("disk full")):
        entries = {"a": h.entry("old")}
        translations.save("S", entries)
        original = (self.dir / "S.json").read_bytes()
        with mock.patch.object(translations.os, target, side_effect=exc):
            with self.assertRaises(type(exc)):
                translations.save("S", {"a": h.entry("new")})
        self.assertEqual((self.dir / "S.json").read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["S.json"])  # no .tmp left

    def test_atomic_fsync_failure(self):
        self._fail_and_check("fsync")

    def test_atomic_replace_failure(self):
        self._fail_and_check("replace")

    def test_atomic_interrupt(self):
        self._fail_and_check("fsync", KeyboardInterrupt())

    def test_atomic_write_failure(self):
        translations.save("S", {"a": h.entry("old")})
        original = (self.dir / "S.json").read_bytes()
        real_open = open

        class Broken:
            def __init__(self, fp):
                self.fp = fp

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.fp.close()

            def write(self, s):
                self.fp.write(s[: len(s) // 2])
                raise OSError("write failed")

        def fake_open(path, mode="r", *a, **k):
            fp = real_open(path, mode, *a, **k)
            return Broken(fp) if "w" in mode else fp

        with mock.patch("builtins.open", fake_open):
            with self.assertRaises(OSError):
                translations.save("S", {"a": h.entry("new" * 1000)})
        self.assertEqual((self.dir / "S.json").read_bytes(), original)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["S.json"])

    def test_stale_tmp_from_an_old_crash_is_overwritten(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "S.json.tmp").write_text("garbage", "utf-8")
        translations.save("S", {"a": h.entry("1")})
        self.assertEqual([p.name for p in self.dir.iterdir()], ["S.json"])
        self.assertEqual(json.loads((self.dir / "S.json").read_text("utf-8")), {"a": h.entry("1")})


class Loaders(unittest.TestCase):
    def test_glossaries(self):
        root = h.new_dir(self)
        h.make_project(root, {"S": [("k", "Hello", "Hello")]},
                       glossary_official={'"Quoted Title"': "「題」", "Plain": "平"},
                       glossary_new=[{"en": '"A Good Prone"', "ja": "『寝かせ方』"}, {"en": "", "ja": "skip"}])
        with h.project_paths(root):
            off = translations.load_glossary_official()
            self.assertEqual(off, {'"Quoted Title"': "「題」", "Plain": "平"})
            new = translations.load_glossary_new()
            self.assertEqual([(r["en"], r["ja"]) for r in new], [('"A Good Prone"', "『寝かせ方』")])
            translations.GLOSSARY_NEW.unlink()
            self.assertEqual(translations.load_glossary_new(), [])

    @unittest.skipUnless((h.PROJECT / "translations" / "ja").exists(), "no translations")
    def test_real_translations_reserialize_identically(self):
        """save() reproduces every real translations/ja file byte for byte (on a copy)."""
        root = h.new_dir(self)
        shutil.copytree(h.PROJECT / "translations" / "ja", root / "translations" / "ja")
        strings = json.loads(h.REAL_STRINGS.read_text("utf-8")) if h.REAL_STRINGS.exists() else None
        with h.project_paths(root):
            for source, entries in translations.load_all().items():
                with self.subTest(source=source):
                    before = (paths.TRANSLATIONS_DIR / f"{source}.json").read_bytes()
                    keys = strings["sources"][source]["keys"] if strings else None
                    translations.save(source, entries, keys)
                    self.assertEqual((paths.TRANSLATIONS_DIR / f"{source}.json").read_bytes(), before)
                    for e in entries.values():
                        self.assertIn(e["status"], translations.STATUSES)


if __name__ == "__main__":
    unittest.main()
