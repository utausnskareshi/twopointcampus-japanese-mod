using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Threading;

namespace TpcJpPatch.Tests
{
    /// <summary>
    /// F-SBP-2: only one TpcJpPatch (GUI or CLI, except status) may change files at a time (named mutex Local\TpcJpPatch).
    /// Second runs are made in-process on another thread and as processes of the Debug TpcJpPatch.exe (the very
    /// assembly the tests load, whose test hooks are verified at start) with TPCJP_PREFS_ROOT / TPCJP_APPDATA in the
    /// temp folder and --game pointing at a sandbox.
    /// </summary>
    static class LockTests
    {
        const string Busy = Util.BusyMessage;

        static string Exe => typeof(Patcher).Assembly.Location;

        sealed class Sandbox
        {
            public Game Game;
            public string Prefs, AppData, Account;
        }

        static Sandbox NewSandbox(Ctx ctx, string name)
        {
            var s = new Sandbox { Game = ctx.NewGame("lock_" + name, ctx.Official) };
            string root = ctx.Dir("lock_env_" + name);
            s.Prefs = Path.Combine(root, "Cloud");
            s.AppData = Path.Combine(root, "appdata");
            s.Account = Path.Combine(s.Prefs, "4242", "preferences.json");
            Directory.CreateDirectory(Path.GetDirectoryName(s.Account));
            File.WriteAllBytes(s.Account, Mp.Prefs(Mp.Int(1)));
            return s;
        }

        static ProcessStartInfo Psi(Ctx ctx, Sandbox s, string[] args)
        {
            foreach (var p in new[] { s.Prefs, s.AppData, s.Game.Dir })
                ctx.Guard(p);
            var psi = new ProcessStartInfo(Exe, string.Join(" ", args.Select(a => a.Contains(' ') ? "\"" + a + "\"" : a)))
            {
                UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true,
                StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
            };
            psi.EnvironmentVariables["TPCJP_PREFS_ROOT"] = s.Prefs;
            psi.EnvironmentVariables["TPCJP_APPDATA"] = s.AppData;
            psi.EnvironmentVariables.Remove("TPCJP_SIMULATE_NOT_INSTALLED");
            return psi;
        }

        static string Collect(Process p)
        {
            var err = p.StandardError.ReadToEndAsync();
            string o = p.StandardOutput.ReadToEnd();
            p.WaitForExit();
            return o + err.Result;
        }

        static int RunExe(Ctx ctx, Sandbox s, out string output, params string[] args)
        {
            using (var p = Process.Start(Psi(ctx, s, args)))
            {
                output = Collect(p);
                return p.ExitCode;
            }
        }

        /// <summary>The second run of every changing command must be refused without changing anything.</summary>
        static readonly string[][] Commands =
        {
            new[] { "apply", "--game", "{G}", "--no-language" },
            new[] { "apply", "--game", "{G}" },
            new[] { "restore", "--game", "{G}", "--keep-language" },
            new[] { "restore", "--game", "{G}" },
            new[] { "language", "ja" },
            new[] { "language", "previous" },
        };

        static string[] Args(string[] cmd, Sandbox s) => cmd.Select(a => a.Replace("{G}", s.Game.Dir)).ToArray();

        static Dictionary<string, string> Snapshot(Sandbox s)
        {
            var d = s.Game.Snapshot();
            d["preferences.json"] = Util.Sha256(File.ReadAllBytes(s.Account));
            return d;
        }

        static void AssertSame(Dictionary<string, string> before, Dictionary<string, string> now, string what)
        {
            T.Eq(string.Join(",", before.Keys.OrderBy(k => k)), string.Join(",", now.Keys.OrderBy(k => k)), what + ": files");
            foreach (var kv in before)
                T.Eq(kv.Value, now[kv.Key], what + ": " + kv.Key);
        }

        public static void Run(Runner r, Ctx ctx)
        {
            r.Test("Lock: the Debug TpcJpPatch.exe used for second processes is the hooked Debug build", () =>
            {
                var cfg = typeof(Patcher).Assembly.GetCustomAttribute<AssemblyConfigurationAttribute>()?.Configuration;
                T.Eq("Debug", cfg, "assembly configuration of " + Exe);
                T.Eq(Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "TpcJpPatch.exe")), Exe, "exe next to the test exe");
            });

