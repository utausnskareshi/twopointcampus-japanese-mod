using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

namespace TpcJpPatch
{
    /// <summary>
    /// Applies / removes the patch.
    ///
    /// Files next to the Japanese bundle:
    ///   *.bundle.tpcjp-original  the official bundle as it was before patching (the patch source)
    ///   *.bundle.tpcjp-state     sha256 of the backup and of the patched file, counts, and the game version
    ///                            the backup was taken for (game_version; states of version 1.0.0 have none)
    ///
    /// The installed bundle is identified as follows:
    ///   1. its hash equals patched_sha256 in the state file -> our output;
    ///   2. its hash equals the backup's, or original_sha256 in the state file -> the official file;
    ///   3. otherwise it is classified by content (see Match): mostly our texts -> our output written
    ///      without a matching state file; otherwise a new official file (first run, game update,
    ///      Steam file verification), which becomes the new backup.
    /// The backup is checked (loads, official content, hash recorded in the state) before it is used;
    /// where only the state file can vouch for it (the installed bundle is missing or damaged, or differs
    /// while the game version is unchanged) it must also be the recorded file of the installed game version
    /// (a missing state file, or one without game_version, cannot vouch for it).
    /// An installed bundle in a format this patcher does not read is never its output; unless the game version
    /// is unchanged it is a newer official file (see IsNewFormat), not damage.
    /// Every file is replaced atomically (SafeFile), and Restore deletes the backup only after the
    /// game's file has been identified or restored.
    /// </summary>
    sealed class Patcher
    {
        public const string BackupSuffix = ".tpcjp-original";
        public const string StateSuffix = ".tpcjp-state";
        static readonly Regex JapaneseRx = new Regex("[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]");
        const string VerifyAdvice = "Steam のライブラリで Two Point Campus を右クリック →「プロパティ」→「インストール済みファイル」→「ゲームファイルの整合性を確認」を実行してから、もう一度お試しください";

        readonly Action<string> log;
        public string GameDir { get; }
        public string Bundle => GameLocator.JapaneseBundle(GameDir);
        public string Backup => Bundle + BackupSuffix;
        public string StatePath => Bundle + StateSuffix;

        public Patcher(string gameDir, Action<string> log)
        {
            GameDir = gameDir;
            this.log = log ?? (_ => { });
        }

        /// <summary>
        /// PatchedNoState: our output (by content) that no state file describes; PatchedNoBackup: our output without a backup
        /// restore could use (none, or not the one the state file records).
        /// Damaged: unreadable, restore can use the backup; DamagedNoBackup: unreadable, and there is no backup restore could use.
        /// Unsupported: in a format this patcher does not read (e.g. after a game update), not damage (IsNewFormat).
        /// </summary>
        public enum BundleState { Missing, Original, Reverted, Patched, PatchedNoState, PatchedNoBackup, Damaged, DamagedNoBackup, Unsupported, Unknown }

        public sealed class Status
        {
            public BundleState State;
            public string GameVersion;
            public Dictionary<string, string> PatchState;
            public List<Tuple<string, string>> Languages = new List<Tuple<string, string>>();
        }

        public static bool GameRunning() => Process.GetProcessesByName(GameLocator.GameExeName).Length > 0;

        /// <param name="data">
        /// When given, a bundle that matches neither the state file nor the backup is classified by content (also when
        /// there is neither a backup nor a state file); without it such a bundle is Unknown, or Original when there is
        /// neither a backup nor a state file (nothing is read but hashes).
        /// </param>
        public Status GetStatus(PatchData data = null)
        {
            var st = new Status { GameVersion = GameLocator.GameVersion(GameDir), PatchState = ReadState() };
            if (!File.Exists(Bundle))
                st.State = BundleState.Missing;
            else
            {
                string cur = Sha256(Bundle);
                bool hasBackup = File.Exists(Backup);
                if (StateDescribes(st.PatchState, cur))
                    // restore needs the backup recorded in the state file: without it only Steam's verification undoes the patch
                    st.State = hasBackup && BackupIsRecorded(st.PatchState) ? BundleState.Patched : BundleState.PatchedNoBackup;
                else if (hasBackup && Sha256(Backup) == cur)
                    st.State = st.PatchState != null ? BundleState.Reverted : BundleState.Original;
                else if (RecordedOriginal(st.PatchState, cur))
                    st.State = BundleState.Reverted; // the backup is missing or another file: apply backs this file up again
                else if (!hasBackup && st.PatchState == null && data == null)
                    st.State = BundleState.Original;
                else
                    st.State = BundleState.Unknown;
                if (st.State == BundleState.Unknown && data != null)
                {
                    // e.g. the state file could not be written after a successful apply, or a file without backup and state
                    try
                    {
                        using (var b = LocalisationBundle.Load(Bundle))
                        {
                            if (Classify(b, data).IsPatched)
                                st.State = hasBackup ? BundleState.PatchedNoState : BundleState.PatchedNoBackup;
                            else if (!hasBackup && st.PatchState == null)
                                st.State = BundleState.Original;
                        }
                    }
                    catch (InvalidDataException e)
                    {
                        st.State = IsNewFormat(e, st.PatchState) ? BundleState.Unsupported
                            : hasBackup && BackupUsable(st.PatchState, data) ? BundleState.Damaged : BundleState.DamagedNoBackup;
                    }
                }
            }
            foreach (var f in GameLocator.PreferenceFiles())
            {
                string account = Path.GetFileName(Path.GetDirectoryName(f));
                string lang;
                try
                {
                    lang = Preferences.LanguageName(Preferences.ReadLanguage(f).Selected);
                }
                catch (Exception e)
                {
                    lang = "読み取れません（" + e.Message + "）";
                }
                st.Languages.Add(Tuple.Create(account, lang));
            }
            return st;
        }

