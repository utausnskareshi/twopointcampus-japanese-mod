using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;

namespace TpcJpPatch.Tests
{
    /// <summary>Minimal MessagePack writer for synthetic preferences.json files.</summary>
    static class Mp
    {
        public static byte[] Str(string s)
        {
            var b = Encoding.UTF8.GetBytes(s);
            if (b.Length < 32)
                return Cat(new[] { (byte)(0xA0 | b.Length) }, b);
            return Cat(new byte[] { 0xD9, (byte)b.Length }, b);
        }

        public static byte[] Str16(string s)
        {
            var b = Encoding.UTF8.GetBytes(s);
            return Cat(new byte[] { 0xDA, (byte)(b.Length >> 8), (byte)b.Length }, b);
        }

        public static byte[] Arr(params byte[][] items) => Cat(new[] { new[] { (byte)(0x90 | items.Length) } }.Concat(items).ToArray());
        public static byte[] Arr16(params byte[][] items) => Cat(new[] { new byte[] { 0xDC, 0, (byte)items.Length } }.Concat(items).ToArray());
        public static byte[] Map(params byte[][] kv) => Cat(new[] { new[] { (byte)(0x80 | kv.Length / 2) } }.Concat(kv).ToArray());
        public static byte[] Map16(params byte[][] kv) => Cat(new[] { new byte[] { 0xDE, 0, (byte)(kv.Length / 2) } }.Concat(kv).ToArray());
        public static byte[] Int(int v) => new[] { (byte)v };
        public static readonly byte[] True = { 0xC3 }, False = { 0xC2 }, Nil = { 0xC0 };

        public static byte[] Cat(params byte[][] parts)
        {
            var ms = new MemoryStream();
            foreach (var p in parts)
                ms.Write(p, 0, p.Length);
            return ms.ToArray();
        }

        /// <summary>[2, 0, {"_other": ..., "_languagePreferences": [2, 6, {...}]}] with the given encoded selected language.</summary>
        public static byte[] Prefs(byte[] selected, bool? hasExpected = true, bool wide = false, byte[] extraLangKv = null)
        {
            var lang = new List<byte[]>
            {
                Str("_selectedLanguage"), selected,
                Str("_selectedAudioLanguage"), Int(0),
            };
            if (hasExpected != null)
                lang.AddRange(new[] { Str("_hasExpectedPlatformLanguage"), hasExpected.Value ? True : False });
            lang.AddRange(new[] { Str("_expectedPlatformLanguage"), Int(0) });
            if (extraLangKv != null)
                lang.Add(extraLangKv);
            byte[] langMap = wide ? Map16(lang.ToArray()) : Map(lang.ToArray());
            byte[] langArr = wide ? Arr16(Int(2), Int(6), langMap) : Arr(Int(2), Int(6), langMap);
            byte[] top = wide
                ? Map16(Str16("_gamePreferences"), Arr(Int(2), Int(2), Map(Str("_x"), new byte[] { 0xCA, 0x3F, 0x80, 0, 0 })),
                    Str16("_languagePreferences"), langArr)
                : Map(Str("_gamePreferences"), Arr(Int(2), Int(2), Map(Str("_x"), new byte[] { 0xCA, 0x3F, 0x80, 0, 0 })),
                    Str("_languagePreferences"), langArr);
            return Arr(Int(2), Int(0), top);
        }
    }

    static class PreferencesTests
    {
        static readonly Random Rng = new Random(20261021);

