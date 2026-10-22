"""The Debug patcher (TpcJpPatch.exe) on sandbox games, checked with the Python tools (verify_bundle.py, tpcloc).

Covers the behaviour changed by the C1 fixes (work/localtest/results/fixes.md):
- same game version + a different bundle that is not ours -> rebuilt / restored from the backup, backup kept
- a real game update (version.txt changed) -> the new official file becomes the backup
- a failed apply leaves no new backup; a failed state-file write is a warning (exit 0)
- status "公式のデータに戻っています" after a Steam-verify-like restore
- one patcher at a time (named mutex Local\\TpcJpPatch)
- restore / language previous without a Japanese account say "変更しませんでした" (fake Cloud folder only)
- --data read failures ("翻訳データを読み込めません") and truncated / non-UTF-8 data are refused

and the round-2 fixes ("修正 2回目"):
- leftovers of an interrupted run (<name>.tpcjp-tmp, File.Replace's <name>~RF*.TMP) are removed by apply / restore
- a missing Japanese bundle is rebuilt (apply) or restored (restore) from the backup
- data that does not end with a newline is refused (a cut inside the last line)
- status classifies a bundle that matches neither the state file nor the backup by content
  (日本語化パッチ適用済み（状態の記録なし） / 壊れているか読み込めません), shorter Reverted text
- tpcloc/official.py agrees with the exe on the same-game-version rule

and the round-3 fixes ("修正 3回目"):
- status reads the bundle also without a backup and a state file
  (日本語化パッチ適用済み（バックアップなし。Steam の整合性の確認で戻せます） / Steam verification only)
- with the installed bundle missing or damaged, the backup must be the recorded file of the installed game version
  (a game update or a swapped backup is refused); the recorded official file installed again repairs the backup

and round 3b:
- the same "バックアップなし" status when the state file describes the bundle but the backup is missing or swapped
- a refused restore is not announced as a restore first ("…バックアップから戻します" only once the checks passed)
- the refusal for an unknown or empty game version says that the backup's version cannot be confirmed

and round 3c:
- with the installed bundle missing or damaged, a missing state file (or one without game_version) cannot vouch for the
  backup: refused like an unknown recorded version until a Steam verification
- a bundle in a format the patcher does not read after a game update is not damage: its own status, apply refuses with the
  patch-update advice, restore keeps it

Only sandbox games under %TEMP%\\tpcjp_pytests are used: apply always gets --no-language, restore gets
--keep-language unless the test runs with a fake Cloud folder (TPCJP_PREFS_ROOT) in the temp root.
"""
import shutil
import unittest

from . import helpers as h
from .test_prefs import document, lang_map
from tpcloc import bundles, i2cache, official

NEW_VERSION = "10.4.0+pytests-update"
DATA_CUT = "翻訳データが途中で切れているか、壊れています"
NO_NEWLINE = "翻訳データが途中で切れています（最後の行に改行がありません）"
ORIGINAL = "公式のまま（パッチ未適用）"
REVERTED = "公式のデータに戻っています（「日本語化する」で適用し直せます）"  # shortened in round 2 (CS-08)
PATCHED_NO_STATE = "日本語化パッチ適用済み（状態の記録なし）"
PATCHED_NO_BACKUP = "日本語化パッチ適用済み（バックアップなし。Steam の整合性の確認で戻せます）"  # round 3 (CS-09)
DAMAGED = "壊れているか読み込めません（「元に戻す」か Steam の整合性の確認で直せます）"
DAMAGED_NO_BACKUP = "壊れているか読み込めません（Steam の整合性の確認で直せます）"  # round 3: no backup restore could use
BACKUP_MISMATCH = "バックアップが記録と一致しません（壊れているか、別のファイルに置き換わっています）。バックアップは削除せずに残しました。"
REBACKUP_RECORDED = "日本語データが記録されている公式データと一致するため、バックアップを作り直します"
UPDATED = "ゲーム側で更新されています（「日本語化する」で適用し直してください）"
MISSING = "見つかりません（ゲームのフォルダーを確認してください）"
UNSUPPORTED = "このパッチが対応していない形式です（パッチの更新をお待ちください）"  # round 3c (S2/S8)
NOT_CHANGED = "表示言語が日本語のアカウントはないため、表示言語は変更しませんでした"
REBUILD_MISSING = "日本語データが見つからないため、バックアップした公式データから作り直します"


def leftovers_removed(n: int) -> str:
    return f"前回中断したときの一時ファイルを {n} 個削除しました"


@unittest.skipUnless(h.OFFICIAL_BUNDLE.exists() and h.REAL_DATA.exists() and h.DEBUG_EXE.exists()
                     and h.REAL_STRINGS.exists(), "official bundle / dist data / Debug exe missing")