        // ---------------- apply ----------------

        public sealed class ApplyResult
        {
            public int Applied, SkippedOfficial, SkippedChanged, Missing;
        }

        public ApplyResult Apply(PatchData data)
        {
            EnsureGameClosed();
            RemoveLeftovers();
            var state = ReadState();
            string version = GameLocator.GameVersion(GameDir);
            bool hasBackup = File.Exists(Backup);
            bool newBackup = false; // the installed file is a new official file; it is backed up once the output is verified
            // game_version in the state is the version the backup was taken for. It is recorded when a new official file is
            // backed up, or the installed file is the backup itself (isBackup) or the official file the state records
            // (isRecordedOriginal, backed up again). When the installed file is only known to be the output of this backup
            // (isOutput: the state describes it, or the rebuilt output equals it), a recorded game_version is refreshed but
            // none is added: that output may itself have been rebuilt from a backup nobody checked (R1). Otherwise the old
            // value is kept, and an unreadable version.txt never replaces a known version of the same file (R3, S3).
            bool isBackup = false, isOutput = false, isRecordedOriginal = false;
            string cur = "";
            var current = new Match();
            if (!File.Exists(Bundle))
            {
                // e.g. a run killed while replacing it: the backup still holds the official data
                if (!hasBackup)
                    throw new PatchException("日本語データ（" + Path.GetFileName(Bundle) + "）が見つかりません。" + VerifyAdvice);
                CheckBackup(state, data, false);
                CheckBackupRecorded(state);
                log("日本語データが見つからないため、バックアップした公式データから作り直します");
            }
            else if (StateDescribes(state, cur = Sha256(Bundle)))
            {
                if (!hasBackup)
                    throw new PatchException("バックアップが見つかりません。" + VerifyAdvice);
                CheckBackup(state, data, true);
                log("適用済みのパッチを、バックアップした公式データから作り直します");
                isOutput = true;
            }
            else if (hasBackup && Sha256(Backup) == cur)
            {
                CheckBackup(state, data, false);
                log("公式の日本語データにパッチを適用します");
                isBackup = true;
            }
            else if (RecordedOriginal(state, cur))
            {
                // The official file recorded in the state file (e.g. after Steam's file verification) while the backup
                // is missing or another file: the installed file becomes the backup again.
                log("日本語データが記録されている公式データと一致するため、バックアップを作り直します");
                newBackup = isRecordedOriginal = true;
            }
            else
            {
                try
                {
                    using (var b = LocalisationBundle.Load(Bundle))
                        current = Classify(b, data);
                }
                catch (InvalidDataException e)
                {
                    // a format this patcher does not read (e.g. written by a newer Unity) after a game update: neither rebuilt
                    // from the backup nor backed up. Steam's verification gives the same file back, so the user also needs to
                    // know that the patch may have to be updated.
                    if (IsNewFormat(e, state))
                        throw new PatchException("日本語データを読み込めません（" + UnsupportedReason(e) + "）。" + VerifyAdvice + "。それでも直らない場合は、パッチの更新をお待ちください");
                    // A damaged game file is never backed up; rebuild from a good backup if there is one.
                    if (!hasBackup)
                        throw new PatchException("日本語データが壊れています（" + e.Message + "）。" + VerifyAdvice);
                    CheckBackup(state, data, false);
                    CheckBackupRecorded(state);
                    log("日本語データが壊れているため、バックアップした公式データから作り直します");
                    current = new Match { Damaged = true };
                }
                if (current.Damaged)
                {
                    // handled above
                }
                else if (current.IsPatched)
                {
                    // Our output without a matching state file (e.g. written by an older version of this tool, or a
                    // state file that could not be replaced after a successful apply: it may describe an older backup).
                    if (!hasBackup)
                        throw new PatchException("日本語データは以前のパッチで書き換えられていますが、バックアップが見つかりません。" + VerifyAdvice);
                    CheckBackup(state, data, false);
                    log("以前に適用したパッチを検出しました。バックアップした公式データから作り直します");
                }
                else if (hasBackup && SameGameVersion(state))
                {
                    // The official file of an unchanged game version is the backup itself, so this file was changed
                    // by something else (or is damaged in a way the checks cannot see): keep the backup.
                    CheckBackup(state, data, false);
                    CheckBackupRecorded(state);
                    log("日本語データがバックアップと異なりますが、ゲームのバージョンは変わっていないため、バックアップした公式データから作り直します");
                }
                else
                {
                    if (hasBackup)
                        log("日本語データが更新されています（ゲームのアップデートなど）。新しいデータをバックアップし直します");
                    newBackup = true;
                }
            }

            byte[] sourceBytes = File.ReadAllBytes(newBackup ? Bundle : Backup);
            string originalSha = Sha256(sourceBytes);
            if (data.BundleSha256.Length > 0 && data.BundleSha256 != originalSha)
                log(data.GameVersion == version
                    ? "注意: 公式の日本語データが、翻訳データを作ったときのものと異なります。公式訳が既にある項目は上書きしません"
                    : $"注意: 翻訳データは別のバージョンのゲーム（{data.GameVersion}）用に作られています。公式訳が既にある項目は上書きしません");

            var result = new ApplyResult();
            byte[] output;
            var expected = new Dictionary<string, string>(StringComparer.Ordinal);
            using (var bundle = LocalisationBundle.Load(sourceBytes, Path.GetFileName(Bundle)))
            {
                var originals = new Dictionary<string, I2Cache>(StringComparer.Ordinal);
                foreach (var name in bundle.Names)
                    originals[name] = new I2Cache(bundle.GetText(name));

                foreach (var source in data.Sources)
                {
                    if (!originals.TryGetValue(source.Key, out var cache))
                    {
                        result.Missing += source.Value.Count;
                        log($"注意: {source.Key} がゲームのデータにありません（{source.Value.Count} 件をスキップ）");
                        continue;
                    }
                    bool changed = false;
                    foreach (var e in source.Value)
                    {
                        if (!cache.TryGetValue(e.Key, out var official))
                        {
                            result.Missing++;
                            continue;
                        }
                        string ja = e.Japanese;
                        if (e.RefSource != null)
                        {
                            // Same English as another term: reuse its official Japanese from the game data.
                            if (!originals.TryGetValue(e.RefSource, out var refCache)
                                || !refCache.TryGetValue(e.RefKey, out ja) || !JapaneseRx.IsMatch(ja)
                                || ja.Contains(PatchData.Separator))
                            {
                                result.Missing++;
                                continue;
                            }
                        }
                        if (PatchData.BaseHash(official) == e.Base)
                        {
                            cache.SetValue(e.Key, ja);
                            changed = true;
                            result.Applied++;
                        }
                        else if (JapaneseRx.IsMatch(official))
                            result.SkippedOfficial++;
                        else
                            result.SkippedChanged++;
                    }
                    if (changed)
                    {
                        string text = cache.ToString();
                        bundle.SetText(source.Key, text);
                        expected[source.Key] = text;
                    }
                }
                if (result.Applied == 0)
                    throw new PatchException("適用できる翻訳がありませんでした（翻訳データがこのバージョンのゲームに対応していない可能性があります）");
                log("日本語データを書き出しています…");
                output = bundle.Save();
            }

            // Verify the new bundle before it replaces the game's file.
            using (var check = LocalisationBundle.Load(output, Path.GetFileName(Bundle)))
            using (var orig = LocalisationBundle.Load(sourceBytes, Path.GetFileName(Bundle)))
            {
                var names = new HashSet<string>(orig.Names, StringComparer.Ordinal);
                if (!names.SetEquals(check.Names))
                    throw new PatchException("書き出したデータの検証に失敗しました（項目の数が一致しません）");
                foreach (var name in names)
                {
                    string want = expected.TryGetValue(name, out var t) ? t : orig.GetText(name);
                    if (check.GetText(name) != want)
                        throw new PatchException("書き出したデータの検証に失敗しました: " + name);
                }
            }

            if (newBackup)
            {
                SafeFile.Write(Backup, sourceBytes);
                if (Sha256(Backup) != originalSha)
                    throw new PatchException("バックアップの作成を確認できませんでした。ディスクの空き容量を確認してください");
                log("バックアップを作成しました: " + Path.GetFileName(Backup));
            }

            string outputSha = Sha256(output);
            if (File.Exists(Bundle) && SafeFile.ClearReadOnly(Bundle))
                log("日本語データの読み取り専用属性を解除しました");
            SafeFile.Write(Bundle, output);
            if (Sha256(Bundle) != outputSha)
                throw new PatchException("日本語データの書き込みを確認できませんでした。" + VerifyAdvice);

            if (current.IsPatched && outputSha == cur)
                isOutput = true; // the installed file was exactly the output of this backup
            var newState = state ?? new Dictionary<string, string>(StringComparer.Ordinal);
            newState["patch_version"] = data.PatchVersion;
            bool recorded = newState.TryGetValue("game_version", out var recordedVersion);
            if ((newBackup && !isRecordedOriginal)
                || ((isBackup || isRecordedOriginal || (isOutput && recorded)) && (KnownVersion(version) || !recorded || !KnownVersion(recordedVersion))))
                newState["game_version"] = version;
            newState["original_sha256"] = originalSha;
            newState["patched_sha256"] = outputSha;
            newState["applied"] = result.Applied.ToString(CultureInfo.InvariantCulture);
            newState["skipped_official"] = result.SkippedOfficial.ToString(CultureInfo.InvariantCulture);
            newState["skipped_changed"] = result.SkippedChanged.ToString(CultureInfo.InvariantCulture);
            newState["time"] = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture);
            try
            {
                WriteState(newState);
            }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
            {
                // The bundle is already patched and verified; without a state file it is recognised by its content.
                log("注意: 状態ファイルを書き込めませんでした（日本語データの書き換えは完了しています）: " + e.Message);
            }

