using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;

namespace TpcJpPatch
{
    /// <summary>
    /// Translations to fill in, as written by tools/build_patch.py (UTF-8, tab-separated):
    ///   #TPCJP-PATCH&lt;TAB&gt;1
    ///   #&lt;name&gt;&lt;TAB&gt;&lt;value&gt;            header values (patch_version, game_version, bundle_sha256, ...)
    ///   &lt;source&gt;&lt;TAB&gt;&lt;key&gt;&lt;TAB&gt;&lt;base&gt;&lt;TAB&gt;&lt;ja&gt;
    ///   &amp;&lt;source&gt;&lt;TAB&gt;&lt;key&gt;&lt;TAB&gt;&lt;base&gt;&lt;TAB&gt;&lt;ref source&gt;&lt;TAB&gt;&lt;ref key&gt;
    ///       (same English as a term that has an official translation: that translation is copied
    ///        from the game's own data at apply time, so it is not redistributed in this file)
    /// base = first 16 hex digits of SHA-256 of the official Japanese value we translate over;
    /// a value is only replaced while the game's value still has that hash (fill-only).
    /// Fields escape backslash, tab, CR and LF as \\ \t \r \n. U+0000 is not allowed anywhere, and the values of
    /// patch_version, game_version and bundle_sha256 may not hold control characters (not even escaped ones).
    /// </summary>
    sealed class PatchData
    {
        public const string FileName = "TpcJpPatch.data.tsv";
        const string EmbeddedName = "TpcJpPatch.data.tsv";

        public sealed class Entry
        {
            public string Key;
            public string Base;
            public string Japanese;
            public string RefSource;
            public string RefKey;
            /// <summary>Our text equals the official text it replaces (applying it changes nothing).</summary>
            public bool IsNoOp;
        }

        public readonly Dictionary<string, string> Header = new Dictionary<string, string>(StringComparer.Ordinal);
        public readonly Dictionary<string, List<Entry>> Sources = new Dictionary<string, List<Entry>>(StringComparer.Ordinal);
        public string Origin { get; private set; }
        public int Count { get; private set; }

        public string PatchVersion => Header.TryGetValue("patch_version", out var v) ? v : "?";
        public string GameVersion => Header.TryGetValue("game_version", out var v) ? v : "?";
        public string BundleSha256 => Header.TryGetValue("bundle_sha256", out var v) ? v : "";

        /// <summary>Loads the data file next to the executable, or the copy embedded in it.</summary>
        public static PatchData Load(string explicitPath = null)
        {
            string path = explicitPath ?? Path.Combine(AppDomain.CurrentDomain.BaseDirectory, FileName);
            if (explicitPath != null || File.Exists(path))
                return Parse(File.ReadAllBytes(path), path);
            using (var s = Assembly.GetExecutingAssembly().GetManifestResourceStream(EmbeddedName))
            {
                if (s == null)
                    throw new FileNotFoundException($"翻訳データ {FileName} が見つかりません");
                var ms = new MemoryStream();
                s.CopyTo(ms);
                return Parse(ms.ToArray(), "（実行ファイルに内蔵）");
            }
        }

        static PatchData Parse(byte[] bytes, string origin)
        {
            string text;
            try
            {
                // strict: a file saved in another encoding, or cut inside a character, is rejected
                text = new UTF8Encoding(false, true).GetString(bytes);
            }
            catch (DecoderFallbackException e)
            {
                throw new InvalidDataException("翻訳データを UTF-8 として読めません（壊れているか、別の文字コードで保存されています）", e);
            }
            if (text.Length > 0 && text[0] == '﻿')
                text = text.Substring(1);
            // build_patch.py ends every line with a newline: a file cut inside its last line has none
            if (!text.EndsWith("\n", StringComparison.Ordinal))
                throw new InvalidDataException("翻訳データが途中で切れています（最後の行に改行がありません）");
            var data = Parse(text, origin);
            // build_patch.py writes the entry count: a truncated file must not be applied in part
            if (!data.Header.TryGetValue("entries", out var declared) || !int.TryParse(declared, out var n) || n != data.Count)
                throw new InvalidDataException($"翻訳データが途中で切れているか、壊れています（{data.Count} 件、見出しの件数 {declared ?? "なし"}）");
            return data;
        }

