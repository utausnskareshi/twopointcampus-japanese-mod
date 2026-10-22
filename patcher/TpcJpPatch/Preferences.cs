using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace TpcJpPatch
{
    /// <summary>
    /// In-place editing of the language setting in the game's preferences.json (MessagePack, not JSON):
    /// [2, 0, {..., "_languagePreferences": [2, 6, {"_selectedLanguage": int, "_selectedAudioLanguage": int,
    /// "_hasExpectedPlatformLanguage": bool, "_expectedPlatformLanguage": int}]}]
    /// The game applies _selectedLanguage at startup without checking it against the languages
    /// offered in the settings menu, so only that integer is rewritten (same encoded width).
    /// </summary>
    static class Preferences
    {
        public const int English = 0;
        public const int Japanese = 12;

        static readonly Dictionary<int, string> Names = new Dictionary<int, string>
        {
            [0] = "英語", [1] = "フランス語", [2] = "イタリア語", [3] = "ドイツ語", [4] = "スペイン語",
            [5] = "ポーランド語", [7] = "中国語（簡体字）", [8] = "中国語（繁体字）", [9] = "韓国語",
            [10] = "ポルトガル語（ブラジル）", [11] = "トルコ語", [12] = "日本語",
        };

        public static string LanguageName(long id) => Names.TryGetValue((int)id, out var n) ? n : $"不明({id})";

        /// <summary>An ID of the game's 13 languages (0..12; 6, Russian, has no name above).</summary>
        public static bool IsGameLanguage(long id) => id >= English && id <= Japanese;

        sealed class Node
        {
            public object Value;
            public int Start;
            public int End;
        }

        public sealed class LanguageInfo
        {
            public long Selected;
            public bool HasExpectedPlatformLanguage;
        }

        public static LanguageInfo ReadLanguage(string path)
        {
            var lang = LanguageMap(File.ReadAllBytes(path));
            var info = new LanguageInfo();
            if (lang.TryGetValue("_selectedLanguage", out var sel) && sel.Value is long l)
                info.Selected = l;
            else
                throw new InvalidDataException("_selectedLanguage が見つかりません");
            info.HasExpectedPlatformLanguage =
                lang.TryGetValue("_hasExpectedPlatformLanguage", out var h) && h.Value is bool b && b;
            return info;
        }

        /// <summary>Rewrites _selectedLanguage and returns the previous value.</summary>
        public static long SetLanguage(string path, int languageId)
        {
            byte[] buf = File.ReadAllBytes(path);
            var lang = LanguageMap(buf);
            if (!lang.TryGetValue("_selectedLanguage", out var node) || !(node.Value is long old))
                throw new InvalidDataException("_selectedLanguage が見つかりません");
            if (!(lang.TryGetValue("_hasExpectedPlatformLanguage", out var h) && h.Value is bool b && b))
                throw new InvalidDataException("ゲームの言語設定がまだ作られていません。一度ゲームを起動してタイトル画面まで進めてください");
            byte[] encoded = EncodeLike(buf[node.Start], languageId);
            if (encoded.Length != node.End - node.Start)
                throw new InvalidDataException("言語設定の形式が想定と異なります");
            Buffer.BlockCopy(encoded, 0, buf, node.Start, encoded.Length);
            if (!(LanguageMap(buf)["_selectedLanguage"].Value is long check) || check != languageId)
                throw new InvalidDataException("言語設定の書き換えを確認できませんでした");
            // A read-only preferences.json was made so on purpose (the game cannot save it either): leave it to the user.
            if ((File.GetAttributes(path) & FileAttributes.ReadOnly) != 0)
                throw new InvalidDataException("設定ファイル（preferences.json）が読み取り専用になっています。ファイルのプロパティで読み取り専用を外してから、もう一度実行してください");
            SafeFile.Write(path, buf);
            return old;
        }

        static Dictionary<object, Node> LanguageMap(byte[] buf)
        {
            var root = Read(buf, 0);
            if (root.End != buf.Length)
                throw new InvalidDataException("preferences.json の末尾に余分なデータがあります");
            try
            {
                var top = (Dictionary<object, Node>)((List<Node>)root.Value)[2].Value;
                var langArr = (List<Node>)top["_languagePreferences"].Value;
                return (Dictionary<object, Node>)langArr[2].Value;
            }
            catch (Exception e) when (e is InvalidCastException || e is KeyNotFoundException
                                      || e is ArgumentOutOfRangeException || e is NullReferenceException)
            {
                throw new InvalidDataException("preferences.json の形式が想定と異なります");
            }
        }

        static byte[] EncodeLike(byte oldHeader, int value)
        {
            if (oldHeader <= 0x7F)
            {
                if (value < 0 || value > 0x7F)
                    throw new InvalidDataException("言語IDを同じ形式で書き込めません");
                return new[] { (byte)value };
            }
            switch (oldHeader)
            {
                case 0xCC: return new[] { oldHeader, (byte)value };
                case 0xCD: return new[] { oldHeader, (byte)(value >> 8), (byte)value };
                case 0xCE:
                case 0xD2:
                    return new[] { oldHeader, (byte)(value >> 24), (byte)(value >> 16), (byte)(value >> 8), (byte)value };
                case 0xD0: return new[] { oldHeader, (byte)(sbyte)value };
                case 0xD1: return new[] { oldHeader, (byte)(value >> 8), (byte)value };
                default: throw new InvalidDataException($"言語IDの形式 0x{oldHeader:X2} には対応していません");
            }
        }

        // ---- minimal MessagePack reader that keeps byte offsets ----
        // Lengths are checked against the remaining bytes without overflow, nesting is limited (the real file is
        // 3 levels deep) and a key repeated inside a map is rejected, so a damaged file gives InvalidDataException.

        const int MaxDepth = 32;

        static InvalidDataException Truncated() => new InvalidDataException("preferences.json が途中で切れています");

        static ulong BE(byte[] b, int pos, int size)
        {
            if (pos < 0 || size > b.Length - pos)
                throw Truncated();
            ulong v = 0;
            for (int i = 0; i < size; i++)
                v = (v << 8) | b[pos + i];
            return v;
        }

        static int Length(byte[] b, int pos, int size)
        {
            ulong n = BE(b, pos, size);
            if (n > int.MaxValue)
                throw Truncated();
            return (int)n;
        }

        static Node Read(byte[] b, int pos, int depth = 0)
        {
            if (pos >= b.Length)
                throw Truncated();
            if (depth > MaxDepth)
                throw new InvalidDataException("preferences.json の入れ子が深すぎます（壊れている可能性があります）");
            int start = pos;
            byte t = b[pos];
            Node N(object v, int end) => new Node { Value = v, Start = start, End = end };

            if (t <= 0x7F) return N((long)t, pos + 1);
            if (t >= 0xE0) return N((long)(sbyte)t, pos + 1);
            if (t >= 0x80 && t <= 0x8F) return ReadMap(b, pos + 1, t & 0x0F, start, depth);
            if (t >= 0x90 && t <= 0x9F) return ReadArray(b, pos + 1, t & 0x0F, start, depth);
            if (t >= 0xA0 && t <= 0xBF) return ReadStr(b, pos + 1, t & 0x1F, start);
            switch (t)
            {
                case 0xC0: return N(null, pos + 1);
                case 0xC2: return N(false, pos + 1);
                case 0xC3: return N(true, pos + 1);
                case 0xCC: return N((long)BE(b, pos + 1, 1), pos + 2);
                case 0xCD: return N((long)BE(b, pos + 1, 2), pos + 3);
                case 0xCE: return N((long)BE(b, pos + 1, 4), pos + 5);
                case 0xCF: return N((long)BE(b, pos + 1, 8), pos + 9);
                case 0xD0: return N((long)(sbyte)BE(b, pos + 1, 1), pos + 2);
                case 0xD1: return N((long)(short)BE(b, pos + 1, 2), pos + 3);
                case 0xD2: return N((long)(int)BE(b, pos + 1, 4), pos + 5);
                case 0xD3: return N((long)BE(b, pos + 1, 8), pos + 9);
                case 0xCA: return N(BitConverter.ToSingle(BitConverter.GetBytes((uint)BE(b, pos + 1, 4)), 0), pos + 5);
                case 0xCB: return N(BitConverter.Int64BitsToDouble((long)BE(b, pos + 1, 8)), pos + 9);
                case 0xD9: return ReadStr(b, pos + 2, Length(b, pos + 1, 1), start);
                case 0xDA: return ReadStr(b, pos + 3, Length(b, pos + 1, 2), start);
                case 0xDB: return ReadStr(b, pos + 5, Length(b, pos + 1, 4), start);
                case 0xC4: return ReadBin(b, pos + 2, Length(b, pos + 1, 1), start);
                case 0xC5: return ReadBin(b, pos + 3, Length(b, pos + 1, 2), start);
                case 0xC6: return ReadBin(b, pos + 5, Length(b, pos + 1, 4), start);
                case 0xDC: return ReadArray(b, pos + 3, Length(b, pos + 1, 2), start, depth);
                case 0xDD: return ReadArray(b, pos + 5, Length(b, pos + 1, 4), start, depth);
                case 0xDE: return ReadMap(b, pos + 3, Length(b, pos + 1, 2), start, depth);
                case 0xDF: return ReadMap(b, pos + 5, Length(b, pos + 1, 4), start, depth);
                case 0xD4: return ReadBin(b, pos + 2, 1, start);
                case 0xD5: return ReadBin(b, pos + 2, 2, start);
                case 0xD6: return ReadBin(b, pos + 2, 4, start);
                case 0xD7: return ReadBin(b, pos + 2, 8, start);
                case 0xD8: return ReadBin(b, pos + 2, 16, start);
                case 0xC7: return ReadBin(b, pos + 3, Length(b, pos + 1, 1), start);
                case 0xC8: return ReadBin(b, pos + 4, Length(b, pos + 1, 2), start);
                case 0xC9: return ReadBin(b, pos + 6, Length(b, pos + 1, 4), start);
            }
            throw new InvalidDataException($"preferences.json に未対応のデータ (0x{t:X2}) があります");
        }

        static void CheckRemaining(byte[] b, int pos, int n)
        {
            if (n < 0 || pos < 0 || pos > b.Length || n > b.Length - pos)
                throw Truncated();
        }

        static Node ReadStr(byte[] b, int pos, int n, int start)
        {
            CheckRemaining(b, pos, n);
            return new Node { Value = Encoding.UTF8.GetString(b, pos, n), Start = start, End = pos + n };
        }

        static Node ReadBin(byte[] b, int pos, int n, int start)
        {
            CheckRemaining(b, pos, n);
            var data = new byte[n];
            Buffer.BlockCopy(b, pos, data, 0, n);
            return new Node { Value = data, Start = start, End = pos + n };
        }

        static Node ReadArray(byte[] b, int pos, int n, int start, int depth)
        {
            CheckRemaining(b, pos, n); // every element takes at least one byte
            var items = new List<Node>(n);
            for (int i = 0; i < n; i++)
            {
                var item = Read(b, pos, depth + 1);
                items.Add(item);
                pos = item.End;
            }
            return new Node { Value = items, Start = start, End = pos };
        }

        static Node ReadMap(byte[] b, int pos, int n, int start, int depth)
        {
            CheckRemaining(b, pos, n);
            var map = new Dictionary<object, Node>();
            for (int i = 0; i < n; i++)
            {
                var k = Read(b, pos, depth + 1);
                var v = Read(b, k.End, depth + 1);
                if (k.Value != null)
                {
                    // a repeated key would leave the game reading a value this tool did not change
                    if (map.ContainsKey(k.Value))
                        throw new InvalidDataException($"preferences.json に同じ項目（{k.Value}）が重複しています");
                    map.Add(k.Value, v);
                }
                pos = v.End;
            }
            return new Node { Value = map, Start = start, End = pos };
        }
    }
}