            log($"日本語データにパッチを適用しました（翻訳 {result.Applied} 件）");
            if (result.SkippedOfficial > 0)
                log($"公式訳が追加されていた {result.SkippedOfficial} 件は公式訳のままにしました");
            if (result.SkippedChanged > 0)
                log($"ゲーム側の原文が変わっていた {result.SkippedChanged} 件は適用しませんでした（パッチの更新をお待ちください）");
            if (result.Missing > 0)
                log($"ゲームのデータに無い {result.Missing} 件はスキップしました");
            return result;
        }

        // ---------------- restore ----------------

        /// <param name="data">Used to recognise our output when the state file does not describe it; may be null.</param>
        public void Restore(PatchData data)
        {
            EnsureGameClosed();
            RemoveLeftovers();
            var state = ReadState();
            string cur = File.Exists(Bundle) ? Sha256(Bundle) : "";
            string bak = File.Exists(Backup) ? Sha256(Backup) : null;
            bool stateIsCurrent = StateDescribes(state, cur);
            if (bak != cur && !stateIsCurrent && RecordedOriginal(state, cur))
            {
                // The official file recorded in the state file (e.g. after Steam's file verification): a missing or
                // other backup is not written over it.
                log("日本語データは公式のままです");
                RemoveBackupAndState();
                return;
            }
            if (bak == null)
            {
                bool patched = stateIsCurrent;
                string notPatched = "パッチは適用されていません（バックアップがありません）";
                if (!patched && cur.Length > 0 && data != null)
                {
                    try
                    {
                        using (var b = LocalisationBundle.Load(Bundle))
                            patched = Classify(b, data).IsPatched;
                    }
                    catch (InvalidDataException e)
                    {
                        if (!IsNewFormat(e, state))
                            throw new PatchException("日本語データが壊れていて、バックアップもありません。" + VerifyAdvice);
                        notPatched = "パッチは適用されていません（日本語データはこのパッチが対応していない形式です）";
                    }
                }
                if (patched)
                    throw new PatchException("日本語データはパッチで書き換えられていますが、バックアップが見つかりません。" + VerifyAdvice);
                SafeFile.Delete(StatePath);
                log(notPatched);
                return;
            }

            if (cur == bak)
            {
                CheckBackup(state, data, false); // a patched backup must not be taken for the official file
                log("日本語データは公式のままです");
                RemoveBackupAndState();
                return;
            }

            bool restore;
            bool onlyState = false; // only the state file vouches for the backup
            string why = null; // logged once the backup passed its checks (a refusal is not announced as a restore)
            if (stateIsCurrent)
                restore = true;
            else if (cur.Length == 0)
                restore = onlyState = true;
            else
            {
                Match current;
                try
                {
                    using (var b = LocalisationBundle.Load(Bundle))
                        current = data != null ? Classify(b, data) : new Match();
                }
                catch (InvalidDataException e) when (IsNewFormat(e, state))
                {
                    // a newer official file (this patcher never wrote it): kept, and the old backup is not written over it
                    log("日本語データはこのパッチが対応していない形式のため、そのままにします");
                    RemoveBackupAndState();
                    return;
                }
                catch (InvalidDataException)
                {
                    current = new Match { Damaged = true };
                }
                if (current.Damaged)
                {
                    why = "日本語データが壊れているため、バックアップから戻します";
                    restore = onlyState = true;
                }
                else if (SameGameVersion(state) && (data == null || !current.IsPatched))
                {
                    // the official file of an unchanged game version is the backup itself
                    why = "日本語データがバックアップと異なりますが、ゲームのバージョンは変わっていないため、バックアップから戻します";
                    restore = onlyState = true;
                }
                else if (data == null)
                    throw new PatchException("翻訳データを読み込めないため、日本語データがパッチで書き換えられたものか確認できません。バックアップは削除せずに残しました。" + VerifyAdvice);
                else
                    restore = current.IsPatched;
            }

            if (!restore)
            {
                // Neither our output nor the backup: the game updated it after patching. Keep the newer file.
                log("日本語データはゲームのアップデートで新しくなっているため、そのままにします");
                RemoveBackupAndState();
                return;
            }

            CheckBackup(state, data, stateIsCurrent);
            if (onlyState)
                CheckBackupRecorded(state);
            if (why != null)
                log(why);
            SafeFile.Write(Bundle, File.ReadAllBytes(Backup));
            if (Sha256(Bundle) != bak)
                throw new PatchException("元に戻したデータの確認に失敗しました。バックアップは削除せずに残しました。" + VerifyAdvice);
            log("日本語データを公式のものに戻しました");
            RemoveBackupAndState();
        }

