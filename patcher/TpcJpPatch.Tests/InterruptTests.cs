using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;

namespace TpcJpPatch.Tests
{
    /// <summary>
    /// F-SBP-7 (round 2): the Debug TpcJpPatch.exe (the hooked build the tests load) is run as a process on a sandbox
    /// and killed at random moments, or watched by a poller while it runs. SafeFile now replaces files with one
    /// rename (MoveFileEx, or a POSIX rename while another program has the file open), so the game's bundle may never
    /// be missing or half-written, and the next run removes the leftovers ("&lt;name&gt;.tpcjp-tmp",
    /// "&lt;name&gt;~RF*.TMP") and completes.
    /// </summary>
    static class InterruptTests
    {
        static string Exe => typeof(Patcher).Assembly.Location;

        sealed class Sandbox
        {
            public Game Game;
            public string Prefs, AppData;
        }

        static Sandbox NewSandbox(Ctx ctx, string name)
        {
            var s = new Sandbox { Game = ctx.NewGame("intr_" + name, ctx.Official) };
            string root = ctx.Dir("intr_env_" + name);
            s.Prefs = Path.Combine(root, "Cloud");
            s.AppData = Path.Combine(root, "appdata");
            Directory.CreateDirectory(s.Prefs);
            return s;
        }

        static Process Start(Ctx ctx, Sandbox s, params string[] args)
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
            return Process.Start(psi);
        }

        static string[] ApplyArgs(Sandbox s) => new[] { "apply", "--game", s.Game.Dir, "--no-language" };
        static string[] RestoreArgs(Sandbox s) => new[] { "restore", "--game", s.Game.Dir, "--keep-language" };

        /// <summary>Runs to completion; retries while another suite holds the lock or a process named TPC runs.</summary>
        static string RunToEnd(Ctx ctx, Sandbox s, string[] args) => RunToEnd(ctx, s, args, out _);

        /// <param name="ms">Run time of the successful process (start to exit).</param>
        static string RunToEnd(Ctx ctx, Sandbox s, string[] args, out int ms)
        {
            int time = 0;
            string result = Util.Retry(() =>
            {
                var sw = Stopwatch.StartNew();
                using (var p = Start(ctx, s, args))
                {
                    var err = p.StandardError.ReadToEndAsync();
                    string o = p.StandardOutput.ReadToEnd() + err.Result;
                    p.WaitForExit();
                    if (o.Contains(Util.BusyMessage) || o.Contains("起動しています"))
                        throw new PatchException(o.Contains(Util.BusyMessage) ? Util.BusyMessage : "起動しています");
                    T.Eq(0, p.ExitCode, string.Join(" ", args.Take(1)) + ": " + o.Trim());
                    time = (int)sw.ElapsedMilliseconds;
                    return o;
                }
            });
            ms = time;
            return result;
        }

