using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;

namespace TpcJpPatch.Tests
{
    /// <summary>
    /// usage: TpcJpPatch.Tests.exe [--root PROJECT] [--temp DIR] [--only NAME[,NAME]] [--python EXE]
    ///                             [--compare-exe BASELINE.exe --compare-exe NEW.exe]
    /// Exit code = number of failed tests (XFAIL = documented product problems are not counted).
    /// Never touches the real game, the real preferences or %LOCALAPPDATA%\TpcJpPatch: the patcher's debug
    /// hooks TPCJP_PREFS_ROOT / TPCJP_APPDATA are pointed into the temp folder and checked before any test runs.
    /// </summary>
    static class Program
    {
        [STAThread]
        static int Main(string[] args)
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            string Opt(string name) => args.SkipWhile(a => a != name).Skip(1).FirstOrDefault();

            var ctx = new Ctx();
            ctx.ProjectRoot = Path.GetFullPath(Opt("--root") ?? FindProjectRoot());
            ctx.TempRoot = Path.GetFullPath(Opt("--temp") ?? Path.Combine(Path.GetTempPath(), "tpcjp_cstests"));
            ctx.RunDir = Path.Combine(ctx.TempRoot, "run");
            ctx.VectorsDir = Path.Combine(ctx.TempRoot, "vectors");
            ctx.ResultsDir = Path.Combine(ctx.ProjectRoot, "work", "localtest", "results");
            ctx.OfficialPath = Path.Combine(ctx.ProjectRoot, "work", "backup", "official", "localisation_assets_localisationjapanese.bundle");
            ctx.DataPath = Path.Combine(ctx.ProjectRoot, "dist", "data", "TpcJpPatch.data.tsv");
            ctx.Python = Opt("--python") ?? Path.Combine(ctx.ProjectRoot, ".venv", "Scripts", "python.exe");
            for (int i = 0; i < args.Length - 1; i++)
                if (args[i] == "--compare-exe")
                    ctx.CompareExes.Add(Path.GetFullPath(args[i + 1]));

            if (args.Length >= 2 && args[0] == "--child-preferences")
                return PreferencesTests.Child(args[1]);
            if (args.Length >= 2 && args[0] == "--child-hold-lock")
                return LockTests.ChildHoldLock(args[1]);
            if (args.Length >= 2 && args[0] == "--write-updated-bundle")
            {
                // for repro scripts: a simulated official update of the bundle (4 I2LS_UI terms get a new official text)
                File.WriteAllBytes(args[1], Util.ModifyBundle(ctx.Official, new System.Collections.Generic.Dictionary<string, System.Collections.Generic.Dictionary<string, string>>
                {
                    ["I2LS_UI"] = Util.ParseI2(Util.Texts(ctx.Official)["I2LS_UI"]).Where(p => ctx.Data.Sources["I2LS_UI"].Any(e => e.Key == p.Key && e.Japanese != null))
                        .Take(4).ToDictionary(p => p.Key, p => "公式の新しい訳"),
                }));
                Console.WriteLine("written " + args[1]);
                return 0;
            }

            // ---- safety: the patcher's test hooks must point into our temp folder ----
            string prefsRoot = Path.Combine(ctx.TempRoot, "run", "prefs_root_default");
            string appData = Path.Combine(ctx.TempRoot, "run", "appdata");
            Environment.SetEnvironmentVariable("TPCJP_PREFS_ROOT", prefsRoot);
            Environment.SetEnvironmentVariable("TPCJP_APPDATA", appData);
            Environment.SetEnvironmentVariable("TPCJP_SIMULATE_NOT_INSTALLED", null);
            if (!string.Equals(Patcher.AppDataDir, appData, StringComparison.OrdinalIgnoreCase))
            {
                Console.WriteLine("ABORT: the patcher was built without its debug test hooks (build with -c Debug); " +
                                  "running would touch %LOCALAPPDATA%\\TpcJpPatch. AppDataDir = " + Patcher.AppDataDir);
                return 100;
            }
            if (Directory.Exists(ctx.RunDir))
                Ctx.ForceDelete(ctx.RunDir);
            Directory.CreateDirectory(ctx.RunDir);
            Directory.CreateDirectory(prefsRoot);
            if (GameLocator.PreferenceFiles().Count != 0)
            {
                Console.WriteLine("ABORT: TPCJP_PREFS_ROOT hook is not active");
                return 100;
            }
            Directory.CreateDirectory(ctx.ResultsDir);

            using (var log = new StreamWriter(Path.Combine(ctx.TempRoot, "last_run.log"), false, new UTF8Encoding(false)))
            {
                var r = new Runner(Opt("--only"), log);
                Console.WriteLine($"project {ctx.ProjectRoot}");
                Console.WriteLine($"temp    {ctx.TempRoot}");
                var sw = Stopwatch.StartNew();

                EnsureVectors(ctx, r);
                I2CacheTests.Run(r, ctx);
                PatchDataTests.Run(r, ctx);
                PreferencesTests.Run(r, ctx);
                BundleTests.Run(r, ctx);
                SafeFileTests.Run(r, ctx);
                PatcherTests.Run(r, ctx);
                LanguageTests.Run(r, ctx);
                LockTests.Run(r, ctx);
                InterruptTests.Run(r, ctx);
                LayoutTests.Run(r, ctx);
                ExeCompareTests.Run(r, ctx);

                string summary = $"passed {r.Passed}, failed {r.Failed}, xfail {r.XFailed}, xpass {r.XPassed}, skipped {r.Skipped} " +
                                 $"({sw.Elapsed.TotalSeconds:F0} s)";
                foreach (var line in new[] { "", "==== " + summary }
                             .Concat(r.Failures.Select(f => "FAIL  " + f.Split('\n')[0]))
                             .Concat(r.ExpectedFailures.Select(f => "XFAIL " + f.Split('\n')[0]))
                             .Concat(r.UnexpectedPasses.Select(f => "XPASS " + f)))
                {
                    Console.WriteLine(line);
                    log.WriteLine(line);
                }
                return r.Failed;
            }
        }

        static string FindProjectRoot()
        {
            var d = new DirectoryInfo(AppDomain.CurrentDomain.BaseDirectory);
            for (; d != null; d = d.Parent)
                if (File.Exists(Path.Combine(d.FullName, "README.md")) && Directory.Exists(Path.Combine(d.FullName, "patcher", "TpcJpPatch")))
                    return d.FullName;
            throw new DirectoryNotFoundException("project root not found; pass --root");
        }

        /// <summary>Generates the Python reference vectors (gen_vectors.py) into the temp folder.</summary>
        static void EnsureVectors(Ctx ctx, Runner r)
        {
            r.Test("setup: python reference vectors", () =>
            {
                string script = Path.Combine(ctx.ProjectRoot, "patcher", "TpcJpPatch.Tests", "gen_vectors.py");
                T.True(File.Exists(ctx.Python), "python not found: " + ctx.Python);
                var psi = new ProcessStartInfo(ctx.Python, $"\"{script}\" --out \"{ctx.VectorsDir}\"")
                {
                    UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true,
                    StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
                    WorkingDirectory = ctx.TempRoot,
                };
                psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                using (var p = Process.Start(psi))
                {
                    var err = p.StandardError.ReadToEndAsync();
                    string outText = p.StandardOutput.ReadToEnd();
                    p.WaitForExit();
                    T.True(p.ExitCode == 0, "gen_vectors.py failed: " + outText + err.Result);
                    r.Note(outText.Trim());
                }
            });
        }
    }
}