            r.Test("Lock: while the lock is held, a second in-process CLI run (other thread) is refused with exit 1 and changes nothing; status still runs", () =>
            {
                var s = NewSandbox(ctx, "inproc");
                string oldPrefs = Environment.GetEnvironmentVariable("TPCJP_PREFS_ROOT"), oldApp = Environment.GetEnvironmentVariable("TPCJP_APPDATA");
                Environment.SetEnvironmentVariable("TPCJP_PREFS_ROOT", s.Prefs);
                Environment.SetEnvironmentVariable("TPCJP_APPDATA", s.AppData);
                try
                {
                    T.Eq(0, LanguageTests.Cli(out _, "apply", "--game", s.Game.Dir, "--no-language"), "setup apply");
                    var before = Snapshot(s);
                    using (Util.Lock())
                    {
                        T.True(Mutex.TryOpenExisting(@"Local\TpcJpPatch", out var m), @"named mutex Local\TpcJpPatch exists");
                        m.Dispose();
                        foreach (var cmd in Commands)
                        {
                            string o = null;
                            int c = -1;
                            var t = new Thread(() => c = LanguageTests.CliOnce(out o, Args(cmd, s)));
                            t.Start();
                            t.Join();
                            string what = string.Join(" ", cmd);
                            T.Eq(1, c, what + ": exit code: " + o);
                            T.Contains(o, "エラー: " + Busy + "。終わってから、もう一度実行してください", what + ": message");
                            AssertSame(before, Snapshot(s), what);
                        }
                        string so = null;
                        int sc = -1;
                        var st = new Thread(() => sc = LanguageTests.CliOnce(out so, "status", "--game", s.Game.Dir));
                        st.Start();
                        st.Join();
                        T.Eq(0, sc, "status while locked: " + so);
                        T.Contains(so, "日本語化パッチ適用済み", "status output");
                        // usage errors are reported before the lock is taken
                        var ut = new Thread(() => sc = LanguageTests.CliOnce(out so, "apply", "--game", s.Game.Dir, "--bogus"));
                        ut.Start();
                        ut.Join();
                        T.Eq(2, sc, "usage error while locked");
                    }
                    T.Eq(0, LanguageTests.Cli(out var o2, "restore", "--game", s.Game.Dir, "--keep-language"), "after the lock is released: " + o2);
                    T.Eq(Util.Sha256(ctx.Official), Util.Sha256(File.ReadAllBytes(s.Game.Bundle)), "restored");
                }
                finally
                {
                    Environment.SetEnvironmentVariable("TPCJP_PREFS_ROOT", oldPrefs);
                    Environment.SetEnvironmentVariable("TPCJP_APPDATA", oldApp);
                }
            });

            r.Test("Lock: while the lock is held, the Debug exe as a second process is refused with exit 1 and changes nothing", () =>
            {
                var s = NewSandbox(ctx, "proc");
                T.Eq(0, Util.Retry(() =>
                {
                    int c = RunExe(ctx, s, out var o, "apply", "--game", s.Game.Dir, "--no-language");
                    if (o.Contains(Busy))
                        throw new PatchException(Busy);
                    T.Eq(0, c, "setup apply: " + o);
                    return 0;
                }), "setup");
                var before = Snapshot(s);
                using (Util.Lock())
                {
                    foreach (var cmd in Commands)
                    {
                        string what = string.Join(" ", cmd);
                        int c = RunExe(ctx, s, out var o, Args(cmd, s));
                        T.Eq(1, c, what + ": exit code: " + o);
                        T.Contains(o, "エラー: " + Busy, what + ": message");
                        AssertSame(before, Snapshot(s), what);
                    }
                    T.Eq(0, RunExe(ctx, s, out var so, "status", "--game", s.Game.Dir), "status while locked: " + so);
                }
                string log = Path.Combine(s.AppData, "TpcJpPatch.log");
                T.True(File.Exists(log) && File.ReadAllText(log, Encoding.UTF8).Contains(Busy), "the refusal is in the TPCJP_APPDATA log");
            });

