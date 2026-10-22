"""tools/dev_patch.py on sandbox games (imported in-process; tpcloc.paths and find_game_dir point at temp folders).

Round 3b (findings R17, R1, R25): apply records game_version like TpcJpPatch.exe (only for a new backup or an
installed bundle identical to the backup; only refreshed for our own output; kept when only the state file vouched
for the backup), status no longer crashes on a missing bundle, and restore treats the official file recorded in
the state file like the exe (公式のまま: backup and state removed, the bundle is not overwritten).
Round 3c: S1 through tpcloc/official.py (no state file or a 1.0.0 state no longer vouches for the backup of a
missing or damaged bundle), S16 (the R3 guard matrix; S3: the recorded official file backed up again keeps a known
version), S4/S17 (a version.txt with a line break is recorded on one line, no ValueError after writing the bundle).
"""
import contextlib
import io
import shutil
import sys
import unittest
from unittest import mock

from . import helpers as h
from tpcloc import bundles, i2cache, official, paths, text

SRC = "I2LS_Mods"
NOW = "RECORDED-NOW"  # what official.installed_version returns in tests that must show whether it was written


@unittest.skipUnless(h.OFFICIAL_BUNDLE.exists(), "official bundle copy missing")
class DevPatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = h.TEMP_ROOT / "t" / "dev_patch_cls"
        shutil.rmtree(cls.root, ignore_errors=True)
        cls.root.mkdir(parents=True)
        if not h.KEEP:
            cls.addClassCleanup(shutil.rmtree, cls.root, True)
        mods = i2cache.parse(bundles.read_texts(h.OFFICIAL_BUNDLE)[SRC])
        keys = list(dict.fromkeys(k for k, v in mods if v.strip() and not text.has_japanese(v)))[:10]
        cls.ours = {k: f"開発用の訳{i}" for i, k in enumerate(keys)}
        cls.official_sha = h.sha256_file(h.OFFICIAL_BUNDLE)
        worklist = [{"source": SRC, "key": k} for k in keys]
        cls.project = h.make_project(cls.root / "proj", {}, worklist=worklist, ja_sha=cls.official_sha,
                                     ours={SRC: {k: h.entry(v) for k, v in cls.ours.items()}})
        cls.bundles = {"official": h.OFFICIAL_BUNDLE}

        def make(name, values):
            dst = cls.root / f"{name}.bundle"
            bundles.write_texts(h.OFFICIAL_BUNDLE, dst, {SRC: i2cache.dumps([(k, values.get(k, v)) for k, v in mods])})
            cls.bundles[name] = dst

        make("patched", cls.ours)  # what dev_patch apply writes from the official backup
        make("upd", {k: f"公式{k}" for k in keys})  # an official update (other Japanese for our terms)
        cls.bundles["zeros"] = cls.root / "zeros.bundle"
        cls.bundles["zeros"].write_bytes(b"\0" * 4096)

    def setUp(self):
        ctx = h.project_paths(self.project)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.dev = h.load_tool("dev_patch")
        self.dev.game_running = lambda: False

    def sha(self, name):
        return h.sha256_file(self.bundles[name])

    def game(self, current, backup=None, state=None, version=h.GAME_VERSION):
        """current/backup: bundle names or None (no file); state: dict or None (no file); version None: no version.txt."""
        g = h.make_game(h.new_dir(self), japanese=self.bundles[current] if current else None)
        b = h.ja_bundle(g)
        if backup:
            shutil.copyfile(self.bundles[backup], official.backup_path(b))
        if state is not None:
            h.write_state(b, **state)
        if version is None:
            (g / "version.txt").unlink()
        else:
            (g / "version.txt").write_text(version, "utf-8")
        return g, b

    def run_dev(self, g, *args, now=None):
        """(exit code, stdout + exit message); now: patch official.installed_version to show whether it is written."""
        out = io.StringIO()
        code, msg = 0, ""
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(paths, "find_game_dir", lambda: h.assert_under_temp(g)))
            stack.enter_context(mock.patch.object(sys, "argv", ["dev_patch.py", *args]))
            stack.enter_context(contextlib.redirect_stdout(out))
            if now is not None:
                stack.enter_context(mock.patch.object(official, "installed_version", lambda b: now))
            try:
                self.dev.main()
            except SystemExit as e:
                code, msg = (1, e.code) if isinstance(e.code, str) else (e.code or 0, "")
        return code, out.getvalue() + msg

    def state(self, b):
        return official.read_state(b)

    def assert_applied(self, b, code, out):
        self.assertEqual(code, 0, out)
        self.assertIn(f"patched {len(self.ours)} entries in 1 sources", out)
        self.assertEqual(h.sha256_file(b), self.sha("patched"))
        self.assertEqual(h.sha256_file(official.backup_path(b)), self.official_sha)
        st = self.state(b)
        self.assertEqual(st["original_sha256"], self.official_sha)
        self.assertEqual(st["patched_sha256"], self.sha("patched"))
        return st

    # ---- apply: game_version (the version the backup was taken for), like Patcher.Apply

    def test_first_apply_records_the_installed_version(self):
        g, b = self.game("official")
        code, out = self.run_dev(g, "apply")
        st = self.assert_applied(b, code, out)
        self.assertIn("backup created:", out)
        self.assertEqual(st["game_version"], h.GAME_VERSION)
        self.assertEqual(st["patch_version"], "dev_patch")

    def test_new_backup_without_version_txt_records_unknown(self):
        """Like GameLocator.GameVersion (version.txt unreadable -> 不明); paths.game_version used to crash here."""
        g, b = self.game("official", version=None)
        st = self.assert_applied(b, *self.run_dev(g, "apply"))
        self.assertEqual(st["game_version"], official.UNKNOWN_VERSION)

    def test_installed_bundle_identical_to_the_backup_records_the_version(self):
        for state in (None, {"original_sha256": self.official_sha, "patched_sha256": "0" * 64}):
            with self.subTest(state=state):
                g, b = self.game("official", "official", state)
                st = self.assert_applied(b, *self.run_dev(g, "apply", now=NOW))
                self.assertEqual(st["game_version"], NOW)

    def test_reapply_refreshes_only_a_recorded_version(self):
        """R1: the state describing our output proves only that it was built from this backup, not for which version."""
        base = {"original_sha256": self.official_sha, "patched_sha256": self.sha("patched")}
        g, b = self.game("patched", "official", {**base, "game_version": "10.2.0+old"})
        st = self.assert_applied(b, *self.run_dev(g, "apply", now=NOW))
        self.assertEqual(st["game_version"], NOW)
        g, b = self.game("patched", "official", base)  # a state of TpcJpPatch 1.0.0 (no game_version)
        st = self.assert_applied(b, *self.run_dev(g, "apply", now=NOW))
        self.assertNotIn("game_version", st)

    def test_content_classified_output_refreshes_only_a_recorded_version(self):
        """Our output without a state describing it: like `current.IsPatched && outputSha == cur` in Patcher.Apply."""
        base = {"original_sha256": self.official_sha, "patched_sha256": "0" * 64}
        for version in ("10.2.0+old", None):
            with self.subTest(game_version=version):
                state = {**base, "game_version": version} if version else dict(base)
                g, b = self.game("patched", "official", state)
                self.assertIn("holds 10 of our translations (not recorded in the state file)",
                              official.official_japanese(b)[1])
                st = self.assert_applied(b, *self.run_dev(g, "apply", now=NOW))
                if version:
                    self.assertEqual(st["game_version"], NOW)
                else:
                    self.assertNotIn("game_version", st)

    def test_rebuild_vouched_only_by_the_state_keeps_game_version(self):
        """R17: missing, unreadable or same-version modified bundle -> rebuilt from the backup with a WARNING; the
        recorded game_version is kept (it used to be set to the current version, the CS-10 state: after a game update
        plus Steam verification the new official file was taken for a modified one). Since round 3c (S1) only a state
        recording the installed version vouches for the backup (see the next test)."""
        o, p = self.official_sha, self.sha("patched")
        same = {"game_version": h.GAME_VERSION, "original_sha256": o, "patched_sha256": p}
        cases = [
            ("missing, same version", None, same, "the installed bundle is missing"),
            ("missing, same version, no original_sha256", None, {"game_version": h.GAME_VERSION},
             "the installed bundle is missing"),
            ("damaged, same version", "zeros", same, "the installed bundle cannot be read (no TextAsset)"),
            ("same version, modified", "upd", same, "but the game version is unchanged"),
        ]
        for what, current, state, why in cases:
            with self.subTest(what):
                g, b = self.game(current, "official", state)
                code, out = self.run_dev(g, "apply", now=NOW)
                self.assertIn("WARNING: ", out)
                self.assertIn(why, out)
                st = self.assert_applied(b, code, out)
                self.assertEqual(st.get("game_version"), h.GAME_VERSION)

    def test_missing_or_damaged_bundle_without_a_state_proving_the_version_is_refused(self):
        """S1 (round 3c), like Patcher.CheckBackupRecorded: no state file or a state of TpcJpPatch 1.0.0 (no
        game_version) cannot vouch for the backup; apply and restore stop with the reason and change nothing (both
        used to rebuild or restore from a backup of an unknown version)."""
        o, p = self.official_sha, self.sha("patched")
        name = official.backup_path(h.ja_bundle(h.TEMP_ROOT)).name
        for current in (None, "zeros"):
            for what, state in (("no state", None), ("1.0.0 state", {"original_sha256": o, "patched_sha256": p})):
                with self.subTest(current=current, state=what):
                    g, b = self.game(current, "official", state)
                    before = h.snapshot_files(g)
                    for cmd in ("apply", "restore"):
                        code, out = self.run_dev(g, cmd, now=NOW)
                        self.assertEqual(code, 1, out)
                        self.assertTrue(out.startswith("error: "), out)
                        self.assertIn(f"it cannot be confirmed which game version {name} was taken for "
                                      f"(recorded: unknown, installed: {h.GAME_VERSION}); verify the game files in Steam",
                                      out)
                        self.assertEqual(h.snapshot_files(g), before)

    def test_rebuild_after_a_game_update_then_steam_verification(self):
        """R17/R25/S1 end to end: 1.0.0 state, game update V2 with the bundle lost. The old backup is not used (it used
        to be rebuilt and later vouched for V2); after Steam's verification an unchanged official file is the backup
        again and gets V2, and a new V2 file is taken as the new official file (backup stale)."""
        state = {"original_sha256": self.official_sha, "patched_sha256": self.sha("patched")}
        g, b = self.game(None, "official", state, version="10.4.0+new")
        before = h.snapshot_files(g)
        for cmd in ("apply", "restore"):
            code, out = self.run_dev(g, cmd)
            self.assertEqual(code, 1, out)
            self.assertIn("it cannot be confirmed which game version", out)
            self.assertEqual(h.snapshot_files(g), before)
        shutil.copyfile(self.bundles["official"], b)  # Steam's verification; the update left the bundle unchanged
        self.assert_applied(b, *self.run_dev(g, "apply"))
        self.assertEqual(self.state(b)["game_version"], "10.4.0+new")  # the backup is that very file
        g, b = self.game(None, "official", state, version="10.4.0+new")
        shutil.copyfile(self.bundles["upd"], b)  # Steam's verification installs the new official file
        path, reason = official.official_japanese(b)
        self.assertEqual(path, b)
        self.assertIn("stale", reason)
        code, out = self.run_dev(g, "apply")  # not built from the old backup: the extract must be redone first
        self.assertEqual(code, 1, out)
        self.assertIn("run tools/extract.py and tools/worklist.py first", out)

    def test_unknown_version_never_replaces_a_known_recorded_one(self):
        """S16: the R3 guard (Patcher.Apply: Known(version) || !recorded || !Known(recorded)) had no test. A version.txt
        that cannot be read (不明) or is empty never replaces a known game_version for the installed backup, our output
        and (S3, round 3c) the official file recorded in the state file backed up again; only a new official file
        always takes version.txt."""
        o, p, unk = self.official_sha, self.sha("patched"), official.UNKNOWN_VERSION
        upd = self.sha("upd")
        cases = [  # (what, installed, backup, state, version.txt as read, expected game_version or None = absent)
            ("our output, known recorded, now unknown", "patched", "official",
             {"original_sha256": o, "patched_sha256": p, "game_version": "10.2"}, unk, "10.2"),
            ("our output, known recorded, now empty", "patched", "official",
             {"original_sha256": o, "patched_sha256": p, "game_version": "10.2"}, "", "10.2"),
            ("our output, unknown recorded, now unknown", "patched", "official",
             {"original_sha256": o, "patched_sha256": p, "game_version": unk}, unk, unk),
            ("our output, empty recorded, now unknown", "patched", "official",
             {"original_sha256": o, "patched_sha256": p, "game_version": ""}, unk, unk),
            ("our output, nothing recorded, now unknown", "patched", "official",
             {"original_sha256": o, "patched_sha256": p}, unk, None),
            ("our output by content, known recorded, now unknown", "patched", "official",
             {"original_sha256": o, "patched_sha256": "0" * 64, "game_version": "10.2"}, unk, "10.2"),
            ("the backup, known recorded, now unknown", "official", "official",
             {"original_sha256": o, "patched_sha256": "0" * 64, "game_version": "10.2"}, unk, "10.2"),
            ("the backup, known recorded, now empty", "official", "official",
             {"original_sha256": o, "patched_sha256": "0" * 64, "game_version": "10.2"}, "", "10.2"),
            ("the backup, nothing recorded, now unknown", "official", "official",
             {"original_sha256": o, "patched_sha256": "0" * 64}, unk, unk),
            ("the backup, no state, now empty", "official", "official", None, "", ""),
            ("the backup, known recorded, now known", "official", "official",
             {"original_sha256": o, "patched_sha256": "0" * 64, "game_version": "10.2"}, NOW, NOW),
            ("recorded official file, no backup, known recorded, now unknown", "official", None,
             {"original_sha256": o, "patched_sha256": "0" * 64, "game_version": "10.2"}, unk, "10.2"),
            ("recorded official file, other backup, known recorded, now empty", "official", "upd",
             {"original_sha256": o, "patched_sha256": "0" * 64, "game_version": "10.2"}, "", "10.2"),
            ("recorded official file, nothing recorded, now unknown", "official", None,
             {"original_sha256": o, "patched_sha256": "0" * 64}, unk, unk),
            ("recorded official file, known recorded, now known", "official", None,
             {"original_sha256": o, "patched_sha256": "0" * 64, "game_version": "10.2"}, NOW, NOW),
            ("new official file (stale backup), known recorded, now unknown", "official", "upd",
             {"original_sha256": upd, "patched_sha256": "0" * 64, "game_version": "10.2"}, unk, unk),
            ("new official file (no backup, no state), now empty", "official", None, None, "", ""),
        ]
        for what, current, backup, state, now, want in cases:
            with self.subTest(what):
                g, b = self.game(current, backup, state)
                st = self.assert_applied(b, *self.run_dev(g, "apply", now=now))
                self.assertEqual(st.get("game_version"), want)

    def test_version_txt_with_a_line_break_is_recorded_on_one_line(self):
        """S4/S17: version.txt is read like GameLocator.GameVersion (BOM removed, line breaks as spaces), so the value
        fits the state file (write_state used to raise ValueError after the bundle was written) and is the same
        version on the next run."""
        g, b = self.game("official")
        (g / "version.txt").write_bytes(b"10.4.0\r\nbuild 1234\r\n")
        st = self.assert_applied(b, *self.run_dev(g, "apply"))
        self.assertEqual(st["game_version"], "10.4.0  build 1234")
        shutil.copyfile(self.bundles["upd"], b)  # changed under the same game version: the backup stays
        code, out = self.run_dev(g, "apply")
        self.assertIn("but the game version is unchanged", out)
        st = self.assert_applied(b, code, out)
        self.assertEqual(st["game_version"], "10.4.0  build 1234")
        g, b = self.game("official", version="\ufeff10.4.0+bom")
        self.assertEqual(self.assert_applied(b, *self.run_dev(g, "apply"))["game_version"], "10.4.0+bom")

    def test_patched_or_damaged_backup_is_never_used(self):
        """R16 through dev_patch: like Patcher.CheckBackup, nothing is changed."""
        for backup in ("patched", "zeros"):
            for current, state in ((None, None), ("zeros", None), ("upd", {"game_version": h.GAME_VERSION})):
                with self.subTest(backup=backup, current=current):
                    g, b = self.game(current, backup, state and {**state, "original_sha256": self.sha(backup)})
                    before = h.snapshot_files(g)
                    for cmd in ("apply", "restore"):
                        code, out = self.run_dev(g, cmd)
                        self.assertEqual(code, 1, out)
                        self.assertTrue(out.startswith("error: "), out)
                        self.assertIn("is patched data (10 of our translations)" if backup == "patched"
                                      else "is damaged (no TextAsset)", out)
                        self.assertEqual(h.snapshot_files(g), before)

    # ---- status

    def test_status_with_a_missing_bundle(self):
        g, b = self.game(None, "official", {"game_version": h.GAME_VERSION, "original_sha256": self.official_sha})
        code, out = self.run_dev(g, "status")
        self.assertEqual(code, 0, out)
        self.assertIn("state: missing | backup exists: True", out)
        self.assertIn("official data: WARNING: the installed bundle is missing", out)
        g, b = self.game(None, "official")  # S1: no state file proves the backup's version
        code, out = self.run_dev(g, "status")
        self.assertEqual(code, 0, out)
        self.assertIn("state: missing | backup exists: True", out)
        self.assertIn("official data: ERROR: ", out)
        self.assertIn("it cannot be confirmed which game version", out)
        g, b = self.game(None)
        code, out = self.run_dev(g, "status")
        self.assertEqual(code, 0, out)
        self.assertIn("state: missing | backup exists: False", out)
        self.assertIn("official data: ERROR: ", out)
        self.assertNotIn("Traceback", out)

    def test_folder_at_the_state_path_is_no_state_file(self):
        """S19: like File.Exists in Patcher.ReadState (status, apply and restore used to stop with a traceback)."""
        g, b = self.game("official", "official")
        official.state_path(b).mkdir()
        code, out = self.run_dev(g, "status")
        self.assertEqual(code, 0, out)
        self.assertIn("official data: installed bundle is identical to", out)
        b.unlink()
        code, out = self.run_dev(g, "restore")
        self.assertEqual(code, 1, out)
        self.assertTrue(out.startswith("error: "), out)
        self.assertIn("it cannot be confirmed which game version", out)
        self.assertTrue(official.state_path(b).is_dir())

    # ---- restore

    def test_restore_with_a_missing_bundle(self):
        """The backup is restored when the state file records the installed game version as the backup's (S1: a state
        of TpcJpPatch 1.0.0 or none cannot vouch for it; see test_missing_or_damaged_bundle_...)."""
        g, b = self.game(None, "official", {"game_version": h.GAME_VERSION, "original_sha256": self.official_sha,
                                            "patched_sha256": self.sha("patched")})
        code, out = self.run_dev(g, "restore")
        self.assertEqual(code, 0, out)
        self.assertIn("WARNING: the installed bundle is missing", out)
        self.assertIn("restored official bundle", out)
        self.assertEqual(h.sha256_file(b), self.official_sha)
        self.assertEqual(sorted(p.name for p in b.parent.iterdir()), [b.name])

    def test_restore_keeps_the_official_file_recorded_in_the_state(self):
        """Like TpcJpPatch.exe (「日本語データは公式のままです」): the backup is not written over it and both the backup and
        the state are removed (it used to say 'the installed bundle is newer than the backup' and keep them)."""
        state = {"game_version": h.GAME_VERSION, "original_sha256": self.official_sha, "patched_sha256": self.sha("patched")}
        for backup in ("upd", "zeros", None):
            with self.subTest(backup=backup):
                g, b = self.game("official", backup, state)
                code, out = self.run_dev(g, "restore")
                self.assertEqual(code, 0, out)
                self.assertIn("official file recorded in", out)
                removed = ", ".join(n for n in (official.backup_path(b).name if backup else None,
                                                official.state_path(b).name) if n)
                self.assertIn(f"the installed bundle is already the official one (not restored); removed: {removed}", out)
                self.assertNotIn("newer than the backup", out)
                self.assertEqual(h.sha256_file(b), self.official_sha)
                self.assertEqual(sorted(p.name for p in b.parent.iterdir()), [b.name])

    def test_restore_keeps_a_newer_official_file(self):
        """Unchanged: after a game update the installed file stays and the stale backup is kept (exit 1)."""
        g, b = self.game("upd", "official", {"original_sha256": self.official_sha, "patched_sha256": self.sha("patched")})
        before = h.snapshot_files(g)
        code, out = self.run_dev(g, "restore")
        self.assertEqual(code, 1, out)
        self.assertIn("not restoring: the installed bundle is newer than the backup; keeping it", out)
        self.assertEqual(h.snapshot_files(g), before)

    def test_restore_without_backup(self):
        g, b = self.game("official")
        code, out = self.run_dev(g, "restore")
        self.assertEqual((code, out), (1, "no backup found"))

    def test_restore_after_apply(self):
        g, b = self.game("official")
        self.assert_applied(b, *self.run_dev(g, "apply"))
        code, out = self.run_dev(g, "restore")
        self.assertEqual(code, 0, out)
        self.assertEqual(h.sha256_file(b), self.official_sha)
        self.assertEqual(sorted(p.name for p in b.parent.iterdir()), [b.name])


if __name__ == "__main__":
    unittest.main()
