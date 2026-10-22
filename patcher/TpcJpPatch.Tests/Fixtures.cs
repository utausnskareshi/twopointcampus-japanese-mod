using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

namespace TpcJpPatch.Tests
{
    /// <summary>Paths and shared data of one test run. Everything written lives under TempRoot.</summary>
    sealed class Ctx
    {
        public string ProjectRoot;
        public string TempRoot;      // %TEMP%\tpcjp_cstests
        public string RunDir;        // TempRoot\run (recreated on every run)
        public string VectorsDir;
        public string ResultsDir;    // work/localtest/results (layout PNGs)
        public string OfficialPath;
        public string DataPath;
        public string Python;
        public List<string> CompareExes = new List<string>();

        byte[] official;
        PatchData data;

        public byte[] Official => official ?? (official = File.ReadAllBytes(OfficialPath));
        public PatchData Data => data ?? (data = PatchData.Load(DataPath));

        public static readonly Regex JapaneseRx = new Regex("[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]");

        /// <summary>A new empty folder under RunDir.</summary>
        public string Dir(string name)
        {
            string d = Path.GetFullPath(Path.Combine(RunDir, name));
            Guard(d);
            if (Directory.Exists(d))
                ForceDelete(d);
            Directory.CreateDirectory(d);
            return d;
        }

