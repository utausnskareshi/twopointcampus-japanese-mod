"""tpcloc.bundles (read_texts / write_texts) and tpcloc.official (which file holds the official Japanese)."""
import contextlib
import shutil
import types
import unittest
from pathlib import Path
from unittest import mock

import UnityPy
from UnityPy.files import SerializedFile

from . import helpers as h
from tpcloc import bundles, i2cache, official, paths, text, translations

SRC = "I2LS_Mods"


@unittest.skipUnless(h.OFFICIAL_BUNDLE.exists(), "official bundle copy missing")
class Bundles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.official = bundles.read_texts(h.OFFICIAL_BUNDLE)

    def setUp(self):
        self.dir = h.new_dir(self)
        self.src = self.dir / "src.bundle"
        shutil.copyfile(h.OFFICIAL_BUNDLE, self.src)

    def test_read_texts(self):
        self.assertIn(SRC, self.official)
        self.assertTrue(all(isinstance(v, str) for v in self.official.values()))
        self.assertTrue(all(n.startswith("I2LS_") for n in self.official))

    def test_write_read_round_trip(self):
        pairs = i2cache.parse(self.official[SRC])
        pairs[0] = (pairs[0][0], "置換\n改行\\n😀=x")
        new = i2cache.dumps(pairs)
        dst = self.dir / "dst.bundle"
        done = bundles.write_texts(self.src, dst, {SRC: new, "I2LS_DLCs": ""})
        self.assertEqual(sorted(done), sorted([SRC, "I2LS_DLCs"]))
        got = bundles.read_texts(dst)
        self.assertEqual(set(got), set(self.official))
        for name, t in self.official.items():
            with self.subTest(name=name):
                want = new if name == SRC else ("" if name == "I2LS_DLCs" else t)
                self.assertEqual(got[name], want)
        self.assertEqual(self._leftovers(), [])
        # the source is untouched and no file handle stays open (Windows would block these)
        self.assertEqual(h.sha256_file(self.src), h.sha256_file(h.OFFICIAL_BUNDLE))
        dst.rename(self.dir / "moved.bundle")
        (self.dir / "moved.bundle").unlink()

    def test_identity_write_keeps_texts(self):
        dst = self.dir / "dst.bundle"
        bundles.write_texts(self.src, dst, {SRC: self.official[SRC]})
        self.assertEqual(bundles.read_texts(dst), self.official)

    def test_in_place(self):
        bundles.write_texts(self.src, self.src, {SRC: "A=1"})
        self.assertEqual(bundles.read_texts(self.src)[SRC], "A=1")
        self.assertEqual(self._leftovers(), [])

    def test_missing_name_raises_and_writes_nothing(self):
        dst = self.dir / "dst.bundle"
        with self.assertRaises(KeyError) as cm:
            bundles.write_texts(self.src, dst, {SRC: "A=1", "I2LS_DoesNotExist": "x"})
        self.assertIn("I2LS_DoesNotExist", str(cm.exception))
        self.assertFalse(dst.exists())
        self.assertEqual(self._leftovers(), [])

    def test_verification_failure_keeps_target(self):
        dst = self.dir / "dst.bundle"
        shutil.copyfile(self.src, dst)
        before = h.sha256_file(dst)
        with mock.patch.object(bundles, "read_texts", return_value={SRC: "something else"}):
            with self.assertRaises(RuntimeError):
                bundles.write_texts(self.src, dst, {SRC: "A=1"})
        self.assertEqual(h.sha256_file(dst), before)
        self.assertEqual(self._leftovers(), [])

    def test_sha256(self):
        self.assertEqual(bundles.sha256(self.src), h.sha256_file(self.src))
        empty = self.dir / "empty"
        empty.write_bytes(b"")
        self.assertEqual(bundles.sha256(empty), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")

    def _leftovers(self):
        return sorted(p.name for p in self.dir.iterdir() if p.name.endswith(".tmp"))


@unittest.skipUnless(h.OFFICIAL_BUNDLE.exists(), "official bundle copy missing")
class Official(unittest.TestCase):
    """Decision table of official.official_japanese() with bundles made by write_texts."""

    @classmethod
    def setUpClass(cls):
        cls.root = h.TEMP_ROOT / "t" / "official_cls"
        shutil.rmtree(cls.root, ignore_errors=True)
        cls.root.mkdir(parents=True)
        if not h.KEEP:
            cls.addClassCleanup(shutil.rmtree, cls.root, True)
        texts = bundles.read_texts(h.OFFICIAL_BUNDLE)
        cls.mods = i2cache.parse(texts[SRC])
        untranslated = [k for k, v in cls.mods if v.strip() and not text.has_japanese(v)]
        cls.keys = untranslated[:10]
        cls.ours = {k: f"当パッチの訳{i}" for i, k in enumerate(cls.keys)}
        # translations: 10 Japanese, one ASCII-only (not counted), one for a key the bundle lacks
        entries = {k: h.entry(v) for k, v in cls.ours.items()}
        entries[untranslated[10]] = h.entry("MOD")
        entries["UI/Mods/NotInTheBundle"] = h.entry("無い項目")
        h.make_project(cls.root / "proj", {}, ours={SRC: entries})
        cls.bundles = {}

        def make(name, values):
            dst = cls.root / f"{name}.bundle"
            new = [(k, values.get(k, v)) for k, v in cls.mods]
            bundles.write_texts(h.OFFICIAL_BUNDLE, dst, {SRC: i2cache.dumps(new)})
            cls.bundles[name] = dst

        cls.bundles["official"] = h.OFFICIAL_BUNDLE
        make("patched", cls.ours)
        k = cls.keys
        # official updates: some terms translated exactly like ours, others differently, the rest still English
        make("upd_few", {**{x: cls.ours[x] for x in k[:4]}, **{x: f"公式{x}" for x in k[4:7]}})  # 4 vs 3+3
        make("upd_many", {**{x: cls.ours[x] for x in k[:6]}, **{x: f"公式{x}" for x in k[6:]}})  # 6 vs 0+4
        make("upd_two", {x: cls.ours[x] for x in k[:2]})  # 2 < 3
        make("upd_all_other", {x: f"公式{x}" for x in k})
        # damaged files: UnityPy finds no TextAsset in zeros and fails to decompress a truncated bundle
        raw = h.OFFICIAL_BUNDLE.read_bytes()
        for name, data in {"zeros": b"\0" * 4096, "truncated": raw[: len(raw) // 2]}.items():
            cls.bundles[name] = cls.root / f"{name}.bundle"
            cls.bundles[name].write_bytes(data)
        # formats TpcJpPatch.exe refuses as unsupported (UnsupportedFormatException) or as damage: UnityFS format
        # versions (UnityPy still reads these), and a serialized file of another version or big endian in an
        # uncompressed bundle (UnityPy cannot read these)
        for version in (9, 5, 0, 100):
            cls.bundles[f"fs{version}"] = cls.root / f"fs{version}.bundle"
            cls.bundles[f"fs{version}"].write_bytes(raw[:8] + version.to_bytes(4, "big") + raw[12:])
        plain = next(iter(UnityPy.load(raw).files.values())).save(packer="none")
        sf = next(f for top in UnityPy.load(plain).files.values() for f in top.files.values() if isinstance(f, SerializedFile))
        sf.reader.Position = 0
        cls.sf_header = bytes(sf.reader.read_bytes(64))
        at = plain.find(cls.sf_header)
        assert at > 0 and plain.count(cls.sf_header) == 1
        for name, offset, value in (("sf23", 8, (23).to_bytes(4, "big")), ("sf_big", 16, b"\x01")):
            data = bytearray(plain)
            data[at + offset:at + offset + len(value)] = value
            cls.bundles[name] = cls.root / f"{name}.bundle"
            cls.bundles[name].write_bytes(data)

    def setUp(self):
        self.ctx = h.project_paths(self.root / "proj")
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)

    def game(self, current: str | None, backup: str | None = None, state: dict | None = None) -> Path:
        """current None: no installed bundle."""
        g = h.make_game(h.new_dir(self), japanese=self.bundles[current] if current else None)
        b = h.ja_bundle(g)
        if backup:
            shutil.copyfile(self.bundles[backup], official.backup_path(b))
        if state is not None:
            h.write_state(b, **state)
        return b

    def sha(self, name):
        return h.sha256_file(self.bundles[name])

    # ---- classify / is_patched

    def test_classify(self):
        table = {"official": (0, 10, 0), "patched": (10, 0, 0), "upd_few": (4, 3, 3), "upd_many": (6, 0, 4),
                 "upd_two": (2, 8, 0), "upd_all_other": (0, 0, 10)}
        for name, want in table.items():
            with self.subTest(name=name):
                self.assertEqual(official.classify(self.bundles[name]), want)

    def test_is_patched(self):
        table = {"official": False, "patched": True, "upd_few": False, "upd_many": True, "upd_two": False,
                 "upd_all_other": False}
        for name, want in table.items():
            with self.subTest(name=name):
                self.assertEqual(official.is_patched(self.bundles[name])[0], want)

    def test_is_patched_boundary_equal_counts(self):
        # ours must exceed untranslated + other: 5 identical vs 5 others is an official update
        k = self.keys
        dst = h.new_dir(self) / "eq.bundle"
        values = {**{x: self.ours[x] for x in k[:5]}, **{x: f"公式{x}" for x in k[5:]}}
        bundles.write_texts(h.OFFICIAL_BUNDLE, dst, {SRC: i2cache.dumps([(kk, values.get(kk, v)) for kk, v in self.mods])})
        self.assertEqual(official.classify(dst), (5, 0, 5))
        self.assertFalse(official.is_patched(dst)[0])

    # ---- official_japanese decision table

    def test_state_current_backup_ok(self):
        b = self.game("patched", "official", {"original_sha256": self.sha("official"), "patched_sha256": self.sha("patched")})
        path, reason = official.official_japanese(b)
        self.assertEqual(path, official.backup_path(b))
        self.assertIn("our patched output", reason)

    def test_state_current_without_original_hash(self):
        b = self.game("patched", "official", {"patched_sha256": self.sha("patched")})
        self.assertEqual(official.official_japanese(b)[0], official.backup_path(b))

    def test_state_current_backup_missing(self):
        b = self.game("patched", None, {"original_sha256": self.sha("official"), "patched_sha256": self.sha("patched")})
        with self.assertRaises(official.OfficialBundleError) as cm:
            official.official_japanese(b)
        self.assertIn("missing", str(cm.exception))

    def test_state_current_backup_does_not_match(self):
        b = self.game("patched", "upd_few", {"original_sha256": self.sha("official"), "patched_sha256": self.sha("patched")})
        with self.assertRaises(official.OfficialBundleError) as cm:
            official.official_japanese(b)
        self.assertIn("does not match original_sha256", str(cm.exception))

    def test_stale_state_bundle_identical_to_backup(self):
        b = self.game("official", "official", {"original_sha256": self.sha("official"), "patched_sha256": "0" * 64})
        path, reason = official.official_japanese(b)
        self.assertEqual(path, official.backup_path(b))
        self.assertIn("identical", reason)

    def test_no_state_bundle_identical_to_backup(self):
        b = self.game("official", "official")
        self.assertEqual(official.official_japanese(b)[0], official.backup_path(b))

    def test_stale_state_patched_content_uses_backup(self):
        b = self.game("patched", "official", {"original_sha256": self.sha("official"), "patched_sha256": "0" * 64})
        path, reason = official.official_japanese(b)
        self.assertEqual(path, official.backup_path(b))
        self.assertIn("not recorded in the state file", reason)

    def test_missing_state_patched_content_without_backup(self):
        b = self.game("patched")
        with self.assertRaises(official.OfficialBundleError) as cm:
            official.official_japanese(b)
        self.assertIn("holds 10 of our translations", str(cm.exception))

    def test_updated_official_stale_backup(self):
        for upd in ("upd_few", "upd_two", "upd_all_other"):
            with self.subTest(upd=upd):
                b = self.game(upd, "official",
                              {"original_sha256": self.sha("official"), "patched_sha256": self.sha("patched")})
                path, reason = official.official_japanese(b)
                self.assertEqual(path, b)
                self.assertTrue(reason.startswith("WARNING"), reason)
                self.assertIn("stale", reason)

    def test_updated_official_after_a_version_change_stale_backup(self):
        """A state file of an older game version (version.txt changed): the installed file is the new official one."""
        b = self.game("upd_few", "official", {"game_version": "10.2.0+old", "original_sha256": self.sha("official"),
                                              "patched_sha256": self.sha("patched")})
        path, reason = official.official_japanese(b)
        self.assertEqual(path, b)
        self.assertIn("stale", reason)

    def test_same_game_version_changed_bundle_keeps_the_backup(self):
        """Since the FZ-05/06 fix TpcJpPatch.exe treats a bundle that differs from the backup while the game version in
        the state file equals version.txt as not a real update: it keeps the backup and rebuilds/restores from it.
        tpcloc/official.py (extract.py, verify_bundle.py, dev_patch.py) shares these files and agrees since round 2
        (was finding pytests-OF1)."""
        for upd in ("upd_few", "upd_two", "upd_all_other"):
            with self.subTest(upd=upd):
                b = self.game(upd, "official", {"game_version": h.GAME_VERSION, "original_sha256": self.sha("official"),
                                                "patched_sha256": self.sha("patched")})
                self.assertEqual((b.parents[4] / "version.txt").read_text("utf-8"), h.GAME_VERSION)
                path, reason = official.official_japanese(b)
                self.assertEqual(path, official.backup_path(b))
                self.assertTrue(reason.startswith("WARNING"), reason)
                self.assertIn("game version is unchanged", reason)
                self.assertNotIn("stale", reason)
                self.assertNotIn("is replaced on the next apply", reason)

    def test_same_game_version_ignores_surrounding_whitespace_in_version_txt(self):
        """Both sides trim version.txt (GameLocator.GameVersion: Trim(); official.py: strip())."""
        b = self.game("upd_few", "official", {"game_version": h.GAME_VERSION, "original_sha256": self.sha("official"),
                                              "patched_sha256": self.sha("patched")})
        (b.parents[4] / "version.txt").write_text(f" {h.GAME_VERSION}\r\n", "utf-8")
        self.assertEqual(official.official_japanese(b)[0], official.backup_path(b))

    def test_same_game_version_rule_needs_a_backup(self):
        """Without a backup there is nothing to keep: the installed bundle is read (no backup reason)."""
        b = self.game("upd_few", None, {"game_version": h.GAME_VERSION, "original_sha256": self.sha("official"),
                                        "patched_sha256": self.sha("patched")})
        path, reason = official.official_japanese(b)
        self.assertEqual(path, b)
        self.assertIn("no backup", reason)

    def test_state_without_game_version_is_not_the_same_version(self):
        """A state file of TpcJpPatch 1.0.0 has no game_version: like Patcher.SameGameVersion, that is not the same
        version, so a bundle that is neither the backup nor ours is a new official file (backup stale)."""
        b = self.game("upd_few", "official", {"original_sha256": self.sha("official"), "patched_sha256": self.sha("patched")})
        path, reason = official.official_japanese(b)
        self.assertEqual(path, b)
        self.assertIn("stale", reason)

    def test_unknown_game_version_is_not_the_same_version(self):
        """Patcher.SameGameVersion needs game_version in the state AND a readable version.txt
        (current != GameLocator.UnknownVersion). A state without game_version and a missing version.txt used to count
        as the same version (None == None; was finding pytests-OF2)."""
        b = self.game("upd_few", "official", {"original_sha256": self.sha("official"), "patched_sha256": self.sha("patched")})
        (b.parents[4] / "version.txt").unlink()
        path, reason = official.official_japanese(b)
        self.assertNotIn("game version is unchanged", reason)
        self.assertEqual(path, b)
        self.assertIn("stale", reason)

    def test_game_versions_that_cannot_be_compared_are_not_the_same(self):
        """Either side missing, empty or the patcher's unknown marker (GameLocator.UnknownVersion "不明", recorded when
        version.txt could not be read) is never the same version: the installed bundle is a new official file."""
        unknown = official.UNKNOWN_VERSION
        # (game_version in the state, None = no key; version.txt, None = no file)
        for v, txt in [(None, None), ("", None), ("", h.GAME_VERSION), (unknown, None), (unknown, unknown),
                       (h.GAME_VERSION, None), (h.GAME_VERSION, ""), (h.GAME_VERSION, unknown),
                       ("", ""), ("", " \r\n"), (None, "")]:  # R15/R27: an empty version.txt is unknown on both sides
            with self.subTest(state=v, version_txt=txt):
                state = {"original_sha256": self.sha("official"), "patched_sha256": self.sha("patched")}
                if v is not None:
                    state["game_version"] = v
                b = self.game("upd_few", "official", state)
                if txt is None:
                    (b.parents[4] / "version.txt").unlink()
                else:
                    (b.parents[4] / "version.txt").write_text(txt, "utf-8")
                path, reason = official.official_japanese(b)
                self.assertEqual(path, b)
                self.assertIn("stale", reason)

    def test_version_txt_is_read_like_the_patcher(self):
        """GameLocator.GameVersion uses File.ReadAllText: a UTF-8 BOM is removed and undecodable bytes become U+FFFD
        (which the patcher then records in the state file); neither stops official.py."""
        b = self.game("upd_few", "official", {"game_version": h.GAME_VERSION, "original_sha256": self.sha("official"),
                                              "patched_sha256": self.sha("patched")})
        vt = b.parents[4] / "version.txt"
        vt.write_bytes(b"\xef\xbb\xbf" + h.GAME_VERSION.encode("utf-8") + b"\r\n")
        self.assertEqual(official.official_japanese(b)[0], official.backup_path(b))
        vt.write_bytes(h.GAME_VERSION.encode("utf-8") + b"\xff")
        path, reason = official.official_japanese(b)
        self.assertEqual(path, b)
        self.assertIn("stale", reason)
        h.write_state(b, game_version=h.GAME_VERSION + "�", original_sha256=self.sha("official"),
                      patched_sha256=self.sha("patched"))
        self.assertEqual(official.official_japanese(b)[0], official.backup_path(b))

    def test_unreadable_version_txt_is_an_unknown_version(self):
        """R15/R27 parity: GameLocator.GameVersion maps an unreadable version.txt to 不明 (also on access denied); here
        any OSError (a folder in its place, PermissionError) is unknown: never the same version as a recorded one."""
        state = {"game_version": h.GAME_VERSION, "original_sha256": self.sha("official"),
                 "patched_sha256": self.sha("patched")}
        real_read_bytes = Path.read_bytes

        def denied(path, *args, **kwargs):
            if path.name == "version.txt":
                raise PermissionError(13, "Access is denied", str(path))
            return real_read_bytes(path, *args, **kwargs)

        for how in ("folder", "access denied"):
            with self.subTest(how=how):
                b = self.game("upd_few", "official", state)
                vt = b.parents[4] / "version.txt"
                with contextlib.ExitStack() as stack:
                    if how == "folder":
                        vt.unlink()
                        vt.mkdir()
                    else:
                        stack.enter_context(mock.patch.object(Path, "read_bytes", denied))
                    self.assertEqual(official.installed_version(b), official.UNKNOWN_VERSION)
                    self.assertEqual(paths.game_version(b.parents[4]), official.UNKNOWN_VERSION)
                    path, reason = official.official_japanese(b)
                    self.assertEqual(path, b)
                    self.assertIn("stale", reason)
                    b.unlink()
                    with self.assertRaises(official.OfficialBundleError) as cm:
                        official.official_japanese(b)
                    self.assertIn(self.unconfirmed(h.GAME_VERSION, "unknown"), str(cm.exception))

    def test_installed_version(self):
        """What dev_patch.py records: version.txt like GameLocator.GameVersion, 不明 when it cannot be read."""
        b = self.game("official")
        vt = b.parents[4] / "version.txt"
        self.assertEqual(official.installed_version(b), h.GAME_VERSION)
        vt.write_bytes(b"\xef\xbb\xbf 10.4.0+x \r\n")
        self.assertEqual(official.installed_version(b), "10.4.0+x")
        vt.write_bytes(b"")
        self.assertEqual(official.installed_version(b), "")
        vt.unlink()
        self.assertEqual(official.installed_version(b), official.UNKNOWN_VERSION)
        self.assertEqual(official.installed_version(Path("x.bundle")), official.UNKNOWN_VERSION)  # no game folder

    def test_version_txt_is_normalised_like_the_recorded_game_version(self):
        """S4/S17 (round 3c): like GameLocator.GameVersion, version.txt is read like File.ReadAllText (byte order mark
        removed: UTF-8, UTF-16, UTF-32), trimmed like String.Trim (Char.IsWhiteSpace: not U+001C..U+001F, which
        str.strip would remove) and every character Patcher.OneLine replaces in the state file (LF, VT, FF, CR,
        U+001C..U+001E, U+0085, U+2028, U+2029) becomes a space, so the recorded value matches it again."""
        g = h.make_game(h.new_dir(self), japanese=None)
        vt = g / "version.txt"
        table = [
            (b"10.4.0", "10.4.0"),
            (b"\xef\xbb\xbf10.4.0\r\n", "10.4.0"),
            ("\ufeff10.4.0\r\n".encode("utf-16-le"), "10.4.0"),  # File.ReadAllText picks UTF-16 by the BOM
            ("\ufeff10.4.0\r\n".encode("utf-16-be"), "10.4.0"),
            ("\ufeff10.4.0\r\n".encode("utf-32-le"), "10.4.0"),
            (b"10.4.0\r\nbuild 1234", "10.4.0  build 1234"),
            (b"10.4.0\nbuild\x0b1\x0c2\x1c3\x1d4\x1e5", "10.4.0 build 1 2 3 4 5"),
            ("a\x85b\u2028c\u2029d".encode("utf-8"), "a b c d"),
            (" \t\u3000\xa010.4.0\u2028\x85\r\n ".encode("utf-8"), "10.4.0"),  # .NET whitespace at both ends
            (b"\x1c10.4.0\x1f", " 10.4.0\x1f"),  # not whitespace for .NET: U+001C stays (as a space), U+001F stays
            (b"10.4.0\xff", "10.4.0\ufffd"),
            (b" \r\n", ""),
        ]
        for raw, want in table:
            with self.subTest(raw=raw):
                vt.write_bytes(raw)
                self.assertEqual(paths.read_game_version(g), want)
                self.assertEqual(paths.game_version(g), want)
                self.assertEqual(official.installed_version(h.ja_bundle(g)), want)
        vt.unlink()
        self.assertIsNone(paths.read_game_version(g))
        self.assertEqual(paths.game_version(g), official.UNKNOWN_VERSION)
        self.assertEqual(official.UNKNOWN_VERSION, "不明")

    def test_version_txt_with_an_inner_line_break_matches_its_recorded_value(self):
        """S4/S17: the patcher records '10.4.0\\r\\nbuild 1234' as '10.4.0  build 1234' (WriteState/OneLine); that is
        the same game version again (it never matched, so a modified bundle was taken for a game update and a
        missing one was refused as another version)."""
        state = {"game_version": "10.4.0  build 1234", "original_sha256": self.sha("official"),
                 "patched_sha256": self.sha("patched")}
        b = self.game("upd_few", "official", state)
        (b.parents[4] / "version.txt").write_bytes(b"10.4.0\r\nbuild 1234\r\n")
        path, reason = official.official_japanese(b)
        self.assertEqual(path, official.backup_path(b))
        self.assertIn("game version is unchanged", reason)
        b.unlink()
        path, reason = official.official_japanese(b)
        self.assertEqual(path, official.backup_path(b))
        self.assertIn("the installed bundle is missing", reason)

    def test_same_game_version_backup_must_match_original_sha256(self):
        """Only the state file vouches for the backup here, so it must be the recorded file: otherwise nothing can be
        trusted (the patcher stops with the Steam verification advice). Its content is checked first, like
        Patcher.CheckBackup before CheckBackupRecorded (round 3b, finding R16)."""
        name = official.backup_path(h.ja_bundle(Path("g"))).name
        for backup, want in (("upd_all_other", f"{name} does not match original_sha256"),
                             ("patched", f"{name} is patched data (10 of our translations), not the official file"),
                             ("zeros", f"{name} is damaged (no TextAsset)")):
            with self.subTest(backup=backup):
                b = self.game("upd_few", backup, {"game_version": h.GAME_VERSION, "original_sha256": self.sha("official"),
                                                  "patched_sha256": self.sha("patched")})
                with self.assertRaises(official.OfficialBundleError) as cm:
                    official.official_japanese(b)
                msg = str(cm.exception)
                self.assertIn("while the game version is unchanged", msg)
                self.assertIn(want, msg)
                self.assertTrue(msg.endswith("verify the game files in Steam"), msg)

    def test_same_game_version_without_original_sha256_keeps_the_backup(self):
        b = self.game("upd_few", "official", {"game_version": h.GAME_VERSION, "patched_sha256": self.sha("patched")})
        path, reason = official.official_japanese(b)
        self.assertEqual(path, official.backup_path(b))
        self.assertIn("game version is unchanged", reason)

    # ---- the installed bundle is the official file recorded in the state (original_sha256)

    def test_installed_bundle_recorded_as_original_without_backup(self):
        b = self.game("official", None, {"game_version": h.GAME_VERSION, "original_sha256": self.sha("official"),
                                         "patched_sha256": self.sha("patched")})
        path, reason = official.official_japanese(b)
        self.assertEqual(path, b)
        self.assertFalse(reason.startswith("WARNING"), reason)
        self.assertIn("official file recorded in", reason)
        self.assertIn(f"{official.backup_path(b).name} is missing", reason)

    def test_installed_bundle_recorded_as_original_wins_over_a_different_backup(self):
        """Even while the game version is unchanged (the same-version rule would take the backup) or when the backup is
        damaged or patched: the installed file is the recorded official file."""
        for backup in ("upd_few", "patched", "zeros"):
            for version in (h.GAME_VERSION, "10.2.0+old", None):
                with self.subTest(backup=backup, version=version):
                    state = {"original_sha256": self.sha("official"), "patched_sha256": self.sha("patched")}
                    if version:
                        state["game_version"] = version
                    b = self.game("official", backup, state)
                    path, reason = official.official_japanese(b)
                    self.assertEqual(path, b)
                    self.assertTrue(reason.startswith("WARNING"), reason)
                    self.assertIn("official file recorded in", reason)
                    self.assertNotIn("game version is unchanged", reason)

    # ---- the installed bundle is missing or cannot be read: only the state file vouches for the backup

    def fitting_states(self):
        """States under which the backup (the official bundle) may stand in for the installed bundle: the state file
        records the installed game version (round 3c, finding S1: it must prove it)."""
        o = self.sha("official")
        return {"same version": {"game_version": h.GAME_VERSION, "original_sha256": o, "patched_sha256": self.sha("patched")},
                "no original_sha256": {"game_version": h.GAME_VERSION}}

    def unconfirmed(self, recorded: str, installed: str) -> str:
        """「バックアップがどのバージョンのゲームのものか確認できません（記録: …、現在のゲームのバージョン: …）」"""
        name = official.backup_path(h.ja_bundle(Path("g"))).name
        return f"it cannot be confirmed which game version {name} was taken for (recorded: {recorded}, installed: {installed})"

    def unfitting_states(self):
        """(state, version.txt or None = no file, expected part of the message) under which the backup is not used."""
        o, p, v = self.sha("official"), self.sha("patched"), h.GAME_VERSION
        name = official.backup_path(h.ja_bundle(Path("g"))).name
        unconfirmed = self.unconfirmed
        return {
            "backup differs from original_sha256": ({"game_version": v, "original_sha256": "1" * 64, "patched_sha256": p}, v,
                                                    f"{name} does not match original_sha256"),
            "older game version": ({"game_version": "10.2.0+old", "original_sha256": o, "patched_sha256": p}, v,
                                   f"{name} was taken for game version 10.2.0+old but the installed version is {v}"),
            # S1 (round 3c): like Patcher.CheckBackupRecorded, no state file or a state of TpcJpPatch 1.0.0 (no
            # game_version) no longer vouches for the backup (both used to be fitting states)
            "1.0.0 state (no game_version)": ({"original_sha256": o, "patched_sha256": p}, v, unconfirmed("unknown", v)),
            "no state file": (None, v, unconfirmed("unknown", v)),
            "no state file, version.txt missing": (None, None, unconfirmed("unknown", "unknown")),
            "version.txt missing": ({"game_version": v, "original_sha256": o, "patched_sha256": p}, None,
                                    unconfirmed(v, "unknown")),
            "unknown version recorded": ({"game_version": official.UNKNOWN_VERSION, "original_sha256": o}, v,
                                         unconfirmed("unknown", v)),
            "both unknown": ({"game_version": official.UNKNOWN_VERSION, "original_sha256": o}, official.UNKNOWN_VERSION,
                             unconfirmed("unknown", "unknown")),
            # R15/R27: an empty version.txt (and an empty recorded version) is unknown, like Patcher.SameGameVersion
            "both empty": ({"game_version": "", "original_sha256": o}, "", unconfirmed("unknown", "unknown")),
            "empty version.txt": ({"game_version": v, "original_sha256": o}, " \r\n", unconfirmed(v, "unknown")),
        }

    def set_version_txt(self, b: Path, txt: str | None):
        if txt is None:
            (b.parents[4] / "version.txt").unlink()
        else:
            (b.parents[4] / "version.txt").write_text(txt, "utf-8")

    def test_missing_bundle_uses_a_backup_that_fits_the_state(self):
        for what, state in self.fitting_states().items():
            with self.subTest(state=what):
                b = self.game(None, "official", state)
                path, reason = official.official_japanese(b)
                self.assertEqual(path, official.backup_path(b))
                self.assertEqual(reason, f"WARNING: the installed bundle is missing; official data from {path.name}")
                self.assertFalse(b.exists())  # read-only

    def test_missing_bundle_with_a_backup_that_does_not_fit_stops(self):
        for what, (state, txt, want) in self.unfitting_states().items():
            with self.subTest(state=what):
                b = self.game(None, "official", state)
                self.set_version_txt(b, txt)
                with self.assertRaises(official.OfficialBundleError) as cm:
                    official.official_japanese(b)
                msg = str(cm.exception)
                self.assertTrue(msg.startswith(f"{b.name} is missing and "), msg)
                self.assertIn(want, msg)
                self.assertTrue(msg.endswith("verify the game files in Steam"), msg)

    def test_missing_bundle_without_backup_stops(self):
        for state in (None, {"game_version": h.GAME_VERSION, "original_sha256": self.sha("official")}):
            with self.subTest(state=state):
                b = self.game(None, None, state)
                with self.assertRaises(official.OfficialBundleError) as cm:
                    official.official_japanese(b)
                self.assertEqual(str(cm.exception), f"{b.name} and {official.backup_path(b).name} are missing; "
                                                    "verify the game files in Steam")

    def test_damaged_bundle_uses_a_backup_that_fits_the_state(self):
        for damaged, why in (("zeros", "(no TextAsset)"), ("truncated", "(LZ4BlockError: ")):
            for what, state in self.fitting_states().items():
                with self.subTest(damaged=damaged, state=what):
                    b = self.game(damaged, "official", state)
                    path, reason = official.official_japanese(b)
                    self.assertEqual(path, official.backup_path(b))
                    self.assertTrue(reason.startswith(f"WARNING: the installed bundle cannot be read {why}"), reason)
                    self.assertTrue(reason.endswith(f"; official data from {path.name}"), reason)
                    self.assertEqual(h.sha256_file(b), self.sha(damaged))  # read-only

    def test_damaged_bundle_with_a_backup_that_does_not_fit_stops(self):
        for damaged in ("zeros", "truncated"):
            for what, (state, txt, want) in self.unfitting_states().items():
                with self.subTest(damaged=damaged, state=what):
                    b = self.game(damaged, "official", state)
                    self.set_version_txt(b, txt)
                    with self.assertRaises(official.OfficialBundleError) as cm:
                        official.official_japanese(b)
                    msg = str(cm.exception)
                    self.assertTrue(msg.startswith(f"{b.name} cannot be read ("), msg)
                    self.assertIn(want, msg)
                    self.assertTrue(msg.endswith("verify the game files in Steam"), msg)

    def test_damaged_bundle_without_backup_stops(self):
        """Never taken for official data (before: 'no backup; the installed bundle is the official one', or a crash)."""
        for damaged in ("zeros", "truncated"):
            with self.subTest(damaged=damaged):
                b = self.game(damaged)
                with self.assertRaises(official.OfficialBundleError) as cm:
                    official.official_japanese(b)
                msg = str(cm.exception)
                self.assertTrue(msg.startswith(f"{b.name} cannot be read ("), msg)
                self.assertTrue(msg.endswith(f") and {official.backup_path(b).name} is missing; verify the game files in Steam"),
                                msg)

    def test_damaged_bundle_identical_to_the_backup_or_recorded_is_not_read(self):
        """The hash checks come first, like the patcher: an installed file equal to the backup or to patched_sha256 is
        not loaded itself; only the backup about to be returned is checked (Patcher.CheckBackup, round 3b R16)."""
        b = self.game("zeros", "official", {"original_sha256": self.sha("official"), "patched_sha256": self.sha("zeros")})
        with mock.patch.object(bundles, "read_texts", wraps=bundles.read_texts) as read:
            self.assertEqual(official.official_japanese(b)[0], official.backup_path(b))
        self.assertNotIn(b, [c.args[0] for c in read.call_args_list])
        b = self.game("zeros", "zeros")  # identical to the backup, which is damaged: the patcher refuses it
        with self.assertRaises(official.OfficialBundleError) as cm:
            official.official_japanese(b)
        self.assertEqual(str(cm.exception), f"{b.name} is identical to {official.backup_path(b).name} and "
                                            f"{official.backup_path(b).name} is damaged (no TextAsset); "
                                            "verify the game files in Steam")

    def test_updated_official_with_most_terms_identical_is_taken_for_ours(self):
        # Documented limit of the content check: when an update translated more of our terms exactly like we
        # did than it left/translated differently, the bundle is taken for our output (backup is used).
        b = self.game("upd_many", "official", {"original_sha256": self.sha("official"), "patched_sha256": self.sha("patched")})
        self.assertEqual(official.official_japanese(b)[0], official.backup_path(b))

    def test_no_backup_official(self):
        for upd in ("official", "upd_few"):
            with self.subTest(upd=upd):
                b = self.game(upd)
                path, reason = official.official_japanese(b)
                self.assertEqual(path, b)
                self.assertIn("no backup", reason)

    # ---- the backup about to be returned must load and hold official content (Patcher.CheckBackup; round 3b R16)

    def test_patched_backup_matching_the_state_is_refused(self):
        """Before round 3b official_japanese() trusted a backup that matched the state file without reading it."""
        b = self.game("patched", "patched", {"original_sha256": self.sha("patched"), "patched_sha256": self.sha("patched")})
        with self.assertRaises(official.OfficialBundleError) as cm:
            official.official_japanese(b)
        self.assertEqual(str(cm.exception), f"{b.name} is our patched output ({official.state_path(b).name}) but "
                                            f"{official.backup_path(b).name} is patched data (10 of our translations), "
                                            "not the official file; verify the game files in Steam")

    def bad_backups(self):
        """{backup name: expected reason} for backups the patcher refuses (「バックアップが壊れています」 /
        「バックアップがパッチ適用後のデータになっています」)."""
        name = official.backup_path(h.ja_bundle(Path("g"))).name
        return {"zeros": f"{name} is damaged (no TextAsset)",
                "truncated": f"{name} is damaged (LZ4BlockError: ",
                "patched": f"{name} is patched data (10 of our translations), not the official file",
                "upd_many": f"{name} is patched data (6 of our translations), not the official file"}

    def test_damaged_or_patched_backup_is_never_returned(self):
        """Every branch that returns the backup checks it first (the hash checks of the branch come first, like the
        patcher); nothing is written. Before round 3b a missing or unreadable installed bundle with no state file
        returned such a backup, so extract.py extracted our translations as official Japanese."""
        p = self.sha("patched")
        for backup, why in self.bad_backups().items():
            bs = self.sha(backup)
            cases = {  # branch: (installed, state, expected message start)
                "missing, no state": (None, None, "localisation_assets_localisationjapanese.bundle is missing and "),
                "missing, 1.0.0 state": (None, {"original_sha256": bs, "patched_sha256": p}, " is missing and "),
                "damaged, no state": ("zeros" if backup != "zeros" else "truncated", None, " cannot be read ("),
                "state describes the bundle": ("upd_two", {"original_sha256": bs, "patched_sha256": self.sha("upd_two")},
                                               " is our patched output ("),
                "identical": (backup, None, " is identical to "),
                "our content, not recorded": ("patched" if backup != "patched" else "upd_many", None,
                                              " of our translations but "),
                "same version": ("upd_few", {"game_version": h.GAME_VERSION, "original_sha256": bs},
                                 " while the game version is unchanged, and "),
            }
            for what, (current, state, start) in cases.items():
                with self.subTest(backup=backup, branch=what):
                    b = self.game(current, backup, state)
                    before = h.snapshot_files(b.parents[4])
                    with self.assertRaises(official.OfficialBundleError) as cm:
                        official.official_japanese(b)
                    msg = str(cm.exception)
                    self.assertIn(start, msg)
                    self.assertIn(why, msg)
                    self.assertTrue(msg.endswith("; verify the game files in Steam"), msg)
                    self.assertEqual(h.snapshot_files(b.parents[4]), before)

    def test_backup_content_is_checked_before_the_recorded_hash_and_version(self):
        """Patcher.Apply runs CheckBackup before CheckBackupRecorded: a damaged backup that also differs from
        original_sha256 (or was taken for another version) is reported as damaged."""
        for state in ({"original_sha256": "1" * 64}, {"game_version": "10.2.0+old"}):
            for current in (None, "truncated"):
                with self.subTest(state=state, current=current):
                    b = self.game(current, "zeros", state)
                    with self.assertRaises(official.OfficialBundleError) as cm:
                        official.official_japanese(b)
                    self.assertIn(f"{official.backup_path(b).name} is damaged (no TextAsset)", str(cm.exception))
                    self.assertNotIn("original_sha256", str(cm.exception))
                    self.assertNotIn("taken for game version", str(cm.exception))

    def test_backup_hash_mismatch_is_reported_before_its_content_when_the_state_describes_the_bundle(self):
        """Patcher.CheckBackup(stateIsCurrent: true) compares original_sha256 before loading the backup."""
        b = self.game("patched", "zeros", {"original_sha256": self.sha("official"), "patched_sha256": self.sha("patched")})
        with self.assertRaises(official.OfficialBundleError) as cm:
            official.official_japanese(b)
        self.assertIn("does not match original_sha256", str(cm.exception))

    def test_official_backups_are_returned_by_every_branch(self):
        """The content check accepts the official file and an official update (also one translating a few of our
        terms identically: upd_few, 4 of ours vs 3 + 3)."""
        for backup in ("official", "upd_few", "upd_all_other"):
            bs = self.sha(backup)
            same = {"game_version": h.GAME_VERSION, "original_sha256": bs}  # S1: the state must prove the version
            for current, state in ((None, same), ("zeros", same), (backup, None), ("patched", None),
                                   ("upd_two", {"original_sha256": bs, "patched_sha256": self.sha("upd_two")}),
                                   ("official" if backup != "official" else "upd_all_other",
                                    {"game_version": h.GAME_VERSION, "original_sha256": bs})):
                with self.subTest(backup=backup, current=current):
                    b = self.game(current, backup, state)
                    self.assertEqual(official.official_japanese(b)[0], official.backup_path(b))

    def test_missing_or_damaged_bundle_needs_a_state_proving_the_version(self):
        """S1 (round 3c): the R1 chain (a backup used without knowing its version gets the new version recorded and then
        defeats the Steam verification) starts here; like Patcher.CheckBackupRecorded, no state file and a 1.0.0 state
        refuse with the 「確認できません」 reason, and nothing is written. The backup's content is still checked first."""
        o, p = self.sha("official"), self.sha("patched")
        for current in (None, "zeros", "truncated"):
            for what, state in (("no state", None), ("1.0.0 state", {"original_sha256": o, "patched_sha256": p})):
                with self.subTest(current=current, state=what):
                    b = self.game(current, "official", state)
                    before = h.snapshot_files(b.parents[4])
                    with self.assertRaises(official.OfficialBundleError) as cm:
                        official.official_japanese(b)
                    self.assertIn(self.unconfirmed("unknown", h.GAME_VERSION), str(cm.exception))
                    self.assertEqual(h.snapshot_files(b.parents[4]), before)
                    official.write_state(b, {"game_version": h.GAME_VERSION})  # what a verified apply records
                    self.assertEqual(official.official_japanese(b)[0], official.backup_path(b))
        b = self.game(None, "zeros", None)
        with self.assertRaises(official.OfficialBundleError) as cm:
            official.official_japanese(b)
        self.assertIn(f"{official.backup_path(b).name} is damaged (no TextAsset)", str(cm.exception))

    # ---- formats the patcher does not read (LocalisationBundle's UnsupportedFormatException; round 3c)

    def fake_unitypy(self, header: bytes, flags: int = 4, only: Path | None = None):
        """official.UnityPy stand-in whose bundle holds one raw entry (as UnityPy keeps a file it does not parse) with
        this header; only: for that file's content alone (others are loaded by UnityPy)."""
        class Entry:
            def __init__(self):
                self.flags, self.Position, self.Length = flags, 0, len(header)

            def read_bytes(self, n):
                self.Position += n
                return header[self.Position - n:self.Position]

        def load(data):
            if only is not None and data != only.read_bytes():
                return UnityPy.load(data)
            return types.SimpleNamespace(files={"bundle": types.SimpleNamespace(files={"CAB-x": Entry()})})
        return types.SimpleNamespace(load=load)

    def test_unsupported_format_rule(self):
        """UnityFS format version 1..99 other than 6..8, or an entry flagged as a serialized file (4) whose header has
        version 1..99 other than 22 or is big endian; 0, 100 and above or another endianness byte are damage."""
        b = self.bundles
        self.assertIsNone(official._unsupported(b["official"], True))  # UnityFS 8, serialized file 22, little endian
        self.assertIsNone(official._unsupported(b["patched"], True))  # written by UnityPy (write_texts)
        for name, readable, want in (("fs9", True, "UnityFS version 9"), ("fs9", False, "UnityFS version 9"),
                                     ("fs5", True, "UnityFS version 5"), ("fs0", True, None), ("fs100", True, None),
                                     ("zeros", False, None), ("truncated", False, None),
                                     ("sf23", False, None), ("sf_big", False, None)):  # UnityPy cannot read these
            with self.subTest(name=name, readable=readable):
                self.assertEqual(official._unsupported(b[name], readable), want)
        sf = self.sf_header[:20]
        for header, flags, want in ((sf, 4, None),
                                    (sf[:8] + (23).to_bytes(4, "big") + sf[12:], 4, "serialized file version 23"),
                                    (sf[:8] + (21).to_bytes(4, "big") + sf[12:], 4, "serialized file version 21"),
                                    (sf[:8] + (0).to_bytes(4, "big") + sf[12:], 4, None),
                                    (sf[:8] + (100).to_bytes(4, "big") + sf[12:], 4, None),
                                    (sf[:16] + b"\x01" + sf[17:], 4, "big-endian serialized file"),
                                    (sf[:16] + b"\x02" + sf[17:], 4, None),
                                    (sf[:8] + (23).to_bytes(4, "big") + sf[12:], 0, None),  # not a serialized file
                                    (sf[:8] + (23).to_bytes(4, "big") + sf[12:19], 4, None)):  # under 20 bytes
            with self.subTest(header=header.hex(), flags=flags):
                with mock.patch.object(official, "UnityPy", self.fake_unitypy(header, flags)):
                    self.assertEqual(official._unsupported(b["official"], True), want)
                    self.assertIsNone(official._unsupported(b["official"], False))

    def test_unsupported_installed_bundle_is_not_trusted_without_a_proven_game_version(self):
        """Parity with Patcher since round 3c (S2/S7/S8/S15): a format the patcher does not read (e.g. after a game update)
        is never rebuilt from the backup and never patched; only a state proving the same game version (then the
        format bytes are damage) lets the backup stand in. Before, UnityPy read UnityFS 9 and returned the installed
        file ('stale' or 'no backup'), while the exe refused."""
        o, p, v = self.sha("official"), self.sha("patched"), h.GAME_VERSION
        name, backup = h.JA_BUNDLE_NAME, official.backup_path(h.ja_bundle(Path("g"))).name
        start = f"{name} is in a format TpcJpPatch.exe does not read (UnityFS version 9)"
        unused = f"; {backup} is not used (the state file does not prove the same game version)"
        end = "; verify the game files in Steam, and if that does not help, the patch needs an update"
        for what, (state, txt, has_backup) in {
                "no backup, no state": (None, v, False),
                "no state": (None, v, True),
                "1.0.0 state": ({"original_sha256": o, "patched_sha256": p}, v, True),
                "older game version": ({"game_version": "10.2.0+old", "original_sha256": o}, v, True),
                "version.txt missing": ({"game_version": v, "original_sha256": o}, None, True),
                "same version, no backup": ({"game_version": v, "original_sha256": o}, v, False)}.items():
            with self.subTest(state=what):
                b = self.game("fs9", "official" if has_backup else None, state)
                self.set_version_txt(b, txt)
                before = h.snapshot_files(b.parents[4])
                with self.assertRaises(official.OfficialBundleError) as cm:
                    official.official_japanese(b)
                self.assertEqual(str(cm.exception), start + (unused if has_backup else "") + end)
                self.assertEqual(h.snapshot_files(b.parents[4]), before)
        b = self.game("fs9", "official", {"game_version": v, "original_sha256": o})
        path, reason = official.official_japanese(b)
        self.assertEqual(path, official.backup_path(b))
        self.assertIn("but the game version is unchanged", reason)
        # a serialized file UnityPy can read (simulated): the same rule
        state = {"original_sha256": o, "patched_sha256": p}
        header = self.sf_header[:8] + (23).to_bytes(4, "big") + self.sf_header[12:20]
        b = self.game("upd_few", "official", state)
        self.assertIn("stale", official.official_japanese(b)[1])
        with mock.patch.object(official, "UnityPy", self.fake_unitypy(header, only=b)):
            with self.assertRaises(official.OfficialBundleError) as cm:
                official.official_japanese(b)
            self.assertEqual(str(cm.exception), f"{name} is in a format TpcJpPatch.exe does not read (serialized file "
                                                f"version 23){unused}{end}")
            official.write_state(b, {"game_version": v})
            self.assertEqual(official.official_japanese(b)[0], official.backup_path(b))

    def test_unsupported_serialized_file_unreadable_by_unitypy_counts_as_damaged(self):
        """The documented difference: a serialized file UnityPy cannot read is not detected as a format here; it takes the
        damaged branch, which since S1 gives the same result (the backup only for a proven unchanged game version)."""
        o = self.sha("official")
        for current in ("sf23", "sf_big"):
            with self.subTest(current=current):
                b = self.game(current, "official", {"original_sha256": o})
                with self.assertRaises(official.OfficialBundleError) as cm:
                    official.official_japanese(b)
                self.assertTrue(str(cm.exception).startswith(f"{b.name} cannot be read ("), str(cm.exception))
                self.assertIn(self.unconfirmed("unknown", h.GAME_VERSION), str(cm.exception))
                official.write_state(b, {"game_version": h.GAME_VERSION})
                path, reason = official.official_japanese(b)
                self.assertEqual(path, official.backup_path(b))
                self.assertTrue(reason.startswith("WARNING: the installed bundle cannot be read ("), reason)

    def test_unsupported_files_found_by_hash_are_not_loaded_but_an_unsupported_backup_is_refused(self):
        """The hash checks come first, like the patcher (the recorded official file is taken without loading it); a
        backup the patcher cannot load is 「バックアップが壊れています」 for Patcher.CheckBackup, so here as well."""
        b = self.game("fs9", None, {"original_sha256": self.sha("fs9")})
        self.assertEqual(official.official_japanese(b)[0], b)  # recorded official file
        damaged = f"{official.backup_path(b).name} is damaged (a format TpcJpPatch.exe does not read: UnityFS version 9)"
        for current, state in (("fs9", None), ("patched", {"original_sha256": self.sha("fs9"), "patched_sha256": self.sha("patched")}),
                               (None, {"game_version": h.GAME_VERSION, "original_sha256": self.sha("fs9")})):
            with self.subTest(current=current):
                b = self.game(current, "fs9", state)
                with self.assertRaises(official.OfficialBundleError) as cm:
                    official.official_japanese(b)
                self.assertIn(damaged, str(cm.exception))

    # ---- state file

    def test_read_state_treats_a_folder_as_no_state_file(self):
        """S19: like File.Exists in Patcher.ReadState, a folder at the state path is no state file (read_text raised
        PermissionError: a traceback in extract.py, verify_bundle.py and dev_patch.py)."""
        b = self.game("official", "official")
        official.state_path(b).mkdir()
        self.assertIsNone(official.read_state(b))
        path, reason = official.official_japanese(b)
        self.assertEqual(path, official.backup_path(b))
        self.assertIn("identical", reason)
        b.unlink()
        with self.assertRaises(official.OfficialBundleError) as cm:  # no state proves the version (S1)
            official.official_japanese(b)
        self.assertIn(self.unconfirmed("unknown", h.GAME_VERSION), str(cm.exception))

    def test_read_state_format(self):
        b = self.game("official")
        p = official.state_path(b)
        p.write_bytes("﻿a=1\r\nb=x=y\r\nnoequals\r\n=empty\r\nc=\r\n".encode("utf-8"))
        self.assertEqual(official.read_state(b), {"a": "1", "b": "x=y", "c": ""})
        p.unlink()
        self.assertIsNone(official.read_state(b))

    def test_read_state_splits_lines_like_file_read_all_lines(self):
        """R18: File.ReadAllLines ends a line at CR LF, CR or LF only; str.splitlines also split at U+0085, U+2028,
        U+2029, VT, FF and FS-RS, so Python could read keys the patcher does not see (e.g. a forged game_version)."""
        b = self.game("official")
        p = official.state_path(b)
        seps = "\x85  \x0b\x0c\x1c\x1d\x1e"
        value = "1.0.0" + "".join(f"{c}k{i}=v" for i, c in enumerate(seps))
        p.write_bytes(f"patch_version={value}\rgame_version=A\nlast=B\r\nc=C".encode("utf-8"))
        self.assertEqual(official.read_state(b), {"patch_version": value, "game_version": "A", "last": "B", "c": "C"})
        p.write_bytes(b"a=x\xff\xfey\r\nb=2\r\n")  # undecodable bytes: U+FFFD like .NET, not a UnicodeDecodeError
        self.assertEqual(official.read_state(b), {"a": "x��y", "b": "2"})

    def test_write_state_refuses_line_breaks(self):
        """A CR or LF in a value would add a line (key) to the state file; nothing is written then."""
        b = self.game("official")
        h.write_state(b, keep="1")
        before = official.state_path(b).read_bytes()
        for updates in ({"game_version": "1.0\ngame_version=FORGED"}, {"x": "a\rb"}, {"a\nb": "1"}, {"a=b": "1"},
                        {"": "1"}):
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    official.write_state(b, updates)
                self.assertEqual(official.state_path(b).read_bytes(), before)
        official.write_state(b, {"game_version": "10.4 x"})  # not a line break for File.ReadAllLines
        self.assertEqual(official.read_state(b), {"keep": "1", "game_version": "10.4 x"})
        self.assertEqual(sorted(p.name for p in b.parent.iterdir()), sorted([b.name, official.state_path(b).name]))

    def test_write_state_keeps_other_keys(self):
        b = self.game("official")
        h.write_state(b, keep="1", patched_sha256="old")
        official.write_state(b, {"patched_sha256": "new", "added": "a=b"})
        raw = official.state_path(b).read_bytes()
        self.assertEqual(raw, b"keep=1\r\npatched_sha256=new\r\nadded=a=b\r\n")
        self.assertEqual(sorted(p.name for p in b.parent.iterdir()),
                         sorted([b.name, official.state_path(b).name]))

    def test_paths(self):
        b = Path("x/localisation_assets_localisationjapanese.bundle")
        self.assertEqual(official.backup_path(b).name, b.name + ".tpcjp-original")
        self.assertEqual(official.state_path(b).name, b.name + ".tpcjp-state")


if __name__ == "__main__":
    unittest.main()