            r.Test("Lock: 6 apply processes started together on one sandbox, then 6 restores: each succeeds or is refused; files stay consistent", () =>
            {
                var s = NewSandbox(ctx, "race");
                string officialSha = Util.Sha256(ctx.Official);
                foreach (var phase in new[] { "apply", "restore" })
                {
                    string[] args = phase == "apply"
                        ? new[] { "apply", "--game", s.Game.Dir, "--no-language" }
                        : new[] { "restore", "--game", s.Game.Dir, "--keep-language" };
                    int ok = 0, busy = 0;
                    for (int round = 0; ok == 0 && round < 20; round++)
                    {
                        var procs = Enumerable.Range(0, 6).Select(_ => Process.Start(Psi(ctx, s, args))).ToList();
                        var outputs = procs.Select(p => System.Threading.Tasks.Task.Run(() => Collect(p))).ToList();
                        for (int i = 0; i < procs.Count; i++)
                        {
                            string o = outputs[i].Result;
                            int code = procs[i].ExitCode;
                            procs[i].Dispose();
                            if (code == 0)
                                ok++;
                            else if (code == 1 && o.Contains(Busy))
                                busy++;
                            else
                                throw new AssertException($"{phase}: process {i} exit {code}: {o.Trim()}");
                        }
                        if (ok == 0)
                            Thread.Sleep(500); // another suite held the lock the whole time
                    }
                    r.Note($"{phase}: {ok} succeeded, {busy} refused as busy");
                    T.True(ok >= 1, phase + ": at least one run succeeded");
                    T.Eq(0, s.Game.Leftovers().Length, phase + ": temp files");
                    if (phase == "apply")
                    {
                        T.Eq(Util.ShippedPatchedSha, Util.Sha256(File.ReadAllBytes(s.Game.Bundle)), "apply: bundle patched");
                        T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(s.Game.Backup)), "apply: backup = official");
                        T.Eq(Util.ShippedPatchedSha, s.Game.ReadState()["patched_sha256"], "apply: state describes the bundle");
                        T.Eq(Patcher.BundleState.Patched, s.Game.Patcher().GetStatus().State, "apply: status");
                    }
                    else
                    {
                        T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(s.Game.Bundle)), "restore: official bundle");
                        T.True(!File.Exists(s.Game.Backup) && !File.Exists(s.Game.State), "restore: backup and state removed");
                    }
                }
            });

            r.Test("Lock: a lock left by a killed TpcJpPatch (abandoned mutex) does not block the next run", () =>
            {
                var s = NewSandbox(ctx, "abandoned");
                string signal = Path.Combine(ctx.Dir("lock_abandoned_signal"), "held");
                var psi = new ProcessStartInfo(Process.GetCurrentProcess().MainModule.FileName, $"--child-hold-lock \"{signal}\"")
                {
                    UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
                };
                using (var holder = Process.Start(psi))
                {
                    var sw = Stopwatch.StartNew();
                    while (!File.Exists(signal) && !holder.HasExited && sw.Elapsed.TotalSeconds < 200)
                        Thread.Sleep(50);
                    T.True(File.Exists(signal), "the child took the lock");
                    int busyCode = RunExe(ctx, s, out var bo, "apply", "--game", s.Game.Dir, "--no-language");
                    T.Eq(1, busyCode, "refused while the child holds the lock: " + bo);
                    T.Contains(bo, Busy, "busy message");
                    holder.Kill();
                    holder.WaitForExit();
                }
                int c = Util.Retry(() =>
                {
                    int code = RunExe(ctx, s, out var o, "apply", "--game", s.Game.Dir, "--no-language");
                    if (o.Contains(Busy))
                        throw new PatchException(Busy);
                    T.Eq(0, code, "apply after the holder was killed: " + o);
                    return code;
                });
                T.Eq(Util.ShippedPatchedSha, Util.Sha256(File.ReadAllBytes(s.Game.Bundle)), "patched");
                using (Util.Lock())
                {
                    // the in-process lock is free as well (it was released by the run above)
                }
            });
        }

        /// <summary>Child process: takes the lock, signals, and waits to be killed (at most 2 minutes).</summary>
        public static int ChildHoldLock(string signal)
        {
            var l = Util.Lock();
            File.WriteAllText(signal, "held");
            Thread.Sleep(120000);
            l.Dispose();
            return 0;
        }
    }
}