        void RemoveLeftovers()
        {
            int n = SafeFile.RemoveLeftovers(Bundle) + SafeFile.RemoveLeftovers(Backup) + SafeFile.RemoveLeftovers(StatePath);
            if (n > 0)
                log($"前回中断したときの一時ファイルを {n} 個削除しました");
        }

        void RemoveBackupAndState()
        {
            SafeFile.Delete(Backup);
            SafeFile.Delete(StatePath);
        }

        // ---------------- language ----------------

        public sealed class LanguageResult
        {
            public int Changed, Unchanged;
            public readonly List<string> Failures = new List<string>();
        }

        /// <summary>Sets the in-game language for every Steam account that has played the game.</summary>
        public LanguageResult SetLanguage(int languageId)
        {
            EnsureGameClosed();
            var result = new LanguageResult();
            var files = GameLocator.PreferenceFiles();
            if (files.Count == 0)
            {
                Fail(result, "ゲームの設定ファイルが見つかりません。一度ゲームを起動してタイトル画面まで進めてから、もう一度実行してください");
                return result;
            }
            foreach (var f in files)
            {
                string account = Path.GetFileName(Path.GetDirectoryName(f));
                RemovePreferencesLeftovers(f, account);
                try
                {
                    var info = Preferences.ReadLanguage(f);
                    if (info.Selected == languageId)
                    {
                        log($"表示言語は既に{Preferences.LanguageName(languageId)}です（{account}）");
                        result.Unchanged++;
                        continue;
                    }
                    BackupPreferences(f, account);
                    long old = Preferences.SetLanguage(f, languageId);
                    result.Changed++;
                    log($"表示言語を{Preferences.LanguageName(old)}から{Preferences.LanguageName(languageId)}に変更しました（{account}）");
                    if (languageId == Preferences.Japanese)
                        RememberPreviousLanguage(account, old);
                }
                catch (Exception e) when (e is InvalidDataException || e is IOException || e is UnauthorizedAccessException)
                {
                    Fail(result, $"表示言語を変更できませんでした（{account}）: {PreferencesError(e)}");
                }
            }
            return result;
        }