        /// <summary>Calls ReadLanguage and SetLanguage on a copy of `bytes`; returns the unexpected exception, or null.</summary>
        static string Probe(string path, byte[] bytes, string label)
        {
            File.WriteAllBytes(path, bytes);
            try
            {
                Preferences.ReadLanguage(path);
            }
            catch (InvalidDataException)
            {
            }
            catch (Exception e)
            {
                return $"{label}: ReadLanguage threw {e.GetType().Name}: {e.Message}";
            }
            try
            {
                Preferences.SetLanguage(path, Preferences.Japanese);
                var after = File.ReadAllBytes(path);
                if (after.Length != bytes.Length)
                    return $"{label}: SetLanguage changed the file length";
                int first = -1, last = -1;
                for (int i = 0; i < after.Length; i++)
                    if (after[i] != bytes[i])
                    {
                        if (first < 0)
                            first = i;
                        last = i;
                    }
                if (first >= 0 && last - first >= 5)
                    return $"{label}: SetLanguage changed bytes {first}..{last}";
                if (Preferences.ReadLanguage(path).Selected != Preferences.Japanese)
                    return $"{label}: SetLanguage succeeded but the file does not read back as Japanese";
            }
            catch (InvalidDataException)
            {
                if (!File.ReadAllBytes(path).SequenceEqual(bytes))
                    return $"{label}: SetLanguage failed but changed the file";
            }
            catch (Exception e)
            {
                return $"{label}: SetLanguage threw {e.GetType().Name}: {e.Message}";
            }
            if (File.Exists(path + SafeFile.TempSuffix))
                return $"{label}: temp file left behind";
            return null;
        }

