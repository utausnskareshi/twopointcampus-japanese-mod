using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;

namespace TpcJpPatch.Tests
{
    /// <summary>
    /// Compares the behaviour of two builds of TpcJpPatch.exe (the pre-fix baseline Release exe and the new one):
    ///   --compare-exe BASELINE.exe --compare-exe NEW.exe
    /// 1. As processes, only with commands that cannot write anything: "status --game SANDBOX" and usage errors
    ///    that always carry "--game SANDBOX" (a Release build has no test hooks, so apply/restore would append
    ///    to %LOCALAPPDATA%\TpcJpPatch\TpcJpPatch.log; they are therefore not started as processes).
    /// 2. In-process: both assemblies are loaded side by side (Assembly.LoadFile) and PatchData.Load /
    ///    Patcher.Apply / Restore / GetStatus are driven by reflection on twin sandboxes (Patcher only logs through
    ///    the delegate it is given).
    /// Behaviour that the fixes were not meant to change must be identical (results, logs, files, the patched
    /// bundle hash of the shipped data). Each intended difference (work/localtest/results/fixes.md) has its own
    /// scenario that asserts the baseline's old and the new build's fixed behaviour and is listed in the notes.
    /// </summary>
    static class ExeCompareTests
    {
        static string RunExe(string exe, params string[] args)
        {
            if (!args.Contains("--game"))
                throw new InvalidOperationException("refusing to start the exe without --game");
            var psi = new ProcessStartInfo(exe, string.Join(" ", args.Select(a => a.Contains(' ') || a.Length == 0 ? "\"" + a + "\"" : a)))
            {
                UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true,
                StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
            };
            psi.EnvironmentVariables.Remove("TPCJP_PREFS_ROOT");
            psi.EnvironmentVariables.Remove("TPCJP_APPDATA");
            using (var p = Process.Start(psi))
            {
                var err = p.StandardError.ReadToEndAsync();
                string o = p.StandardOutput.ReadToEnd();
                p.WaitForExit();
                return $"exit {p.ExitCode}\n{o}{err.Result}";
            }
        }

        sealed class Build
        {
            public string Label;
            public Assembly Asm;
            public Type PatcherType, DataType;

            /// <summary>PatchData.Load(path): "ok N" or "error Type: message".</summary>
            public string TryData(string path, out object data)
            {
                data = null;
                try
                {
                    data = DataType.GetMethod("Load", BindingFlags.Public | BindingFlags.Static).Invoke(null, new object[] { path });
                    return "ok " + DataType.GetProperty("Count").GetValue(data);
                }
                catch (TargetInvocationException e)
                {
                    return "error " + e.InnerException.GetType().Name + ": " + e.InnerException.Message;
                }
            }

            public object Data(string path)
            {
                string r = TryData(path, out var d);
                if (d == null)
                    throw new AssertException(Label + ": loading " + path + ": " + r);
                return d;
            }

            object NewPatcher(string gameDir, List<string> log) =>
                Activator.CreateInstance(PatcherType, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic, null,
                    new object[] { gameDir, new Action<string>(log.Add) }, null);

            public string Call(string gameDir, string method, object data, List<string> log)
            {
                var p = NewPatcher(gameDir, log);
                try
                {
                    object res = Util.Retry(() =>
                    {
                        try
                        {
                            return PatcherType.GetMethod(method).Invoke(p, new[] { data });
                        }
                        catch (TargetInvocationException e) when (e.InnerException?.Message.Contains("起動しています") == true)
                        {
                            throw new PatchException(e.InnerException.Message);
                        }
                    });
                    if (res == null)
                        return "ok";
                    return "ok " + string.Join(",", res.GetType().GetFields().Select(f => f.Name + "=" + f.GetValue(res)));
                }
                catch (TargetInvocationException e)
                {
                    return "error " + e.InnerException.GetType().Name + ": " + e.InnerException.Message;
                }
            }

            /// <summary>GetStatus() of the baseline, GetStatus(data) of the new build (data may be null).</summary>
            public string State(string gameDir, object data = null)
            {
                var m = PatcherType.GetMethod("GetStatus");
                var args = m.GetParameters().Length == 0 ? null : new[] { data };
                var st = m.Invoke(NewPatcher(gameDir, new List<string>()), args);
                return st.GetType().GetField("State").GetValue(st).ToString();
            }
        }

        static Build LoadBuild(string exe, string label)
        {
            var asm = Assembly.LoadFile(exe);
            return new Build { Label = label, Asm = asm, PatcherType = asm.GetType("TpcJpPatch.Patcher", true), DataType = asm.GetType("TpcJpPatch.PatchData", true) };
        }