        /// <summary>Switches accounts that are set to Japanese back to the language they had before (default English).</summary>
        public LanguageResult RestoreLanguage()
        {
            EnsureGameClosed();
            var result = new LanguageResult();
            foreach (var f in GameLocator.PreferenceFiles())
            {
                string account = Path.GetFileName(Path.GetDirectoryName(f));
                RemovePreferencesLeftovers(f, account);
                try
                {
                    if (Preferences.ReadLanguage(f).Selected != Preferences.Japanese)
                    {
                        result.Unchanged++;
                        continue;
                    }
                    // the record is only a hint (English is the default): when this patcher's own record cannot be read, the
                    // account is still switched back, to English, and preferences.json is not blamed
                    int previous = Preferences.English;
                    string unreadable = null;
                    try
                    {
                        string saved = PreviousLanguagePath(account);
                        // one of the game's languages other than Japanese; anything else (a damaged record) -> English
                        if (File.Exists(saved) && int.TryParse(File.ReadAllText(saved).Trim(), out var v) && v != Preferences.Japanese
                            && Preferences.IsGameLanguage(v))
                            previous = v;
                    }
                    catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
                    {
                        unreadable = e.Message;
                    }
                    BackupPreferences(f, account);
                    Preferences.SetLanguage(f, previous);
                    result.Changed++;
                    log($"表示言語を{Preferences.LanguageName(previous)}に戻しました（{account}）");
                    if (unreadable != null)
                        log("注意: 元の表示言語の記録を読み取れないため、英語に戻しました（" + unreadable + "）");
                }
                catch (Exception e) when (e is InvalidDataException || e is IOException || e is UnauthorizedAccessException)
                {
                    Fail(result, $"表示言語を戻せませんでした（{account}）: {PreferencesError(e)}");
                }
            }
            if (result.Changed == 0 && result.Failures.Count == 0)
                log("表示言語が日本語のアカウントはないため、表示言語は変更しませんでした");
            return result;
        }

