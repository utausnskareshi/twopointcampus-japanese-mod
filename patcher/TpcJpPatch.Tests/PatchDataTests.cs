using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;

namespace TpcJpPatch.Tests
{
    static class PatchDataTests
    {
        static readonly MethodInfo UnescapeMethod =
            typeof(PatchData).GetMethod("Unescape", BindingFlags.NonPublic | BindingFlags.Static);

        static string Unescape(string s)
        {
            try
            {
                return (string)UnescapeMethod.Invoke(null, new object[] { s });
            }
            catch (TargetInvocationException e)
            {
                throw e.InnerException;
            }
        }

        static IEnumerable<PatchData.Entry> All(PatchData d) => d.Sources.SelectMany(s => s.Value);

        public static void Run(Runner r, Ctx ctx)
        {
            r.Test("PatchData: the shipped dist/data/TpcJpPatch.data.tsv (count, references, no-ops, header)", () =>
            {
                var d = ctx.Data;
                T.Eq(2391, d.Count, "entries");
                T.Eq(d.Header["entries"], d.Count.ToString(), "#entries header");
                T.Eq(57, All(d).Count(e => e.RefSource != null), "reference entries");
                T.Eq(2, All(d).Count(e => e.IsNoOp), "no-op entries");
                T.True(All(d).Where(e => e.RefSource != null).All(e => e.Japanese == null && !e.IsNoOp && e.RefKey != null), "references carry no text");
                T.True(All(d).Where(e => e.RefSource == null).All(e => e.Japanese != null && e.RefKey == null), "inline entries carry text");
                T.True(All(d).All(e => e.Base.Length == 16 && e.Base.All(Uri.IsHexDigit)), "base hashes are 16 hex digits");
                T.Eq(global::TpcJpPatch.Program.Version, d.PatchVersion, "patch_version = the exe's version (csproj <Version>)");
                T.Eq("10.3.169253+2024-12-06.1241", d.GameVersion, "game_version");
                T.Eq(Util.Sha256(ctx.Official), d.BundleSha256, "bundle_sha256 = official copy");
                T.Eq(ctx.DataPath, d.Origin, "origin");
                r.Note($"{d.Sources.Count} sources");
            });

            r.Test("PatchData: every entry equals the independent Python decoding of the same file", () =>
            {
                var d = ctx.Data;
                var perSource = d.Sources.ToDictionary(s => s.Key, s => new Queue<PatchData.Entry>(s.Value));
                int n = 0;
                foreach (var line in File.ReadAllLines(Path.Combine(ctx.VectorsDir, "patchdata_expect.tsv"), Encoding.UTF8))
                {
                    var f = line.Split('\t');
                    if (f[0] == "H")
                        T.Eq(Util.B64(f[2]), d.Header[f[1]], "header " + f[1]);
                    else if (f[0] == "E")
                    {
                        var e = perSource[f[1]].Dequeue();
                        string where = f[1] + "|" + Util.B64(f[2]);
                        T.Eq(Util.B64(f[2]), e.Key, "key");
                        T.Eq(f[3], e.Base, "base of " + where);
                        T.Eq(Util.B64(f[4]), e.Japanese, "japanese of " + where);
                        T.Eq(f[5] == "-" ? null : f[5], e.RefSource, "ref source of " + where);
                        T.Eq(Util.B64(f[6]), e.RefKey, "ref key of " + where);
                        T.Eq(f[7] == "1", e.IsNoOp, "no-op of " + where);
                        if (e.Japanese != null)
                            T.Eq(e.IsNoOp, PatchData.BaseHash(e.Japanese) == e.Base, "BaseHash consistency of " + where);
                        n++;
                    }
                    else if (f[0] == "S")
                    {
                        T.Eq(int.Parse(f[1]), d.Count, "count");
                        T.Eq(int.Parse(f[2]), All(d).Count(e => e.RefSource != null), "refs");
                        T.Eq(int.Parse(f[3]), All(d).Count(e => e.IsNoOp), "no-ops");
                    }
                }
                T.True(perSource.All(q => q.Value.Count == 0), "C# has entries Python does not");
                T.Eq(d.Count, n, "entries compared");
            });

            r.Test("PatchData: the embedded copy equals the file (Load() without a data file next to the exe)", () =>
            {
                T.True(!File.Exists(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, PatchData.FileName)), "no data file next to the test exe");
                var emb = PatchData.Load();
                T.Eq("（実行ファイルに内蔵）", emb.Origin, "origin");
                T.Eq(ctx.Data.Count, emb.Count, "count");
                var a = All(ctx.Data).ToList();
                var b = All(emb).ToList();
                for (int i = 0; i < a.Count; i++)
                    T.True(a[i].Key == b[i].Key && a[i].Base == b[i].Base && a[i].Japanese == b[i].Japanese
                           && a[i].RefSource == b[i].RefSource && a[i].RefKey == b[i].RefKey, "entry " + a[i].Key);
                T.True(ctx.Data.Header.Where(h => h.Key != "built").All(h => emb.Header[h.Key] == h.Value), "header");
            });