        public static void Run(Runner r, Ctx ctx)
        {
            string orig = Path.Combine(ctx.TempRoot, "preferences.orig.json");
            byte[] real = File.Exists(orig) ? File.ReadAllBytes(orig) : null;

            r.Test("Preferences: copy of the real preferences.json: ReadLanguage", () =>
            {
                if (real == null)
                    throw new SkipException("no copy at " + orig);
                string dir = ctx.Dir("prefs_real");
                string p = Path.Combine(dir, "preferences.json");
                File.WriteAllBytes(p, real);
                var info = Preferences.ReadLanguage(p);
                r.Note($"selected {info.Selected} ({Preferences.LanguageName(info.Selected)}), hasExpected {info.HasExpectedPlatformLanguage}");
                T.True(info.Selected >= 0 && info.Selected <= 12, "language id in range");
                T.True(info.HasExpectedPlatformLanguage, "_hasExpectedPlatformLanguage");
            });

            r.Test("Preferences: SetLanguage round trip on the real file copy restores identical bytes", () =>
            {
                if (real == null)
                    throw new SkipException("no copy");
                string dir = ctx.Dir("prefs_roundtrip");
                string p = Path.Combine(dir, "preferences.json");
                File.WriteAllBytes(p, real);
                long start = Preferences.ReadLanguage(p).Selected;
                int other = start == Preferences.Japanese ? Preferences.English : Preferences.Japanese;
                T.Eq(start, Preferences.SetLanguage(p, other), "returns the previous value");
                T.Eq((long)other, Preferences.ReadLanguage(p).Selected, "reads back");
                var mid = File.ReadAllBytes(p);
                T.Eq(real.Length, mid.Length, "same length");
                T.Eq(1, real.Zip(mid, (a, b) => a != b).Count(x => x), "exactly one byte differs (positive fixint)");
                foreach (int id in new[] { 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 0 })
                {
                    Preferences.SetLanguage(p, id);
                    T.Eq((long)id, Preferences.ReadLanguage(p).Selected, "language " + id);
                }
                Preferences.SetLanguage(p, (int)start);
                T.Bytes(real, File.ReadAllBytes(p), "bytes after the round trip");
                T.True(!File.Exists(p + SafeFile.TempSuffix), "no temp file");
                T.Bytes(real, File.ReadAllBytes(orig), "the source copy was not written");
            });

            r.Test("Preferences: every supported integer encoding is read and rewritten at the same width", () =>
            {
                string dir = ctx.Dir("prefs_enc");
                string p = Path.Combine(dir, "preferences.json");
                var encodings = new Dictionary<string, Tuple<byte[], long>>
                {
                    ["fixint 0"] = Tuple.Create(new byte[] { 0x00 }, 0L),
                    ["fixint 5"] = Tuple.Create(new byte[] { 0x05 }, 5L),
                    ["fixint 127"] = Tuple.Create(new byte[] { 0x7F }, 127L),
                    ["uint8"] = Tuple.Create(new byte[] { 0xCC, 0x03 }, 3L),
                    ["uint16"] = Tuple.Create(new byte[] { 0xCD, 0x00, 0x04 }, 4L),
                    ["uint32"] = Tuple.Create(new byte[] { 0xCE, 0, 0, 0, 0x09 }, 9L),
                    ["int8"] = Tuple.Create(new byte[] { 0xD0, 0x01 }, 1L),
                    ["int16"] = Tuple.Create(new byte[] { 0xD1, 0x00, 0x0B }, 11L),
                    ["int32"] = Tuple.Create(new byte[] { 0xD2, 0, 0, 0, 0x02 }, 2L),
                };
                foreach (bool wide in new[] { false, true })
                    foreach (var enc in encodings)
                    {
                        string what = enc.Key + (wide ? " (map16/array16/str16 containers)" : "");
                        var bytes = Mp.Prefs(enc.Value.Item1, wide: wide);
                        File.WriteAllBytes(p, bytes);
                        T.Eq(enc.Value.Item2, Preferences.ReadLanguage(p).Selected, what + ": read");
                        T.Eq(enc.Value.Item2, Preferences.SetLanguage(p, Preferences.Japanese), what + ": previous");
                        var after = File.ReadAllBytes(p);
                        T.Eq(bytes.Length, after.Length, what + ": width kept");
                        T.Eq(12L, Preferences.ReadLanguage(p).Selected, what + ": Japanese");
                        Preferences.SetLanguage(p, 0);
                        T.Eq(0L, Preferences.ReadLanguage(p).Selected, what + ": English");
                        Preferences.SetLanguage(p, (int)enc.Value.Item2);
                        T.Bytes(bytes, File.ReadAllBytes(p), what + ": back to the original bytes");
                    }
            });

            r.Test("Preferences: unsupported encodings and values that do not fit give InvalidDataException, file unchanged", () =>
            {
                string dir = ctx.Dir("prefs_enc_bad");
                string p = Path.Combine(dir, "preferences.json");
                var cases = new List<Tuple<string, byte[], int>>
                {
                    Tuple.Create("uint64", new byte[] { 0xCF, 0, 0, 0, 0, 0, 0, 0, 0x01 }, 12),
                    Tuple.Create("int64", new byte[] { 0xD3, 0, 0, 0, 0, 0, 0, 0, 0x01 }, 12),
                    Tuple.Create("negative fixint", new byte[] { 0xFF }, 12),
                    Tuple.Create("fixint, value 200", new byte[] { 0x01 }, 200),
                    Tuple.Create("fixint, value -1", new byte[] { 0x01 }, -1),
                    Tuple.Create("uint8, value 300", new byte[] { 0xCC, 0x01 }, 300),
                    Tuple.Create("int8, value 200", new byte[] { 0xD0, 0x01 }, 200),
                    Tuple.Create("uint16, value 70000", new byte[] { 0xCD, 0, 0x01 }, 70000),
                    Tuple.Create("float32", new byte[] { 0xCA, 0x3F, 0x80, 0, 0 }, 12),
                    Tuple.Create("string", Mp.Str("12"), 12),
                    Tuple.Create("nil", Mp.Nil, 12),
                };
                foreach (var c in cases)
                {
                    var bytes = Mp.Prefs(c.Item2);
                    File.WriteAllBytes(p, bytes);
                    T.Throws<InvalidDataException>(() => Preferences.SetLanguage(p, c.Item3), c.Item1);
                    T.Bytes(bytes, File.ReadAllBytes(p), c.Item1 + ": unchanged");
                    T.True(!File.Exists(p + SafeFile.TempSuffix), c.Item1 + ": no temp file");
                }
            });

            r.Test("Preferences: missing/false _hasExpectedPlatformLanguage refuses to write; ReadLanguage still works", () =>
            {
                string dir = ctx.Dir("prefs_expected");
                string p = Path.Combine(dir, "preferences.json");
                foreach (bool? has in new bool?[] { false, null })
                {
                    var bytes = Mp.Prefs(Mp.Int(0), hasExpected: has);
                    File.WriteAllBytes(p, bytes);
                    var info = Preferences.ReadLanguage(p);
                    T.Eq(0L, info.Selected, "read");
                    T.True(!info.HasExpectedPlatformLanguage, "flag");
                    var e = T.Throws<InvalidDataException>(() => Preferences.SetLanguage(p, 12), "hasExpected " + has);
                    T.Contains(e.Message, "一度ゲームを起動", "message tells the user to start the game once");
                    T.Bytes(bytes, File.ReadAllBytes(p), "unchanged");
                }
            });

            r.Test("Preferences: a read-only file is refused (InvalidDataException) and left as it was", () =>
            {
                string dir = ctx.Dir("prefs_ro");
                string p = Path.Combine(dir, "preferences.json");
                var bytes = Mp.Prefs(Mp.Int(0));
                File.WriteAllBytes(p, bytes);
                File.SetAttributes(p, FileAttributes.ReadOnly);
                var e = T.Throws<InvalidDataException>(() => Preferences.SetLanguage(p, 12), "read-only");
                T.Contains(e.Message, "読み取り専用", "message");
                T.True((File.GetAttributes(p) & FileAttributes.ReadOnly) != 0, "still read-only");
                T.Bytes(bytes, File.ReadAllBytes(p), "unchanged");
                File.SetAttributes(p, FileAttributes.Normal);
            });

            r.Test("Preferences: trailing data, wrong structure and empty files give InvalidDataException", () =>
            {
                string dir = ctx.Dir("prefs_struct");
                string p = Path.Combine(dir, "preferences.json");
                var good = Mp.Prefs(Mp.Int(0));
                var cases = new Dictionary<string, byte[]>
                {
                    ["empty"] = new byte[0],
                    ["trailing byte"] = Mp.Cat(good, new byte[] { 0x00 }),
                    ["trailing JSON text"] = Mp.Cat(good, Encoding.ASCII.GetBytes("{}")),
                    ["real JSON"] = Encoding.UTF8.GetBytes("{\"_selectedLanguage\": 12}"),
                    ["root is a map"] = Mp.Map(Mp.Str("_languagePreferences"), Mp.Int(1)),
                    ["root array too short"] = Mp.Arr(Mp.Int(2), Mp.Int(0)),
                    ["root is nil"] = Mp.Nil,
                    ["no _languagePreferences"] = Mp.Arr(Mp.Int(2), Mp.Int(0), Mp.Map(Mp.Str("_x"), Mp.Int(1))),
                    ["_languagePreferences not an array"] = Mp.Arr(Mp.Int(2), Mp.Int(0), Mp.Map(Mp.Str("_languagePreferences"), Mp.Int(1))),
                    ["language map missing"] = Mp.Arr(Mp.Int(2), Mp.Int(0), Mp.Map(Mp.Str("_languagePreferences"), Mp.Arr(Mp.Int(2), Mp.Int(6)))),
                    ["no _selectedLanguage"] = Mp.Arr(Mp.Int(2), Mp.Int(0), Mp.Map(Mp.Str("_languagePreferences"),
                        Mp.Arr(Mp.Int(2), Mp.Int(6), Mp.Map(Mp.Str("_hasExpectedPlatformLanguage"), Mp.True)))),
                    ["reserved byte 0xC1"] = new byte[] { 0x93, 0x02, 0x00, 0xC1 },
                    ["zeros"] = new byte[761],
                    ["0xFF bytes"] = Enumerable.Repeat((byte)0xFF, 761).ToArray(),
                };
                foreach (var c in cases)
                {
                    File.WriteAllBytes(p, c.Value);
                    T.Throws<InvalidDataException>(() => Preferences.ReadLanguage(p), c.Key + " (read)");
                    T.Throws<InvalidDataException>(() => Preferences.SetLanguage(p, 12), c.Key + " (set)");
                    T.Bytes(c.Value, File.ReadAllBytes(p), c.Key + ": unchanged");
                }
            });

            r.Test("Preferences: every truncation of the real file gives only InvalidDataException, file unchanged", () =>
            {
                var src = real ?? Mp.Prefs(Mp.Int(12));
                string dir = ctx.Dir("prefs_trunc");
                string p = Path.Combine(dir, "preferences.json");
                var problems = new List<string>();
                for (int len = 0; len < src.Length; len++)
                {
                    var bytes = src.Take(len).ToArray();
                    File.WriteAllBytes(p, bytes);
                    try
                    {
                        Preferences.ReadLanguage(p);
                        problems.Add($"length {len}: ReadLanguage accepted a truncated file");
                    }
                    catch (InvalidDataException) { }
                    catch (Exception e) { problems.Add($"length {len}: {e.GetType().Name}"); }
                    string pr = Probe(p, bytes, "length " + len);
                    if (pr != null)
                        problems.Add(pr);
                    else if (!File.ReadAllBytes(p).SequenceEqual(bytes))
                        problems.Add($"length {len}: SetLanguage wrote a truncated file");
                }
                T.True(problems.Count == 0, string.Join("; ", problems.Take(5)));
                r.Note($"{src.Length} truncations");
            });

            r.Test("Preferences: random byte flips/inserts/deletes give only InvalidDataException (or a clean 1-field change)", () =>
            {
                var src = real ?? Mp.Prefs(Mp.Int(12));
                string dir = ctx.Dir("prefs_fuzz");
                string p = Path.Combine(dir, "preferences.json");
                var problems = new List<string>();
                int accepted = 0;
                for (int i = 0; i < 4000; i++)
                {
                    var bytes = src.ToList();
                    int edits = 1 + Rng.Next(4);
                    for (int k = 0; k < edits; k++)
                    {
                        int pos = Rng.Next(bytes.Count);
                        switch (Rng.Next(3))
                        {
                            case 0: bytes[pos] = (byte)Rng.Next(256); break;
                            case 1: bytes.Insert(pos, (byte)Rng.Next(256)); break;
                            default: bytes.RemoveAt(pos); break;
                        }
                    }
                    string pr = Probe(p, bytes.ToArray(), "case " + i);
                    if (pr != null)
                        problems.Add(pr);
                    else
                    {
                        try
                        {
                            if (Preferences.ReadLanguage(p).Selected == 12)
                                accepted++;
                        }
                        catch (InvalidDataException)
                        {
                        }
                    }
                }
                r.Note($"4000 cases, {accepted} still valid after the edit");
                T.True(problems.Count == 0, problems.Count + " problems: " + string.Join("; ", problems.Take(5)));
            });

            r.Test("Preferences: huge length fields (str32/bin32/ext32 near 2^31) give InvalidDataException (CS-03)", () =>
            {
                string dir = ctx.Dir("prefs_huge");
                string p = Path.Combine(dir, "preferences.json");
                var problems = new List<string>();
                var headers = new[]
                {
                    new byte[] { 0xDB, 0x7F, 0xFF, 0xFF, 0xFF }, new byte[] { 0xDB, 0x7F, 0xFF, 0xFF, 0xF0 }, new byte[] { 0xDB, 0xFF, 0xFF, 0xFF, 0xFF },
                    new byte[] { 0xC6, 0x7F, 0xFF, 0xFF, 0xFF }, new byte[] { 0xC6, 0x7F, 0xFF, 0xFF, 0xF0 }, new byte[] { 0xC6, 0x80, 0, 0, 0 },
                    new byte[] { 0xC9, 0x7F, 0xFF, 0xFF, 0xFF, 0x01 }, new byte[] { 0xC9, 0x7F, 0xFF, 0xFF, 0xF8, 0x01 },
                    new byte[] { 0xDD, 0x7F, 0xFF, 0xFF, 0xFF }, new byte[] { 0xDF, 0x7F, 0xFF, 0xFF, 0xFF },
                    new byte[] { 0xDD, 0xFF, 0xFF, 0xFF, 0xFF }, new byte[] { 0xDF, 0x80, 0, 0, 0 },
                    new byte[] { 0xDA, 0xFF, 0xFF }, new byte[] { 0xC5, 0xFF, 0xFF }, new byte[] { 0xC8, 0xFF, 0xFF, 0x01 },
                    new byte[] { 0xDC, 0xFF, 0xFF }, new byte[] { 0xDE, 0xFF, 0xFF }, new byte[] { 0xD9, 0xFF }, new byte[] { 0xC4, 0xFF },
                };
                foreach (var h in headers)
                {
                    // as the value of a key in the language map, i.e. somewhere in the middle of the file
                    var bytes = Mp.Prefs(Mp.Int(0), extraLangKv: Mp.Cat(Mp.Str("_junk"), h, new byte[] { 0x41, 0x42, 0x43 }));
                    bytes[bytes.Length - 1 - (Mp.Str("_junk").Length + h.Length + 3)] |= 0; // keep the map count as built
                    string label = BitConverter.ToString(h);
                    string pr = Probe(p, FixMapCount(bytes), label);
                    if (pr != null)
                        problems.Add(pr);
                }
                T.True(problems.Count == 0, problems.Count + " problems: " + string.Join("; ", problems));
            });

            r.Test("Preferences: huge length fields give the 'truncated' message (no overflow) (CS-03)", () =>
            {
                string dir = ctx.Dir("prefs_huge_msg");
                string p = Path.Combine(dir, "preferences.json");
                foreach (var h in new[]
                         {
                             new byte[] { 0xDB, 0x7F, 0xFF, 0xFF, 0xF0 }, new byte[] { 0xDB, 0xFF, 0xFF, 0xFF, 0xFF }, new byte[] { 0xC6, 0x7F, 0xFF, 0xFF, 0xF0 },
                             new byte[] { 0xC9, 0x80, 0, 0, 0, 0x01 }, new byte[] { 0xDD, 0x7F, 0xFF, 0xFF, 0xFF }, new byte[] { 0xDF, 0xFF, 0xFF, 0xFF, 0xFF },
                         })
                {
                    var bytes = FixMapCount(Mp.Prefs(Mp.Int(0), extraLangKv: Mp.Cat(Mp.Str("_junk"), h, new byte[] { 0x41, 0x42, 0x43 })));
                    File.WriteAllBytes(p, bytes);
                    var e = T.Throws<InvalidDataException>(() => Preferences.ReadLanguage(p), BitConverter.ToString(h));
                    T.Contains(e.Message, "途中で切れています", BitConverter.ToString(h) + ": message");
                }
            });

            r.Test("Preferences: nested data (300 levels) gives InvalidDataException naming the nesting (CS-04)", () =>
            {
                string dir = ctx.Dir("prefs_nested");
                string p = Path.Combine(dir, "preferences.json");
                File.WriteAllBytes(p, Enumerable.Repeat((byte)0x91, 300).Concat(new byte[] { 0x00 }).ToArray());
                T.Contains(T.Throws<InvalidDataException>(() => Preferences.ReadLanguage(p), "nesting").Message, "入れ子が深すぎます", "message");
            });

            r.Test("Preferences: nesting limit is 32 levels: a value at depth 32 is read, at depth 33 rejected; real files are 4 deep", () =>
            {
                string dir = ctx.Dir("prefs_depth_limit");
                string p = Path.Combine(dir, "preferences.json");
                // the extra value of the language map sits at depth 4 (root array, top map, language array, language map)
                foreach (int k in new[] { 0, 1, 27, 28, 29, 30, 40 })
                {
                    byte[] nested = Enumerable.Repeat((byte)0x91, k).Concat(new byte[] { 0x07 }).ToArray(); // innermost value at depth 4 + k
                    var bytes = FixMapCount(Mp.Prefs(Mp.Int(3), extraLangKv: Mp.Cat(Mp.Str("_junk"), nested)));
                    File.WriteAllBytes(p, bytes);
                    if (4 + k <= 32)
                    {
                        T.Eq(3L, Preferences.ReadLanguage(p).Selected, $"depth {4 + k}: read");
                        T.Eq(3L, Preferences.SetLanguage(p, 12), $"depth {4 + k}: set");
                        T.Eq(12L, Preferences.ReadLanguage(p).Selected, $"depth {4 + k}: Japanese");
                    }
                    else
                    {
                        T.Contains(T.Throws<InvalidDataException>(() => Preferences.ReadLanguage(p), $"depth {4 + k}").Message, "入れ子が深すぎます", $"depth {4 + k}: message");
                        T.Throws<InvalidDataException>(() => Preferences.SetLanguage(p, 12), $"depth {4 + k}: set");
                        T.Bytes(bytes, File.ReadAllBytes(p), $"depth {4 + k}: unchanged");
                    }
                }
                // maps nest the same way (key and value of a map count one level deeper)
                byte[] maps = new byte[0];
                for (int i = 0; i < 29; i++)
                    maps = Mp.Map(Mp.Str("m"), maps.Length == 0 ? Mp.Int(1) : maps);
                File.WriteAllBytes(p, FixMapCount(Mp.Prefs(Mp.Int(3), extraLangKv: Mp.Cat(Mp.Str("_junk"), maps))));
                T.Contains(T.Throws<InvalidDataException>(() => Preferences.ReadLanguage(p), "29 nested maps (depth 33)").Message, "入れ子が深すぎます", "maps: message");
            });

            r.Test("Preferences: a key repeated inside a map gives InvalidDataException; the file is never written (FZ-12)", () =>
            {
                string dir = ctx.Dir("prefs_dupkeys");
                string p = Path.Combine(dir, "preferences.json");
                byte[] Lang(params byte[][] kv) => Mp.Arr(Mp.Int(2), Mp.Int(0), Mp.Map(Mp.Str("_languagePreferences"), Mp.Arr(Mp.Int(2), Mp.Int(6), Mp.Map(kv))));
                byte[] sel = Mp.Str("_selectedLanguage"), has = Mp.Str("_hasExpectedPlatformLanguage");
                var str8Sel = Mp.Cat(new byte[] { 0xD9, (byte)"_selectedLanguage".Length }, Encoding.UTF8.GetBytes("_selectedLanguage"));
                var cases = new Dictionary<string, byte[]>
                {
                    ["_selectedLanguage twice (English first, Japanese second)"] = Lang(sel, Mp.Int(0), has, Mp.True, sel, Mp.Int(12)),
                    ["_selectedLanguage twice, same value"] = Lang(sel, Mp.Int(0), sel, Mp.Int(0), has, Mp.True),
                    ["_selectedLanguage as fixstr and as str8"] = Lang(sel, Mp.Int(0), has, Mp.True, str8Sel, Mp.Int(1)),
                    ["_hasExpectedPlatformLanguage twice"] = Lang(sel, Mp.Int(0), has, Mp.True, has, Mp.False),
                    ["_languagePreferences twice at the top"] = Mp.Arr(Mp.Int(2), Mp.Int(0), Mp.Map(
                        Mp.Str("_languagePreferences"), Mp.Arr(Mp.Int(2), Mp.Int(6), Mp.Map(sel, Mp.Int(0), has, Mp.True)),
                        Mp.Str("_languagePreferences"), Mp.Arr(Mp.Int(2), Mp.Int(6), Mp.Map(sel, Mp.Int(12), has, Mp.True)))),
                    ["integer key 1 as fixint and as uint8 in another map"] = Mp.Arr(Mp.Int(2), Mp.Int(0), Mp.Map(
                        Mp.Str("_other"), Mp.Map(Mp.Int(1), Mp.Int(5), new byte[] { 0xCC, 0x01 }, Mp.Int(6)),
                        Mp.Str("_languagePreferences"), Mp.Arr(Mp.Int(2), Mp.Int(6), Mp.Map(sel, Mp.Int(0), has, Mp.True)))),
                };
                foreach (var c in cases)
                {
                    File.WriteAllBytes(p, c.Value);
                    T.Contains(T.Throws<InvalidDataException>(() => Preferences.ReadLanguage(p), c.Key).Message, "重複", c.Key + ": message");
                    T.Throws<InvalidDataException>(() => Preferences.SetLanguage(p, 12), c.Key + " (set)");
                    T.Bytes(c.Value, File.ReadAllBytes(p), c.Key + ": unchanged");
                }
                // nil keys are skipped, not stored: two of them are not a duplicate
                File.WriteAllBytes(p, Lang(sel, Mp.Int(0), has, Mp.True, Mp.Nil, Mp.Int(1), Mp.Nil, Mp.Int(2)));
                T.Eq(0L, Preferences.ReadLanguage(p).Selected, "two nil keys are accepted");
                // equal keys in different maps are fine (the real file has _x in several maps)
                File.WriteAllBytes(p, Mp.Prefs(Mp.Int(0), extraLangKv: null));
                T.Eq(0L, Preferences.ReadLanguage(p).Selected, "control");
            });

            // Deeper nesting used to overflow the stack (CS-04), which no catch handles: it still runs in a child process
            // so that a regression is reported as a failure instead of killing the test run.
            foreach (int depth in new[] { 5000, 20000, 1000000 })
            {
                r.Test($"Preferences: deeply nested data ({depth:N0} levels of 0x91) is rejected without crashing the process (CS-04)", () =>
                {
                    string dir = ctx.Dir("prefs_deep_" + depth);
                    string p = Path.Combine(dir, "preferences.json");
                    File.WriteAllBytes(p, Enumerable.Repeat((byte)0x91, depth).Concat(new byte[] { 0x00 }).ToArray());
                    var psi = new ProcessStartInfo(Process.GetCurrentProcess().MainModule.FileName, $"--child-preferences \"{p}\"")
                    {
                        UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
                        StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
                    };
                    using (var proc = Process.Start(psi))
                    {
                        var err = proc.StandardError.ReadToEndAsync();
                        string output = (proc.StandardOutput.ReadToEnd() + err.Result).Trim().Split('\n')[0].Trim();
                        proc.WaitForExit();
                        r.Note($"child exit code 0x{proc.ExitCode:X8}: {output}");
                        T.True(proc.ExitCode == 0, $"child process crashed (exit 0x{proc.ExitCode:X8}: {output}) instead of reporting InvalidDataException");
                        T.Contains(output, "入れ子が深すぎます", "child output");
                    }
                });
            }
        }