        void Fail(LanguageResult result, string message)
        {
            result.Failures.Add(message);
            log(message);
        }

        /// <summary>
        /// Deletes what an interrupted write of an account's preferences.json left next to it ("preferences.json.tpcjp-tmp",
        /// e.g. held by a scanner when SafeFile gave up, and the "~RF*.TMP" copies of version 1.0.0's File.Replace), so that
        /// Steam Cloud does not keep them. Best effort: a leftover still in use stays for the next run.
        /// </summary>
        void RemovePreferencesLeftovers(string file, string account)
        {
            int n;
            try
            {
                n = SafeFile.RemoveLeftovers(file);
            }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
            {
                return; // the folder cannot be listed: reading preferences.json reports the problem
            }
            if (n > 0)
                log($"前回中断したときの一時ファイルを {n} 個削除しました（{account}）");
        }

        // a read-only preferences.json is refused earlier by Preferences.SetLanguage (and SafeFile clears the attribute),
        // so the reason of the failure is all that helps here; access can be denied to reading as well as to writing
        static string PreferencesError(Exception e) =>
            SafeFile.IsReplaceError(e) ? e.Message // names preferences.json and the reason already
            : e is UnauthorizedAccessException ? "設定ファイルにアクセスできません（" + e.Message + "）"
            : e is IOException ? "設定ファイルを読み書きできません（" + e.Message + "）"
            : e.Message;

        static void BackupPreferences(string file, string account)
        {
            byte[] content = File.ReadAllBytes(file);
            string backupDir = Path.Combine(AppDataDir, "backup", "preferences", account);
            try
            {
                Directory.CreateDirectory(backupDir);
                File.WriteAllBytes(Path.Combine(backupDir, DateTime.Now.ToString("yyyyMMdd-HHmmss-fff", CultureInfo.InvariantCulture) + ".json"), content);
            }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
            {
                throw new InvalidDataException("設定ファイルのバックアップを作成できません（" + backupDir + "）: " + e.Message, e);
            }
        }

        void RememberPreviousLanguage(string account, long language)
        {
            try
            {
                File.WriteAllText(PreviousLanguageFile(account), language.ToString(CultureInfo.InvariantCulture));
            }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
            {
                // an older remembered language must not be used instead
                try
                {
                    SafeFile.Delete(PreviousLanguageFile(account));
                    log("注意: 元の表示言語を記録できませんでした（「元に戻す」では英語に戻します）");
                }
                catch (Exception e2) when (e2 is IOException || e2 is UnauthorizedAccessException)
                {
                    // without a record (e.g. its folder cannot be created) restore goes to English
                    log(File.Exists(PreviousLanguagePath(account))
                        ? "注意: 元の表示言語を記録できませんでした（「元に戻す」では以前に記録した言語に戻る場合があります）"
                        : "注意: 元の表示言語を記録できませんでした（「元に戻す」では英語に戻します）");
                }
            }
        }

        /// <summary>The record of an account's language before Japanese, with its folder created (for writing).</summary>
        static string PreviousLanguageFile(string account)
        {
            string path = PreviousLanguagePath(account);
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            return path;
        }

        static string PreviousLanguagePath(string account) => Path.Combine(AppDataDir, "previous-language", account + ".txt");

        // ---------------- identification ----------------

        /// <summary>
        /// How the terms of a bundle relate to the patch data. No-op entries (our text equals the official
        /// one) and references are not counted, so an untouched official bundle scores Ours ~ 0.
        /// </summary>
        struct Match
        {
            public int Ours;      // holds our translation
            public int Official;  // still holds the official value we translate over (base hash)
            public int Other;     // other Japanese text: an official translation added by an update
            public bool Damaged;

            /// <summary>Our output: our texts clearly outnumber the official ones (an update may translate a few terms like we did).</summary>
            public bool IsPatched => Ours >= 3 && Ours > Official + Other;
        }