        /// <summary>Files next to the bundle with their hashes; the state file without its time line.</summary>
        static string Files(Game g)
        {
            var sb = new StringBuilder();
            foreach (var f in Directory.GetFiles(GameLocator.BundlesDir(g.Dir)).OrderBy(x => x, StringComparer.Ordinal))
            {
                string name = Path.GetFileName(f);
                if (name.EndsWith(Patcher.StateSuffix, StringComparison.Ordinal))
                    sb.Append(name + ": " + string.Join("|", File.ReadAllLines(f).Where(l => !l.StartsWith("time=", StringComparison.Ordinal)).OrderBy(l => l))).Append('\n');
                else
                    sb.Append(name + ": " + Util.Sha256(File.ReadAllBytes(f))).Append('\n');
            }
            return sb.ToString();
        }

        const string BackupLine = "バックアップを作成しました";
        const string WritingLine = "日本語データを書き出しています";
        const string DamagedNoBackupText = "壊れているか読み込めません（Steam の整合性の確認で直せます）";
        const string PatchedNoBackupText = "日本語化パッチ適用済み（バックアップなし。Steam の整合性の確認で戻せます）";

        /// <summary>
        /// Logs must be equal, except that the new build writes a new backup after the output was built and verified
        /// (FZ-09), so its "バックアップを作成しました" line moved behind "日本語データを書き出しています…".
        /// Returns a note when that intended reordering was seen.
        /// </summary>
        static string CompareLogs(List<string> a, List<string> b, string what)
        {
            if (a.SequenceEqual(b))
                return null;
            T.Eq(string.Join("\n", a.OrderBy(x => x, StringComparer.Ordinal)), string.Join("\n", b.OrderBy(x => x, StringComparer.Ordinal)), what + ": log lines");
            T.Eq(string.Join("\n", a.Where(l => !l.StartsWith(BackupLine, StringComparison.Ordinal))),
                string.Join("\n", b.Where(l => !l.StartsWith(BackupLine, StringComparison.Ordinal))), what + ": log order apart from the backup line");
            int w = b.FindIndex(l => l.StartsWith(WritingLine, StringComparison.Ordinal)), k = b.FindIndex(l => l.StartsWith(BackupLine, StringComparison.Ordinal));
            T.True(w >= 0 && k > w, what + ": new build logs the backup after building the output");
            return "intended (FZ-09): backup line after the output was built";
        }