        /// <summary>Mp.Prefs appends extraLangKv as one item: bump the language map's count by one more pair-half.</summary>
        static byte[] FixMapCount(byte[] bytes)
        {
            // The language map is the last fixmap (0x84 / 0x85) before "_selectedLanguage" in the file.
            byte[] key = Mp.Str("_selectedLanguage");
            for (int i = 1; i + key.Length <= bytes.Length; i++)
            {
                if (bytes.Skip(i).Take(key.Length).SequenceEqual(key))
                {
                    // extraLangKv holds a key and a value but Mp.Map counted it as half a pair; fix the count.
                    byte b = bytes[i - 1];
                    if ((b & 0xF0) == 0x80)
                    {
                        var copy = (byte[])bytes.Clone();
                        copy[i - 1] = (byte)(0x80 | 5);
                        return copy;
                    }
                }
            }
            throw new InvalidOperationException("language map not found");
        }

        /// <summary>Child process for the deep-nesting test: prints the outcome of ReadLanguage.</summary>
        public static int Child(string path)
        {
            try
            {
                Preferences.ReadLanguage(path);
                Console.WriteLine("accepted");
                return 2;
            }
            catch (InvalidDataException e)
            {
                Console.WriteLine("InvalidDataException: " + e.Message);
                return 0;
            }
            catch (Exception e)
            {
                Console.WriteLine(e.GetType().FullName + ": " + e.Message);
                return 3;
            }
        }
    }
}
