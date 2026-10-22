using System;
using System.Collections.Generic;
using System.IO;
using System.Text.RegularExpressions;
using Microsoft.Win32;

namespace TpcJpPatch
{
    /// <summary>Finds the Steam install of Two Point Campus and the files this patch touches.</summary>
    static class GameLocator
    {
        public const string SteamAppId = "1649080";
        public const string GameExeName = "TPC";
        public const string UnknownVersion = "不明";

        /// <summary>The game's install folder from Steam's library manifests, or null when it is not installed.</summary>
        public static string FindGameDir()
        {
#if DEBUG
            // Test hook (debug builds only): behave as if the game were not installed.
            if (Environment.GetEnvironmentVariable("TPCJP_SIMULATE_NOT_INSTALLED") == "1")
                return null;
#endif
            foreach (var lib in SteamLibraries())
            {
                try
                {
                    string manifest = Path.Combine(lib, "steamapps", $"appmanifest_{SteamAppId}.acf");
                    if (!File.Exists(manifest))
                        continue;
                    var m = Regex.Match(File.ReadAllText(manifest), "\"installdir\"\\s+\"([^\"]+)\"");
                    if (!m.Success)
                        continue;
                    string dir = Path.Combine(lib, "steamapps", "common", m.Groups[1].Value);
                    if (IsGameDir(dir))
                        return dir;
                }
                catch (Exception e) when (IsPathError(e))
                {
                    // an unreadable or invalid library entry: try the next one
                }
            }
            return null;
        }

        static List<string> SteamLibraries()
        {
            var roots = new List<string>();
            // HKLM first: it keeps the folder's real letter case (HKCU SteamPath is lower-cased).
            AddRegistryPath(roots, Registry.LocalMachine, @"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath");
            AddRegistryPath(roots, Registry.LocalMachine, @"SOFTWARE\Valve\Steam", "InstallPath");
            AddRegistryPath(roots, Registry.CurrentUser, @"Software\Valve\Steam", "SteamPath");
            roots.Add(@"C:\Program Files (x86)\Steam");

            var libraries = new List<string>();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var root in roots)
            {
                if (!seen.Add(root))
                    continue;
                libraries.Add(root);
                try
                {
                    string vdf = Path.Combine(root, "steamapps", "libraryfolders.vdf");
                    if (!File.Exists(vdf))
                        continue;
                    foreach (Match m in Regex.Matches(File.ReadAllText(vdf), "\"path\"\\s+\"([^\"]+)\""))
                    {
                        string lib = m.Groups[1].Value.Replace(@"\\", @"\");
                        if (seen.Add(lib))
                            libraries.Add(lib);
                    }
                }
                catch (Exception e) when (IsPathError(e))
                {
                    // unreadable library list: the Steam folder itself is still searched
                }
            }
            return libraries;
        }

        static bool IsPathError(Exception e) =>
            e is IOException || e is UnauthorizedAccessException || e is ArgumentException || e is NotSupportedException;

        static void AddRegistryPath(List<string> list, RegistryKey hive, string subKey, string name)
        {
            try
            {
                using (var key = hive.OpenSubKey(subKey))
                {
                    if (key?.GetValue(name) is string s && s.Length > 0)
                        list.Add(Path.GetFullPath(s.Replace('/', '\\')));
                }
            }
            catch (Exception)
            {
                // Registry access is best effort; the default Steam folder is tried as well.
            }
        }

        public static bool IsGameDir(string dir) =>
            !string.IsNullOrEmpty(dir)
            && File.Exists(Path.Combine(dir, GameExeName + ".exe"))
            && Directory.Exists(Path.Combine(dir, "TPC_Data"));

        public static string BundlesDir(string gameDir) =>
            Path.Combine(gameDir, "TPC_Data", "StreamingAssets", "aa", "StandaloneWindows64");

        public static string JapaneseBundle(string gameDir) =>
            Path.Combine(BundlesDir(gameDir), "localisation_assets_localisationjapanese.bundle");

        /// <summary>
        /// version.txt of the game (may be empty), or UnknownVersion when it cannot be read (missing, in use, a folder, no access).
        /// Read the way the state file stores it (line breaks inside become spaces, Patcher.OneLine), so a recorded
        /// game_version compares equal to it (S4).
        /// </summary>
        public static string GameVersion(string gameDir)
        {
            try
            {
                return Patcher.OneLine(File.ReadAllText(Path.Combine(gameDir, "version.txt")).Trim());
            }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
            {
                return UnknownVersion;
            }
        }

        /// <summary>preferences.json of every Steam account that has played the game on this PC.</summary>
        public static List<string> PreferenceFiles()
        {
            var result = new List<string>();
            string cloud = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                "AppData", "LocalLow", "Two Point Studios", "Two Point Campus", "Cloud");
#if DEBUG
            // Test hook (debug builds only): use a fake Cloud folder instead of the real one (Steam Cloud syncs it).
            cloud = Environment.GetEnvironmentVariable("TPCJP_PREFS_ROOT") ?? cloud;
#endif
            if (!Directory.Exists(cloud))
                return result;
            foreach (var dir in Directory.GetDirectories(cloud))
            {
                string f = Path.Combine(dir, "preferences.json");
                if (File.Exists(f))
                    result.Add(f);
            }
            result.Sort(StringComparer.OrdinalIgnoreCase);
            return result;
        }
    }
}