        static Match Classify(LocalisationBundle bundle, PatchData data)
        {
            var m = new Match();
            foreach (var source in data.Sources)
            {
                if (!bundle.Contains(source.Key))
                    continue;
                var cache = new I2Cache(bundle.GetText(source.Key));
                foreach (var e in source.Value)
                {
                    if (e.Japanese == null || e.IsNoOp || !cache.TryGetValue(e.Key, out var v))
                        continue;
                    if (v == e.Japanese)
                        m.Ours++;
                    else if (PatchData.BaseHash(v) == e.Base)
                        m.Official++;
                    else if (JapaneseRx.IsMatch(v))
                        m.Other++;
                }
            }
            return m;
        }

        /// <summary>
        /// The backup must load and hold official content; when the state file describes the installed
        /// bundle, the backup must also be the file recorded in it. Nothing is changed when it fails.
        /// </summary>
        void CheckBackup(Dictionary<string, string> state, PatchData data, bool stateIsCurrent)
        {
            if (stateIsCurrent && !BackupIsRecorded(state))
                throw new PatchException(BackupMismatch);
            try
            {
                using (var b = LocalisationBundle.Load(Backup))
                {
                    if (data != null && Classify(b, data).IsPatched)
                        throw new PatchException("バックアップがパッチ適用後のデータになっています。バックアップは削除せずに残しました。" + VerifyAdvice);
                }
            }
            catch (InvalidDataException e)
            {
                throw new PatchException("バックアップが壊れています（" + e.Message + "）。バックアップは削除せずに残しました。" + VerifyAdvice);
            }
        }

        const string BackupMismatch = "バックアップが記録と一致しません（壊れているか、別のファイルに置き換わっています）。バックアップは削除せずに残しました。" + VerifyAdvice;

        /// <summary>
        /// Where nothing but the state file vouches for the backup (the installed bundle is missing or damaged, or it
        /// differs from the backup while the game version is unchanged): the backup must be the file recorded in the
        /// state file (when it records one), taken for the installed game version (both versions known). A missing state
        /// file, or one without game_version (version 1.0.0, or a lost / damaged / edited state), says nothing about the
        /// backup's version: refused like an unknown recorded version, since a backup used unchecked would later be recorded
        /// for the installed version and keep its old data after every Steam verification (S1). Nothing is changed when it fails.
        /// </summary>
        void CheckBackupRecorded(Dictionary<string, string> state)
        {
            if (state != null && !BackupIsRecorded(state))
                throw new PatchException(BackupMismatch);
            if (SameGameVersion(state))
                return;
            string v = null;
            state?.TryGetValue("game_version", out v);
            string current = GameLocator.GameVersion(GameDir);
            // "another version" only when both are known: an unknown one may well be the same version
            throw new PatchException((KnownVersion(v) && KnownVersion(current)
                                         ? $"バックアップは別のバージョンのゲーム（{v}）の公式データです（現在のゲームのバージョン: {current}）。"
                                         : $"バックアップがどのバージョンのゲームのものか確認できません（記録: {ShowVersion(v)}、現在のゲームのバージョン: {ShowVersion(current)}）。")
                                     + "バックアップは削除せずに残しました。" + VerifyAdvice);
        }

        /// <summary>The backup is the file recorded in the state file (original_sha256), or the state file records none.</summary>
        bool BackupIsRecorded(Dictionary<string, string> state) =>
            !state.TryGetValue("original_sha256", out var o) || o == Sha256(Backup);

        /// <summary>A game version that can be compared: version.txt was read (not 「不明」) and is not empty.</summary>
        static bool KnownVersion(string v) => !string.IsNullOrEmpty(v) && v != GameLocator.UnknownVersion;

        static string ShowVersion(string v) => string.IsNullOrEmpty(v) ? GameLocator.UnknownVersion : v;

        /// <summary>
        /// The reason given by an UnsupportedFormatException in e's inner exceptions (a format this patcher does not read),
        /// or null when e reports damaged data.
        /// </summary>
        static string UnsupportedReason(Exception e)
        {
            if (!UnsupportedFormatException.In(e))
                return null;
            while (!(e is UnsupportedFormatException))
                e = e.InnerException;
            return e.Message;
        }

        /// <summary>
        /// The installed bundle failed to load (e) in a format this patcher does not read, and the state file does not prove
        /// that the game version is unchanged: a newer official file (e.g. written by a newer Unity after a game update), not
        /// damage. It is never this patcher's output (it writes only formats it reads), so it is neither rebuilt from nor
        /// restored over with the backup, and not backed up. With an unchanged game version the official file is the backup,
        /// which loaded when it was taken: the format bytes are damage then.
        /// </summary>
        bool IsNewFormat(Exception e, Dictionary<string, string> state) => UnsupportedFormatException.In(e) && !SameGameVersion(state);