            r.Test("PatchData: Unescape and BaseHash match tools/build_patch.py esc()/base_hash on random strings", () =>
            {
                int n = 0;
                foreach (var line in File.ReadAllLines(Path.Combine(ctx.VectorsDir, "escape_vectors.tsv"), Encoding.UTF8))
                {
                    if (line.Length == 0)
                        continue;
                    var f = line.Split('\t');
                    T.Eq(3, f.Length, "vector line shape");
                    string want = Util.B64(f[1]);
                    T.True(Unescape(f[0]) == want, "unescape of " + T.Show(f[0]));
                    T.Eq(f[2], PatchData.BaseHash(want), "BaseHash of " + T.Show(want));
                    n++;
                }
                T.True(n > 3000, "vector count " + n);
                r.Note($"{n} vectors");
            });

            r.Test("PatchData: a data file of random escaped keys/texts (written like build_patch.py) parses exactly", () =>
            {
                string path = Path.Combine(ctx.VectorsDir, "escape_patch.tsv");
                var lines = File.ReadAllText(path, Encoding.UTF8).Split('\n').ToList();
                var want = File.ReadAllLines(Path.Combine(ctx.VectorsDir, "escape_patch_expect.tsv"), Encoding.UTF8).Where(l => l.Length > 0).ToList();
                int firstEntry = lines.FindIndex(l => l.StartsWith("I2LS_Fuzz\t", StringComparison.Ordinal) || l.StartsWith("&I2LS_Fuzz\t", StringComparison.Ordinal));
                T.Eq(want.Count, lines.Count - 1 - firstEntry, "test precondition (one expected line per entry line, the entries last)");
                // the random keys/texts may contain U+0000, which the patcher refuses (fuzz finding
                // nul_translation_blamed_on_game_file): the file is refused at its first such line, and without
                // those lines (#entries corrected) it must parse exactly
                int firstNul = lines.FindIndex(l => l.IndexOf('\0') >= 0), removed = 0;
                if (firstNul >= 0)
                {
                    var e = T.Throws<InvalidDataException>(() => PatchData.Load(path), "the generated file with U+0000");
                    T.Eq(NulMessage(firstNul + 1), e.Message, "the generated file with U+0000: message");
                    for (int i = want.Count - 1; i >= 0; i--)
                    {
                        if (lines[firstEntry + i].IndexOf('\0') >= 0)
                        {
                            lines.RemoveAt(firstEntry + i);
                            want.RemoveAt(i);
                            removed++;
                        }
                    }
                    lines[lines.FindIndex(l => l.StartsWith("#entries\t", StringComparison.Ordinal))] = "#entries\t" + want.Count;
                    path = Path.Combine(ctx.Dir("patchdata_escape_no_nul"), "escape_patch.tsv");
                    File.WriteAllText(path, string.Join("\n", lines), new UTF8Encoding(false));
                }
                r.Note($"{want.Count} entries ({removed} with U+0000 left out)");
                T.True(want.Count > 1000, "entries compared");
                var d = PatchData.Load(path);
                T.Eq("header\twith\\escapes\n", d.Header["note"], "escaped header value");
                T.Eq("9.9.9-test", d.PatchVersion, "patch_version");
                var got = d.Sources["I2LS_Fuzz"];
                T.Eq(want.Count, got.Count, "entries");
                T.Eq(want.Count, d.Count, "count");
                for (int i = 0; i < want.Count; i++)
                {
                    var f = want[i].Split('\t');
                    var e = got[i];
                    T.True(e.Key == Util.B64(f[1]), "key " + i);
                    T.Eq(f[2], e.Base, "base " + i);
                    T.True(e.Japanese == (f[0] == "I" ? Util.B64(f[3]) : null), "japanese " + i);
                    T.Eq(f[0] == "R" ? "I2LS_Ref" : null, e.RefSource, "ref source " + i);
                    T.True(e.RefKey == (f[0] == "R" ? Util.B64(f[5]) : null), "ref key " + i);
                    T.Eq(f[6] == "1", e.IsNoOp, "no-op " + i);
                }
            });