        public static void Run(Runner r, Ctx ctx)
        {
            if (ctx.CompareExes.Count != 2)
            {
                r.Test("ExeCompare: baseline vs new exe", () => throw new SkipException("pass --compare-exe BASELINE --compare-exe NEW"));
                return;
            }
            string exeA = ctx.CompareExes[0], exeB = ctx.CompareExes[1];
            T.True(File.Exists(exeA) && File.Exists(exeB), "both exes exist");
            byte[] official = ctx.Official;
            string officialSha = Util.Sha256(official);
            byte[] damaged = official.Take(official.Length / 2).ToArray();
            byte[] updated = Util.ModifyBundle(official, new Dictionary<string, Dictionary<string, string>>
            {
                ["I2LS_UI"] = Util.ParseI2(Util.Texts(official)["I2LS_UI"]).Where(p => ctx.Data.Sources["I2LS_UI"].Any(e => e.Key == p.Key && e.Japanese != null))
                    .Take(4).ToDictionary(p => p.Key, p => "公式の新しい訳"),
            });
            string updatedSha = Util.Sha256(updated);
            var intended = new List<string>();
            Build a = null, b = null;

            r.Test("ExeCompare: embedded resources (translation data apart from its #built line, AssetsTools.NET) are identical", () =>
            {
                a = LoadBuild(exeA, "baseline");
                b = LoadBuild(exeB, "new");
                T.Eq(string.Join(",", a.Asm.GetManifestResourceNames().OrderBy(x => x)), string.Join(",", b.Asm.GetManifestResourceNames().OrderBy(x => x)), "resource names");
                foreach (var name in a.Asm.GetManifestResourceNames())
                {
                    using (var sa = a.Asm.GetManifestResourceStream(name))
                    using (var sb = b.Asm.GetManifestResourceStream(name))
                    {
                        var ma = new MemoryStream();
                        var mb = new MemoryStream();
                        sa.CopyTo(ma);
                        sb.CopyTo(mb);
                        if (name == PatchData.FileName)
                        {
                            // build_patch.py stamps "#built<TAB><time>" on every build (tools/package.py rebuilds the data):
                            // the translation data must be the same apart from that line
                            string Unstamped(byte[] d) => string.Join("\n", Encoding.UTF8.GetString(d).Split('\n').Where(l => !l.StartsWith("#built\t", StringComparison.Ordinal)));
                            T.Eq(Unstamped(ma.ToArray()), Unstamped(mb.ToArray()), "resource " + name + " (apart from #built)");
                        }
                        else
                            T.Bytes(ma.ToArray(), mb.ToArray(), "resource " + name);
                    }
                }
                T.Eq(a.Asm.GetName().Version, b.Asm.GetName().Version, "assembly version");
                foreach (var asm in new[] { a.Asm, b.Asm })
                    T.Eq("Release", asm.GetCustomAttribute<AssemblyConfigurationAttribute>()?.Configuration, "configuration of " + asm.Location);
                r.Note($"baseline {Util.Sha256(File.ReadAllBytes(exeA)).Substring(0, 12)}…, new {Util.Sha256(File.ReadAllBytes(exeB)).Substring(0, 12)}…");
            });
            if (a == null)
                return;

            r.Test("ExeCompare: status and usage errors as processes (always with --game SANDBOX) print the same", () =>
            {
                var scenarios = new Dictionary<string, byte[]> { ["official"] = official, ["damaged"] = damaged, ["missing"] = null, ["patched"] = official };
                foreach (var sc in scenarios)
                {
                    var ga = ctx.NewGame("cmp_status_a_" + sc.Key, sc.Value);
                    var gb = ctx.NewGame("cmp_status_b_" + sc.Key, sc.Value);
                    if (sc.Key == "patched")
                        foreach (var g in new[] { ga, gb })
                            Util.Retry(() => g.Patcher().Apply(ctx.Data));
                    foreach (var args in new[]
                             {
                                 new[] { "status", "--game", "{G}" }, new[] { "STATUS", "--game", "{G}\"" },
                                 new[] { "bogus", "--game", "{G}" }, new[] { "status", "--game", "{G}", "extra" },
                                 new[] { "apply", "--game", "{G}", "--no-language", "--bogus" },
                                 new[] { "apply", "--game", "{G}", "--no-language", "--data" },
                                 new[] { "restore", "--game", "{G}", "--keep-language", "--no-language" },
                             })
                    {
                        string oa = RunExe(exeA, args.Select(x => x.Replace("{G}", ga.Dir)).ToArray()).Replace(ga.Dir, "{G}");
                        string ob = RunExe(exeB, args.Select(x => x.Replace("{G}", gb.Dir)).ToArray()).Replace(gb.Dir, "{G}");
                        // CS-09 (round 3): without backup and state the new build reads the bundle; its state line for the damaged
                        // bundle is an intended difference, asserted by the CS-09 scenario below
                        if (sc.Key == "damaged")
                            ob = ob.Replace("日本語データ: " + DamagedNoBackupText, "日本語データ: 公式のまま（パッチ未適用）");
                        // the patched state carries the apply time (seconds) in its status line
                        if (sc.Key == "patched")
                        {
                            oa = System.Text.RegularExpressions.Regex.Replace(oa, @"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", "{TIME}");
                            ob = System.Text.RegularExpressions.Regex.Replace(ob, @"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", "{TIME}");
                        }
                        T.Eq(oa, ob, sc.Key + ": " + string.Join(" ", args));
                        T.Contains(oa, args[0].ToLowerInvariant() == "status" && args.Length == 3 ? "日本語データ: " : "使い方", "output of " + string.Join(" ", args));
                    }
                    T.Eq(Files(ga).Replace("cmp_status_a_", "X"), Files(gb).Replace("cmp_status_b_", "X"), sc.Key + ": files");
                    T.True(sc.Value == null || sc.Key == "patched" || Util.Sha256(File.ReadAllBytes(ga.Bundle)) == Util.Sha256(sc.Value), sc.Key + ": status changed nothing");
                }
            });

            r.Test("ExeCompare: status as processes after a Steam-verify-like restore differs only in the intended state line (I-2)", () =>
            {
                var ga = ctx.NewGame("cmp_status_a_reverted", official);
                var gb = ctx.NewGame("cmp_status_b_reverted", official);
                foreach (var g in new[] { ga, gb })
                {
                    Util.Retry(() => g.Patcher().Apply(ctx.Data));
                    File.WriteAllBytes(g.Bundle, official);
                }
                var la = RunExe(exeA, "status", "--game", ga.Dir).Replace(ga.Dir, "{G}").Split('\n');
                var lb = RunExe(exeB, "status", "--game", gb.Dir).Replace(gb.Dir, "{G}").Split('\n');
                T.Eq(la.Length, lb.Length, "line count");
                for (int i = 0; i < la.Length; i++)
                {
                    if (la[i].StartsWith("日本語データ: ", StringComparison.Ordinal))
                    {
                        T.Eq("日本語データ: 公式のまま（パッチ未適用）", la[i].TrimEnd('\r'), "baseline state line");
                        T.Eq("日本語データ: 公式のデータに戻っています（「日本語化する」で適用し直せます）", lb[i].TrimEnd('\r'), "new state line (shortened by CS-08)");
                    }
                    else
                        T.Eq(la[i], lb[i], "line " + i);
                }
                T.Eq("Original", a.State(ga.Dir), "baseline GetStatus");
                T.Eq("Reverted", b.State(gb.Dir), "new GetStatus");
                intended.Add("I-2 / CS-08 status after a Steam file verification: baseline '公式のまま（パッチ未適用）' (Original), new '公式のデータに戻っています（「日本語化する」で適用し直せます）' (Reverted)");
            });

            r.Test("ExeCompare: Apply/Restore scenarios through both assemblies give identical results and files (patched hash 20f1034b…)", () =>
            {
                var steps = new List<Tuple<string, Action<Game>, string>>
                {
                    Tuple.Create<string, Action<Game>, string>("apply fresh", null, "Apply"),
                    Tuple.Create<string, Action<Game>, string>("apply again", null, "Apply"),
                    Tuple.Create<string, Action<Game>, string>("steam verify", g => File.WriteAllBytes(g.Bundle, official), "Apply"),
                    Tuple.Create<string, Action<Game>, string>("game update (version.txt changed)", g => g.Update(updated), "Apply"),
                    Tuple.Create<string, Action<Game>, string>("damage bundle", g => File.WriteAllBytes(g.Bundle, damaged), "Apply"),
                    Tuple.Create<string, Action<Game>, string>("state lost", g => File.Delete(g.State), "Apply"),
                    Tuple.Create<string, Action<Game>, string>("restore", null, "Restore"),
                    Tuple.Create<string, Action<Game>, string>("restore again", null, "Restore"),
                    Tuple.Create<string, Action<Game>, string>("apply on the update", null, "Apply"),
                    Tuple.Create<string, Action<Game>, string>("second update, then restore (keeps the newer file)",
                        g => g.Update(official, "10.5.0+2025-06-01.0000"), "Restore"),
                    Tuple.Create<string, Action<Game>, string>("patched orphan", g => File.WriteAllBytes(g.Bundle, File.ReadAllBytes(Path.Combine(ctx.RunDir, "cmp_patched.bundle"))), "Apply"),
                    Tuple.Create<string, Action<Game>, string>("orphan restore", null, "Restore"),
                };
                // a patched bundle for the orphan step
                var gp = ctx.NewGame("cmp_make_patched", official);
                Util.Retry(() => gp.Patcher().Apply(ctx.Data));
                File.Copy(gp.Bundle, Path.Combine(ctx.RunDir, "cmp_patched.bundle"), true);

                foreach (bool embedded in new[] { false, true })
                {
                    var ga = ctx.NewGame("cmp_api_a", official);
                    var gb = ctx.NewGame("cmp_api_b", official);
                    object da = a.Data(embedded ? null : ctx.DataPath), db = b.Data(embedded ? null : ctx.DataPath);
                    foreach (var s in steps)
                    {
                        s.Item2?.Invoke(ga);
                        s.Item2?.Invoke(gb);
                        var la = new List<string>();
                        var lb = new List<string>();
                        string ra = a.Call(ga.Dir, s.Item3, da, la), rb = b.Call(gb.Dir, s.Item3, db, lb);
                        string what = (embedded ? "embedded data, " : "data file, ") + s.Item1;
                        T.Eq(ra, rb, what + ": result");
                        string note = CompareLogs(la, lb, what);
                        string fa = Files(ga).Replace("cmp_api_a", "X"), fb = Files(gb).Replace("cmp_api_b", "X");
                        if (s.Item1 == "state lost")
                        {
                            // round 3b (R1): our output rebuilt without a state file proves nothing about the backup's version,
                            // so the new build writes the state without game_version (the baseline records the installed one)
                            var gvLine = new System.Text.RegularExpressions.Regex(@"game_version=[^|\n]*\|?");
                            T.True(gvLine.IsMatch(fa), what + ": baseline records game_version: " + fa);
                            T.True(!gvLine.IsMatch(fb), what + ": new build records no game_version: " + fb);
                            fa = gvLine.Replace(fa, "");
                            note = (note != null ? note + "; " : "") + "intended (R1): no game_version without a state file";
                        }
                        T.Eq(fa, fb, what + ": files");
                        if (s.Item1 == "apply fresh" || s.Item1 == "apply again" || s.Item1 == "steam verify")
                        {
                            T.Eq(Util.ShippedPatchedSha, Util.Sha256(File.ReadAllBytes(ga.Bundle)), what + ": baseline patched hash");
                            T.Eq(Util.ShippedPatchedSha, Util.Sha256(File.ReadAllBytes(gb.Bundle)), what + ": new patched hash");
                        }
                        r.Note($"{what}: {ra}" + (note != null ? "  [" + note + "]" : ""));
                    }
                }
                intended.Add("FZ-09 log order: when a new official file is backed up, 'バックアップを作成しました' now comes after '日本語データを書き出しています…' (files identical)");
                intended.Add("R1 (round 3b) apply after the state file was lost: baseline records game_version (the installed version) for the backup, new writes the state without game_version");
            });

            r.Test("ExeCompare: damaged (half) bundle without backup: both refuse with the reason and the Steam verification advice", () =>
            {
                var ga = ctx.NewGame("cmp_dmg_a", damaged);
                var gb = ctx.NewGame("cmp_dmg_b", damaged);
                object da = a.Data(ctx.DataPath), db = b.Data(ctx.DataPath);
                string ra = a.Call(ga.Dir, "Apply", da, new List<string>()), rb = b.Call(gb.Dir, "Apply", db, new List<string>());
                foreach (var res in new[] { ra, rb })
                {
                    T.Contains(res, "error PatchException: 日本語データが壊れています（", "result " + res);
                    T.Contains(res, "ゲームファイルの整合性を確認", "advice in " + res);
                }
                T.Contains(rb, "ファイルの大きさが一致しません", "new: the header size check names the reason");
                T.Eq(Files(ga).Replace("cmp_dmg_a", "X"), Files(gb).Replace("cmp_dmg_b", "X"), "files");
                r.Note(ra == rb ? "same message: " + Between(ra, "壊れています（", "）。") : "baseline: " + ra + " / new: " + rb);
                if (ra != rb)
                    intended.Add("FZ-05/06 damaged-bundle reason: baseline '" + Between(ra, "壊れています（", "）。") + "', new '" + Between(rb, "壊れています（", "）。") + "'");
            });

            r.Test("ExeCompare: intended: same game version + different bundle -> baseline takes it as a new official file, new rebuilds/restores from the backup (FZ-05/06)", () =>
            {
                object da = a.Data(ctx.DataPath), db = b.Data(ctx.DataPath);
                var ga = ctx.NewGame("cmp_samever_a", official);
                var gb = ctx.NewGame("cmp_samever_b", official);
                a.Call(ga.Dir, "Apply", da, new List<string>());
                b.Call(gb.Dir, "Apply", db, new List<string>());
                File.WriteAllBytes(ga.Bundle, updated);
                File.WriteAllBytes(gb.Bundle, updated);
                var la = new List<string>();
                var lb = new List<string>();
                a.Call(ga.Dir, "Apply", da, la);
                b.Call(gb.Dir, "Apply", db, lb);
                T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(ga.Backup)), "baseline: backup replaced by the changed file");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(gb.Backup)), "new: backup untouched");
                T.Eq(Util.ShippedPatchedSha, Util.Sha256(File.ReadAllBytes(gb.Bundle)), "new: rebuilt from the backup");
                T.True(lb.Any(l => l.Contains("ゲームのバージョンは変わっていないため")), "new: log");