        /// <summary>Whether Restore could use the backup for a damaged bundle (the checks of CheckBackup and CheckBackupRecorded).</summary>
        bool BackupUsable(Dictionary<string, string> state, PatchData data)
        {
            try
            {
                CheckBackup(state, data, false);
                CheckBackupRecorded(state);
                return true;
            }
            catch (PatchException)
            {
                return false;
            }
        }

        /// <summary>The installed file is the official file recorded in the state file (original_sha256).</summary>
        static bool RecordedOriginal(Dictionary<string, string> state, string currentSha) =>
            currentSha.Length > 0 && state != null && state.TryGetValue("original_sha256", out var o) && o == currentSha;

        /// <summary>
        /// The state file records the installed game version as the backup's (false without a state file or a game_version:
        /// states of version 1.0.0 have none); an unreadable or empty version.txt is never the same version.
        /// </summary>
        bool SameGameVersion(Dictionary<string, string> state)
        {
            string current = GameLocator.GameVersion(GameDir);
            return state != null && state.TryGetValue("game_version", out var v) && KnownVersion(current) && v == current;
        }

        static bool StateDescribes(Dictionary<string, string> state, string currentSha) =>
            currentSha.Length > 0 && state != null && state.TryGetValue("patched_sha256", out var p) && p == currentSha;

        // ---------------- helpers ----------------

        /// <summary>
        /// Only one TpcJpPatch (GUI or CLI) may change files at a time: concurrent runs would remove each
        /// other's temp files. Dispose on the thread that acquired it.
        /// </summary>
        public static IDisposable Lock()
        {
            var mutex = new Mutex(false, @"Local\TpcJpPatch");
            bool owned;
            try
            {
                owned = mutex.WaitOne(0);
            }
            catch (AbandonedMutexException)
            {
                owned = true; // the previous owner ended without releasing it
            }
            if (!owned)
            {
                mutex.Dispose();
                throw new PatchException("別の TpcJpPatch が実行中です。終わってから、もう一度実行してください");
            }
            return new MutexLock(mutex);
        }

        sealed class MutexLock : IDisposable
        {
            Mutex mutex;
            public MutexLock(Mutex mutex) => this.mutex = mutex;
            public void Dispose()
            {
                if (mutex == null)
                    return;
                mutex.ReleaseMutex();
                mutex.Dispose();
                mutex = null;
            }
        }

        public static string AppDataDir =>
#if DEBUG
            // Test hook (debug builds only): keep the test's backups, logs and previous languages apart.
            Environment.GetEnvironmentVariable("TPCJP_APPDATA") ??
#endif
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "TpcJpPatch");

        static void EnsureGameClosed()
        {
            if (GameRunning())
                throw new PatchException("Two Point Campus が起動しています。ゲームを終了してから実行してください");
        }

        Dictionary<string, string> ReadState()
        {
            if (!File.Exists(StatePath))
                return null;
            var d = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (var line in File.ReadAllLines(StatePath, Encoding.UTF8))
            {
                int eq = line.IndexOf('=');
                if (eq > 0)
                    d[line.Substring(0, eq)] = line.Substring(eq + 1);
            }
            return d;
        }

        // one line per entry: a line break inside a key or value would add entries (File.ReadAllLines splits at CR / LF,
        // Python's str.splitlines also at VT, FF, U+001C..U+001E, U+0085, U+2028 and U+2029)
        void WriteState(Dictionary<string, string> d) =>
            SafeFile.Write(StatePath, new UTF8Encoding(false).GetBytes(
                string.Concat(d.Select(kv => OneLine(kv.Key) + "=" + OneLine(kv.Value) + Environment.NewLine))));

        /// <summary>The value as the state file stores it (also how GameLocator.GameVersion reads version.txt, S4).</summary>
        internal static string OneLine(string s) => s == null ? "" : new string(s.Select(c => IsLineBreak(c) ? ' ' : c).ToArray());

        static bool IsLineBreak(char c) => c == '\n' || c == '\v' || c == '\f' || c == '\r' || (c >= 0x1C && c <= 0x1E) || c == 0x85 || c == 0x2028 || c == 0x2029;

        public static string Sha256(string path)
        {
            using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                return Sha256(fs);
        }

        static string Sha256(byte[] data)
        {
            using (var ms = new MemoryStream(data, false))
                return Sha256(ms);
        }

        static string Sha256(Stream s)
        {
            using (var sha = SHA256.Create())
            {
                var h = sha.ComputeHash(s);
                var sb = new StringBuilder(64);
                foreach (var b in h)
                    sb.Append(b.ToString("x2"));
                return sb.ToString();
            }
        }
    }

    sealed class PatchException : Exception
    {
        public PatchException(string message) : base(message) { }
    }
}