            r.Test("PatchData: CRLF line endings and a UTF-8 BOM give the same data", () =>
            {
                string dir = ctx.Dir("patchdata_variants");
                string text = File.ReadAllText(ctx.DataPath, Encoding.UTF8);
                string crlf = Path.Combine(dir, "crlf.tsv");
                File.WriteAllText(crlf, text.Replace("\n", "\r\n"), new UTF8Encoding(false));
                string bom = Path.Combine(dir, "bom.tsv");
                File.WriteAllText(bom, text, new UTF8Encoding(true));
                foreach (var p in new[] { crlf, bom })
                {
                    var d = PatchData.Load(p);
                    T.Eq(ctx.Data.Count, d.Count, Path.GetFileName(p) + " count");
                    T.True(All(ctx.Data).Zip(All(d), (a, b) => a.Key == b.Key && a.Japanese == b.Japanese && a.RefKey == b.RefKey).All(x => x),
                        Path.GetFileName(p) + " entries");
                }
            });

            // Each malformed case must be rejected for its own reason (the expected message part), so the
            // well-formed parts carry a correct #entries count.
            const string Bad = "形式が正しくありません", BadLine = "行目の形式が正しくありません", BadEsc = "エスケープが正しくありません",
                BadContent = "行目の内容が正しくありません", Count = "途中で切れているか、壊れています",
                NoEol = "途中で切れています（最後の行に改行がありません）";
            string Ok(int entries) => $"#TPCJP-PATCH\t1\n#patch_version\t1\n#entries\t{entries}\n";

            r.Test("PatchData: a minimal well-formed file (with #entries) loads (control for the malformed cases)", () =>
            {
                string dir = ctx.Dir("patchdata_ok");
                string p = Path.Combine(dir, "ok.tsv");
                File.WriteAllText(p, Ok(2) + "I2LS_UI\tk\t0123456789abcdef\t訳\n&I2LS_UI\tk2\tfedcba9876543210\tI2LS_UI\tk=ref[i2t]\n", new UTF8Encoding(false));
                var d = PatchData.Load(p);
                T.Eq(2, d.Count, "count");
                T.Eq("k=ref[i2t]", d.Sources["I2LS_UI"][1].RefKey, "a reference key is not checked for '=' / '[i2t]' (it is only looked up)");
            });