                var ra = ctx.NewGame("cmp_samever_restore_a", official);
                var rb = ctx.NewGame("cmp_samever_restore_b", official);
                a.Call(ra.Dir, "Apply", da, new List<string>());
                b.Call(rb.Dir, "Apply", db, new List<string>());
                File.WriteAllBytes(ra.Bundle, updated);
                File.WriteAllBytes(rb.Bundle, updated);
                a.Call(ra.Dir, "Restore", da, la);
                b.Call(rb.Dir, "Restore", db, lb);
                T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(ra.Bundle)), "baseline restore keeps the changed file");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(rb.Bundle)), "new restore restores the backup");
                intended.Add("FZ-05/06 same version.txt + different bundle: baseline backs it up as a new official file (apply) / keeps it (restore); new rebuilds from / restores the backup");
            });

            r.Test("ExeCompare: intended: a failed apply leaves a backup in the baseline, none in the new build (FZ-09)", () =>
            {
                string fuzzPath = Path.Combine(ctx.VectorsDir, "escape_patch.tsv"); // applies nothing to the real bundle
                var ga = ctx.NewGame("cmp_failed_a", official);
                var gb = ctx.NewGame("cmp_failed_b", official);
                string ra = a.Call(ga.Dir, "Apply", a.Data(fuzzPath), new List<string>()), rb = b.Call(gb.Dir, "Apply", b.Data(fuzzPath), new List<string>());
                T.Eq(ra, rb, "result");
                T.Contains(ra, "適用できる翻訳がありませんでした", "message");
                T.True(File.Exists(ga.Backup), "baseline: a backup was left");
                T.True(!File.Exists(gb.Backup), "new: no backup");
                intended.Add("FZ-09 failed apply (nothing applicable): baseline leaves a .tpcjp-original backup, new leaves none");
            });

            r.Test("ExeCompare: intended: an unwritable state file fails the baseline apply, the new one warns and completes (FZ-10)", () =>
            {
                var ga = ctx.NewGame("cmp_statefail_a", official);
                var gb = ctx.NewGame("cmp_statefail_b", official);
                foreach (var g in new[] { ga, gb })
                    Directory.CreateDirectory(g.State + SafeFile.TempSuffix);
                var lb = new List<string>();
                string ra = a.Call(ga.Dir, "Apply", a.Data(ctx.DataPath), new List<string>()), rb = b.Call(gb.Dir, "Apply", b.Data(ctx.DataPath), lb);
                T.True(ra.StartsWith("error ", StringComparison.Ordinal), "baseline: " + ra);
                T.True(rb.StartsWith("ok ", StringComparison.Ordinal), "new: " + rb);
                T.True(lb.Any(l => l.Contains("状態ファイルを書き込めませんでした")), "new: warning");
                T.Eq(Util.Sha256(File.ReadAllBytes(ga.Bundle)), Util.Sha256(File.ReadAllBytes(gb.Bundle)), "the bundle was written by both");
                intended.Add("FZ-10 unwritable state file: baseline apply ends with '" + ra.Split(':')[0] + "' (exit 1), new completes with a warning (exit 0)");
            });

            r.Test("ExeCompare: intended: translation data checks (CS-01, CS-02, FZ-07, CS-06); the shipped data loads the same", () =>
            {
                T.Eq(a.TryData(ctx.DataPath, out _), b.TryData(ctx.DataPath, out _), "shipped data");
                T.Eq(a.TryData(null, out _), b.TryData(null, out _), "embedded data");
                string dir = ctx.Dir("cmp_data");
                string text = File.ReadAllText(ctx.DataPath, Encoding.UTF8);
                var cases = new Dictionary<string, byte[]>
                {
                    ["CS-02 half of the file"] = Encoding.UTF8.GetBytes(text.Substring(0, text.IndexOf('\n', text.Length / 2) + 1)),
                    ["CS-01 invalid UTF-8"] = Encoding.UTF8.GetBytes("#TPCJP-PATCH\t1\n#entries\t1\nI2LS_UI\tk\t0123456789abcdef\t").Concat(new byte[] { 0xE6, 0x97, 0xFF, 0x0A }).ToArray(),
                    ["FZ-07 key with '='"] = Encoding.UTF8.GetBytes("#TPCJP-PATCH\t1\n#entries\t1\nI2LS_UI\tk=v\t0123456789abcdef\t訳\n"),
                    ["FZ-07 translation with [i2t]"] = Encoding.UTF8.GetBytes("#TPCJP-PATCH\t1\n#entries\t1\nI2LS_UI\tk\t0123456789abcdef\t訳[i2t]X=Y\n"),
                    ["CS-06 last translation cut ('区画15' -> '区')"] = File.ReadAllBytes(ctx.DataPath).Take(File.ReadAllBytes(ctx.DataPath).Length - 6).ToArray(),
                };
                foreach (var c in cases)
                {
                    string p = Path.Combine(dir, "case.tsv");
                    File.WriteAllBytes(p, c.Value);
                    string ra = a.TryData(p, out _), rb = b.TryData(p, out _);
                    T.True(rb.StartsWith("error InvalidDataException: ", StringComparison.Ordinal), c.Key + ": new " + rb);
                    T.True(ra != rb, c.Key + ": baseline behaved the same: " + ra);
                    intended.Add($"{c.Key}: baseline '{T.Show(ra)}', new '{T.Show(rb)}'");
                }
            });

            r.Test("ExeCompare: intended: status as processes classifies by content (CS-07): state deleted -> '状態の記録なし', damaged with backup -> '壊れているか読み込めません'", () =>
            {
                var cases = new Dictionary<string, Tuple<Action<Game>, string, string>>
                {
                    ["state deleted"] = Tuple.Create<Action<Game>, string, string>(g => File.Delete(g.State), "日本語化パッチ適用済み（状態の記録なし）", "PatchedNoState"),
                    ["damaged bundle with backup and state"] = Tuple.Create<Action<Game>, string, string>(g => File.WriteAllBytes(g.Bundle, damaged),
                        "壊れているか読み込めません（「元に戻す」か Steam の整合性の確認で直せます）", "Damaged"),
                };
                object db = b.Data(null);
                foreach (var c in cases)
                {
                    var ga = ctx.NewGame("cmp_content_a", official);
                    var gb = ctx.NewGame("cmp_content_b", official);
                    foreach (var g in new[] { ga, gb })
                    {
                        Util.Retry(() => g.Patcher().Apply(ctx.Data));
                        c.Value.Item1(g);
                    }
                    var la = RunExe(exeA, "status", "--game", ga.Dir).Replace(ga.Dir, "{G}").Split('\n');
                    var lb = RunExe(exeB, "status", "--game", gb.Dir).Replace(gb.Dir, "{G}").Split('\n');
                    T.Eq(la.Length, lb.Length, c.Key + ": line count");
                    for (int i = 0; i < la.Length; i++)
                    {
                        if (la[i].StartsWith("日本語データ: ", StringComparison.Ordinal))
                        {
                            T.Eq("日本語データ: ゲーム側で更新されています（「日本語化する」で適用し直してください）", la[i].TrimEnd('\r'), c.Key + ": baseline state line");
                            T.Eq("日本語データ: " + c.Value.Item2, lb[i].TrimEnd('\r'), c.Key + ": new state line");
                        }
                        else
                            T.Eq(la[i], lb[i], c.Key + ": line " + i);
                    }
                    T.Eq("Unknown", a.State(ga.Dir), c.Key + ": baseline GetStatus");
                    T.Eq(c.Value.Item3, b.State(gb.Dir, db), c.Key + ": new GetStatus(data)");
                    T.Eq("Unknown", b.State(gb.Dir, null), c.Key + ": new GetStatus() without data");
                    T.Eq(Files(ga).Replace("cmp_content_a", "X"), Files(gb).Replace("cmp_content_b", "X"), c.Key + ": files (status changed nothing)");
                    intended.Add($"CS-07 status, {c.Key}: baseline 'ゲーム側で更新されています（…）' (Unknown), new '{c.Value.Item2}' ({c.Value.Item3})");
                }
            });

            r.Test("ExeCompare: intended: status as processes without backup and state reads the bundle (CS-09): patched orphan -> '日本語化パッチ適用済み（バックアップなし。Steam の整合性の確認で戻せます）', damaged -> Steam verification only", () =>
            {
                var gp = ctx.NewGame("cmp_nobackup_make", official);
                Util.Retry(() => gp.Patcher().Apply(ctx.Data));
                var cases = new Dictionary<string, Tuple<byte[], string, string>>
                {
                    ["patched orphan"] = Tuple.Create(File.ReadAllBytes(gp.Bundle), PatchedNoBackupText, "PatchedNoBackup"),
                    ["damaged"] = Tuple.Create(damaged, DamagedNoBackupText, "DamagedNoBackup"),
                };
                object db = b.Data(null);
                foreach (var c in cases)
                {
                    var ga = ctx.NewGame("cmp_nobackup_a", c.Value.Item1);
                    var gb = ctx.NewGame("cmp_nobackup_b", c.Value.Item1);
                    var la = RunExe(exeA, "status", "--game", ga.Dir).Replace(ga.Dir, "{G}").Split('\n');
                    var lb = RunExe(exeB, "status", "--game", gb.Dir).Replace(gb.Dir, "{G}").Split('\n');
                    T.Eq(la.Length, lb.Length, c.Key + ": line count");
                    for (int i = 0; i < la.Length; i++)
                    {
                        if (la[i].StartsWith("日本語データ: ", StringComparison.Ordinal))
                        {
                            T.Eq("日本語データ: 公式のまま（パッチ未適用）", la[i].TrimEnd('\r'), c.Key + ": baseline state line");
                            T.Eq("日本語データ: " + c.Value.Item2, lb[i].TrimEnd('\r'), c.Key + ": new state line");
                        }
                        else
                            T.Eq(la[i], lb[i], c.Key + ": line " + i);
                    }
                    T.Eq("Original", a.State(ga.Dir), c.Key + ": baseline GetStatus");
                    T.Eq(c.Value.Item3, b.State(gb.Dir, db), c.Key + ": new GetStatus(data)");
                    T.Eq("Original", b.State(gb.Dir, null), c.Key + ": new GetStatus() without data (nothing read)");
                    T.Eq(Files(ga).Replace("cmp_nobackup_a", "X"), Files(gb).Replace("cmp_nobackup_b", "X"), c.Key + ": files (status changed nothing)");
                    intended.Add($"CS-09 status without backup and state, {c.Key}: baseline '公式のまま（パッチ未適用）' (Original), new '{c.Value.Item2}' ({c.Value.Item3})");
                }
            });

            r.Test("ExeCompare: intended: bundle missing with a backup but no state file + leftovers of an interrupted 1.0.0 run: baseline apply refuses ('見つかりません') and leaves them, restore restores the backup; new removes them and refuses both until a Steam verification (F-SBP-7, S1)", () =>
            {
                object da = a.Data(ctx.DataPath), db = b.Data(ctx.DataPath);
                var ga = ctx.NewGame("cmp_missing_a", official);
                var gb = ctx.NewGame("cmp_missing_b", official);
                foreach (var g in new[] { ga, gb })
                {
                    File.WriteAllBytes(g.Backup, official);
                    File.Move(g.Bundle, g.Bundle + "~RF38b3fbc.TMP");
                    File.WriteAllBytes(g.Bundle + SafeFile.TempSuffix, File.ReadAllBytes(Path.Combine(ctx.RunDir, "cmp_patched.bundle")));
                }
                // round 3c (S1): without a state file nothing says which game version the backup is for, so the new build does
                // not use it (round 2 rebuilt from it); the leftovers are removed first
                string unconfirmed = "error PatchException: バックアップがどのバージョンのゲームのものか確認できません（記録: 不明、現在のゲームのバージョン: " + Game.DefaultVersion + "）。バックアップは削除せずに残しました。";
                var la = new List<string>();
                var lb = new List<string>();
                string ra = a.Call(ga.Dir, "Apply", da, la), rb = b.Call(gb.Dir, "Apply", db, lb);
                T.Contains(ra, "error PatchException: 日本語データ（localisation_assets_localisationjapanese.bundle）が見つかりません", "baseline result");
                T.True(rb.StartsWith(unconfirmed, StringComparison.Ordinal), "new result: " + rb);
                T.Eq(2, ga.Leftovers().Length, "baseline leaves both leftovers");
                T.Eq(0, gb.Leftovers().Length, "new removed them");
                T.True(!File.Exists(ga.Bundle) && !File.Exists(gb.Bundle), "both: still no bundle");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(gb.Backup)), "new: backup kept");
                // the baseline's restore recovers the bundle from the backup (and leaves the ~RF copy); the new one refuses as well
                string sa = a.Call(ga.Dir, "Restore", da, la), sbr = b.Call(gb.Dir, "Restore", db, lb);
                T.Eq("ok", sa, "baseline restore");
                T.True(sbr.StartsWith(unconfirmed, StringComparison.Ordinal), "new restore: " + sbr);
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(ga.Bundle)), "baseline restored");
                T.True(!File.Exists(gb.Bundle), "new: no bundle made up");
                T.True(ga.Leftovers().Any(n => n.Contains("~RF")), "baseline restore leaves the ~RF*.TMP copy");
                T.Eq(0, gb.Leftovers().Length, "new: no leftovers");
                // the Steam verification brings the official file back: the new build patches it (the installed file is the backup)
                File.WriteAllBytes(gb.Bundle, official);
                string rv = b.Call(gb.Dir, "Apply", db, lb);
                T.True(rv.StartsWith("ok Applied=2391", StringComparison.Ordinal), "new apply after the verification: " + rv);
                T.Eq(Util.ShippedPatchedSha, Util.Sha256(File.ReadAllBytes(gb.Bundle)), "new: patched after the verification");
                intended.Add("F-SBP-7 / S1 bundle missing + backup without a state file: baseline apply '" + Between(ra, "error PatchException: ", "。")
                             + "' and restore restores the backup, leaving '<bundle>~RF*.TMP' / '<bundle>.tpcjp-tmp'; new removes them and refuses both ('"
                             + Between(rb, "error PatchException: ", "。") + "') until a Steam verification");
            });

            r.Test("ExeCompare: apply while another program holds the bundle open with FileShare.ReadWrite|Delete gives the same result as the baseline", () =>
            {
                object da = a.Data(ctx.DataPath), db = b.Data(ctx.DataPath);
                var ga = ctx.NewGame("cmp_open_a", official);
                var gb = ctx.NewGame("cmp_open_b", official);
                string ra, rb;
                using (new FileStream(ga.Bundle, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                    ra = a.Call(ga.Dir, "Apply", da, new List<string>());
                using (new FileStream(gb.Bundle, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                    rb = b.Call(gb.Dir, "Apply", db, new List<string>());
                r.Note($"baseline: {ra}; new: {rb}");
                T.Eq(ra, rb, "result");
            });

            r.Test("ExeCompare: list of intended differences (fixes.md) that were observed", () =>
            {
                foreach (var d in intended)
                    r.Note("intended difference: " + d);
                T.True(intended.Count >= 14, "all intended-difference scenarios ran (" + intended.Count + ")");
            });
        }

        static string Between(string s, string start, string end)
        {
            int i = s.IndexOf(start, StringComparison.Ordinal);
            if (i < 0)
                return s;
            i += start.Length;
            int j = s.IndexOf(end, i, StringComparison.Ordinal);
            return j < 0 ? s.Substring(i) : s.Substring(i, j - i);
        }
    }
}
