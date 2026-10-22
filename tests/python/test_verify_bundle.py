"""tools/verify_bundle.py on sandbox games (fake TPC.exe + bundle copies under %TEMP%\\tpcjp_pytests).

Patched bundles are made with a Python port of the patcher's fill-only rule (helpers.fill_texts) and
bundles.write_texts; one end-to-end test uses the real Debug patcher (apply --no-language on a sandbox).
"""
import json
import shutil
import unittest

from . import helpers as h
from tpcloc import bundles, i2cache, text

SRC = "I2LS_Mods"


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


@unittest.skipUnless(h.OFFICIAL_BUNDLE.exists(), "official bundle copy missing")
class VerifyBundle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = h.TEMP_ROOT / "t" / "verify_cls"
        shutil.rmtree(cls.base, ignore_errors=True)
        cls.base.mkdir(parents=True)
        if not h.KEEP:
            cls.addClassCleanup(shutil.rmtree, cls.base, True)
        cls.official = bundles.read_texts(h.OFFICIAL_BUNDLE)
        cls.official_sha = h.sha256_file(h.OFFICIAL_BUNDLE)
        mods = i2cache.to_dict(cls.official[SRC])
        untranslated = [k for k, v in i2cache.parse(cls.official[SRC]) if v.strip() and not text.has_japanese(v)]
        untranslated = list(dict.fromkeys(untranslated))
        cls.keys = untranslated[:10]
        cls.ref_key, cls.changed_key = untranslated[10], untranslated[11]
        cls.untouched_key = untranslated[12]
        # an official Japanese text elsewhere for the reference line
        cls.ref_target = next((s, k) for s, t in cls.official.items() if s != SRC
                              for k, v in i2cache.parse(t) if text.has_japanese(v) and len(v) > 3)
        cls.ours = {k: f"当パッチの訳{i}\n改行\\n" for i, k in enumerate(cls.keys)}
        lines = ["#TPCJP-PATCH\t1", "#patch_version\t9.9.9", f"#game_version\t{h.GAME_VERSION}",
                 f"#bundle_sha256\t{cls.official_sha}", "#built\t2026-01-01T00:00:00", "#entries\t12"]
        for k in cls.keys:
            lines.append(f"{SRC}\t{esc(k)}\t{h.base_hash(mods[k])}\t{esc(cls.ours[k])}")
        lines.append(f"&{SRC}\t{esc(cls.ref_key)}\t{h.base_hash(mods[cls.ref_key])}\t{cls.ref_target[0]}\t{esc(cls.ref_target[1])}")
        # built against an older English value: the patcher keeps the official value ("source changed")
        lines.append(f"{SRC}\t{esc(cls.changed_key)}\t{'0' * 16}\t古い原文の訳")
        cls.data_text = "\n".join(lines) + "\n"
        cls.data = cls.base / "data.tsv"
        cls.data.write_text(cls.data_text, "utf-8", newline="\n")
        entries = {k: h.entry(v) for k, v in cls.ours.items()}
        entries[cls.changed_key] = h.entry("古い原文の訳")
        cls.project = h.make_project(cls.base / "proj", {}, ours={SRC: entries}, ja_sha=cls.official_sha)
        # work/backup/official: used when the game folder has no backup
        (cls.project / "work" / "backup" / "official").mkdir(parents=True)
        shutil.copyfile(h.OFFICIAL_BUNDLE, cls.project / "work" / "backup" / "official" / h.JA_BUNDLE_NAME)
        cls.bundles = {"official": h.OFFICIAL_BUNDLE}
        cls.patched_texts = h.fill_texts(cls.official, cls.data_text)
        assert set(cls.patched_texts) == {SRC}, cls.patched_texts.keys()
        cls.make("patched", cls.patched_texts)
        p = dict(i2cache.parse(cls.patched_texts[SRC]))
        cls.make("wrong", {SRC: cls.replace(cls.patched_texts[SRC], {cls.keys[0]: "違う訳"})})
        cls.make("changed", {SRC: cls.replace(cls.patched_texts[SRC], {cls.untouched_key: "勝手な変更"})})
        cls.make("updated", {SRC: cls.replace(cls.official[SRC], {k: f"公式の新訳{i}" for i, k in enumerate(cls.keys)})})
        cls.make("updated2", {SRC: cls.replace(cls.official[SRC], {cls.untouched_key: "公式の別の変更"})})  # official too
        assert p[cls.keys[0]] == cls.ours[cls.keys[0]]
        raw = h.OFFICIAL_BUNDLE.read_bytes()
        cls.bundles["truncated"] = cls.base / "truncated.bundle"  # damaged: UnityPy fails to decompress it
        cls.bundles["truncated"].write_bytes(raw[: len(raw) // 2])

    @classmethod
    def replace(cls, cache: str, values: dict) -> str:
        return i2cache.dumps([(k, values.get(k, v)) for k, v in i2cache.parse(cache)])

    @classmethod
    def make(cls, name, repl):
        dst = cls.base / f"{name}.bundle"
        bundles.write_texts(h.OFFICIAL_BUNDLE, dst, repl)
        cls.bundles[name] = dst

    def sha(self, name):
        return h.sha256_file(self.bundles[name])

    def game(self, current, backup="official", state="auto"):
        g = h.make_game(h.new_dir(self), japanese=self.bundles[current])
        b = h.ja_bundle(g)
        if backup:
            shutil.copyfile(self.bundles[backup], b.with_name(b.name + ".tpcjp-original"))
        if state == "auto":
            h.write_state(b, patch_version="9.9.9", original_sha256=self.sha(backup or "official"),
                          patched_sha256=h.sha256_file(b))
        elif state:
            h.write_state(b, **state)
        return g

    def verify(self, game, *args, data=None):
        return h.run_tool("verify_bundle.py", "--data", data or self.data, *args, project=self.project, game=game)

    def counts(self, r):
        return next(l for l in r.out.splitlines() if l.startswith("translated occurrences OK"))

    # ------------------------------------------------------------------ results

    def test_pass(self):
        g = self.game("patched")
        before = h.snapshot_files(g)
        r = self.verify(g)
        self.assertEqual(r.code, 0, r)
        self.assertIn("official data: installed bundle is our patched output", r.out)
        total = sum(len(i2cache.parse(t)) for t in self.official.values())
        occurrences = sum(1 for k, _ in i2cache.parse(self.official[SRC]) if k in self.keys or k == self.ref_key)
        changed = sum(1 for k, _ in i2cache.parse(self.official[SRC]) if k == self.changed_key)
        self.assertEqual(self.counts(r), f"translated occurrences OK: {occurrences}, not applied: 0, "
                                         f"official unchanged: {total - occurrences - changed}, unexpected changes: 0")
        self.assertIn(f"official kept (official Japanese changed since the build): 0, source changed (rebuild the patch): "
                      f"{changed}, missing in game data: 0", r.out)
        self.assertNotIn("WARNING", r.out)
        self.assertEqual(h.snapshot_files(g), before)  # read-only

    def test_wrong_text(self):
        r = self.verify(self.game("wrong"))
        self.assertEqual(r.code, 1, r)
        self.assertIn(f"NOT APPLIED {SRC}|{self.keys[0]}", r.out)
        self.assertIn("not applied: 1,", self.counts(r))

    def test_unexpected_change(self):
        r = self.verify(self.game("changed"))
        self.assertEqual(r.code, 1, r)
        self.assertIn(f"CHANGED {SRC}|{self.untouched_key}", r.out)
        self.assertIn("unexpected changes: 1", self.counts(r))

    def test_unpatched_game(self):
        r = self.verify(self.game("official", state=None))
        self.assertEqual(r.code, 1, r)
        self.assertIn("official data: installed bundle is identical to", r.out)
        self.assertEqual(len(r.lines("NOT APPLIED")), 11)

    def test_backup_patched(self):
        """Since round 3b (R16) tpcloc/official.py refuses a patched backup itself, like Patcher.CheckBackup."""
        g = self.game("patched", backup="patched",
                      state={"original_sha256": self.sha("patched"), "patched_sha256": self.sha("patched")})
        r = self.verify(g)
        self.assert_nothing_compared(r, f"ERROR {h.JA_BUNDLE_NAME} is our patched output ({h.JA_BUNDLE_NAME}.tpcjp-state) "
                                        f"but {h.JA_BUNDLE_NAME}.tpcjp-original is patched data (10 of our translations), "
                                        "not the official file; verify the game files in Steam")

    def test_backup_damaged(self):
        for current, state in (("patched", {"original_sha256": self.sha("truncated"), "patched_sha256": self.sha("patched")}),
                               ("truncated", None)):
            with self.subTest(current=current):
                r = self.verify(self.game(current, backup="truncated", state=state))
                self.assert_nothing_compared(r, f"{h.JA_BUNDLE_NAME}.tpcjp-original is damaged (LZ4BlockError: ",
                                             "verify the game files in Steam")
                self.assertNotIn("Traceback", r.out)

    def test_patched_work_backup_is_still_refused(self):
        """Without a backup in the game folder verify_bundle.py compares with work/backup/official, which it checks
        itself."""
        work = self.project / "work" / "backup" / "official" / h.JA_BUNDLE_NAME
        self.addCleanup(shutil.copyfile, h.OFFICIAL_BUNDLE, work)
        shutil.copyfile(self.bundles["patched"], work)
        r = self.verify(self.game("patched", backup=None, state=None))
        self.assert_nothing_compared(r, f"ERROR {h.JA_BUNDLE_NAME} already holds 10 of our translations (patched data, "
                                        "not official)")

    def test_backup_not_matching_original_sha(self):
        g = self.game("patched", backup="updated",
                      state={"original_sha256": self.official_sha, "patched_sha256": self.sha("patched")})
        r = self.verify(g)
        self.assertEqual(r.code, 1, r)
        self.assertIn("does not match original_sha256", r.out)
        self.assertIn("nothing compared", r.out)

    def test_stale_state(self):
        g = self.game("patched", state={"original_sha256": self.official_sha, "patched_sha256": "0" * 64})
        r = self.verify(g)
        self.assertEqual(r.code, 1, r)
        self.assertIn("(stale state file)", r.out)
        self.assertIn("nothing compared", r.out)

    def test_state_original_sha_wrong(self):
        # content check finds our output (state not current), but the recorded original does not match the backup
        g = self.game("patched", state={"original_sha256": "1" * 64, "patched_sha256": "0" * 64})
        r = self.verify(g)
        self.assertEqual(r.code, 1, r)
        self.assertIn("original_sha256 in", r.out)

    def test_updated_official_with_stale_backup(self):
        g = self.game("updated", state={"original_sha256": self.official_sha, "patched_sha256": self.sha("patched")})
        r = self.verify(g)
        self.assertEqual(r.code, 1, r)
        self.assertIn("is stale", r.out)
        self.assertIn("the installed bundle is not our patched output", r.out)

    def test_no_backup_in_game_folder_uses_work_backup(self):
        g = self.game("patched", backup=None, state=None)
        r = self.verify(g)
        self.assertEqual(r.code, 0, r)
        self.assertIn("official data: no backup in the game folder; comparing with", r.out)

    # ---- tpcloc/official.py warnings and errors stop the check (round 3: same rules as the patcher)

    def assert_nothing_compared(self, r, *parts):
        self.assertEqual(r.code, 1, r)
        self.assertIn("the official baseline cannot be trusted; nothing compared", r.out)
        self.assertNotIn("translated occurrences OK", r.out)
        errors = r.lines("ERROR ")
        self.assertEqual(len(errors), 1, r)
        for p in parts:
            self.assertIn(p, errors[0])

    def test_missing_bundle(self):
        for backup in ("official", None):
            with self.subTest(backup=backup):
                g = self.game("patched", backup=backup)
                h.ja_bundle(g).unlink()
                r = self.verify(g)
                self.assertEqual(r.code, 1, r)
                self.assertIn(f"{h.JA_BUNDLE_NAME} is missing; nothing compared (verify the game files in Steam)", r.out)
                self.assertNotIn("Traceback", r.out)

    def test_damaged_bundle_with_a_fitting_backup(self):
        g = self.game("truncated", state={"game_version": h.GAME_VERSION, "original_sha256": self.official_sha,
                                          "patched_sha256": self.sha("patched")})
        r = self.verify(g)
        self.assert_nothing_compared(r, "ERROR the installed bundle cannot be read (LZ4BlockError: ",
                                     f"official data from {h.JA_BUNDLE_NAME}.tpcjp-original; "
                                     "the installed bundle is not our patched output")

    def test_damaged_bundle_with_a_backup_of_another_game_version(self):
        g = self.game("truncated", state={"game_version": "10.2.0+old", "original_sha256": self.official_sha,
                                          "patched_sha256": self.sha("patched")})
        r = self.verify(g)
        self.assert_nothing_compared(r, f"ERROR {h.JA_BUNDLE_NAME} cannot be read (",
                                     "was taken for game version 10.2.0+old but the installed version is "
                                     f"{h.GAME_VERSION}; verify the game files in Steam")

    def test_damaged_bundle_with_a_backup_of_an_unconfirmed_game_version(self):
        """S1 (round 3c): a state of TpcJpPatch 1.0.0 (no game_version) or none no longer vouches for the backup."""
        for state in ({"original_sha256": self.official_sha, "patched_sha256": self.sha("patched")}, None):
            with self.subTest(state=state):
                r = self.verify(self.game("truncated", state=state))
                self.assert_nothing_compared(r, f"ERROR {h.JA_BUNDLE_NAME} cannot be read (",
                                             f"it cannot be confirmed which game version {h.JA_BUNDLE_NAME}.tpcjp-original "
                                             f"was taken for (recorded: unknown, installed: {h.GAME_VERSION}); "
                                             "verify the game files in Steam")

    def test_folder_at_the_state_path_is_no_state_file(self):
        """S19: like File.Exists in the patcher (verify_bundle.py stopped with a PermissionError traceback)."""
        g = self.game("patched", state=None)
        h.ja_bundle(g).with_name(h.JA_BUNDLE_NAME + ".tpcjp-state").mkdir()
        r = self.verify(g)
        self.assertEqual(r.code, 0, r)
        self.assertIn("holds 10 of our translations (not recorded in the state file)", r.out)
        self.assertNotIn("Traceback", r.out)

    def test_damaged_bundle_without_backup(self):
        r = self.verify(self.game("truncated", backup=None, state=None))
        self.assertEqual(r.code, 1, r)
        self.assertIn(f"{h.JA_BUNDLE_NAME} cannot be read (LZ4BlockError: ", r.out)
        self.assertIn("nothing compared (verify the game files in Steam)", r.out)
        self.assertNotIn("Traceback", r.out)

    def test_same_game_version_modified_bundle(self):
        g = self.game("updated", state={"game_version": h.GAME_VERSION, "original_sha256": self.official_sha,
                                        "patched_sha256": self.sha("patched")})
        r = self.verify(g)
        self.assert_nothing_compared(r, "but the game version is unchanged (modified or damaged file)",
                                     "the installed bundle is not our patched output")

    def test_same_game_version_backup_not_matching_original_sha(self):
        state = {"game_version": h.GAME_VERSION, "original_sha256": self.official_sha, "patched_sha256": self.sha("patched")}
        r = self.verify(self.game("updated", backup="updated2", state=state))
        self.assert_nothing_compared(r, "while the game version is unchanged",
                                     f"{h.JA_BUNDLE_NAME}.tpcjp-original does not match original_sha256")
        # a patched backup is reported as such first (Patcher.CheckBackup runs before CheckBackupRecorded; round 3b)
        r = self.verify(self.game("updated", backup="wrong", state=state))
        self.assert_nothing_compared(r, "while the game version is unchanged",
                                     f"{h.JA_BUNDLE_NAME}.tpcjp-original is patched data (9 of our translations)")

    def test_installed_official_recorded_in_the_state_with_a_different_backup(self):
        g = self.game("official", backup="updated", state={"original_sha256": self.official_sha,
                                                           "patched_sha256": self.sha("patched")})
        r = self.verify(g)
        self.assert_nothing_compared(r, "which is the official file recorded in",
                                     "the installed bundle is not our patched output")

    def test_extract_sha_mismatch_is_a_warning(self):
        strings = self.project / "work" / "extract" / "strings.json"
        orig = strings.read_bytes()
        self.addCleanup(strings.write_bytes, orig)
        d = json.loads(orig)
        d["bundles"]["Japanese"]["sha256"] = "f" * 64
        h.write_json(strings, d)
        r = self.verify(self.game("patched"))
        self.assertEqual(r.code, 0, r)
        self.assertIn("sha256 differs from work/extract/strings.json (it is not the extracted official bundle)", r.out)
        d["game_version"] = "0.0.0"
        h.write_json(strings, d)
        r = self.verify(self.game("patched"))
        self.assertIn("the game was updated since the extract", r.out)

    def test_extract_sha_mismatch_hint_reads_version_txt_like_the_patcher(self):
        """S18 (round 3c): the hint read version.txt with paths.game_version's old reader: a traceback when it was
        missing or unreadable, and a UTF-8 BOM made the same version look like an update."""
        strings = self.project / "work" / "extract" / "strings.json"
        orig = strings.read_bytes()
        self.addCleanup(strings.write_bytes, orig)
        d = json.loads(orig)
        d["bundles"]["Japanese"]["sha256"] = "f" * 64
        h.write_json(strings, d)
        g = self.game("patched")
        (g / "version.txt").unlink()
        r = self.verify(g)
        self.assertEqual(r.code, 0, r)
        self.assertIn("sha256 differs from work/extract/strings.json (it is not the extracted official bundle; "
                      "version.txt is missing, empty or unreadable)", r.out)
        self.assertNotIn("Traceback", r.out)
        g = self.game("patched")
        (g / "version.txt").write_bytes(b"\xef\xbb\xbf" + h.GAME_VERSION.encode("utf-8") + b"\r\n")
        r = self.verify(g)
        self.assertEqual(r.code, 0, r)
        self.assertIn("sha256 differs from work/extract/strings.json (it is not the extracted official bundle)", r.out)

    def test_data_differing_from_translations_is_a_warning(self):
        other = self.base / "data_other.tsv"
        other.write_text(self.data_text.replace(esc(self.ours[self.keys[1]]), "データだけの訳"), "utf-8", newline="\n")
        # the installed bundle carries the data's text
        texts = h.fill_texts(self.official, other.read_text("utf-8"))
        dst = h.new_dir(self) / "x.bundle"
        bundles.write_texts(h.OFFICIAL_BUNDLE, dst, texts)
        g = h.make_game(h.new_dir(self), japanese=dst)
        b = h.ja_bundle(g)
        shutil.copyfile(h.OFFICIAL_BUNDLE, b.with_name(b.name + ".tpcjp-original"))
        h.write_state(b, original_sha256=self.official_sha, patched_sha256=h.sha256_file(b))
        r = self.verify(g, data=other)
        self.assertEqual(r.code, 0, r)
        self.assertIn("WARNING: 1 entries in data_other.tsv differ from translations/ja", r.out)
        self.assertIn(f"DATA DIFFERS {SRC}|{self.keys[1]}", r.out)

    def test_missing_term_in_game_data(self):
        other = self.base / "data_missing.tsv"
        other.write_text(self.data_text + f"{SRC}\tUI/Mods/DoesNotExist\t{'0' * 16}\t無い\n", "utf-8", newline="\n")
        r = self.verify(self.game("patched"), data=other)
        self.assertEqual(r.code, 0, r)
        self.assertIn("missing in game data: 1", r.out)

    def test_status_option_is_ignored(self):
        r = self.verify(self.game("patched"), "--status", "reviewed")
        self.assertEqual(r.code, 0, r)
        self.assertIn("note: --status is ignored", r.out)

    def test_not_a_patch_data_file(self):
        bad = self.base / "bad.tsv"
        bad.write_text("hello\n", "utf-8")
        r = self.verify(self.game("patched"), data=bad)
        self.assertEqual(r.code, 1, r)
        self.assertIn("not a patch data file", r.out)


@unittest.skipUnless(h.OFFICIAL_BUNDLE.exists() and h.REAL_DATA.exists() and h.DEBUG_EXE.exists()
                     and h.REAL_STRINGS.exists(), "official bundle / dist data / Debug exe missing")
class EndToEndWithDebugPatcher(unittest.TestCase):
    def test_debug_patcher_output_verifies(self):
        root = h.new_dir(self)
        project = root / "proj"
        (project / "work" / "extract").mkdir(parents=True)
        shutil.copyfile(h.REAL_STRINGS, project / "work" / "extract" / "strings.json")
        shutil.copytree(h.PROJECT / "translations" / "ja", project / "translations" / "ja")
        game = h.make_game(root / "game")
        code, out = h.run_debug_exe("apply", "--data", h.REAL_DATA, game=game, appdata=root / "appdata")
        self.assertEqual(code, 0, out)
        r = h.run_tool("verify_bundle.py", "--data", h.REAL_DATA, project=project, game=game)
        self.assertEqual(r.code, 0, r)
        self.assertIn("not applied: 0", r.out)
        self.assertIn("unexpected changes: 0", r.out)
        self.assertNotIn("DATA DIFFERS", r.out)
        # the Python port of the fill-only rule produces the same texts as the patcher
        want = h.fill_texts(bundles.read_texts(h.OFFICIAL_BUNDLE), h.REAL_DATA.read_text("utf-8"))
        got = bundles.read_texts(h.ja_bundle(game))
        for name, t in want.items():
            self.assertEqual(got[name], t, name)


if __name__ == "__main__":
    unittest.main()