            r.Test("PatchData: malformed files give InvalidDataException for the specific reason", () =>
            {
                string dir = ctx.Dir("patchdata_bad");
                string ok = Ok(1);
                var cases = new Dictionary<string, Tuple<string, string>>
                {
                    ["empty"] = Tuple.Create("", NoEol), // CS-06: no final newline (was: 形式が正しくありません)
                    ["newline only"] = Tuple.Create("\n", Bad),
                    ["no header"] = Tuple.Create("I2LS_UI\tk\t0123456789abcdef\t訳\n", Bad),
                    ["wrong format version"] = Tuple.Create("#TPCJP-PATCH\t2\nI2LS_UI\tk\t0123456789abcdef\t訳\n", Bad),
                    ["header with space"] = Tuple.Create("#TPCJP-PATCH 1\n", Bad),
                    ["3 fields"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcdef\n", BadLine),
                    ["5 fields inline"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcdef\t訳\textra\n", BadLine),
                    ["ref with 4 fields"] = Tuple.Create(ok + "&I2LS_UI\tk\t0123456789abcdef\tI2LS_UI\n", BadLine),
                    ["ref with 6 fields"] = Tuple.Create(ok + "&I2LS_UI\tk\t0123456789abcdef\tI2LS_UI\tk2\tx\n", BadLine),
                    ["bad escape \\x"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcdef\t訳\\x\n", BadEsc),
                    ["trailing backslash"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcdef\t訳\\\n", BadEsc),
                    ["bad escape in key"] = Tuple.Create(ok + "I2LS_UI\tk\\q\t0123456789abcdef\t訳\n", BadEsc),
                    ["bad escape in ref key"] = Tuple.Create(ok + "&I2LS_UI\tk\t0123456789abcdef\tI2LS_UI\tk\\\n", BadEsc),
                    ["uppercase escape \\N"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcdef\t訳\\N\n", BadEsc),
                    ["bad escape in header"] = Tuple.Create(ok + "#x\tval\\z\nI2LS_UI\tk\t0123456789abcdef\t訳\n", BadEsc),
                    // FZ-07: what would change the game's "Term=Value[i2t]Term=Value" table, or a base that is no hash
                    ["empty key"] = Tuple.Create(ok + "I2LS_UI\t\t0123456789abcdef\t訳\n", BadContent),
                    ["empty key (reference)"] = Tuple.Create(ok + "&I2LS_UI\t\t0123456789abcdef\tI2LS_UI\tk\n", BadContent),
                    ["key with '='"] = Tuple.Create(ok + "I2LS_UI\tk=v\t0123456789abcdef\t訳\n", BadContent),
                    ["key with [i2t]"] = Tuple.Create(ok + "I2LS_UI\tk[i2t]x\t0123456789abcdef\t訳\n", BadContent),
                    ["reference key with [i2t]"] = Tuple.Create(ok + "&I2LS_UI\t[i2t]\t0123456789abcdef\tI2LS_UI\tk\n", BadContent),
                    ["translation with [i2t]"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcdef\t訳[i2t]Other=値\n", BadContent),
                    ["translation with escaped CR/LF around [i2t]"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcdef\t訳\\n[i2t]\\r\n", BadContent),
                    ["base 15 digits"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcde\t訳\n", BadContent),
                    ["base 17 digits"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcdef0\t訳\n", BadContent),
                    ["base upper case"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789ABCDEF\t訳\n", BadContent),
                    ["base not hex"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcdeg\t訳\n", BadContent),
                    ["base empty (reference)"] = Tuple.Create(ok + "&I2LS_UI\tk\t\tI2LS_UI\tk2\n", BadContent),
                    // CS-02: the declared count must be present and match
                    ["#entries missing"] = Tuple.Create("#TPCJP-PATCH\t1\nI2LS_UI\tk\t0123456789abcdef\t訳\n", Count),
                    ["#entries 2, 1 line"] = Tuple.Create(Ok(2) + "I2LS_UI\tk\t0123456789abcdef\t訳\n", Count),
                    ["#entries 0, 1 line"] = Tuple.Create(Ok(0) + "I2LS_UI\tk\t0123456789abcdef\t訳\n", Count),
                    ["#entries not a number"] = Tuple.Create("#TPCJP-PATCH\t1\n#entries\tabc\nI2LS_UI\tk\t0123456789abcdef\t訳\n", Count),
                    ["#entries 1, header only"] = Tuple.Create(ok, Count),
                };
                foreach (var c in cases)
                {
                    string p = Path.Combine(dir, "case.tsv");
                    File.WriteAllText(p, c.Value.Item1, new UTF8Encoding(false));
                    var e = T.Throws<InvalidDataException>(() => PatchData.Load(p), c.Key);
                    T.Contains(e.Message, c.Value.Item2, c.Key + ": message");
                }
                T.Throws<IOException>(() => PatchData.Load(Path.Combine(dir, "missing.tsv")), "missing file is an IOException (the CLI reports it as a data problem)");
                T.Throws<UnauthorizedAccessException>(() => PatchData.Load(dir), "a folder is an UnauthorizedAccessException (the CLI reports it as a data problem)");
            });

            r.Test("PatchData: invalid UTF-8 / Shift-JIS / UTF-16 data files give InvalidDataException naming UTF-8 (CS-01)", () =>
            {
                string dir = ctx.Dir("patchdata_utf8");
                string p = Path.Combine(dir, "bad.tsv");
                var cases = new Dictionary<string, byte[]>();
                var broken = new List<byte>(Encoding.UTF8.GetBytes(Ok(1) + "I2LS_UI\tk\t0123456789abcdef\t"));
                broken.AddRange(new byte[] { 0xE6, 0x97, 0xFF, 0x0A }); // a broken multi-byte sequence
                cases["broken multi-byte sequence"] = broken.ToArray();
                string text = File.ReadAllText(ctx.DataPath, Encoding.UTF8);
                cases["the shipped data saved as Shift-JIS"] = Encoding.GetEncoding(932).GetBytes(text);
                cases["the shipped data saved as UTF-16 (Notepad 'Unicode')"] = Encoding.Unicode.GetPreamble().Concat(Encoding.Unicode.GetBytes(text)).ToArray();
                var full = File.ReadAllBytes(ctx.DataPath);
                int lastMultiByte = Array.FindLastIndex(full, b => b >= 0xE0 && b <= 0xEF); // lead byte of a 3-byte character
                cases["cut inside the last multi-byte character"] = full.Take(lastMultiByte + 2).ToArray();
                cases["lone surrogate encoded as CESU-8"] = Encoding.UTF8.GetBytes(Ok(1) + "I2LS_UI\tk\t0123456789abcdef\t").Concat(new byte[] { 0xED, 0xA0, 0x80, 0x0A }).ToArray();
                foreach (var c in cases)
                {
                    File.WriteAllBytes(p, c.Value);
                    var e = T.Throws<InvalidDataException>(() => PatchData.Load(p), c.Key);
                    T.Contains(e.Message, "UTF-8", c.Key + ": message");
                }
            });

            r.Test("PatchData: a data file cut at a line end (lines lost) is rejected by the #entries count (CS-02)", () =>
            {
                string dir = ctx.Dir("patchdata_trunc");
                string text = File.ReadAllText(ctx.DataPath, Encoding.UTF8);
                string p = Path.Combine(dir, "cut.tsv");
                var lineEnds = new List<int>();
                for (int i = text.IndexOf('\n'); i >= 0; i = text.IndexOf('\n', i + 1))
                    lineEnds.Add(i + 1);
                // the last 3 line ends before the end, the middle, and the end of the header
                var cuts = new[] { lineEnds[lineEnds.Count - 2], lineEnds[lineEnds.Count - 3], lineEnds[lineEnds.Count - 4],
                                   text.IndexOf('\n', text.Length / 2) + 1, text.IndexOf("\nI2LS_", StringComparison.Ordinal) + 1 };
                foreach (int cut in cuts)
                {
                    File.WriteAllText(p, text.Substring(0, cut), new UTF8Encoding(false));
                    var e = T.Throws<InvalidDataException>(() => PatchData.Load(p), "cut at offset " + cut);
                    T.Contains(e.Message, "見出しの件数 2391", "cut at offset " + cut + ": message");
                }
                // a cut inside a middle line: that line has too few fields, or the count is short
                foreach (int back in new[] { 1, 3, 20 })
                {
                    File.WriteAllText(p, text.Substring(0, text.IndexOf('\n', text.Length / 2) - back), new UTF8Encoding(false));
                    T.Throws<InvalidDataException>(() => PatchData.Load(p), $"cut {back} characters before a line end in the middle");
                }
            });

            r.Test("PatchData: a data file cut inside its last translation is rejected (CS-06: the file must end with a newline)", () =>
            {
                string dir = ctx.Dir("patchdata_trunc_last");
                string text = File.ReadAllText(ctx.DataPath, Encoding.UTF8);
                // cut in the middle of the last inline translation of the file: every line still has 4 fields and the
                // count still matches; only the missing final newline shows that the file is incomplete
                int lastLine = text.TrimEnd('\n').LastIndexOf('\n') + 1;
                T.True(text.EndsWith("\n", StringComparison.Ordinal) && !text.EndsWith("\n\n", StringComparison.Ordinal),
                    "test precondition (build_patch.py ends the file with exactly one newline)");
                string body = text.Substring(0, text.Length - 1);
                string crlf = text.Replace("\n", "\r\n");
                string p = Path.Combine(dir, "trunc.tsv");
                var cuts = new Dictionary<string, string>
                {
                    ["3 characters cut from the last translation"] = body.Substring(0, body.Length - 3),
                    ["1 character cut from the last translation"] = body.Substring(0, body.Length - 1),
                    ["only the final newline missing"] = body,
                    ["cut right after the last tab (empty translation)"] = body.Substring(0, body.LastIndexOf('\t') + 1),
                    ["cut inside the key of the last line"] = body.Substring(0, lastLine + body.Substring(lastLine).IndexOf('\t') + 4),
                    ["CRLF file cut between CR and LF"] = crlf.Substring(0, crlf.Length - 1),
                };
                foreach (var c in cuts)
                {
                    T.True(c.Value.Length > lastLine, c.Key + ": precondition (cut inside the last line)");
                    File.WriteAllText(p, c.Value, new UTF8Encoding(false));
                    var e = T.Throws<InvalidDataException>(() => PatchData.Load(p), c.Key);
                    T.Contains(e.Message, NoEol, c.Key + ": message");
                }
                // controls: the complete file, its CRLF form, a BOM and an extra blank line at the end still load
                var ok = new Dictionary<string, byte[]>
                {
                    ["complete file"] = Encoding.UTF8.GetBytes(text),
                    ["CRLF line ends"] = Encoding.UTF8.GetBytes(crlf),
                    ["with BOM"] = Encoding.UTF8.GetPreamble().Concat(Encoding.UTF8.GetBytes(text)).ToArray(),
                    ["extra blank line at the end"] = Encoding.UTF8.GetBytes(text + "\n"),
                };
                foreach (var c in ok)
                {
                    File.WriteAllBytes(p, c.Value);
                    var d = PatchData.Load(p);
                    T.Eq(2391, d.Count, c.Key + ": count");
                    T.Eq("区画15", d.Sources.SelectMany(s => s.Value).Last().Japanese, c.Key + ": last translation");
                }
                // the embedded copy (what the GUI and a plain 'apply' use) still loads
                T.Eq(2391, PatchData.Load().Count, "embedded data count");
            });

            // Fuzz finding nul_translation_blamed_on_game_file: a translation with U+0000 used to load, and the written
            // bundle was then refused by its own check ("<bundle> を読み込めません（TextAsset に壊れたデータがあります …）"),
            // blaming the game's file. U+0000 is now refused anywhere in the data as the content of its line.
            string NulMessage(int line) => $"翻訳データ {line} 行目の内容が正しくありません";

            r.Test("PatchData: U+0000 in any field (source, key, base, translation, reference) or header line is refused naming the line", () =>
            {
                string dir = ctx.Dir("patchdata_nul");
                string p = Path.Combine(dir, "nul.tsv");
                const string Inline = "I2LS_UI\tk\t0123456789abcdef\t訳\n";
                string ok = Ok(1), ok2 = Ok(2); // the first entry is line 4
                var cases = new Dictionary<string, Tuple<string, int>>
                {
                    ["translation: NUL only"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcdef\t\0\n", 4),
                    ["translation: leading"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcdef\t\0訳\n", 4),
                    ["translation: middle"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcdef\t訳\0訳\n", 4),
                    ["translation: trailing"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcdef\t訳\0\n", 4),
                    ["translation: next to an escape"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcdef\t訳\\n\0\\t\n", 4),
                    ["translation: CRLF line, NUL before the CR"] = Tuple.Create(ok.Replace("\n", "\r\n") + "I2LS_UI\tk\t0123456789abcdef\t訳\0\r\n", 4),
                    ["translation: second entry"] = Tuple.Create(ok2 + Inline + "I2LS_UI\tk3\t0123456789abcdef\t訳\0\n", 5),
                    ["translation: no-op entry (same as the official text)"] = Tuple.Create(ok + $"I2LS_UI\tk\t{PatchData.BaseHash("訳\0")}\t訳\0\n", 4),
                    ["source"] = Tuple.Create(ok + "I2LS_\0UI\tk\t0123456789abcdef\t訳\n", 4),
                    ["source: leading"] = Tuple.Create(ok + "\0I2LS_UI\tk\t0123456789abcdef\t訳\n", 4),
                    ["key"] = Tuple.Create(ok + "I2LS_UI\tk\0\t0123456789abcdef\t訳\n", 4),
                    ["key: NUL only"] = Tuple.Create(ok + "I2LS_UI\t\0\t0123456789abcdef\t訳\n", 4),
                    ["base (16 characters with the NUL)"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcde\0\t訳\n", 4),
                    ["reference: after the '&'"] = Tuple.Create(ok + "&\0I2LS_UI\tk2\tfedcba9876543210\tI2LS_UI\tk\n", 4),
                    ["reference: source"] = Tuple.Create(ok + "&I2LS_\0UI\tk2\tfedcba9876543210\tI2LS_UI\tk\n", 4),
                    ["reference: key"] = Tuple.Create(ok + "&I2LS_UI\tk\0\tfedcba9876543210\tI2LS_UI\tk\n", 4),
                    ["reference: ref source"] = Tuple.Create(ok + "&I2LS_UI\tk2\tfedcba9876543210\tI2LS_\0UI\tk\n", 4),
                    ["reference: ref key"] = Tuple.Create(ok + "&I2LS_UI\tk2\tfedcba9876543210\tI2LS_UI\tk\0\n", 4),
                    ["reference: ref key NUL only"] = Tuple.Create(ok + "&I2LS_UI\tk2\tfedcba9876543210\tI2LS_UI\t\0\n", 4),
                    ["a line with too many fields"] = Tuple.Create(ok + "I2LS_UI\tk\t0123456789abcdef\t訳\t\0\n", 4),
                    // header lines are refused alike: the values reach the state file and the window, and
                    // int.TryParse would read "1\0" as 1
                    ["header value #patch_version"] = Tuple.Create("#TPCJP-PATCH\t1\n#patch_version\t1.0\0\n#entries\t1\n" + Inline, 2),
                    ["header value #entries (int.TryParse ignores trailing NULs)"] = Tuple.Create("#TPCJP-PATCH\t1\n#entries\t1\0\n" + Inline, 2),
                    ["header name"] = Tuple.Create(ok + "#no\0te\tx\n" + Inline, 4),
                    ["header line without a value (otherwise ignored)"] = Tuple.Create(ok + "#\0\n" + Inline, 4),
                    ["unknown header after the entries"] = Tuple.Create(ok + Inline + "#x\t\0\n", 5),
                    ["format line (its rest is otherwise ignored)"] = Tuple.Create("#TPCJP-PATCH\t1\0\n#entries\t1\n" + Inline, 1),
                };
                foreach (var c in cases)
                {
                    File.WriteAllText(p, c.Value.Item1, new UTF8Encoding(false));
                    var e = T.Throws<InvalidDataException>(() => PatchData.Load(p), c.Key);
                    T.Eq(NulMessage(c.Value.Item2), e.Message, c.Key + ": message");
                }
                // with a BOM the line numbers stay the same
                File.WriteAllText(p, ok + "I2LS_UI\tk\t0123456789abcdef\t訳\0\n", new UTF8Encoding(true));
                T.Eq(NulMessage(4), T.Throws<InvalidDataException>(() => PatchData.Load(p), "BOM").Message, "BOM: message");

                // no escape gives a NUL: "\0", "\u0000", "\x00" are bad escapes
                foreach (var esc in new[] { "\\0", "\\u0000", "\\x00", "\\U00000000" })
                {
                    File.WriteAllText(p, ok + "I2LS_UI\tk\t0123456789abcdef\t訳" + esc + "\n", new UTF8Encoding(false));
                    var e = T.Throws<InvalidDataException>(() => PatchData.Load(p), "escape " + esc);
                    T.Eq("翻訳データのエスケープが正しくありません", e.Message, "escape " + esc + ": message");
                }

                // the format check comes first: a file that is not our data (the NULs of big-endian UTF-16 without a BOM,
                // which is valid UTF-8 when it is all ASCII) is no line problem
                string ascii = ok + "I2LS_UI\tk\t0123456789abcdef\tx\n";
                File.WriteAllBytes(p, new UnicodeEncoding(true, false).GetBytes(ascii));
                T.Eq("翻訳データの形式が正しくありません", T.Throws<InvalidDataException>(() => PatchData.Load(p), "UTF-16BE without BOM").Message,
                    "UTF-16BE without BOM: message");
                File.WriteAllText(p, "\0" + ascii, new UTF8Encoding(false));
                T.Eq("翻訳データの形式が正しくありません", T.Throws<InvalidDataException>(() => PatchData.Load(p), "NUL before the format line").Message,
                    "NUL before the format line: message");
            });

            r.Test("PatchData: U+0000 inserted into the shipped data (the fuzz case: line 1289) is refused naming that line; the data itself loads", () =>
            {
                string dir = ctx.Dir("patchdata_nul_real");
                string p = Path.Combine(dir, "nul.tsv");
                string text = File.ReadAllText(ctx.DataPath, Encoding.UTF8);
                T.Eq(-1, text.IndexOf('\0'), "the shipped data has no NUL");
                var lines = text.Split('\n');
                int refLine = Array.FindIndex(lines, l => l.StartsWith("&", StringComparison.Ordinal));
                int lastLine = lines.Length - 2; // the file ends with a newline
                T.True(lines[1288].StartsWith("I2LS_Challenges\t", StringComparison.Ordinal) && lines[1288].Split('\t').Length == 4,
                    "test precondition (line 1289 is an inline translation)");
                T.True(refLine > 0 && lines[lastLine].Split('\t').Length == 4, "test precondition (a reference line, an inline last line)");
                foreach (int index in new[] { 1288, refLine, lastLine, 5 })
                {
                    var f = lines[index].Split('\t');
                    // the start of every field, the middle of the last one and the end of the line
                    var positions = new List<int>();
                    for (int j = 0, at = 0; j < f.Length; at += f[j].Length + 1, j++)
                        positions.Add(at);
                    positions.Add(lines[index].Length - f[f.Length - 1].Length / 2);
                    positions.Add(lines[index].Length);
                    foreach (int pos in positions)
                    {
                        var copy = (string[])lines.Clone();
                        copy[index] = copy[index].Insert(pos, "\0");
                        File.WriteAllText(p, string.Join("\n", copy), new UTF8Encoding(false));
                        string where = $"line {index + 1} offset {pos}";
                        var e = T.Throws<InvalidDataException>(() => PatchData.Load(p), where);
                        T.Eq(NulMessage(index + 1), e.Message, where + ": message");
                    }
                }
                // controls: the shipped file, its embedded copy and the official texts' characters (TAB, LF) still load
                T.Eq(2391, PatchData.Load(ctx.DataPath).Count, "shipped data count");
                T.Eq(2391, PatchData.Load().Count, "embedded data count");
                T.True(All(ctx.Data).Any(e => e.Japanese != null && e.Japanese.Contains("\n")), "the shipped data has translations with LF");
            });

            // Round 3b (R18): an escaped line break in a header value reached the state file as a line of its own
            // ("#patch_version\t1.0.0\ngame_version=FORGED" forged game_version). The values the patcher uses may not
            // hold control characters or Unicode line breaks, escaped or not; other header lines are not restricted.
            r.Test("PatchData: control characters / line breaks in #patch_version, #game_version, #bundle_sha256 are refused naming the line; other header lines may hold them", () =>
            {
                string dir = ctx.Dir("patchdata_header_controls");
                string p = Path.Combine(dir, "header.tsv");
                const string Inline = "I2LS_UI\tk\t0123456789abcdef\t訳\n";
                var values = new Dictionary<string, string>
                {
                    ["escaped LF"] = "1.0.0\\ngame_version=FORGED",
                    ["escaped CR"] = "1.0.0\\rinjected=1",
                    ["escaped CRLF at the end"] = "1.0.0\\r\\n",
                    ["escaped TAB"] = "1.0\\t0",
                    ["raw CR inside"] = "1.0\rX",
                    ["U+0001"] = "1.0" + (char)0x01,
                    ["U+001F"] = (char)0x1F + "1.0",
                    ["U+007F"] = "1.0" + (char)0x7F,
                    ["U+0085 (NEL)"] = "1.0" + (char)0x85 + "game_version=FORGED",
                    ["U+2028 (line separator)"] = "1.0" + (char)0x2028 + "x",
                    ["U+2029 (paragraph separator)"] = "1.0" + (char)0x2029 + "x",
                };
                foreach (string name in new[] { "patch_version", "game_version", "bundle_sha256" })
                {
                    foreach (var v in values)
                    {
                        string what = "#" + name + ", " + v.Key;
                        File.WriteAllText(p, "#TPCJP-PATCH\t1\n#entries\t1\n#" + name + "\t" + v.Value + "\n" + Inline, new UTF8Encoding(false));
                        var e = T.Throws<InvalidDataException>(() => PatchData.Load(p), what);
                        T.Eq(NulMessage(3), e.Message, what + ": message");
                    }
                    // a later duplicate (the last one wins) is checked as well
                    File.WriteAllText(p, $"#TPCJP-PATCH\t1\n#{name}\tok\n#entries\t1\n#{name}\tx\\ny\n" + Inline, new UTF8Encoding(false));
                    T.Eq(NulMessage(4), T.Throws<InvalidDataException>(() => PatchData.Load(p), "#" + name + " duplicate").Message, "#" + name + " duplicate: message");
                }
                // controls: printable values (also non-ASCII and spaces) still load; other header lines may keep escaped
                // CR / LF / TAB (gen_vectors.py writes "#note\theader\twith\\escapes\n") and raw control characters
                File.WriteAllText(p, "#TPCJP-PATCH\t1\n#patch_version\t1.0.0 テスト版\n#game_version\t10.3.169253+2024-12-06.1241\n#bundle_sha256\t" + new string('a', 64)
                    + "\n#note\theader\\twith\\\\escapes\\n\n#built\tx" + (char)0x01 + (char)0x2028 + "y\n#entries\t1\n" + Inline, new UTF8Encoding(false));
                var d = PatchData.Load(p);
                T.Eq("1.0.0 テスト版", d.PatchVersion, "patch_version");
                T.Eq("10.3.169253+2024-12-06.1241", d.GameVersion, "game_version");
                T.Eq(new string('a', 64), d.BundleSha256, "bundle_sha256");
                T.Eq("header\twith\\escapes\n", d.Header["note"], "#note keeps its escaped TAB / LF");
                T.Eq(1, d.Count, "count");
                // the shipped data and the test vector with the escaped #note load
                T.Eq(2391, PatchData.Load(ctx.DataPath).Count, "shipped data");
                T.Eq("header\twith\\escapes\n", PatchData.Load(Path.Combine(ctx.VectorsDir, "escape_patch.tsv")).Header["note"], "escape_patch.tsv #note");
            });

            r.Test("PatchData: only U+0000 is refused; other control and format characters in a translation still load", () =>
            {
                // the written bundle checks for NUL only (and strict UTF-8, which the data already passes); official
                // texts use TAB and LF, other languages U+200B / U+200E / U+200F / U+00AD
                string dir = ctx.Dir("patchdata_nul_controls");
                string p = Path.Combine(dir, "ok.tsv");
                foreach (int cp in new[] { 0x01, 0x08, 0x1f, 0x7f, 0x85, 0xad, 0x200b, 0x200e, 0x200f, 0x2028, 0xfeff, 0x1f600 })
                {
                    string ja = "訳" + char.ConvertFromUtf32(cp) + "訳";
                    File.WriteAllText(p, Ok(1) + "I2LS_UI\tk\t0123456789abcdef\t" + ja + "\n", new UTF8Encoding(false));
                    var d = PatchData.Load(p);
                    T.Eq(ja, d.Sources["I2LS_UI"][0].Japanese, "U+" + cp.ToString("X4"));
                }
                File.WriteAllText(p, Ok(1) + "I2LS_UI\tk\t0123456789abcdef\t訳\\t訳\\r\\n訳\n", new UTF8Encoding(false));
                T.Eq("訳\t訳\r\n訳", PatchData.Load(p).Sources["I2LS_UI"][0].Japanese, "escaped TAB / CR / LF");
            });
        }
    }
}
