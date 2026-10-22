using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace TpcJpPatch.Tests
{
    /// <summary>
    /// Patcher.SetLanguage / RestoreLanguage and the CLI (Cli.Run in-process, Debug build) with the test hooks
    /// TPCJP_PREFS_ROOT / TPCJP_APPDATA pointing into the temp folder. The real preferences are never seen.
    /// </summary>
    static class LanguageTests
    {
        sealed class Env : IDisposable
        {
            readonly string oldPrefs = Environment.GetEnvironmentVariable("TPCJP_PREFS_ROOT");
            readonly string oldApp = Environment.GetEnvironmentVariable("TPCJP_APPDATA");
            public readonly string Prefs, AppData;

            public Env(Ctx ctx, string name)
            {
                string root = ctx.Dir("lang_" + name);
                Prefs = Path.Combine(root, "Cloud");
                AppData = Path.Combine(root, "appdata");
                Directory.CreateDirectory(Prefs);
                ctx.Guard(Prefs);
                ctx.Guard(AppData);
                Environment.SetEnvironmentVariable("TPCJP_PREFS_ROOT", Prefs);
                Environment.SetEnvironmentVariable("TPCJP_APPDATA", AppData);
                T.Eq(AppData, Patcher.AppDataDir, "TPCJP_APPDATA hook active");
            }

            public string Account(string id, byte[] prefs)
            {
                string dir = Path.Combine(Prefs, id);
                Directory.CreateDirectory(dir);
                string p = Path.Combine(dir, "preferences.json");
                File.WriteAllBytes(p, prefs);
                return p;
            }

            public string Previous(string id) => Path.Combine(AppData, "previous-language", id + ".txt");

            public string[] Backups(string id)
            {
                string d = Path.Combine(AppData, "backup", "preferences", id);
                return Directory.Exists(d) ? Directory.GetFiles(d) : new string[0];
            }

            public void Dispose()
            {
                Environment.SetEnvironmentVariable("TPCJP_PREFS_ROOT", oldPrefs);
                Environment.SetEnvironmentVariable("TPCJP_APPDATA", oldApp);
            }
        }

        static long Lang(string p) => Preferences.ReadLanguage(p).Selected;

        static Patcher NewPatcher(Ctx ctx, List<string> log) => new Patcher(Path.Combine(ctx.RunDir, "no_game"), s => log.Add(s));

        /// <summary>
        /// Runs Cli.Run in-process and captures its console output. Retries while a process named TPC runs or
        /// while another suite's TpcJpPatch holds the lock (both refusals change nothing); CliOnce does not retry.
        /// </summary>
        public static int Cli(out string output, params string[] args)
        {
            string o = null;
            int code = Util.Retry(() =>
            {
                int c = CliOnce(out o, args);
                if (o.Contains("起動しています"))
                    throw new PatchException("起動しています");
                if (o.Contains(Util.BusyMessage))
                    throw new PatchException(Util.BusyMessage);
                return c;
            });
            output = o;
            return code;
        }

        static readonly object ConsoleGate = new object();

        public static int CliOnce(out string output, params string[] args)
        {
            lock (ConsoleGate)
            {
                var old = Console.Out;
                var sw = new StringWriter();
                Console.SetOut(sw);
                try
                {
                    return TpcJpPatch.Cli.Run(args);
                }
                finally
                {
                    Console.SetOut(old);
                    output = sw.ToString();
                }
            }
        }

        public static void Run(Runner r, Ctx ctx)
        {
            string origCopy = Path.Combine(ctx.TempRoot, "preferences.orig.json");
            byte[] real = File.Exists(origCopy) ? File.ReadAllBytes(origCopy) : Mp.Prefs(Mp.Int(0));
            long realLang = Preferences.ReadLanguage(WriteTemp(ctx, real)).Selected;
            var french = Mp.Prefs(Mp.Int(1));

            r.Test("Language: two accounts -> Japanese with backups and previous languages; RestoreLanguage gives the bytes back", () =>
            {
                using (var env = new Env(ctx, "two"))
                {
                    var src = realLang == 12 ? Mp.Prefs(Mp.Int(0)) : real;
                    string a = env.Account("1111", src), b = env.Account("2222", french);
                    var log = new List<string>();
                    var res = NewPatcher(ctx, log).SetLanguage(Preferences.Japanese);
                    T.Eq(2, res.Changed, "changed");
                    T.Eq(0, res.Failures.Count, "failures: " + string.Join(" / ", res.Failures));
                    T.Eq(12L, Lang(a), "account 1 Japanese");
                    T.Eq(12L, Lang(b), "account 2 Japanese");
                    T.Eq(Preferences.ReadLanguage(WriteTemp(ctx, src)).Selected.ToString(), File.ReadAllText(env.Previous("1111")), "previous 1");
                    T.Eq("1", File.ReadAllText(env.Previous("2222")), "previous 2");
                    T.Eq(1, env.Backups("1111").Length, "one backup for account 1");
                    T.Bytes(src, File.ReadAllBytes(env.Backups("1111")[0]), "backup bytes 1");
                    T.Bytes(french, File.ReadAllBytes(env.Backups("2222")[0]), "backup bytes 2");
                    T.True(log.Any(l => l.Contains("フランス語から日本語")), "log names the languages");

                    var res2 = NewPatcher(ctx, log).SetLanguage(Preferences.Japanese);
                    T.Eq(0, res2.Changed, "second run changes nothing");
                    T.Eq(2, res2.Unchanged, "unchanged");
                    T.Eq(1, env.Backups("1111").Length, "no new backup when unchanged");

                    var back = NewPatcher(ctx, log).RestoreLanguage();
                    T.Eq(2, back.Changed, "restored");
                    T.Bytes(src, File.ReadAllBytes(a), "account 1 bytes");
                    T.Bytes(french, File.ReadAllBytes(b), "account 2 bytes");
                    T.Eq(2, NewPatcher(ctx, log).RestoreLanguage().Unchanged, "restore again: nothing to do");
                    T.True(!File.Exists(a + SafeFile.TempSuffix) && !File.Exists(b + SafeFile.TempSuffix), "temp files");
                }
            });

            r.Test("Language: RestoreLanguage falls back to English for a missing / Japanese / garbage / out-of-range previous-language file", () =>
            {
                using (var env = new Env(ctx, "prev"))
                {
                    string a = env.Account("1", Mp.Prefs(Mp.Int(12)));
                    string b = env.Account("2", Mp.Prefs(Mp.Int(12)));
                    string c = env.Account("3", Mp.Prefs(Mp.Int(12)));
                    string d = env.Account("4", Mp.Prefs(Mp.Int(12)));
                    // round 3c review: a number that is not one of the game's languages 0..12 gives English (it was written
                    // into preferences.json, or failed the account when it did not fit); 6 (Russian, unnamed) is kept
                    string e = env.Account("5", Mp.Prefs(Mp.Int(12)));
                    string f = env.Account("6", Mp.Prefs(Mp.Int(12)));
                    string g = env.Account("7", Mp.Prefs(Mp.Int(12)));
                    Directory.CreateDirectory(Path.GetDirectoryName(env.Previous("2")));
                    File.WriteAllText(env.Previous("2"), "12");
                    File.WriteAllText(env.Previous("3"), "abc");
                    File.WriteAllText(env.Previous("4"), " 9 \r\n");
                    File.WriteAllText(env.Previous("5"), "99");
                    File.WriteAllText(env.Previous("6"), "-1");
                    File.WriteAllText(env.Previous("7"), "6");
                    var res = NewPatcher(ctx, new List<string>()).RestoreLanguage();
                    T.Eq(0, res.Failures.Count, "no failure: " + string.Join(" / ", res.Failures));
                    T.Eq(7, res.Changed, "changed");
                    T.Eq(0L, Lang(a), "missing -> English");
                    T.Eq(0L, Lang(b), "12 -> English");
                    T.Eq(0L, Lang(c), "garbage -> English");
                    T.Eq(9L, Lang(d), "9 (with spaces) -> Korean");
                    T.Eq(0L, Lang(e), "99 -> English");
                    T.Eq(0L, Lang(f), "-1 -> English");
                    T.Eq(6L, Lang(g), "6 -> Russian");
                }
            });

            r.Test("Language: no preferences.json at all -> one failure telling the user to start the game", () =>
            {
                using (new Env(ctx, "none"))
                {
                    var res = NewPatcher(ctx, new List<string>()).SetLanguage(Preferences.Japanese);
                    T.Eq(1, res.Failures.Count, "failures");
                    T.Contains(res.Failures[0], "設定ファイルが見つかりません", "message");
                    T.Eq(0, NewPatcher(ctx, new List<string>()).RestoreLanguage().Failures.Count, "restore: nothing to do, no failure");
                }
            });

            r.Test("Language: a broken / read-only / locked / not-yet-initialised account fails alone; the others are switched", () =>
            {
                using (var env = new Env(ctx, "mixed"))
                {
                    var broken = Mp.Prefs(Mp.Int(0)).Take(40).ToArray();
                    var noFlag = Mp.Prefs(Mp.Int(0), hasExpected: false);
                    string pb = env.Account("1_broken", broken);
                    string pr = env.Account("2_readonly", Mp.Prefs(Mp.Int(0)));
                    string pl = env.Account("3_locked", Mp.Prefs(Mp.Int(0)));
                    string pn = env.Account("4_noflag", noFlag);
                    string pg = env.Account("5_good", Mp.Prefs(Mp.Int(0)));
                    File.SetAttributes(pr, FileAttributes.ReadOnly);
                    Patcher.LanguageResult res;
                    using (new FileStream(pl, FileMode.Open, FileAccess.Read, FileShare.None))
                        res = NewPatcher(ctx, new List<string>()).SetLanguage(Preferences.Japanese);
                    File.SetAttributes(pr, FileAttributes.Normal);
                    T.Eq(1, res.Changed, "only the good account changed");
                    T.Eq(4, res.Failures.Count, "failures: " + string.Join(" / ", res.Failures));
                    T.Contains(res.Failures[0], "1_broken", "account named");
                    T.Contains(res.Failures[1], "読み取り専用", "read-only message");
                    T.Contains(res.Failures[2], "設定ファイルを読み書きできません（", "locked message (round 3b, R12/R21)");
                    T.Contains(res.Failures[3], "一度ゲームを起動", "not initialised message");
                    T.Bytes(broken, File.ReadAllBytes(pb), "broken unchanged");
                    T.Eq(0L, Lang(pr), "read-only unchanged");
                    T.Eq(0L, Lang(pl), "locked unchanged");
                    T.Bytes(noFlag, File.ReadAllBytes(pn), "not initialised unchanged");
                    T.Eq(12L, Lang(pg), "good switched");
                    foreach (var f in res.Failures)
                        r.Note(f);
                }
            });

            r.Test("Language: a preferences.json with a huge str32 length fails alone (others still switched)", () =>
            {
                using (var env = new Env(ctx, "huge"))
                {
                    var bad = Mp.Prefs(Mp.Int(0), extraLangKv: Mp.Cat(Mp.Str("_junk"), new byte[] { 0xDB, 0x7F, 0xFF, 0xFF, 0xF0 }, new byte[] { 0x41 }));
                    bad[IndexOfLangMap(bad)] = 0x85; // the language map now has 5 pairs
                    string pb = env.Account("1_bad", bad);
                    string pg = env.Account("2_good", Mp.Prefs(Mp.Int(0)));
                    Patcher.LanguageResult res = null;
                    Exception escaped = null;
                    try
                    {
                        res = NewPatcher(ctx, new List<string>()).SetLanguage(Preferences.Japanese);
                    }
                    catch (Exception e)
                    {
                        escaped = e;
                    }
                    T.Bytes(bad, File.ReadAllBytes(pb), "bad file unchanged");
                    T.True(escaped == null, "SetLanguage threw " + escaped?.GetType().FullName + ": " + escaped?.Message + " (the remaining accounts were not processed)");
                    T.Eq(1, res.Failures.Count, "one failure");
                    T.Contains(res.Failures[0], "1_bad", "the account is named");
                    T.Contains(res.Failures[0], "途中で切れています", "the reason is given");
                    T.Eq(12L, Lang(pg), "the good account is switched");
                }
            });

            r.Test("Language: duplicate keys / too deep nesting in one account fail alone (others still switched) (FZ-12, CS-04)", () =>
            {
                using (var env = new Env(ctx, "dup_deep"))
                {
                    var sel = Mp.Str("_selectedLanguage");
                    var dup = Mp.Arr(Mp.Int(2), Mp.Int(0), Mp.Map(Mp.Str("_languagePreferences"), Mp.Arr(Mp.Int(2), Mp.Int(6),
                        Mp.Map(sel, Mp.Int(0), Mp.Str("_hasExpectedPlatformLanguage"), Mp.True, sel, Mp.Int(1)))));
                    var deep = Enumerable.Repeat((byte)0x91, 100000).Concat(new byte[] { 0x00 }).ToArray();
                    string pd = env.Account("1_dup", dup), pn = env.Account("2_deep", deep), pg = env.Account("3_good", Mp.Prefs(Mp.Int(0)));
                    var res = NewPatcher(ctx, new List<string>()).SetLanguage(Preferences.Japanese);
                    T.Eq(2, res.Failures.Count, "failures: " + string.Join(" / ", res.Failures));
                    T.Contains(res.Failures[0], "重複", "duplicate message");
                    T.Contains(res.Failures[1], "入れ子が深すぎます", "nesting message");
                    T.Bytes(dup, File.ReadAllBytes(pd), "duplicate file unchanged");
                    T.Bytes(deep, File.ReadAllBytes(pn), "deep file unchanged");
                    T.Eq(12L, Lang(pg), "good account switched");
                    var st = NewPatcher(ctx, new List<string>()).GetStatus();
                    T.Contains(st.Languages[0].Item2, "読み取れません", "status of the duplicate account");
                    T.Contains(st.Languages[1].Item2, "読み取れません", "status of the deep account");
                    T.Eq("日本語", st.Languages[2].Item2, "status of the good account");
                }
            });

            r.Test("Language: the previous language cannot be recorded -> the old record is deleted (restore goes to English) (F-SBP-4)", () =>
            {
                using (var env = new Env(ctx, "prev_fail"))
                {
                    string p = env.Account("77", Mp.Prefs(Mp.Int(1))); // French
                    string prev = env.Previous("77");
                    Directory.CreateDirectory(Path.GetDirectoryName(prev));
                    File.WriteAllText(prev, "9"); // Korean, recorded by an older run
                    File.SetAttributes(prev, FileAttributes.ReadOnly); // writing fails, deleting works (SafeFile clears the attribute)
                    var log = new List<string>();
                    var res = NewPatcher(ctx, log).SetLanguage(Preferences.Japanese);
                    T.Eq(1, res.Changed, "switched");
                    T.Eq(0, res.Failures.Count, "the record is not a failure");
                    T.True(log.Any(l => l.Contains("元の表示言語を記録できませんでした（「元に戻す」では英語に戻します）")), "warning: " + string.Join(" / ", log));
                    T.True(!File.Exists(prev), "the stale record was deleted");
                    NewPatcher(ctx, log).RestoreLanguage();
                    T.Eq(0L, Lang(p), "restore goes to English, not to the stale Korean");

                    // the record can be neither written nor deleted (held open): the message says so
                    File.WriteAllBytes(p, Mp.Prefs(Mp.Int(1)));
                    File.WriteAllText(prev, "9");
                    log.Clear();
                    using (new FileStream(prev, FileMode.Open, FileAccess.Read, FileShare.None))
                        res = NewPatcher(ctx, log).SetLanguage(Preferences.Japanese);
                    T.Eq(1, res.Changed, "switched (record locked)");
                    T.True(log.Any(l => l.Contains("以前に記録した言語に戻る場合があります")), "warning when the record cannot be deleted: " + string.Join(" / ", log));
                }
            });

            r.Test("Language: the preferences backup cannot be written -> that account fails with the backup folder named, file unchanged (F-SBP-5)", () =>
            {
                using (var env = new Env(ctx, "backup_fail"))
                {
                    var src = Mp.Prefs(Mp.Int(0));
                    string pa = env.Account("88", src), pb = env.Account("99", src);
                    string blocked = Path.Combine(env.AppData, "backup", "preferences", "88");
                    Directory.CreateDirectory(Path.GetDirectoryName(blocked));
                    File.WriteAllText(blocked, "a file where the backup folder should be");
                    var res = NewPatcher(ctx, new List<string>()).SetLanguage(Preferences.Japanese);
                    T.Eq(1, res.Failures.Count, "failures: " + string.Join(" / ", res.Failures));
                    T.Contains(res.Failures[0], "設定ファイルのバックアップを作成できません（" + blocked + "）", "message");
                    T.Contains(res.Failures[0], "（88）", "account named");
                    T.Bytes(src, File.ReadAllBytes(pa), "account 88 unchanged");
                    T.Eq(12L, Lang(pb), "account 99 switched");
                }
            });

            r.Test("Language: RestoreLanguage / CLI 'language previous' with no Japanese account say that nothing was changed (I-1)", () =>
            {
                using (var env = new Env(ctx, "nothing_ja"))
                {
                    var en = Mp.Prefs(Mp.Int(0));
                    string p = env.Account("1", en);
                    env.Account("2", Mp.Prefs(Mp.Int(1)));
                    var log = new List<string>();
                    var res = NewPatcher(ctx, log).RestoreLanguage();
                    T.Eq(0, res.Changed, "changed");
                    T.Eq(2, res.Unchanged, "unchanged");
                    T.True(log.Any(l => l.Contains("表示言語が日本語のアカウントはないため、表示言語は変更しませんでした")), "log: " + string.Join(" / ", log));
                    T.Eq(0, Cli(out var o, "language", "previous"), "language previous exit");
                    T.Contains(o, "変更しませんでした", "CLI output");
                    T.Bytes(en, File.ReadAllBytes(p), "unchanged");
                    T.Eq(0, env.Backups("1").Length, "no backup made");
                    var g = ctx.NewGame("nothing_ja", ctx.Official);
                    T.Eq(0, Cli(out _, "apply", "--game", g.Dir, "--no-language"), "apply");
                    T.Eq(0, Cli(out var o2, "restore", "--game", g.Dir), "restore (with language) exit: " + o2);
                    T.Contains(o2, "日本語データを公式のものに戻しました", "restore output");
                    T.Contains(o2, "変更しませんでした", "restore output names the unchanged language");
                }
                using (new Env(ctx, "nothing_ja_noprefs"))
                {
                    T.Eq(0, Cli(out var o3, "language", "previous"), "no preferences at all: exit");
                    T.Contains(o3, "変更しませんでした", "no preferences at all: output");
                }
            });

            r.Test("CLI (in-process, Debug): status / apply / restore / usage errors on a sandbox", () =>
            {
                using (var env = new Env(ctx, "cli"))
                {
                    var g = ctx.NewGame("cli", ctx.Official);
                    T.Eq(0, Cli(out var o1, "status", "--game", g.Dir), "status exit");
                    T.Contains(o1, "日本語データ: 公式のまま", "status output");
                    T.Contains(o1, "設定ファイルがありません", "no preferences");
                    T.Eq(0, Cli(out var o2, "apply", "--game", g.Dir, "--data", ctx.DataPath, "--no-language"), "apply exit: " + o2);
                    T.Contains(o2, "日本語データにパッチを適用しました", "apply output");
                    T.Eq(0, Cli(out var o3, "APPLY", "--game", g.Dir + "\"", "--no-language"), "apply (upper case, trailing quote, embedded data): " + o3);
                    T.Contains(o3, "（実行ファイルに内蔵）", "embedded data used");
                    T.Eq(0, Cli(out var o4, "status", "--game", g.Dir), "status exit");
                    T.Contains(o4, "日本語化パッチ適用済み", "status patched");
                    T.Eq(0, Cli(out var o5, "restore", "--game", g.Dir, "--keep-language"), "restore exit: " + o5);
                    T.Eq(Util.Sha256(ctx.Official), Util.Sha256(File.ReadAllBytes(g.Bundle)), "restored");
                    T.Eq(1, Cli(out var o6, "apply", "--game", Path.Combine(g.Dir, "nope"), "--no-language"), "missing game dir");
                    T.Contains(o6, "見つかりません", "missing game message");
                    foreach (var bad in new[]
                             {
                                 new[] { "bogus", "--game", g.Dir }, new[] { "apply", "--game" }, new[] { "apply", "--game", g.Dir, "--keep-language" },
                                 new[] { "status", "--game", g.Dir, "extra" }, new[] { "apply", "--game", "--no-language" },
                                 new[] { "apply", "--game", " ", "--no-language" }, new[] { "language" }, new[] { "language", "fr" },
                                 new[] { "language", "ja", "--game", g.Dir },
                             })
                    {
                        int c = Cli(out var o, bad);
                        T.Eq(2, c, "usage error for: " + string.Join(" ", bad) + " -> " + o);
                        T.Contains(o, "使い方", "usage text");
                    }
                    string logFile = Path.Combine(env.AppData, "TpcJpPatch.log");
                    T.True(File.Exists(logFile), "the CLI log went to the TPCJP_APPDATA folder");
                }
            });

            r.Test("CLI (in-process, Debug): apply with language switch on a sandbox account; language ja/en/previous", () =>
            {
                using (var env = new Env(ctx, "cli_lang"))
                {
                    string p = env.Account("555", french);
                    var g = ctx.NewGame("cli_lang", ctx.Official);
                    T.Eq(0, Cli(out var o1, "apply", "--game", g.Dir), "apply + language: " + o1);
                    T.Eq(12L, Lang(p), "Japanese");
                    T.Eq(0, Cli(out var o2, "restore", "--game", g.Dir), "restore + language: " + o2);
                    T.Eq(1L, Lang(p), "back to French");
                    T.Eq(0, Cli(out _, "language", "JA"), "language ja");
                    T.Eq(12L, Lang(p), "ja");
                    T.Eq(0, Cli(out _, "language", "en"), "language en");
                    T.Eq(0L, Lang(p), "en");
                    T.Eq(0, Cli(out _, "language", "ja"), "language ja");
                    T.Eq(0, Cli(out _, "language", "previous"), "language previous");
                    T.Eq(0L, Lang(p), "previous = English (the language before the last switch to Japanese)");
                    File.Delete(p);
                    T.Eq(1, Cli(out var o3, "apply", "--game", g.Dir), "apply without preferences -> exit 1");
                    T.Contains(o3, "表示言語を日本語に切り替えられませんでした", "message");
                    T.Eq(Patcher.BundleState.Patched, g.Patcher().GetStatus().State, "the bundle is still patched");
                }
            });

            r.Test("CLI (in-process, Debug): restore with an unreadable (invalid UTF-8) --data falls back to the state file", () =>
            {
                using (new Env(ctx, "cli_badutf8"))
                {
                    var g = ctx.NewGame("cli_badutf8", ctx.Official);
                    T.Eq(0, Cli(out _, "apply", "--game", g.Dir, "--no-language"), "apply");
                    string bad = Path.Combine(ctx.Dir("cli_badutf8_data"), "bad.tsv");
                    var bytes = new List<byte>(Encoding.UTF8.GetBytes("#TPCJP-PATCH\t1\nI2LS_UI\tk\t0123456789abcdef\t"));
                    bytes.AddRange(new byte[] { 0xE6, 0x97, 0xFF, 0x0A });
                    File.WriteAllBytes(bad, bytes.ToArray());
                    int c = Cli(out var o, "restore", "--game", g.Dir, "--data", bad, "--keep-language");
                    T.True(!o.Contains("予期しないエラー"), "unexpected error: " + o.Trim().Split('\n').Skip(1).FirstOrDefault());
                    T.Eq(0, c, "exit code");
                    T.Contains(o, "注意: 翻訳データを読み込めないため、状態ファイルだけで判断します", "fallback notice");
                    T.Contains(o, "UTF-8", "the reason is given");
                    T.Eq(Util.Sha256(ctx.Official), Util.Sha256(File.ReadAllBytes(g.Bundle)), "restored");
                    T.True(!File.Exists(g.Backup) && !File.Exists(g.State), "backup and state removed");
                }
            });

            r.Test("CLI (in-process, Debug): apply with an invalid UTF-8 --data reports a data error, not an unexpected error (CS-01)", () =>
            {
                using (new Env(ctx, "cli_badutf8_apply"))
                {
                    var g = ctx.NewGame("cli_badutf8_apply", ctx.Official);
                    string bad = Path.Combine(ctx.Dir("cli_badutf8_apply_data"), "bad.tsv");
                    File.WriteAllBytes(bad, new byte[] { 0x23, 0x54, 0xFF, 0xFE, 0x0A });
                    var before = g.Snapshot();
                    int c = Cli(out var o, "apply", "--game", g.Dir, "--data", bad, "--no-language");
                    T.Eq(1, c, "exit code");
                    g.AssertUnchanged(before, "files");
                    T.True(!o.Contains("予期しないエラー"), "printed as an unexpected error with a stack trace: " + o.Trim().Split('\n').Skip(1).FirstOrDefault());
                    T.Contains(o, "エラー: 翻訳データを UTF-8 として読めません", "message");
                }
            });

            r.Test("CLI (in-process, Debug): apply with a missing --data file names the data file in the error", () =>
            {
                using (new Env(ctx, "cli_missing_data"))
                {
                    var g = ctx.NewGame("cli_missing_data", ctx.Official);
                    string missing = Path.Combine(ctx.RunDir, "no_such_data.tsv");
                    int c = Cli(out var o, "apply", "--game", g.Dir, "--data", missing, "--no-language");
                    T.Eq(1, c, "exit code");
                    r.Note(o.Trim().Split('\n').Last().Trim());
                    T.Contains(o, "no_such_data.tsv", "the file is named");
                }
            });

            r.Test("CLI (in-process, Debug): a missing / folder / locked --data is reported as '翻訳データを読み込めません', not as a game file (CS-05)", () =>
            {
                using (new Env(ctx, "cli_missing_data2"))
                {
                    var g = ctx.NewGame("cli_missing_data2", ctx.Official);
                    string dataDir = ctx.Dir("cli_missing_data2_files");
                    string locked = Path.Combine(dataDir, "locked.tsv");
                    File.Copy(ctx.DataPath, locked);
                    var before = g.Snapshot();
                    using (new FileStream(locked, FileMode.Open, FileAccess.Read, FileShare.None))
                    {
                        foreach (var kv in new Dictionary<string, string>
                                 {
                                     ["missing file"] = Path.Combine(dataDir, "no_such_data.tsv"),
                                     ["missing folder"] = Path.Combine(dataDir, "no", "such", "data.tsv"),
                                     ["a folder"] = dataDir,
                                     ["a locked file"] = locked,
                                 })
                        {
                            int c = Cli(out var o, "apply", "--game", g.Dir, "--data", kv.Value, "--no-language");
                            string line = o.Trim().Split('\n').Last().Trim();
                            T.Eq(1, c, kv.Key + ": exit code");
                            T.True(!line.Contains("ゲームや Steam がファイルを使っていないか"), kv.Key + ": misleading advice: " + line);
                            T.Contains(line, "エラー: 翻訳データを読み込めません: " + kv.Value, kv.Key + ": message");
                            g.AssertUnchanged(before, kv.Key + ": files");
                        }
                    }
                }
            });

            r.Test("CLI (in-process, Debug): restore with a missing / folder / truncated --data falls back to the state file (CS-05, F-SBP-1)", () =>
            {
                using (new Env(ctx, "cli_restore_fallback"))
                {
                    string dataDir = ctx.Dir("cli_restore_fallback_files");
                    string half = Path.Combine(dataDir, "half.tsv");
                    string text = File.ReadAllText(ctx.DataPath, Encoding.UTF8);
                    File.WriteAllText(half, text.Substring(0, text.IndexOf('\n', text.Length / 2) + 1), new UTF8Encoding(false));
                    foreach (var kv in new Dictionary<string, string>
                             {
                                 ["missing file"] = Path.Combine(dataDir, "no_such_data.tsv"),
                                 ["a folder"] = dataDir,
                                 ["truncated file"] = half,
                             })
                    {
                        var g = ctx.NewGame("cli_restore_fallback", ctx.Official);
                        T.Eq(0, Cli(out _, "apply", "--game", g.Dir, "--no-language"), kv.Key + ": apply");
                        int c = Cli(out var o, "restore", "--game", g.Dir, "--data", kv.Value, "--keep-language");
                        T.Eq(0, c, kv.Key + ": exit code: " + o);
                        T.Contains(o, "注意: 翻訳データを読み込めないため、状態ファイルだけで判断します", kv.Key + ": notice");
                        T.Eq(Util.Sha256(ctx.Official), Util.Sha256(File.ReadAllBytes(g.Bundle)), kv.Key + ": restored");
                        T.True(!File.Exists(g.Backup) && !File.Exists(g.State), kv.Key + ": backup and state removed");
                    }
                }
            });

            r.Test("CLI (in-process, Debug): apply with a truncated --data refuses before touching the game (CS-02)", () =>
            {
                using (new Env(ctx, "cli_trunc"))
                {
                    var g = ctx.NewGame("cli_trunc", ctx.Official);
                    string half = Path.Combine(ctx.Dir("cli_trunc_data"), "half.tsv");
                    string text = File.ReadAllText(ctx.DataPath, Encoding.UTF8);
                    File.WriteAllText(half, text.Substring(0, text.IndexOf('\n', text.Length / 2) + 1), new UTF8Encoding(false));
                    var before = g.Snapshot();
                    int c = Cli(out var o, "apply", "--game", g.Dir, "--data", half, "--no-language");
                    T.Eq(1, c, "exit code");
                    T.Contains(o, "エラー: 翻訳データが途中で切れているか、壊れています", "message");
                    T.Contains(o, "見出しの件数 2391", "the declared count is named");
                    g.AssertUnchanged(before, "files (no backup either)");
                }
            });

            r.Test("CLI (in-process, Debug): status prints '公式のデータに戻っています' after a Steam-verify-like restore of the bundle (I-2, text shortened by CS-08)", () =>
            {
                using (new Env(ctx, "cli_reverted"))
                {
                    var g = ctx.NewGame("cli_reverted", ctx.Official);
                    T.Eq(0, Cli(out _, "apply", "--game", g.Dir, "--no-language"), "apply");
                    File.WriteAllBytes(g.Bundle, ctx.Official); // what "verify integrity of game files" does
                    T.Eq(0, Cli(out var o, "status", "--game", g.Dir), "status exit");
                    T.Contains(o, "日本語データ: 公式のデータに戻っています（「日本語化する」で適用し直せます）\r\n", "status line");
                    T.Eq(0, Cli(out var o2, "apply", "--game", g.Dir, "--no-language"), "apply again");
                    T.Contains(o2, "公式の日本語データにパッチを適用します", "apply again output");
                    T.Eq(0, Cli(out var o3, "status", "--game", g.Dir), "status exit");
                    T.Contains(o3, "日本語データ: 日本語化パッチ適用済み", "patched again");
                }
            });

            r.Test("CLI (in-process, Debug): a state file that cannot be written gives a warning and exit 0 (FZ-10)", () =>
            {
                using (new Env(ctx, "cli_state_fail"))
                {
                    var g = ctx.NewGame("cli_state_fail", ctx.Official);
                    Directory.CreateDirectory(g.State + SafeFile.TempSuffix);
                    int c = Cli(out var o, "apply", "--game", g.Dir, "--no-language");
                    T.Eq(0, c, "exit code: " + o);
                    T.Contains(o, "注意: 状態ファイルを書き込めませんでした", "warning");
                    T.Contains(o, "日本語データにパッチを適用しました", "completion");
                    T.Eq(Util.ShippedPatchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "patched");
                    // CS-07 / F-SBP-6 / pytests-E2E1: status classifies the bundle by content (embedded data)
                    T.Eq(0, Cli(out var so, "status", "--game", g.Dir), "status exit");
                    T.Contains(so, "日本語データ: 日本語化パッチ適用済み（状態の記録なし）\r\n", "status right after the warning");
                    T.Eq(0, Cli(out var o1, "apply", "--game", g.Dir, "--no-language"), "apply again: " + o1);
                    T.Contains(o1, "以前に適用したパッチを検出しました", "apply again recognises the output");
                    T.Eq(0, Cli(out so, "status", "--game", g.Dir), "status exit");
                    T.Contains(so, "日本語データ: 日本語化パッチ適用済み（状態の記録なし）", "status after the second apply");
                    Directory.Delete(g.State + SafeFile.TempSuffix);
                    T.Eq(0, Cli(out var o2, "restore", "--game", g.Dir, "--keep-language"), "restore: " + o2);
                    T.Eq(Util.Sha256(ctx.Official), Util.Sha256(File.ReadAllBytes(g.Bundle)), "restored");
                }
            });
            // ---- round 2 ----

            r.Test("CLI (in-process, Debug): status by content: state deleted -> '状態の記録なし', damaged -> '壊れているか読み込めません', update -> 'ゲーム側で更新されています'; status changes nothing", () =>
            {
                using (new Env(ctx, "cli_status_content"))
                {
                    var cases = new Dictionary<string, Tuple<Action<Game>, string>>
                    {
                        ["state deleted"] = Tuple.Create<Action<Game>, string>(g => File.Delete(g.State), "日本語化パッチ適用済み（状態の記録なし）"),
                        ["damaged bundle"] = Tuple.Create<Action<Game>, string>(g => File.WriteAllBytes(g.Bundle, ctx.Official.Take(ctx.Official.Length / 2).ToArray()),
                            "壊れているか読み込めません（「元に戻す」か Steam の整合性の確認で直せます）"),
                        ["game update"] = Tuple.Create<Action<Game>, string>(g => g.Update(Util.ModifyBundle(ctx.Official, new Dictionary<string, Dictionary<string, string>>
                        {
                            ["I2LS_UI"] = Util.ParseI2(Util.Texts(ctx.Official)["I2LS_UI"]).Where(p => ctx.Data.Sources["I2LS_UI"].Any(e => e.Key == p.Key && e.Japanese != null))
                                .Take(3).ToDictionary(p => p.Key, p => "公式の新しい訳"),
                        })), "ゲーム側で更新されています（「日本語化する」で適用し直してください）"),
                        ["Steam verify"] = Tuple.Create<Action<Game>, string>(g => File.WriteAllBytes(g.Bundle, ctx.Official), "公式のデータに戻っています（「日本語化する」で適用し直せます）"),
                        ["bundle deleted"] = Tuple.Create<Action<Game>, string>(g => File.Delete(g.Bundle), "見つかりません（ゲームのフォルダーを確認してください）"),
                    };
                    int i = 0;
                    foreach (var c in cases)
                    {
                        var g = ctx.NewGame("cli_status_content_" + i++, ctx.Official);
                        T.Eq(0, Cli(out var ao, "apply", "--game", g.Dir, "--no-language"), c.Key + ": apply: " + ao);
                        c.Value.Item1(g);
                        var before = g.Snapshot();
                        T.Eq(0, Cli(out var o, "status", "--game", g.Dir), c.Key + ": status exit: " + o);
                        T.Contains(o, "日本語データ: " + c.Value.Item2 + "\r\n", c.Key + ": status line");
                        g.AssertUnchanged(before, c.Key + ": status");
                    }
                }
            });

            r.Test("CLI (in-process, Debug): apply / restore with the bundle missing but a backup rebuild / restore it; leftovers are removed and reported (F-SBP-7)", () =>
            {
                using (new Env(ctx, "cli_missing"))
                {
                    var g = ctx.NewGame("cli_missing", ctx.Official);
                    T.Eq(0, Cli(out _, "apply", "--game", g.Dir, "--no-language"), "apply");
                    File.Move(g.Bundle, g.Bundle + "~RF3a1b2c.TMP"); // what 1.0.0 left when it was killed inside File.Replace
                    File.WriteAllBytes(g.Bundle + SafeFile.TempSuffix, new byte[100]);
                    T.Eq(0, Cli(out var so, "status", "--game", g.Dir), "status");
                    T.Contains(so, "日本語データ: 見つかりません", "status: missing");
                    T.Eq(0, Cli(out var o, "apply", "--game", g.Dir, "--no-language"), "apply with the bundle missing: " + o);
                    T.Contains(o, "前回中断したときの一時ファイルを 2 個削除しました", "clean-up line");
                    T.Contains(o, "日本語データが見つからないため、バックアップした公式データから作り直します", "rebuild line");
                    T.Contains(o, "日本語データにパッチを適用しました（翻訳 2391 件）", "completion");
                    T.Eq(Util.ShippedPatchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "patched");
                    T.Eq(0, g.Leftovers().Length, "no leftovers");

                    File.Delete(g.Bundle);
                    File.WriteAllBytes(g.Backup + "~RF77.TMP", new byte[10]);
                    T.Eq(0, Cli(out var ro, "restore", "--game", g.Dir, "--keep-language"), "restore with the bundle missing: " + ro);
                    T.Contains(ro, "前回中断したときの一時ファイルを 1 個削除しました", "restore clean-up line");
                    T.Contains(ro, "日本語データを公式のものに戻しました", "restore line");
                    T.Eq(Util.Sha256(ctx.Official), Util.Sha256(File.ReadAllBytes(g.Bundle)), "official");
                    T.Eq(1, Directory.GetFiles(GameLocator.BundlesDir(g.Dir)).Length, "only the bundle is left");

                    var g2 = ctx.NewGame("cli_missing_nobackup", null);
                    var before = g2.Snapshot();
                    T.Eq(1, Cli(out var eo, "apply", "--game", g2.Dir, "--no-language"), "no bundle, no backup");
                    T.Contains(eo, "エラー: 日本語データ（localisation_assets_localisationjapanese.bundle）が見つかりません。", "message");
                    T.Contains(eo, "ゲームファイルの整合性を確認", "advice");
                    g2.AssertUnchanged(before, "no bundle, no backup");
                }
            });

            r.Test("CLI (in-process, Debug): --data without its final newline: apply refuses before touching the game, restore falls back to the state file (CS-06)", () =>
            {
                using (new Env(ctx, "cli_noeol"))
                {
                    var g = ctx.NewGame("cli_noeol", ctx.Official);
                    string p = Path.Combine(ctx.Dir("cli_noeol_data"), "cut.tsv");
                    var bytes = File.ReadAllBytes(ctx.DataPath);
                    File.WriteAllBytes(p, bytes.Take(bytes.Length - 6).ToArray()); // '区画15\n' -> '区'
                    var before = g.Snapshot();
                    T.Eq(1, Cli(out var o, "apply", "--game", g.Dir, "--data", p, "--no-language"), "apply exit: " + o);
                    T.Contains(o, "エラー: 翻訳データが途中で切れています（最後の行に改行がありません）", "message");
                    T.True(!o.Contains("日本語データにパッチを適用しました"), "nothing applied");
                    g.AssertUnchanged(before, "files");
                    T.Eq(0, Cli(out _, "apply", "--game", g.Dir, "--no-language"), "apply with the embedded data");
                    T.Eq(0, Cli(out var ro, "restore", "--game", g.Dir, "--data", p, "--keep-language"), "restore exit: " + ro);
                    T.Contains(ro, "注意: 翻訳データを読み込めないため、状態ファイルだけで判断します（翻訳データが途中で切れています（最後の行に改行がありません））", "fallback notice");
                    T.Eq(Util.Sha256(ctx.Official), Util.Sha256(File.ReadAllBytes(g.Bundle)), "restored");
                }
            });

            r.Test("CLI (process, Debug): status still works (exit 0) when the TpcJpPatch.data.tsv next to the exe is broken; apply then refuses", () =>
            {
                // a copy of the Debug exe in the temp folder with a cut data file next to it (the data file next to the
                // exe wins over the embedded copy)
                string dir = ctx.Dir("cli_brokendata_exe");
                string exeDir = Path.GetDirectoryName(typeof(Patcher).Assembly.Location);
                foreach (var f in new[] { "TpcJpPatch.exe", "TpcJpPatch.exe.config", "AssetsTools.NET.dll" })
                    if (File.Exists(Path.Combine(exeDir, f)))
                        File.Copy(Path.Combine(exeDir, f), Path.Combine(dir, f));
                var bytes = File.ReadAllBytes(ctx.DataPath);
                File.WriteAllBytes(Path.Combine(dir, PatchData.FileName), bytes.Take(bytes.Length - 1).ToArray());
                string root = ctx.Dir("cli_brokendata_env");
                var g = ctx.NewGame("cli_brokendata", ctx.Official);
                string Run(params string[] args)
                {
                    var psi = new System.Diagnostics.ProcessStartInfo(Path.Combine(dir, "TpcJpPatch.exe"), string.Join(" ", args.Select(a => "\"" + a + "\"")))
                    {
                        UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true,
                        StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
                    };
                    psi.EnvironmentVariables["TPCJP_PREFS_ROOT"] = Path.Combine(root, "Cloud");
                    psi.EnvironmentVariables["TPCJP_APPDATA"] = Path.Combine(root, "appdata");
                    using (var pr = System.Diagnostics.Process.Start(psi))
                    {
                        var err = pr.StandardError.ReadToEndAsync();
                        string o = pr.StandardOutput.ReadToEnd();
                        pr.WaitForExit();
                        return "exit " + pr.ExitCode + "\n" + o + err.Result;
                    }
                }
                string so = Run("status", "--game", g.Dir);
                T.Contains(so, "exit 0", "status exit: " + so);
                T.Contains(so, "日本語データ: 公式のまま（パッチ未適用）", "status line");
                string ao = Util.Retry(() =>
                {
                    string x = Run("apply", "--game", g.Dir, "--no-language");
                    if (x.Contains(Util.BusyMessage))
                        throw new PatchException(Util.BusyMessage);
                    return x;
                });
                T.Contains(ao, "exit 1", "apply exit: " + ao);
                T.Contains(ao, "エラー: 翻訳データが途中で切れています（最後の行に改行がありません）", "apply message");
                T.Eq(Util.Sha256(ctx.Official), Util.Sha256(File.ReadAllBytes(g.Bundle)), "bundle unchanged");
            });
        }

        static string WriteTemp(Ctx ctx, byte[] bytes)
        {
            string p = Path.Combine(ctx.RunDir, "lang_probe.json");
            File.WriteAllBytes(p, bytes);
            return p;
        }

        static int IndexOfLangMap(byte[] bytes)
        {
            byte[] key = Mp.Str("_selectedLanguage");
            for (int i = 1; i + key.Length <= bytes.Length; i++)
                if (bytes.Skip(i).Take(key.Length).SequenceEqual(key) && (bytes[i - 1] & 0xF0) == 0x80)
                    return i - 1;
            throw new InvalidOperationException("language map not found");
        }
    }
}