        static PatchData Parse(string text, string origin)
        {
            var data = new PatchData { Origin = origin };
            var lines = text.Split('\n');
            if (lines.Length == 0 || !lines[0].TrimEnd('\r').StartsWith("#TPCJP-PATCH\t1", StringComparison.Ordinal))
                throw new InvalidDataException("翻訳データの形式が正しくありません");
            for (int i = 0; i < lines.Length; i++)
            {
                string line = lines[i].TrimEnd('\r');
                // build_patch.py never writes U+0000 (and no escape gives one): in a translation it would make the
                // written bundle fail its own check (a TextAsset may not contain NUL); other lines are refused alike
                if (line.IndexOf('\0') >= 0)
                    throw new InvalidDataException($"翻訳データ {i + 1} 行目の内容が正しくありません");
                if (i == 0 || line.Length == 0)
                    continue;
                var f = line.Split('\t');
                if (line[0] == '#')
                {
                    if (f.Length >= 2)
                    {
                        string name = f[0].Substring(1), value = Unescape(f[1]);
                        // the values the patcher uses go into the state file and the log one line each: no line break
                        // (an escaped \n could add a forged game_version to the state file) or other control character
                        if (UsedHeaders.Contains(name) && value.Any(IsControl))
                            throw new InvalidDataException($"翻訳データ {i + 1} 行目の内容が正しくありません");
                        data.Header[name] = value;
                    }
                    continue;
                }
                bool isRef = line[0] == '&';
                if (f.Length != (isRef ? 5 : 4))
                    throw new InvalidDataException($"翻訳データ {i + 1} 行目の形式が正しくありません");
                string source = isRef ? f[0].Substring(1) : f[0];
                if (!data.Sources.TryGetValue(source, out var list))
                    data.Sources[source] = list = new List<Entry>();
                var entry = new Entry { Key = Unescape(f[1]), Base = f[2] };
                if (isRef)
                {
                    entry.RefSource = f[3];
                    entry.RefKey = Unescape(f[4]);
                }
                else
                {
                    entry.Japanese = Unescape(f[3]);
                    entry.IsNoOp = BaseHash(entry.Japanese) == entry.Base;
                }
                // "Term=Value[i2t]Term=Value": a separator inside a key or value would change the game's term table
                if (entry.Key.Length == 0 || entry.Key.Contains("=") || entry.Key.Contains(Separator)
                    || (entry.Japanese != null && entry.Japanese.Contains(Separator))
                    || !IsBaseHash(entry.Base))
                    throw new InvalidDataException($"翻訳データ {i + 1} 行目の内容が正しくありません");
                list.Add(entry);
                data.Count++;
            }
            return data;
        }

        public const string Separator = "[i2t]";

        /// <summary>Header values the patcher uses (state file, log, notices); other header lines (#note, ...) may hold anything escaped.</summary>
        static readonly HashSet<string> UsedHeaders = new HashSet<string>(StringComparer.Ordinal) { "patch_version", "game_version", "bundle_sha256" };

        /// <summary>U+0000..U+001F, U+007F and the Unicode line breaks U+0085, U+2028, U+2029.</summary>
        static bool IsControl(char c) => c < 0x20 || c == 0x7F || c == 0x85 || c == 0x2028 || c == 0x2029;

        static bool IsBaseHash(string s)
        {
            if (s.Length != 16)
                return false;
            foreach (char c in s)
            {
                if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')))
                    return false;
            }
            return true;
        }

        static string Unescape(string s)
        {
            if (s.IndexOf('\\') < 0)
                return s;
            var sb = new StringBuilder(s.Length);
            for (int i = 0; i < s.Length; i++)
            {
                char c = s[i];
                if (c != '\\')
                {
                    sb.Append(c);
                    continue;
                }
                if (++i >= s.Length)
                    throw new InvalidDataException("翻訳データのエスケープが正しくありません");
                switch (s[i])
                {
                    case '\\': sb.Append('\\'); break;
                    case 'n': sb.Append('\n'); break;
                    case 'r': sb.Append('\r'); break;
                    case 't': sb.Append('\t'); break;
                    default: throw new InvalidDataException("翻訳データのエスケープが正しくありません");
                }
            }
            return sb.ToString();
        }

        /// <summary>First 16 hex digits of SHA-256 over the UTF-8 bytes (same as build_patch.py).</summary>
        public static string BaseHash(string value)
        {
            using (var sha = SHA256.Create())
            {
                var h = sha.ComputeHash(Encoding.UTF8.GetBytes(value));
                var sb = new StringBuilder(16);
                for (int i = 0; i < 8; i++)
                    sb.Append(h[i].ToString("x2"));
                return sb.ToString();
            }
        }
    }
}