        public void Guard(string path)
        {
            string full = Path.GetFullPath(path);
            if (!full.StartsWith(TempRoot.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("refusing to touch a path outside the test temp root: " + full);
        }

        public static void ForceDelete(string dir)
        {
            foreach (var f in Directory.GetFiles(dir, "*", SearchOption.AllDirectories))
                File.SetAttributes(f, FileAttributes.Normal);
            for (int i = 0; ; i++)
            {
                try
                {
                    Directory.Delete(dir, true);
                    return;
                }
                catch (IOException) when (i < 5)
                {
                    Thread.Sleep(200);
                }
            }
        }

        public Game NewGame(string name, byte[] bundle) => new Game(this, name, bundle);
    }

    /// <summary>A sandbox game folder: empty TPC.exe, version.txt and the Japanese bundle.</summary>
    sealed class Game
    {
        public readonly string Dir;
        public readonly List<string> Log = new List<string>();

        public const string DefaultVersion = "10.3.169253+2024-12-06.1241";

        public Game(Ctx ctx, string name, byte[] bundle)
        {
            Dir = ctx.Dir(Path.Combine("games", name));
            File.WriteAllBytes(Path.Combine(Dir, "TPC.exe"), new byte[0]);
            SetVersion(DefaultVersion);
            Directory.CreateDirectory(GameLocator.BundlesDir(Dir));
            if (bundle != null)
                File.WriteAllBytes(Bundle, bundle);
        }

        public void SetVersion(string version) => File.WriteAllText(Path.Combine(Dir, "version.txt"), version);

        /// <summary>A simulated game update: a new bundle and a new version.txt (like Steam does).</summary>
        public void Update(byte[] bundle, string version = Util.UpdatedVersion)
        {
            File.WriteAllBytes(Bundle, bundle);
            SetVersion(version);
        }

        public string Bundle => GameLocator.JapaneseBundle(Dir);
        public string Backup => Bundle + TpcJpPatch.Patcher.BackupSuffix;
        public string State => Bundle + TpcJpPatch.Patcher.StateSuffix;
        public Patcher Patcher() => new Patcher(Dir, s => Log.Add(s));
        public string LogText => string.Join("\n", Log);

        /// <summary>Temp files of an interrupted write: "&lt;name&gt;.tpcjp-tmp" and File.Replace's "&lt;name&gt;~RF*.TMP" (F-SBP-7).</summary>
        public string[] Leftovers() =>
            Directory.GetFiles(GameLocator.BundlesDir(Dir)).Select(Path.GetFileName).Where(IsLeftover).ToArray();

        public static bool IsLeftover(string name) =>
            name.EndsWith(SafeFile.TempSuffix, StringComparison.Ordinal) || Regex.IsMatch(name, @"~RF.*\.TMP$", RegexOptions.IgnoreCase);

        /// <summary>Name -> sha256 of every file next to the bundle (to prove that nothing changed).</summary>
        public Dictionary<string, string> Snapshot() =>
            Directory.GetFiles(GameLocator.BundlesDir(Dir)).ToDictionary(Path.GetFileName, f => Util.Sha256(File.ReadAllBytes(f)));

        public void AssertUnchanged(Dictionary<string, string> before, string what)
        {
            var now = Snapshot();
            T.Eq(string.Join(",", before.Keys.OrderBy(k => k)), string.Join(",", now.Keys.OrderBy(k => k)), what + ": files");
            foreach (var kv in before)
                T.Eq(kv.Value, now[kv.Key], what + ": " + kv.Key);
        }

        public Dictionary<string, string> ReadState()
        {
            var d = new Dictionary<string, string>();
            foreach (var line in File.ReadAllLines(State))
            {
                int eq = line.IndexOf('=');
                if (eq > 0)
                    d[line.Substring(0, eq)] = line.Substring(eq + 1);
            }
            return d;
        }
    }

    static class Util
    {
        public static string Sha256(byte[] data)
        {
            using (var sha = SHA256.Create())
                return string.Concat(sha.ComputeHash(data).Select(b => b.ToString("x2")));
        }

        public static string Sha256(string text) => Sha256(Encoding.UTF8.GetBytes(text));

        public static string B64(string b64) => b64 == "-" ? null : Encoding.UTF8.GetString(Convert.FromBase64String(b64));

        public const string BusyMessage = "別の TpcJpPatch が実行中です";

        /// <summary>
        /// Waits and retries while another test run's fake TPC.exe makes the patcher refuse to run, or while another
        /// test suite's TpcJpPatch holds the single-instance lock (Local\TpcJpPatch). Both refusals change nothing.
        /// </summary>
        public static TR Retry<TR>(Func<TR> f)
        {
            int busy = 0;
            for (int attempt = 0; ; attempt++)
            {
                try
                {
                    return f();
                }
                catch (PatchException e) when (e.Message.Contains("起動しています") && attempt < 15)
                {
                    Console.WriteLine("     (a process named TPC is running; waiting 40 s and retrying)");
                    Thread.Sleep(40000);
                }
                catch (PatchException e) when (e.Message.Contains(BusyMessage) && busy++ < 300)
                {
                    if (busy == 1)
                        Console.WriteLine("     (another TpcJpPatch holds the lock; retrying every 0.5 s)");
                    Thread.Sleep(500);
                }
            }
        }

        /// <summary>Patcher.Lock(), waiting while another suite's TpcJpPatch holds it.</summary>
        public static IDisposable Lock() => Retry(() => Patcher.Lock());

        /// <summary>sha256 of the bundle patched with dist/data (shipped 1.0.0 data) from the official copy.</summary>
        public const string ShippedPatchedSha = "20f1034be65a32a905bd6402659e1ea6de36a9dd8da46fefe698fbb2bc7eceb4";

        /// <summary>A game version different from the sandbox default (a real game update changes version.txt).</summary>
        public const string UpdatedVersion = "10.4.170001+2025-03-01.0900";

        public static void Retry(Action f) => Retry<object>(() => { f(); return null; });

        /// <summary>Same split as tools/tpcloc/i2cache.parse: entries at "[i2t]", key/value at the first '='.</summary>
        public static List<KeyValuePair<string, string>> ParseI2(string text)
        {
            var list = new List<KeyValuePair<string, string>>();
            if (text.Length == 0)
                return list;
            foreach (var part in text.Split(new[] { "[i2t]" }, StringSplitOptions.None))
            {
                int eq = part.IndexOf('=');
                list.Add(eq < 0
                    ? new KeyValuePair<string, string>(part, "")
                    : new KeyValuePair<string, string>(part.Substring(0, eq), part.Substring(eq + 1)));
            }
            return list;
        }

        public static string DumpI2(IEnumerable<KeyValuePair<string, string>> pairs) =>
            string.Join("[i2t]", pairs.Select(p => p.Key + "=" + p.Value));

        public static Dictionary<string, string> Texts(LocalisationBundle b) =>
            b.Names.ToDictionary(n => n, n => b.GetText(n), StringComparer.Ordinal);

        public static Dictionary<string, string> Texts(byte[] bundle)
        {
            using (var b = LocalisationBundle.Load(bundle, "test.bundle"))
                return Texts(b);
        }

        /// <summary>Returns a copy of the bundle with values replaced: source -> key -> value (every occurrence).</summary>
        public static byte[] ModifyBundle(byte[] bundle, Dictionary<string, Dictionary<string, string>> changes)
        {
            using (var b = LocalisationBundle.Load(bundle, "test.bundle"))
            {
                foreach (var src in changes)
                {
                    var pairs = ParseI2(b.GetText(src.Key));
                    foreach (var kv in src.Value)
                        if (!pairs.Any(p => p.Key == kv.Key))
                            throw new InvalidOperationException("no such key " + kv.Key);
                    b.SetText(src.Key, DumpI2(pairs.Select(p => src.Value.TryGetValue(p.Key, out var v)
                        ? new KeyValuePair<string, string>(p.Key, v) : p)));
                }
                return b.Save();
            }
        }

        /// <summary>
        /// Independent model of the fill-only apply: texts of `official` with every applicable entry of `data`
        /// replaced. Returns the expected texts and the number of applied entries.
        /// </summary>
        public static Dictionary<string, string> ExpectedPatched(Dictionary<string, string> official, PatchData data, out int applied)
        {
            applied = 0;
            var lastOfficial = official.ToDictionary(kv => kv.Key, kv =>
            {
                var d = new Dictionary<string, string>(StringComparer.Ordinal);
                foreach (var p in ParseI2(kv.Value))
                    d[p.Key] = p.Value;
                return d;
            });
            var result = new Dictionary<string, string>(official, StringComparer.Ordinal);
            foreach (var src in data.Sources)
            {
                if (!lastOfficial.TryGetValue(src.Key, out var values))
                    continue;
                var replace = new Dictionary<string, string>(StringComparer.Ordinal);
                foreach (var e in src.Value)
                {
                    if (!values.TryGetValue(e.Key, out var off))
                        continue;
                    string ja = e.Japanese;
                    if (e.RefSource != null)
                    {
                        if (!lastOfficial.TryGetValue(e.RefSource, out var rv) || !rv.TryGetValue(e.RefKey, out ja) || !Ctx.JapaneseRx.IsMatch(ja))
                            continue;
                    }
                    if (PatchData.BaseHash(off) != e.Base)
                        continue;
                    replace[e.Key] = ja;
                    applied++;
                }
                if (replace.Count > 0)
                    result[src.Key] = DumpI2(ParseI2(official[src.Key]).Select(p =>
                        replace.TryGetValue(p.Key, out var v) ? new KeyValuePair<string, string>(p.Key, v) : p));
            }
            return result;
        }

        public static void AssertTexts(Dictionary<string, string> expected, Dictionary<string, string> actual, string what)
        {
            T.Eq(string.Join(",", expected.Keys.OrderBy(k => k, StringComparer.Ordinal)),
                string.Join(",", actual.Keys.OrderBy(k => k, StringComparer.Ordinal)), what + ": TextAsset names");
            foreach (var kv in expected)
            {
                if (kv.Value == actual[kv.Key])
                    continue;
                var e = ParseI2(kv.Value);
                var a = ParseI2(actual[kv.Key]);
                string detail = e.Count != a.Count ? $"{e.Count} vs {a.Count} entries"
                    : e.Zip(a, (x, y) => x.Key != y.Key ? "key order at " + x.Key : x.Value != y.Value ? "value of " + x.Key : null)
                        .FirstOrDefault(s => s != null);
                throw new AssertException($"{what}: {kv.Key} differs ({detail})");
            }
        }
    }
}