class PatcherE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = h.TEMP_ROOT / "t" / "e2e_cls"
        shutil.rmtree(cls.base, ignore_errors=True)
        cls.base.mkdir(parents=True)
        if not h.KEEP:
            cls.addClassCleanup(shutil.rmtree, cls.base, True)
        cls.official_sha = h.sha256_file(h.OFFICIAL_BUNDLE)
        cls.official = bundles.read_texts(h.OFFICIAL_BUNDLE)
        cls.data_text = h.REAL_DATA.read_text("utf-8")
        _, entries = h.cs_parse_patch_data(cls.data_text)
        last = {n: i2cache.to_dict(t) for n, t in cls.official.items()}
        applicable = [e for e in entries if e["ja"] is not None and e["key"] in last.get(e["source"], {})
                      and h.base_hash(last[e["source"]][e["key"]]) == e["base"]]
        # a game update that translated 5 of our terms officially
        cls.updated_keys = applicable[:5]
        upd = {}
        for i, e in enumerate(cls.updated_keys):
            upd.setdefault(e["source"], {})[e["key"]] = f"公式の新しい訳{i}"
        cls.updated = cls.base / "updated.bundle"
        bundles.write_texts(h.OFFICIAL_BUNDLE, cls.updated, {
            s: i2cache.dumps([(k, v.get(k, x)) for k, x in i2cache.parse(cls.official[s])]) for s, v in upd.items()})
        # a bundle changed by something else (another tool): one term that is not in our data, not our output
        in_data = {(e["source"], e["key"]) for e in entries}
        src = applicable[0]["source"]
        other_key = next(k for k, v in i2cache.parse(cls.official[src]) if (src, k) not in in_data and v.strip())
        cls.foreign = cls.base / "foreign.bundle"
        bundles.write_texts(h.OFFICIAL_BUNDLE, cls.foreign, {
            src: i2cache.dumps([(k, "他のツールが書き換えた" if k == other_key else v)
                                for k, v in i2cache.parse(cls.official[src])])})
        # data that applies to nothing (every base hash wrong) -> "適用できる翻訳がありませんでした"
        lines = cls.data_text.split("\n")
        bad = []
        for line in lines:
            if line and not line.startswith("#"):
                f = line.split("\t")
                f[2] = "0" * 16
                line = "\t".join(f)
            bad.append(line)
        cls.nothing_data = cls.base / "nothing.tsv"
        cls.nothing_data.write_text("\n".join(bad), "utf-8", newline="\n")
        # project for verify_bundle.py: the real extract and translations (copies)
        cls.project = cls.base / "proj"
        (cls.project / "work" / "extract").mkdir(parents=True)
        shutil.copyfile(h.REAL_STRINGS, cls.project / "work" / "extract" / "strings.json")
        shutil.copytree(h.PROJECT / "translations" / "ja", cls.project / "translations" / "ja")
        cls.expected = h.fill_texts(cls.official, cls.data_text)

    # ------------------------------------------------------------------ helpers

    def setUp(self):
        self.root = h.new_dir(self)
        self.appdata = self.root / "appdata"

    def game(self, bundle=h.OFFICIAL_BUNDLE):
        g = h.make_game(self.root / "game", japanese=bundle)
        return g, h.ja_bundle(g)

    def exe(self, *args, game=None, **kw):
        return h.run_debug_exe(*args, game=game, appdata=self.appdata, **kw)

    def apply(self, game, data=None, expect=0):
        code, out = self.exe("apply", "--data", data or h.REAL_DATA, game=game)
        self.assertEqual(code, expect, out)
        return out

    def verify(self, game):
        r = h.run_tool("verify_bundle.py", "--data", h.REAL_DATA, project=self.project, game=game)
        self.assertEqual(r.code, 0, r)
        self.assertIn("not applied: 0", r.out)
        self.assertIn("unexpected changes: 0", r.out)
        return r

    def status_line(self, game):
        code, out = self.exe("status", game=game)
        self.assertEqual(code, 0, out)
        return next(l for l in out.splitlines() if l.startswith("日本語データ: "))[len("日本語データ: "):]

    @staticmethod
    def folder(bundle):
        return {p.name: p.read_bytes() for p in bundle.parent.iterdir() if p.is_file()}

    def assert_patched_from_official(self, bundle):
        got = bundles.read_texts(bundle)
        for name, t in self.official.items():
            self.assertEqual(got[name], self.expected.get(name, t), name)

    # ------------------------------------------------------------------ same game version, different bundle

    def test_same_version_foreign_bundle_is_rebuilt_from_the_backup(self):
        g, b = self.game()
        self.apply(g)
        first = b.read_bytes()
        backup = official.backup_path(b)
        self.assertEqual(h.sha256_file(backup), self.official_sha)
        shutil.copyfile(self.foreign, b)  # changed by something else; version.txt unchanged
        out = self.apply(g)
        self.assertIn("ゲームのバージョンは変わっていないため、バックアップした公式データから作り直します", out)
        self.assertNotIn("バックアップし直します", out)
        self.assertEqual(h.sha256_file(backup), self.official_sha)  # the backup was not replaced
        state = official.read_state(b)
        self.assertEqual(state["original_sha256"], self.official_sha)
        self.assertEqual(state["patched_sha256"], h.sha256_file(b))
        self.assertEqual(state["game_version"], h.GAME_VERSION)
        self.assert_patched_from_official(b)
        self.assertEqual(b.read_bytes(), first)  # same backup + same data -> the same output
        self.verify(g)

    def test_same_version_foreign_bundle_restore_uses_the_backup(self):
        g, b = self.game()
        self.apply(g)
        shutil.copyfile(self.foreign, b)
        code, out = self.exe("restore", "--data", h.REAL_DATA, game=g)
        self.assertEqual(code, 0, out)
        self.assertIn("ゲームのバージョンは変わっていないため、バックアップから戻します", out)
        self.assertEqual(h.sha256_file(b), self.official_sha)
        self.assertEqual(sorted(self.folder(b)), [b.name])  # backup and state removed

    def test_same_version_foreign_bundle_restore_without_data(self):
        """restore with unreadable data still decides from the state file's game version (no data needed)."""
        g, b = self.game()
        self.apply(g)
        shutil.copyfile(self.foreign, b)
        bad = self.root / "missing.tsv"
        code, out = self.exe("restore", "--data", bad, game=g)
        self.assertEqual(code, 0, out)
        self.assertIn("注意: 翻訳データを読み込めないため、状態ファイルだけで判断します", out)
        self.assertEqual(h.sha256_file(b), self.official_sha)

    # ------------------------------------------------------------------ real update

    def simulate_update(self, game, bundle):
        (game / "version.txt").write_text(NEW_VERSION, "utf-8")
        shutil.copyfile(self.updated, bundle)

    def test_real_update_makes_the_new_official_file_the_backup(self):
        g, b = self.game()
        self.apply(g)
        self.simulate_update(g, b)
        out = self.apply(g)
        self.assertIn("日本語データが更新されています（ゲームのアップデートなど）。新しいデータをバックアップし直します", out)
        self.assertIn("公式訳が追加されていた 5 件は公式訳のままにしました", out)
        backup = official.backup_path(b)
        self.assertEqual(backup.read_bytes(), self.updated.read_bytes())
        state = official.read_state(b)
        self.assertEqual(state["game_version"], NEW_VERSION)
        self.assertEqual(state["original_sha256"], h.sha256_file(self.updated))
        self.assertEqual(state["patched_sha256"], h.sha256_file(b))
        got = bundles.read_texts(b)
        for i, e in enumerate(self.updated_keys):
            self.assertEqual(i2cache.to_dict(got[e["source"]])[e["key"]], f"公式の新しい訳{i}")
        self.assertTrue(self.status_line(g).startswith("日本語化パッチ適用済み"))
        r = self.verify(g)
        self.assertIn("official kept (official Japanese changed since the build): 5", r.out)

    def test_failed_apply_after_an_update_keeps_the_old_backup(self):
        g, b = self.game()
        self.apply(g)
        self.simulate_update(g, b)
        before = self.folder(b)
        out = self.apply(g, data=self.nothing_data, expect=1)
        self.assertIn("適用できる翻訳がありませんでした", out)
        self.assertNotIn("バックアップを作成しました", out)
        self.assertEqual(self.folder(b), before)  # old backup, updated bundle and old state untouched, no temp files
        self.assertEqual(h.sha256_file(official.backup_path(b)), self.official_sha)

    def test_failed_first_apply_creates_no_backup(self):
        g, b = self.game()
        out = self.apply(g, data=self.nothing_data, expect=1)
        self.assertIn("適用できる翻訳がありませんでした", out)
        self.assertEqual(sorted(self.folder(b)), [b.name])
        self.assertEqual(h.sha256_file(b), self.official_sha)
        self.assertEqual(self.status_line(g), "公式のまま（パッチ未適用）")

    # ------------------------------------------------------------------ state file write failure

    def block_state_write(self, b):
        blocker = b.with_name(b.name + ".tpcjp-state.tpcjp-tmp")
        blocker.mkdir()  # SafeFile cannot create its temp file -> the state file cannot be written
        return blocker

    def test_state_write_failure_is_a_warning(self):
        g, b = self.game()
        blocker = self.block_state_write(b)
        out = self.apply(g)
        self.assertIn("注意: 状態ファイルを書き込めませんでした（日本語データの書き換えは完了しています）", out)
        self.assertIn("日本語データにパッチを適用しました", out)
        self.assertIsNone(official.read_state(b))
        self.assertEqual(h.sha256_file(official.backup_path(b)), self.official_sha)
        self.assert_patched_from_official(b)
        self.verify(g)  # recognised by content without a state file
        blocker.rmdir()
        out = self.apply(g)  # the next run recognises its own output by content
        self.assertIn("以前に適用したパッチを検出しました", out)
        self.assertEqual(official.read_state(b)["patched_sha256"], h.sha256_file(b))
        self.assertEqual(h.sha256_file(official.backup_path(b)), self.official_sha)

    def test_state_write_failure_keeps_the_old_state_and_restore_still_works(self):
        g, b = self.game()
        self.apply(g)
        old_state = official.state_path(b).read_bytes()
        self.simulate_update(g, b)
        self.block_state_write(b)
        out = self.apply(g)
        self.assertIn("注意: 状態ファイルを書き込めませんでした", out)
        self.assertEqual(official.state_path(b).read_bytes(), old_state)  # stale, from the old game version
        self.assertEqual(official.backup_path(b).read_bytes(), self.updated.read_bytes())
        self.assertEqual(self.status_line(g), PATCHED_NO_STATE)  # a stale state is classified by content too
        code, out = self.exe("restore", "--data", h.REAL_DATA, game=g)
        self.assertEqual(code, 0, out)
        self.assertEqual(b.read_bytes(), self.updated.read_bytes())  # back to the new official file

    def test_status_after_a_state_write_failure_is_patched_without_state(self):
        """After "書き換えは完了しています", status shows the bundle as patched (was finding pytests-E2E1: it said
        "ゲーム側で更新されています…"). Fixed in round 2: GetStatus(data) classifies by content."""
        g, b = self.game()
        self.block_state_write(b)
        self.apply(g)
        self.assertEqual(self.status_line(g), PATCHED_NO_STATE)
        # the next successful apply writes the state again: the normal patched status comes back
        self.block_state_write_off(b)
        self.apply(g)
        self.assertTrue(self.status_line(g).startswith("日本語化パッチ適用済み（1.0.0、翻訳 "))

    def block_state_write_off(self, b):
        b.with_name(b.name + ".tpcjp-state.tpcjp-tmp").rmdir()

    # ------------------------------------------------------------------ status after a Steam-verify-like restore

    def test_reverted_status_after_the_official_file_comes_back(self):
        g, b = self.game()
        self.assertEqual(self.status_line(g), "公式のまま（パッチ未適用）")
        self.apply(g)
        self.assertTrue(self.status_line(g).startswith("日本語化パッチ適用済み（1.0.0、翻訳 "), self.status_line(g))
        shutil.copyfile(official.backup_path(b), b)  # Steam "verify integrity" puts the official file back
        self.assertEqual(self.status_line(g), REVERTED)
        out = self.apply(g)
        self.assertIn("公式の日本語データにパッチを適用します", out)
        self.assertEqual(h.sha256_file(official.backup_path(b)), self.official_sha)
        self.assertTrue(self.status_line(g).startswith("日本語化パッチ適用済み"))

    def test_backup_without_state_identical_bundle_is_original(self):
        g, b = self.game()
        shutil.copyfile(h.OFFICIAL_BUNDLE, official.backup_path(b))
        self.assertEqual(self.status_line(g), ORIGINAL)

    # ------------------------------------------------------------------ status: content classification (round 2)

    def damaged_variants(self, b):
        raw = h.OFFICIAL_BUNDLE.read_bytes()
        return {"half": raw[:len(raw) // 2], "empty": b"", "garbage": bytes(range(256)) * 64,
                "tail_cut": raw[:-1000]}

    def test_status_damaged_bundle_with_a_backup(self):
        g, b = self.game()
        self.apply(g)
        backup_before = official.backup_path(b).read_bytes()
        for name, raw in self.damaged_variants(b).items():
            with self.subTest(damage=name):
                b.write_bytes(raw)
                code, out = self.exe("status", game=g)
                self.assertEqual(code, 0, out)
                self.assertIn("日本語データ: " + DAMAGED, out)
                self.assertEqual(b.read_bytes(), raw)  # status changes nothing
        out = self.apply(g)  # the advertised fix works: rebuilt from the backup
        self.assertIn("日本語データが壊れているため、バックアップした公式データから作り直します", out)
        self.assertEqual(official.backup_path(b).read_bytes(), backup_before)
        self.assertTrue(self.status_line(g).startswith("日本語化パッチ適用済み（1.0.0"))
        self.assert_patched_from_official(b)

    def test_status_damaged_bundle_after_a_state_write_failure(self):
        """No state file at all, a backup, and a damaged bundle: classified as damaged, not as a game update. Round 3c (S1):
        without a state file nothing says which game version the backup is for, so it is not offered (Steam verification
        only), apply refuses and changes nothing; after the verification apply patches and records the version."""
        g, b = self.game()
        blocker = self.block_state_write(b)
        self.apply(g)
        blocker.rmdir()
        b.write_bytes(h.OFFICIAL_BUNDLE.read_bytes()[:5000])
        self.assertEqual(self.status_line(g), DAMAGED_NO_BACKUP)
        before = self.folder(b)
        out = self.apply(g, expect=1)
        self.assertIn(f"エラー: バックアップがどのバージョンのゲームのものか確認できません（記録: 不明、現在のゲームのバージョン: {h.GAME_VERSION}）。"
                      "バックアップは削除せずに残しました。", out)
        self.assertNotIn("作り直します", out)
        self.assertEqual(self.folder(b), before)
        shutil.copyfile(h.OFFICIAL_BUNDLE, b)  # Steam "verify integrity"
        out = self.apply(g)
        self.assertIn("公式の日本語データにパッチを適用します", out)
        self.assertEqual(official.read_state(b)["game_version"], h.GAME_VERSION)
        self.assert_patched_from_official(b)

    def test_unsupported_bundle_format_after_an_update(self):
        """Round 3c (S2/S7/S8/S15): after an update the installed bundle has a format the patcher does not read (simulated:
        UnityFS format version 9). It is not damage: status says so, apply refuses (verify, then wait for a patch update)
        and changes nothing, restore keeps it and removes the old backup and state."""
        g, b = self.game()
        self.apply(g)
        raw = bytearray(h.OFFICIAL_BUNDLE.read_bytes())
        self.assertTrue(raw[:8] == b"UnityFS\0" and raw[8:11] == b"\0\0\0" and 6 <= raw[11] <= 8)  # format version 6..8
        raw[11] = 9
        (g / "version.txt").write_text(NEW_VERSION, "utf-8")
        b.write_bytes(bytes(raw))
        self.assertEqual(self.status_line(g), UNSUPPORTED)
        before = self.folder(b)
        out = self.apply(g, expect=1)
        self.assertIn("エラー: 日本語データを読み込めません（対応していないバンドルの形式です（版 9））。", out)
        self.assertIn("それでも直らない場合は、パッチの更新をお待ちください", out)
        self.assertNotIn("別のバージョン", out)
        self.assertEqual(self.folder(b), before)
        code, out = self.exe("restore", "--data", h.REAL_DATA, game=g)
        self.assertEqual(code, 0, out)
        self.assertIn("日本語データはこのパッチが対応していない形式のため、そのままにします", out)
        self.assertEqual(sorted(self.folder(b)), [b.name])
        self.assertEqual(b.read_bytes(), bytes(raw))
        self.assertEqual(self.status_line(g), UNSUPPORTED)

    def test_status_real_update_and_foreign_bundle(self):
        """Neither ours nor the backup and not damaged: a real update shows "ゲーム側で更新されています"; a bundle
        changed by something else with the same game version shows the same text (apply rebuilds it)."""
        g, b = self.game()
        self.apply(g)
        shutil.copyfile(self.foreign, b)
        self.assertEqual(self.status_line(g), UPDATED)
        self.simulate_update(g, b)
        self.assertEqual(self.status_line(g), UPDATED)

    def test_status_patched_without_state_when_an_old_state_is_left(self):
        """A state file that describes another output (stale patched_sha256) and our current output: patched."""
        g, b = self.game()
        self.apply(g)
        state = official.read_state(b)
        h.write_state(b, **{**state, "patched_sha256": "0" * 64})
        self.assertEqual(self.status_line(g), PATCHED_NO_STATE)

    def test_status_missing_bundle(self):
        g, b = self.game()
        self.apply(g)
        b.unlink()
        self.assertEqual(self.status_line(g), MISSING)

    def test_status_without_backup_and_state_is_classified_by_content(self):
        """Without a backup and a state file status reads the bundle too (was finding pytests-ST1: both showed
        "公式のまま（パッチ未適用）"). Fixed in round 3: our output -> "日本語化パッチ適用済み（バックアップなし。Steam の整合性の確認で
        戻せます）", a damaged bundle -> damaged with the Steam verification as the only fix (restore cannot help without a
        backup); apply agrees."""
        g, b = self.game()
        self.apply(g)
        official.backup_path(b).unlink()
        official.state_path(b).unlink()
        self.assertEqual(self.status_line(g), PATCHED_NO_BACKUP)
        out = self.apply(g, expect=1)
        self.assertIn("日本語データは以前のパッチで書き換えられていますが、バックアップが見つかりません", out)
        b.write_bytes(h.OFFICIAL_BUNDLE.read_bytes()[:5000])
        self.assertEqual(self.status_line(g), DAMAGED_NO_BACKUP)
        out = self.apply(g, expect=1)
        self.assertIn("日本語データが壊れています", out)
        shutil.copyfile(h.OFFICIAL_BUNDLE, b)  # Steam "verify integrity"
        self.assertEqual(self.status_line(g), ORIGINAL)

    def test_status_patched_without_a_usable_backup_while_the_state_describes_it(self):
        """Round 3b (R2/R19): the state file still describes our output, but the backup is missing or not the recorded
        file, so restore refuses; status says "バックアップなし" (as without a state file) instead of the plain patched text."""
        for how in ("missing", "swapped"):
            with self.subTest(backup=how):
                g = h.make_game(self.root / f"game_{how}")
                b = h.ja_bundle(g)
                self.apply(g)
                backup = official.backup_path(b)
                if how == "missing":
                    backup.unlink()
                else:
                    shutil.copyfile(self.foreign, backup)
                self.assertEqual(self.status_line(g), PATCHED_NO_BACKUP)
                before = self.folder(b)
                code, out = self.exe("restore", "--data", h.REAL_DATA, game=g)
                self.assertEqual(code, 1, out)
                self.assertIn("ゲームファイルの整合性を確認", out)
                if how == "swapped":
                    self.assertIn("エラー: " + BACKUP_MISMATCH, out)
                self.assertEqual(self.folder(b), before)
                shutil.copyfile(h.OFFICIAL_BUNDLE, b)  # Steam "verify integrity": the recorded official file
                self.assertEqual(self.status_line(g), REVERTED)
                out = self.apply(g)
                self.assertIn(REBACKUP_RECORDED, out)
                self.assertEqual(h.sha256_file(backup), self.official_sha)

    # ------------------------------------------------------------------ only the state file vouches for the backup (round 3)

    def test_bundle_lost_after_an_update_is_not_rebuilt_from_the_old_backup(self):
        """F-SBP-8 / CS-10: after a game update the backup is the old version's official file; with the installed bundle
        missing or damaged apply and restore refuse (Steam verification advice, nothing changed). After the
        verification the new official file becomes the backup."""
        for how in ("missing", "damaged"):
            with self.subTest(how=how):
                g = h.make_game(self.root / f"game_{how}")
                b = h.ja_bundle(g)
                self.apply(g)
                self.simulate_update(g, b)
                if how == "missing":
                    b.unlink()
                else:
                    b.write_bytes(self.updated.read_bytes()[:5000])
                before = self.folder(b)
                for cmd in ("apply", "restore"):
                    code, out = self.exe(cmd, "--data", h.REAL_DATA, game=g)
                    self.assertEqual(code, 1, out)
                    self.assertIn(f"エラー: バックアップは別のバージョンのゲーム（{h.GAME_VERSION}）の公式データです"
                                  f"（現在のゲームのバージョン: {NEW_VERSION}）。バックアップは削除せずに残しました。", out)
                    self.assertIn("ゲームファイルの整合性を確認", out)
                    # round 3b (R20): nothing announces a restore / rebuild before the refusal
                    self.assertNotIn("バックアップから戻します", out)
                    self.assertNotIn("作り直します", out)
                    self.assertEqual(self.folder(b), before)
                self.assertEqual(self.status_line(g), MISSING if how == "missing" else DAMAGED_NO_BACKUP)
                shutil.copyfile(self.updated, b)  # Steam "verify integrity" brings the new official file
                out = self.apply(g)
                self.assertIn("新しいデータをバックアップし直します", out)
                self.assertEqual(official.backup_path(b).read_bytes(), self.updated.read_bytes())
                self.assertEqual(official.read_state(b)["game_version"], NEW_VERSION)

    def test_unknown_or_empty_game_version_refusal_says_the_version_cannot_be_confirmed(self):
        """Round 3b (R4/R14/R26, R15/R27): with version.txt missing or empty the installed version is unknown, so a missing
        bundle is not rebuilt from the backup; the refusal says that the backup's version cannot be confirmed (an empty
        version is shown as 不明) instead of calling it another version."""
        for how in ("missing", "empty"):
            with self.subTest(version_txt=how):
                g = h.make_game(self.root / f"game_{how}")
                b = h.ja_bundle(g)
                self.apply(g)
                if how == "missing":
                    (g / "version.txt").unlink()
                else:
                    (g / "version.txt").write_text("", "utf-8")
                b.unlink()
                before = self.folder(b)
                for cmd in ("apply", "restore"):
                    code, out = self.exe(cmd, "--data", h.REAL_DATA, game=g)
                    self.assertEqual(code, 1, out)
                    self.assertIn(f"エラー: バックアップがどのバージョンのゲームのものか確認できません（記録: {h.GAME_VERSION}、"
                                  "現在のゲームのバージョン: 不明）。バックアップは削除せずに残しました。", out)
                    self.assertIn("ゲームファイルの整合性を確認", out)
                    self.assertNotIn("別のバージョン", out)
                    self.assertEqual(self.folder(b), before)

    def test_swapped_backup_is_refused_and_the_recorded_official_file_repairs_it(self):
        """Fuzz finding backup_hash_not_checked: the backup replaced by another valid bundle and the installed bundle cut ->
        apply and restore refuse ("バックアップが記録と一致しません"). After a Steam verification the installed file is the
        official file recorded in the state: apply backs it up again and patches."""
        g, b = self.game()
        self.apply(g)
        backup = official.backup_path(b)
        shutil.copyfile(self.foreign, backup)
        b.write_bytes(b.read_bytes()[:300000])
        before = self.folder(b)
        for cmd in ("apply", "restore"):
            code, out = self.exe(cmd, "--data", h.REAL_DATA, game=g)
            self.assertEqual(code, 1, out)
            self.assertIn("エラー: " + BACKUP_MISMATCH, out)
            self.assertEqual(self.folder(b), before)
        self.assertEqual(self.status_line(g), DAMAGED_NO_BACKUP)
        shutil.copyfile(h.OFFICIAL_BUNDLE, b)  # Steam "verify integrity"
        self.assertEqual(self.status_line(g), REVERTED)
        out = self.apply(g)
        self.assertIn(REBACKUP_RECORDED, out)
        self.assertEqual(h.sha256_file(backup), self.official_sha)
        self.assert_patched_from_official(b)
        self.verify(g)

    # ------------------------------------------------------------------ leftovers of an interrupted run (round 2)

    def make_leftovers(self, b):
        """Leftovers that apply/restore must delete, and look-alikes that must stay."""
        backup, state = official.backup_path(b), official.state_path(b)
        remove = [b.with_name(b.name + ".tpcjp-tmp"), b.with_name(b.name + "~RF1a2b3c.TMP"),
                  backup.with_name(backup.name + ".tpcjp-tmp"), state.with_name(state.name + ".tpcjp-tmp"),
                  state.with_name(state.name + "~rf9.tmp")]
        keep = [b.with_name(b.name + ".tpcjp-tmp.keep"), b.with_name(b.name + "~RF1.TMP.bak"),
                b.with_name("localisation_assets_localisationenglish.bundle.tpcjp-tmp"),
                b.with_name("other~RF1.TMP")]
        for p in remove + keep:
            p.write_bytes(b"partial " + p.name.encode())
        remove[3].chmod(0o444)  # read-only: cleared before deleting
        remove[1].write_bytes(b.read_bytes())  # File.Replace renamed the old bundle aside
        return remove, keep

    def test_apply_removes_leftovers_of_an_interrupted_run(self):
        g, b = self.game()
        self.apply(g)
        first = b.read_bytes()
        remove, keep = self.make_leftovers(b)
        self.status_line(g)
        self.assertTrue(all(p.exists() for p in remove + keep))  # status (no lock) leaves them alone
        out = self.apply(g)
        self.assertIn(leftovers_removed(5), out)
        self.assertEqual([p.name for p in remove if p.exists()], [])
        self.assertTrue(all(p.exists() for p in keep))
        self.assertEqual(sorted(self.folder(b)), sorted([b.name, official.backup_path(b).name,
                                                        official.state_path(b).name] + [p.name for p in keep]))
        self.assertEqual(b.read_bytes(), first)
        self.assertEqual(h.sha256_file(official.backup_path(b)), self.official_sha)
        self.verify(g)
        out = self.apply(g)  # nothing left: no message
        self.assertNotIn("前回中断したときの一時ファイル", out)

    def test_restore_removes_leftovers_of_an_interrupted_run(self):
        g, b = self.game()
        self.apply(g)
        remove, keep = self.make_leftovers(b)
        code, out = self.exe("restore", "--data", h.REAL_DATA, game=g)
        self.assertEqual(code, 0, out)
        self.assertIn(leftovers_removed(5), out)
        self.assertIn("日本語データを公式のものに戻しました", out)
        self.assertEqual(sorted(self.folder(b)), sorted([b.name] + [p.name for p in keep]))
        self.assertEqual(h.sha256_file(b), self.official_sha)

    def test_leftover_in_use_is_left_for_the_next_run(self):
        """A leftover another process keeps open cannot be deleted: the cleanup skips it, the write that needs that
        temp name fails as a file error (exit 1, nothing changed), and the next run removes it."""
        g, b = self.game()
        self.apply(g)
        tmp = b.with_name(b.name + ".tpcjp-tmp")
        tmp.write_bytes(b"in use")
        before = self.folder(b)
        with open(tmp, "rb"):  # Python opens without FILE_SHARE_DELETE: File.Delete fails while it is open
            out = self.apply(g, expect=1)
        self.assertNotIn("前回中断したときの一時ファイル", out)
        self.assertIn("エラー: ファイルを読み書きできません", out)
        self.assertEqual(self.folder(b), before)
        out = self.apply(g)
        self.assertIn(leftovers_removed(1), out)
        self.assertFalse(tmp.exists())

    def test_first_apply_with_a_leftover_temp_file(self):
        """A leftover <bundle>.tpcjp-tmp before the first apply (no backup): removed, and the apply works."""
        g, b = self.game()
        tmp = b.with_name(b.name + ".tpcjp-tmp")
        tmp.write_bytes(b"x" * 100)
        out = self.apply(g)
        self.assertIn(leftovers_removed(1), out)
        self.assertFalse(tmp.exists())
        self.assertEqual(h.sha256_file(official.backup_path(b)), self.official_sha)
        self.assert_patched_from_official(b)

    # ------------------------------------------------------------------ missing bundle (round 2)

    def test_missing_bundle_is_rebuilt_from_the_backup(self):
        g, b = self.game()
        self.apply(g)
        first = b.read_bytes()
        b.unlink()
        out = self.apply(g)
        self.assertIn(REBUILD_MISSING, out)
        self.assertNotIn("バックアップを作成しました", out)
        self.assertEqual(b.read_bytes(), first)
        self.assertEqual(h.sha256_file(official.backup_path(b)), self.official_sha)
        self.assertEqual(official.read_state(b)["patched_sha256"], h.sha256_file(b))
        self.verify(g)

    def test_interrupted_replace_of_version_1_0_0_is_repaired(self):
        """TpcJpPatch 1.0.0 (File.Replace) killed mid-replace: the bundle renamed aside to <bundle>~RF*.TMP, the new
        output still in <bundle>.tpcjp-tmp, no bundle. apply deletes both leftovers and rebuilds from the backup."""
        g, b = self.game()
        self.apply(g)
        first = b.read_bytes()
        b.rename(b.with_name(b.name + "~RF7f3e.TMP"))
        b.with_name(b.name + ".tpcjp-tmp").write_bytes(first[:len(first) // 3])
        out = self.apply(g)
        self.assertIn(leftovers_removed(2), out)
        self.assertIn(REBUILD_MISSING, out)
        self.assertEqual(sorted(self.folder(b)), sorted([b.name, official.backup_path(b).name, official.state_path(b).name]))
        self.assertEqual(b.read_bytes(), first)
        self.verify(g)

    def test_missing_bundle_restore_uses_the_backup(self):
        g, b = self.game()
        self.apply(g)
        b.unlink()
        code, out = self.exe("restore", "--data", h.REAL_DATA, game=g)
        self.assertEqual(code, 0, out)
        self.assertIn("日本語データを公式のものに戻しました", out)
        self.assertEqual(sorted(self.folder(b)), [b.name])
        self.assertEqual(h.sha256_file(b), self.official_sha)

    def test_missing_bundle_with_a_patched_backup_is_not_rebuilt(self):
        """The rebuild checks the backup first: a backup holding our output is refused and nothing is written."""
        g, b = self.game()
        self.apply(g)
        shutil.copyfile(b, official.backup_path(b))
        b.unlink()
        before = self.folder(b)
        out = self.apply(g, expect=1)
        self.assertIn("バックアップがパッチ適用後のデータになっています", out)
        self.assertEqual(self.folder(b), before)

    def test_missing_bundle_without_a_backup(self):
        g, b = self.game(bundle=None)
        out = self.apply(g, expect=1)
        self.assertIn("エラー: 日本語データ（localisation_assets_localisationjapanese.bundle）が見つかりません。", out)
        self.assertEqual(list(b.parent.iterdir()), [])
        code, out = self.exe("restore", "--data", h.REAL_DATA, game=g)
        self.assertEqual(code, 0, out)
        self.assertIn("パッチは適用されていません（バックアップがありません）", out)
        self.assertEqual(list(b.parent.iterdir()), [])
        self.assertEqual(self.status_line(g), MISSING)

    # ------------------------------------------------------------------ tpcloc/official.py agrees with the exe

    def official_japanese(self, b):
        with h.project_paths(self.project):
            return official.official_japanese(b)

    def test_official_py_and_the_exe_agree_on_the_same_game_version_rule(self):
        g, b = self.game()
        self.apply(g)
        first = b.read_bytes()
        shutil.copyfile(self.foreign, b)
        path, reason = self.official_japanese(b)
        self.assertEqual(path, official.backup_path(b))  # was finding pytests-OF1
        self.assertIn("game version is unchanged", reason)
        out = self.apply(g)
        self.assertIn("ゲームのバージョンは変わっていないため", out)
        self.assertEqual(b.read_bytes(), first)
        # a real update: both take the installed file as the new official one
        self.simulate_update(g, b)
        path, reason = self.official_japanese(b)
        self.assertEqual(path, b)
        self.assertIn("stale", reason)
        out = self.apply(g)
        self.assertIn("新しいデータをバックアップし直します", out)
        self.assertEqual(self.official_japanese(b)[0], official.backup_path(b))
        self.assertEqual(official.backup_path(b).read_bytes(), self.updated.read_bytes())

    def test_unknown_game_version_is_a_new_official_file_for_the_exe(self):
        """version.txt missing and a state file of 1.0.0 (no game_version): Patcher.SameGameVersion is false, so the
        exe takes a foreign bundle as a new official file (tpcloc/official.py agrees since round 3; was finding pytests-OF2)."""
        g, b = self.game()
        self.apply(g)
        state = official.read_state(b)
        del state["game_version"]
        official.state_path(b).unlink()
        h.write_state(b, **state)
        (g / "version.txt").unlink()
        shutil.copyfile(self.foreign, b)
        out = self.apply(g)
        self.assertIn("新しいデータをバックアップし直します", out)
        self.assertEqual(official.backup_path(b).read_bytes(), self.foreign.read_bytes())
        self.assertEqual(official.read_state(b)["game_version"], "不明")

    # ------------------------------------------------------------------ one patcher at a time

    def test_second_patcher_is_refused_while_one_runs(self):
        g, b = self.game()
        before = self.folder(b)
        with h.hold_patcher_mutex():
            code, out = self.exe("apply", "--data", h.REAL_DATA, game=g, retry_locked=False)
            code2, out2 = self.exe("restore", "--data", h.REAL_DATA, game=g, retry_locked=False)
            code3, out3 = self.exe("status", game=g, retry_locked=False)
        self.assertEqual(code, 1, out)
        self.assertIn("エラー: 別の TpcJpPatch が実行中です。終わってから、もう一度実行してください", out)
        self.assertNotIn("翻訳データ:", out)  # refused before anything was read or written
        self.assertEqual(code2, 1, out2)
        self.assertIn(h.LOCKED_MESSAGE, out2)
        self.assertEqual(code3, 0, out3)  # status does not take the lock
        self.assertEqual(self.folder(b), before)
        self.assertNotIn("パッチを適用しました", out)
        self.apply(g)  # released: the next run works
        self.verify(g)

    # ------------------------------------------------------------------ language messages (fake Cloud folder)

    def account(self, name, selected: bytes):
        p = self.appdata / "prefs" / name / "preferences.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(document(lang_map(selected=selected)))
        return p

    def test_restore_without_a_japanese_account_changes_nothing(self):
        g, b = self.game()
        self.apply(g)
        prefs = self.account("76561190000000001", b"\x00")  # English
        before = prefs.read_bytes()
        code, out = self.exe("restore", "--data", h.REAL_DATA, game=g, language=True)
        self.assertEqual(code, 0, out)
        self.assertIn("日本語データを公式のものに戻しました", out)
        self.assertIn(NOT_CHANGED, out)
        self.assertEqual(prefs.read_bytes(), before)
        self.assertFalse((self.appdata / "backup" / "preferences").exists())  # nothing to back up

    def test_language_previous_without_a_japanese_account(self):
        for setup in ("none", "english"):
            with self.subTest(setup=setup):
                shutil.rmtree(self.appdata / "prefs", ignore_errors=True)
                prefs = self.account("76561190000000002", b"\x03") if setup == "english" else None
                code, out = self.exe("language", "previous", language=True)
                self.assertEqual(code, 0, out)
                self.assertIn(NOT_CHANGED, out)
                if prefs:
                    self.assertEqual(prefs.read_bytes(), document(lang_map(selected=b"\x03")))

    def test_language_previous_with_a_japanese_account_does_not_say_unchanged(self):
        ja = self.account("76561190000000003", b"\x0c")
        en = self.account("76561190000000004", b"\x00")
        code, out = self.exe("language", "previous", language=True)
        self.assertEqual(code, 0, out)
        self.assertIn("表示言語を英語に戻しました（76561190000000003）", out)
        self.assertNotIn(NOT_CHANGED, out)
        self.assertEqual(ja.read_bytes(), document(lang_map(selected=b"\x00")))
        self.assertEqual(en.read_bytes(), document(lang_map(selected=b"\x00")))

    # ------------------------------------------------------------------ translation data problems

    def test_unreadable_data_is_a_data_problem(self):
        g, b = self.game()
        before = self.folder(b)
        for path in (self.root / "no_such.tsv", self.root):  # missing file, a folder
            with self.subTest(path=path.name):
                out = self.apply(g, data=path, expect=1)
                self.assertIn(f"エラー: 翻訳データを読み込めません: {path}", out)
                self.assertNotIn("ゲームや Steam", out)
                self.assertEqual(self.folder(b), before)

    def test_truncated_or_non_utf8_data_is_refused(self):
        g, b = self.game()
        before = self.folder(b)
        cut = self.root / "cut.tsv"
        cut.write_text("\n".join(self.data_text.split("\n")[:-50]) + "\n", "utf-8", newline="\n")
        out = self.apply(g, data=cut, expect=1)
        self.assertIn(DATA_CUT, out)
        sjis = self.root / "sjis.tsv"
        sjis.write_bytes(self.data_text.encode("cp932", "replace"))
        out = self.apply(g, data=sjis, expect=1)
        self.assertIn("翻訳データを UTF-8 として読めません", out)
        sep = self.root / "sep.tsv"
        lines = self.data_text.split("\n")
        i = next(i for i, l in enumerate(lines) if l and not l.startswith(("#", "&")))
        f = lines[i].split("\t")
        f[3] += "[i2t]注入"
        lines[i] = "\t".join(f)
        sep.write_text("\n".join(lines), "utf-8", newline="\n")
        out = self.apply(g, data=sep, expect=1)
        self.assertIn(f"翻訳データ {i + 1} 行目の内容が正しくありません", out)
        self.assertEqual(self.folder(b), before)
        bom = self.root / "bom.tsv"
        bom.write_bytes(b"\xef\xbb\xbf" + self.data_text.encode("utf-8"))
        self.apply(g, data=bom)  # a BOM is accepted
        self.assert_patched_from_official(b)

    def test_data_without_a_final_newline_is_refused(self):
        """Round-2 fix CS-06: a file cut inside its last line keeps the field count and the #entries count, so only
        the missing final newline shows the cut; such data is refused and nothing changes."""
        self.assertTrue(self.data_text.endswith("\n"))
        last = self.data_text[:-1].rsplit("\n", 1)[1]
        self.assertFalse(last.startswith(("#", "&")))  # the last line is an inline translation
        g, b = self.game()
        before = self.folder(b)
        cut_inside = self.data_text[:-2]  # last translation loses its last character and the newline
        # the older checks alone would accept it: same number of fields and entries (Python port of Parse)
        header, entries = h.cs_parse_patch_data(cut_inside)
        self.assertEqual(int(header["entries"]), len(entries))
        variants = {"cut_inside_last_line": cut_inside.encode("utf-8"),
                    "no_final_newline": self.data_text[:-1].encode("utf-8"),
                    "cr_only": (self.data_text[:-1] + "\r").encode("utf-8"),
                    "empty": b"", "bom_only": b"\xef\xbb\xbf",
                    "header_only": "#TPCJP-PATCH\t1\n#entries\t0".encode("utf-8")}
        for name, raw in variants.items():
            with self.subTest(variant=name):
                p = self.root / f"{name}.tsv"
                p.write_bytes(raw)
                with self.assertRaises(h.PatchDataFormatError):
                    h.cs_load_patch_data(raw)  # the Python port agrees
                out = self.apply(g, data=p, expect=1)
                self.assertIn("エラー: " + NO_NEWLINE, out)
                self.assertNotIn("パッチを適用しました", out)
                self.assertEqual(self.folder(b), before)

    def test_crlf_data_is_accepted(self):
        """Line ends converted to CRLF (e.g. by an editor or git) still end with a newline and give the same output."""
        g, b = self.game()
        crlf = self.root / "crlf.tsv"
        crlf.write_bytes(self.data_text.replace("\n", "\r\n").encode("utf-8"))
        self.apply(g, data=crlf)
        self.assert_patched_from_official(b)
        self.verify(g)

    def test_restore_with_data_without_a_final_newline(self):
        """restore does not need the data: it warns and decides from the state file."""
        g, b = self.game()
        self.apply(g)
        cut = self.root / "cut.tsv"
        cut.write_bytes(self.data_text[:-2].encode("utf-8"))
        code, out = self.exe("restore", "--data", cut, game=g)
        self.assertEqual(code, 0, out)
        self.assertIn("注意: 翻訳データを読み込めないため、状態ファイルだけで判断します（" + NO_NEWLINE + "）", out)
        self.assertIn("日本語データを公式のものに戻しました", out)
        self.assertEqual(h.sha256_file(b), self.official_sha)


if __name__ == "__main__":
    unittest.main()