        /// <summary>What may be in the bundle folder at any moment: the bundle official or patched, never missing or partial.</summary>
        static void CheckInvariants(Game g, string officialSha, string patchedSha, string what)
        {
            T.True(File.Exists(g.Bundle), what + ": the bundle exists");
            string sha = Util.Sha256(File.ReadAllBytes(g.Bundle));
            T.True(sha == officialSha || sha == patchedSha, what + ": the bundle is the official or the patched file (sha " + sha.Substring(0, 8) + ")");
            if (File.Exists(g.Backup))
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), what + ": backup = official");
            if (File.Exists(g.State))
            {
                var st = g.ReadState();
                T.True(st.TryGetValue("patched_sha256", out var p) && p == patchedSha, what + ": the state file is complete");
            }
        }

        public static void Run(Runner r, Ctx ctx)
        {
            string officialSha = Util.Sha256(ctx.Official);
            string patchedSha = Util.ShippedPatchedSha;

            r.Test("Interrupt: apply / restore processes killed at random moments never leave the bundle missing or half-written; the next run completes and cleans up (F-SBP-7)", () =>
            {
                var s = NewSandbox(ctx, "kill");
                var rnd = new Random(20261021);
                int killed = 0, finished = 0, busy = 0, cleanups = 0;
                var leftoverKinds = new SortedSet<string>();
                var states = new SortedDictionary<string, int>();
                // kill between 35 % and 105 % of the measured duration of a complete run (process start-up included),
                // so that most kills land while the files are being read, written or renamed
                RunToEnd(ctx, s, ApplyArgs(s), out int applyMs);
                RunToEnd(ctx, s, RestoreArgs(s), out int restoreMs);
                r.Note($"complete runs: apply {applyMs} ms, restore {restoreMs} ms");
                for (int i = 0; i < 60; i++)
                {
                    bool apply = i % 2 == 0;
                    int full = apply ? applyMs : restoreMs;
                    int delay = rnd.Next(full * 35 / 100, full * 105 / 100 + 1);
                    using (var p = Start(ctx, s, apply ? ApplyArgs(s) : RestoreArgs(s)))
                    {
                        var err = p.StandardError.ReadToEndAsync();
                        var outp = p.StandardOutput.ReadToEndAsync();
                        if (p.WaitForExit(delay))
                        {
                            p.WaitForExit();
                            string o = outp.Result + err.Result;
                            if (o.Contains(Util.BusyMessage))
                                busy++;
                            else
                            {
                                T.Eq(0, p.ExitCode, $"run {i} ({(apply ? "apply" : "restore")}) finished with an error: {o.Trim()}");
                                finished++;
                                if (o.Contains("前回中断したときの一時ファイルを"))
                                    cleanups++;
                            }
                        }
                        else
                        {
                            try
                            {
                                p.Kill();
                            }
                            catch (Exception e) when (e is InvalidOperationException || e is Win32Exception)
                            {
                                // it ended just now
                            }
                            p.WaitForExit();
                            killed++;
                        }
                    }
                    string what = $"after run {i} ({(apply ? "apply" : "restore")}, {delay} ms)";
                    CheckInvariants(s.Game, officialSha, patchedSha, what);
                    foreach (var n in s.Game.Leftovers())
                        leftoverKinds.Add(n.Replace("localisation_assets_localisationjapanese.bundle", "<bundle>"));
                    string state = s.Game.Patcher().GetStatus(ctx.Data).State.ToString();
                    states[state] = states.TryGetValue(state, out var c) ? c + 1 : 1;
                }
                r.Note($"{killed} killed, {finished} finished ({cleanups} of them removed leftovers), {busy} refused as busy; leftovers seen: {(leftoverKinds.Count == 0 ? "none" : string.Join(", ", leftoverKinds))}");
                r.Note("states seen: " + string.Join(", ", states.Select(kv => kv.Key + " x" + kv.Value)));
                T.True(killed >= 10, "at least 10 runs were interrupted (" + killed + ")");

                // the next complete runs remove every leftover and end in a consistent state
                string ao = RunToEnd(ctx, s, ApplyArgs(s));
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(s.Game.Bundle)), "patched after a complete apply");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(s.Game.Backup)), "backup after a complete apply");
                T.Eq(patchedSha, s.Game.ReadState()["patched_sha256"], "state after a complete apply");
                T.Eq(0, s.Game.Leftovers().Length, "no leftovers after a complete apply: " + string.Join(", ", s.Game.Leftovers()));
                T.Eq(Patcher.BundleState.Patched, s.Game.Patcher().GetStatus(ctx.Data).State, "status after a complete apply");
                RunToEnd(ctx, s, RestoreArgs(s));
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(s.Game.Bundle)), "official after a complete restore");
                T.True(!File.Exists(s.Game.Backup) && !File.Exists(s.Game.State), "backup and state removed");
                T.Eq(1, Directory.GetFiles(GameLocator.BundlesDir(s.Game.Dir)).Length, "only the bundle is left");
                if (ao.Contains("一時ファイル"))
                    r.Note("the complete apply logged: " + string.Join(" / ", ao.Split('\n').Where(l => l.Contains("一時ファイル")).Select(l => l.Trim())));
            });

            r.Test("Interrupt: a watcher polling the bundle while 20 apply / restore processes run never finds it missing (one rename, F-SBP-7)", () =>
            {
                var s = NewSandbox(ctx, "watch");
                int polls = 0, missing = 0;
                bool done = false;
                // The watcher lists the folder instead of calling File.Exists: GetFileAttributes opens the file for a
                // moment, and MoveFileEx cannot replace a file that is open (SafeFile then renames with POSIX semantics
                // or retries; see the SafeFile poller tests).
                string dir = GameLocator.BundlesDir(s.Game.Dir), name = Path.GetFileName(s.Game.Bundle);
                var watcher = new Thread(() =>
                {
                    while (!Volatile.Read(ref done))
                    {
                        polls++;
                        if (!Directory.EnumerateFiles(dir).Any(f => string.Equals(Path.GetFileName(f), name, StringComparison.OrdinalIgnoreCase)))
                            missing++;
                    }
                });
                watcher.Start();
                try
                {
                    for (int i = 0; i < 20; i++)
                        RunToEnd(ctx, s, i % 2 == 0 ? ApplyArgs(s) : RestoreArgs(s));
                }
                finally
                {
                    Volatile.Write(ref done, true);
                    watcher.Join();
                }
                r.Note($"{polls} folder listings, {missing} without the bundle");
                T.Eq(0, missing, "polls that found the bundle missing");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(s.Game.Bundle)), "official after the last restore");
                T.Eq(0, s.Game.Leftovers().Length, "no leftovers");
            });
        }
    }
}
