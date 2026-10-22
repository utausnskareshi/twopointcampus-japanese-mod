using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace TpcJpPatch.Tests
{
    static class BundleTests
    {
        /// <summary>
        /// Load must either succeed or throw InvalidDataException; anything else is reported. Damage must not be
        /// reported as an unsupported format (R10).
        /// </summary>
        static string LoadOutcome(byte[] data, out Dictionary<string, string> texts)
        {
            texts = null;
            try
            {
                using (var b = LocalisationBundle.Load(data, "case.bundle"))
                    texts = Util.Texts(b);
                return "loaded";
            }
            catch (InvalidDataException e)
            {
                return UnsupportedFormatException.In(e) ? "unsupported format: " + e.Message : "invalid";
            }
            catch (Exception e)
            {
                return "unexpected " + e.GetType().FullName + ": " + e.Message;
            }
        }

        public static void Run(Runner r, Ctx ctx)
        {
            r.Test("Bundle: the official copy loads with 25 I2LS_* TextAssets", () =>
            {
                using (var b = LocalisationBundle.Load(ctx.OfficialPath))
                {
                    var names = b.Names.ToList();
                    T.Eq(25, names.Count, "TextAssets");
                    T.True(names.All(n => n.StartsWith("I2LS_", StringComparison.Ordinal)), "names start with I2LS_");
                    T.True(b.Contains("I2LS_UI") && !b.Contains("I2LS_Nope"), "Contains");
                    T.True(names.All(n => b.GetText(n).Length > 0), "every text non-empty");
                    T.Throws<KeyNotFoundException>(() => b.GetText("I2LS_Nope"), "unknown name");
                    r.Note($"{ctx.Official.Length:N0} bytes, {names.Sum(n => b.GetText(n).Length):N0} characters of text");
                }
            });

            r.Test("Bundle: Save() without changes reloads with identical texts and is deterministic", () =>
            {
                var want = Util.Texts(ctx.Official);
                byte[] s1, s2, s3;
                using (var b = LocalisationBundle.Load(ctx.Official, "a.bundle"))
                {
                    s1 = b.Save();
                    s2 = b.Save();
                }
                using (var b = LocalisationBundle.Load(ctx.Official, "b.bundle"))
                    s3 = b.Save();
                T.Bytes(s1, s2, "two saves of one instance");
                T.Bytes(s1, s3, "saves of two loads");
                Util.AssertTexts(want, Util.Texts(s1), "reloaded texts");
                byte[] s4;
                using (var b = LocalisationBundle.Load(s1, "c.bundle"))
                    s4 = b.Save();
                Util.AssertTexts(want, Util.Texts(s4), "texts after a second round trip");
                r.Note($"saved {s1.Length:N0} bytes (official {ctx.Official.Length:N0}); " +
                       (s4.SequenceEqual(s1) ? "save(load(save)) is a fixpoint" : "save(load(save)) differs in bytes"));
            });

            r.Test("Bundle: SetText of every alignment / Japanese / emoji / empty text reloads exactly; others untouched", () =>
            {
                var want = Util.Texts(ctx.Official);
                var samples = new[] { "", "a", "ab", "abc", "abcd", "日本語", "😀𠮷=値[i2t]k=\r\n", new string('あ', 100001) };
                foreach (var s in samples)
                {
                    byte[] saved;
                    using (var b = LocalisationBundle.Load(ctx.Official, "x.bundle"))
                    {
                        b.SetText("I2LS_DLCs", s);
                        T.Eq(s, b.GetText("I2LS_DLCs"), "GetText after SetText");
                        saved = b.Save();
                    }
                    var got = Util.Texts(saved);
                    var exp = new Dictionary<string, string>(want) { ["I2LS_DLCs"] = s };
                    Util.AssertTexts(exp, got, $"SetText of {Encoding.UTF8.GetByteCount(s)} bytes");
                    using (var b = LocalisationBundle.Load(ctx.Official, "y.bundle"))
                    {
                        b.SetText("I2LS_DLCs", s);
                        T.Bytes(saved, b.Save(), "deterministic for " + Encoding.UTF8.GetByteCount(s) + " bytes");
                    }
                }
            });

            r.Test("Bundle: SetText of a lone surrogate is refused (strict UTF-8) and the bundle stays usable", () =>
            {
                using (var b = LocalisationBundle.Load(ctx.Official, "x.bundle"))
                {
                    string before = b.GetText("I2LS_DLCs");
                    T.Throws<ArgumentException>(() => b.SetText("I2LS_DLCs", "bad\uD800"), "lone surrogate");
                    T.Eq(before, b.GetText("I2LS_DLCs"), "text unchanged");
                    Util.AssertTexts(Util.Texts(ctx.Official), Util.Texts(b.Save()), "save still identical");
                }
            });

            r.Test("Bundle: truncated data gives InvalidDataException at every tested length", () =>
            {
                var o = ctx.Official;
                var lengths = new List<int> { 0, 1, 4, 8, 16, 31, 32, 48, 64, 100, 127, 128, 200, 256, 512, 1000, 4096, o.Length / 2, o.Length - 4096, o.Length - 4, o.Length - 1 };
                var rng = new Random(1);
                for (int i = 0; i < 40; i++)
                    lengths.Add(rng.Next(o.Length));
                var bad = new List<string>();
                foreach (int len in lengths.Distinct())
                {
                    string res = LoadOutcome(o.Take(len).ToArray(), out _);
                    if (res != "invalid")
                        bad.Add($"length {len}: {res}");
                }
                T.True(bad.Count == 0, string.Join("; ", bad));
            });

            r.Test("Bundle: padded data (extra zeros / garbage / a second copy appended) gives InvalidDataException", () =>
            {
                var o = ctx.Official;
                var cases = new Dictionary<string, byte[]>
                {
                    ["+1 zero"] = o.Concat(new byte[1]).ToArray(),
                    ["+4 zeros"] = o.Concat(new byte[4]).ToArray(),
                    ["+4096 zeros"] = o.Concat(new byte[4096]).ToArray(),
                    ["+garbage"] = o.Concat(Enumerable.Range(0, 333).Select(i => (byte)(i * 7))).ToArray(),
                    ["twice"] = o.Concat(o).ToArray(),
                };
                var bad = cases.Select(c => new { c.Key, R = LoadOutcome(c.Value, out _) }).Where(x => x.R != "invalid").Select(x => x.Key + ": " + x.R).ToList();
                T.True(bad.Count == 0, string.Join("; ", bad));
            });

            r.Test("Bundle: zeroed / garbage / foreign data gives InvalidDataException", () =>
            {
                var o = ctx.Official;
                var rng = new Random(2);
                var garbage = new byte[o.Length];
                rng.NextBytes(garbage);
                var headerZero = (byte[])o.Clone();
                Array.Clear(headerZero, 0, 64);
                var cases = new Dictionary<string, byte[]>
                {
                    ["all zeros"] = new byte[o.Length],
                    ["random bytes"] = garbage,
                    ["first 64 bytes zeroed"] = headerZero,
                    ["text file"] = Encoding.UTF8.GetBytes("UnityFS is not here\n"),
                    ["UnityFS signature only"] = Encoding.ASCII.GetBytes("UnityFS\0"),
                    ["0xFF bytes"] = Enumerable.Repeat((byte)0xFF, o.Length).ToArray(),
                    ["the patch data tsv"] = File.ReadAllBytes(ctx.DataPath),
                    ["a PE file (test exe)"] = File.ReadAllBytes(System.Reflection.Assembly.GetExecutingAssembly().Location),
                };
                var bad = cases.Select(c => new { c.Key, R = LoadOutcome(c.Value, out _) }).Where(x => x.R != "invalid").Select(x => x.Key + ": " + x.R).ToList();
                T.True(bad.Count == 0, string.Join("; ", bad));
            });

            r.Test("Bundle: zeroed ranges / byte flips inside the data never give another exception type", () =>
            {
                var o = ctx.Official;
                var want = Util.Texts(o);
                var rng = new Random(3);
                int invalid = 0, identical = 0, changed = 0;
                var bad = new List<string>();
                for (int i = 0; i < 160; i++)
                {
                    var d = (byte[])o.Clone();
                    string what;
                    if (i % 2 == 0)
                    {
                        int start = rng.Next(d.Length), len = 1 + rng.Next(4096);
                        Array.Clear(d, start, Math.Min(len, d.Length - start));
                        what = $"zeroed {start}+{len}";
                    }
                    else
                    {
                        int n = 1 + rng.Next(8);
                        var pos = new List<int>();
                        for (int k = 0; k < n; k++)
                        {
                            int p = rng.Next(d.Length);
                            d[p] ^= (byte)(1 + rng.Next(255));
                            pos.Add(p);
                        }
                        what = "flipped " + string.Join(",", pos);
                    }
                    string res = LoadOutcome(d, out var texts);
                    if (res == "invalid")
                        invalid++;
                    else if (res == "loaded")
                    {
                        if (texts.Count == want.Count && texts.All(kv => want.TryGetValue(kv.Key, out var w) && w == kv.Value))
                            identical++;
                        else
                            changed++;
                    }
                    else
                        bad.Add(what + ": " + res);
                }
                r.Note($"160 damaged copies: {invalid} rejected, {identical} loaded identical, {changed} loaded with changed text (no checksum in the format)");
                T.True(bad.Count == 0, string.Join("; ", bad.Take(5)));
            });

            r.Test("Bundle: Load(path) of a missing file is a FileNotFoundException (not wrapped)", () =>
            {
                string dir = ctx.Dir("bundle_missing");
                T.Throws<FileNotFoundException>(() => LocalisationBundle.Load(Path.Combine(dir, "nope.bundle")), "missing file");
            });

            StructureTests(r, ctx);
            StrictTests(r, ctx);
        }

        /// <summary>Load must refuse the data as damaged (InvalidDataException, not an unsupported format); returns the message.</summary>
        static string Invalid(byte[] data, string what)
        {
            var e = T.Throws<InvalidDataException>(() => LocalisationBundle.Load(data, "case.bundle").Dispose(), what);
            T.True(!UnsupportedFormatException.In(e), $"{what}: damage reported as an unsupported format: {e.Message}");
            return e.Message;
        }

        /// <summary>
        /// R10: Load must refuse the data as a format it does not read: the wrapper「case.bundle を読み込めません（reason）」
        /// keeps the inner chain InvalidDataException -> UnsupportedFormatException.
        /// </summary>
        static void Unsupported(byte[] data, string what, string reason)
        {
            var e = T.Throws<InvalidDataException>(() => LocalisationBundle.Load(data, "case.bundle").Dispose(), what);
            T.Eq($"case.bundle を読み込めません（{reason}）", e.Message, what + ": message");
            T.True(UnsupportedFormatException.In(e), $"{what}: UnsupportedFormatException.In");
            T.True(e.InnerException is InvalidDataException && e.InnerException.InnerException is UnsupportedFormatException,
                $"{what}: inner chain {e.InnerException?.GetType().Name} -> {e.InnerException?.InnerException?.GetType().Name}");
        }

        /// <summary>FZ-02 / FZ-05 / FZ-06: header, block/directory and asset table checks of LocalisationBundle.Load.</summary>
        static void StructureTests(Runner r, Ctx ctx)
        {
            var o = ctx.Official;
            var want = Util.Texts(o);

            r.Test("Bundle structure: the forging helper re-serializes the official bundle (uncompressed block info) to an equal bundle", () =>
            {
                var h = Forge.Parse(o);
                r.Note($"version {h.Version}, '{h.Gen}' / '{h.Eng}', flags 0x{h.Flags:X}, {h.Blocks.Count} blocks, {h.Dirs.Count} directories ({string.Join(",", h.Dirs.Select(d => d.Name))})");
                Util.AssertTexts(want, Util.Texts(Forge.Write(h)), "re-serialized texts");
            });

            r.Test("Bundle structure: other valid layouts load with identical texts (uncompressed; block info at the end as UnityPy writes)", () =>
            {
                Util.AssertTexts(want, Util.Texts(Forge.Unpacked(o)), "uncompressed (AssetBundleFile.Unpack)");
                Util.AssertTexts(want, Util.Texts(Forge.Packed(Forge.Unpacked(o), true)), "LZ4, block info at the end (flag 0x80)");
                Util.AssertTexts(want, Util.Texts(Forge.Packed(Forge.Unpacked(o), false)), "LZ4, block info at the start");
            });

            r.Test("Bundle structure: a wrong signature or version string is rejected", () =>
            {
                var sig = (byte[])o.Clone();
                sig[5] = (byte)'X'; // "UnityXS"
                Invalid(sig, "signature UnityXS");
                foreach (var v in new[] { "5.x", "5.x.x.x", "5#x.x", "", "x.5.5" })
                {
                    var hv = Forge.Parse(o);
                    hv.Gen = v;
                    T.Contains(Invalid(Forge.Write(hv), "generation version " + v), "版の情報", "generation version " + v + ": message");
                }
                foreach (var v in new[] { "0.0", "2020.3.33f1xé", "0..0", "zero" })
                {
                    var hv = Forge.Parse(o);
                    hv.Eng = v;
                    T.Contains(Invalid(Forge.Write(hv), "engine version " + v), "版の情報", "engine version " + v + ": message");
                }
                var ok = Forge.Parse(o);
                ok.Eng = "2020.3.33f1";
                Util.AssertTexts(want, Util.Texts(Forge.Write(ok)), "a real engine version is accepted");
            });

            r.Test("Bundle structure: a declared file size that differs from the file length is rejected", () =>
            {
                foreach (long delta in new long[] { -1, 1, 4096, -o.Length, long.MaxValue - o.Length })
                {
                    var d = Forge.SetTotalSize(o, o.Length + delta);
                    T.Contains(Invalid(d, "declared size " + delta), "大きさが一致しません", "declared size " + delta + ": message");
                }
                // padding and declared size both grown: the data blocks no longer add up
                var padded = Forge.SetTotalSize(o.Concat(new byte[16]).ToArray(), o.Length + 16);
                T.Contains(Invalid(padded, "16 bytes appended, size field adjusted"), "データブロックの大きさが一致しません", "appended: message");
            });

            r.Test("Bundle structure: forged block sizes are rejected (incl. a 2 GB block: no OutOfMemoryException) (FZ-02)", () =>
            {
                var cases = new Dictionary<string, Action<Forge.Header>>
                {
                    ["block 0 compressed +1"] = h => h.Blocks[0].Compressed += 1,
                    ["block 0 compressed -1"] = h => h.Blocks[0].Compressed -= 1,
                    ["last block compressed 0"] = h => h.Blocks[h.Blocks.Count - 1].Compressed = 0,
                    ["block 0 decompressed 2 GB"] = h => h.Blocks[0].Decompressed = 0x7FFFFFF0,
                    ["block 0 decompressed 4 GB - 1"] = h => h.Blocks[0].Decompressed = 0xFFFFFFFF,
                    ["block 0 decompressed 0"] = h => h.Blocks[0].Decompressed = 0,
                    ["one block missing"] = h => h.Blocks.RemoveAt(h.Blocks.Count - 1),
                    ["two blocks swapped"] = h => { var b = h.Blocks[0]; h.Blocks[0] = h.Blocks[1]; h.Blocks[1] = b; },
                };
                var bad = new List<string>();
                foreach (var c in cases)
                {
                    var h = Forge.Parse(o);
                    c.Value(h);
                    try
                    {
                        LocalisationBundle.Load(Forge.Write(h), "case.bundle").Dispose();
                        bad.Add(c.Key + ": loaded");
                    }
                    catch (InvalidDataException e)
                    {
                        r.Note($"{c.Key}: {e.Message}" + (e.InnerException != null ? $" (from {e.InnerException.GetType().Name})" : ""));
                    }
                    catch (Exception e)
                    {
                        bad.Add($"{c.Key}: unexpected {e.GetType().FullName}: {e.Message}");
                    }
                }
                T.True(bad.Count == 0, string.Join("; ", bad));
            });

            r.Test("Bundle structure: forged directory entries are rejected (range, size, count) (FZ-05/06)", () =>
            {
                var cases = new Dictionary<string, Tuple<Action<Forge.Header>, string>>
                {
                    ["dir size +1"] = Tuple.Create<Action<Forge.Header>, string>(h => h.Dirs[0].Size += 1, "位置が正しくありません"),
                    ["dir size huge"] = Tuple.Create<Action<Forge.Header>, string>(h => h.Dirs[0].Size = long.MaxValue / 2, "位置が正しくありません"),
                    ["dir offset -1"] = Tuple.Create<Action<Forge.Header>, string>(h => h.Dirs[0].Offset = -1, "位置が正しくありません"),
                    ["dir size -1"] = Tuple.Create<Action<Forge.Header>, string>(h => h.Dirs[0].Size = -1, "位置が正しくありません"),
                    ["dir offset +1"] = Tuple.Create<Action<Forge.Header>, string>(h => h.Dirs[0].Offset += 1, "位置が正しくありません"),
                    ["dir size -16"] = Tuple.Create<Action<Forge.Header>, string>(h => h.Dirs[0].Size -= 16, null),
                    ["no directory"] = Tuple.Create<Action<Forge.Header>, string>(h => h.Dirs.Clear(), "アセットファイルが見つかりません"),
                    ["the assets file listed twice"] = Tuple.Create<Action<Forge.Header>, string>(
                        h => h.Dirs.Add(new Forge.Dir { Offset = h.Dirs[0].Offset, Size = h.Dirs[0].Size, Flags = h.Dirs[0].Flags, Name = h.Dirs[0].Name + "x" }), "複数のアセットファイル"),
                };
                foreach (var c in cases)
                {
                    var h = Forge.Parse(o);
                    c.Value.Item1(h);
                    string msg = Invalid(Forge.Write(h), c.Key);
                    if (c.Value.Item2 != null)
                        T.Contains(msg, c.Value.Item2, c.Key + ": message");
                }
            });

            r.Test("Bundle structure: a repeated asset path ID, a repeated TextAsset name and NUL in a text are rejected (FZ-05/06)", () =>
            {
                var dupId = Forge.Rebuild(o, (am, inst) =>
                {
                    var infos = inst.file.AssetInfos;
                    infos[1].PathId = infos[0].PathId;
                });
                T.Contains(Invalid(dupId, "duplicate path ID"), "番号が重複しています", "duplicate path ID: message");

                var dupName = Forge.Rebuild(o, (am, inst) =>
                {
                    var texts = inst.file.GetAssetsOfType(AssetsTools.NET.Extra.AssetClassID.TextAsset);
                    texts[1].SetNewData(Forge.TextAssetRaw(Forge.TextAssetName(inst, texts[0]), "I2LS dup=値"));
                });
                T.Contains(Invalid(dupName, "duplicate TextAsset name"), "同名の TextAsset", "duplicate name: message");

                byte[] nul;
                using (var b = LocalisationBundle.Load(o, "n.bundle"))
                {
                    b.SetText("I2LS_DLCs", "Key=value\0[i2t]K2=v");
                    nul = b.Save();
                }
                T.Contains(Invalid(nul, "NUL in a text"), "壊れたデータ", "NUL: message");

                var control = Forge.Rebuild(o, (am, inst) => { });
                Util.AssertTexts(want, Util.Texts(control), "control: the rebuild helper itself changes no text");
            });
        }

        static void Expect(byte[] data, string what, params string[] reasons)
        {
            string msg = Invalid(data, what);
            T.True(reasons.Any(x => msg.Contains(x)), $"{what}: <{msg}> does not name the reason ({string.Join(" / ", reasons)})");
        }

        /// <summary>
        /// Round 3: damage AssetsTools.NET reads leniently but Unity / UnityPy cannot read (fuzz: backup_incomplete,
        /// backup_unloadable, output_unloadable, restore_wrote_damaged, checker_crash, status_damage_not_detected):
        /// strict LZ4, header / block / directory flags, the serialized file's header, metadata, type trees, object
        /// table and data, and the AssetBundle's container. Genuine layouts must still load.
        /// </summary>
        static void StrictTests(Runner r, Ctx ctx)
        {
            var o = ctx.Official;
            var want = Util.Texts(o);
            var encoders = new Dictionary<string, Func<byte[], byte[]>>
            {
                ["Encode32"] = d => LZ4ps.LZ4Codec.Encode32(d, 0, d.Length),
                ["Encode64"] = d => LZ4ps.LZ4Codec.Encode64(d, 0, d.Length),
                ["Encode32HC"] = d => LZ4ps.LZ4Codec.Encode32HC(d, 0, d.Length),
                ["Encode64HC"] = d => LZ4ps.LZ4Codec.Encode64HC(d, 0, d.Length),
            };

            r.Test("Bundle strict: Lz4DecodeExact decodes LZ4Codec (AssetsTools.NET) blocks of random / repetitive / text data exactly", () =>
            {
                var rng = new Random(11);
                byte[] text = Encoding.UTF8.GetBytes(string.Concat(want.Values));
                var sizes = new List<int> { 1, 5, 11, 12, 13, 16, 17, 64, 255, 256, 1000, 4096, 65535, 65536, 131072 };
                for (int i = 0; i < 40; i++)
                    sizes.Add(1 + rng.Next(140000));
                int blocks = 0;
                foreach (int size in sizes)
                {
                    for (int kind = 0; kind < 3; kind++)
                    {
                        var data = new byte[size];
                        if (kind == 0)
                            rng.NextBytes(data);
                        else if (kind == 1)
                            for (int k = 0; k < size; k++)
                                data[k] = (byte)(k % 7 < 5 ? 'a' + k % 3 : k / 1000);
                        else
                            Buffer.BlockCopy(text, rng.Next(text.Length - size), data, 0, size);
                        foreach (var enc in encoders)
                        {
                            byte[] packed = enc.Value(data);
                            var dst = new byte[size];
                            T.True(LocalisationBundle.Lz4DecodeExact(packed, 0, packed.Length, dst, size), $"{enc.Key} of {size} bytes (kind {kind}) decodes");
                            T.Bytes(data, dst, $"{enc.Key} of {size} bytes (kind {kind})");
                            blocks++;
                        }
                    }
                }
                r.Note($"{blocks} blocks");
            });

            r.Test("Bundle strict: Lz4DecodeExact rejects trailing input, a cut block, a wrong size, offset 0 / beyond the output and a match too close to the end", () =>
            {
                byte[] data = Encoding.UTF8.GetBytes(want["I2LS_UI"]).Take(50000).ToArray();
                byte[] packed = LZ4ps.LZ4Codec.Encode32HC(data, 0, data.Length);
                var dst = new byte[data.Length + 8192];
                bool Decodes(byte[] p, int size) => LocalisationBundle.Lz4DecodeExact(p, 0, p.Length, dst, size);
                T.True(Decodes(packed, data.Length), "control");
                foreach (int extra in new[] { 1, 2, 7 })
                    T.True(!Decodes(packed.Concat(new byte[extra]).ToArray(), data.Length), $"{extra} trailing bytes");
                foreach (int cut in new[] { 1, 2, 100 })
                    T.True(!Decodes(packed.Take(packed.Length - cut).ToArray(), data.Length), $"cut by {cut} bytes");
                foreach (int delta in new[] { -1, 1, 4096 })
                    T.True(!Decodes(packed, data.Length + delta), $"declared size {delta:+0;-0}");
                T.True(!LocalisationBundle.Lz4DecodeExact(packed, 0, packed.Length, new byte[10], data.Length), "output buffer smaller than the size");

                // 16 literals, a match (offset 16, 8 bytes), 5 last literals: 29 bytes
                byte[] Block(byte token, byte ext, int literals, int offset, int last) =>
                    new[] { token, ext }.Concat(Enumerable.Range(1, literals).Select(i => (byte)i))
                        .Concat(new[] { (byte)offset, (byte)(offset >> 8), (byte)(last << 4) }).Concat(Enumerable.Range(100, last).Select(i => (byte)i)).ToArray();
                T.True(Decodes(Block(0xF4, 1, 16, 16, 5), 29), "hand-made control block");
                T.Eq((byte)1, dst[16], "the match copies the literals");
                T.True(!Decodes(Block(0xF4, 1, 16, 0, 5), 29), "offset 0");
                T.True(!Decodes(Block(0xF4, 1, 16, 17, 5), 29), "offset beyond the output");
                T.True(!Decodes(Block(0xF5, 1, 16, 16, 4), 29), "a match into the last 5 bytes (9-byte match, 4 literals left)");
                T.True(!Decodes(Block(0xF0, 1, 16, 16, 5), 25), "the last match starts less than 12 bytes before the end");
                T.True(!Decodes(Block(0xF4, 1, 16, 16, 4), 28), "only 4 last literals");
                T.True(!Decodes(new byte[] { 0xF0 }, 15), "input ends inside a length");
                T.True(Decodes(new byte[] { 0x30, 1, 2, 3 }, 3) && !Decodes(new byte[0], 0), "literals-only block / empty input");
            });

            r.Test("Bundle strict: Lz4DecodeExact returns false (no exception, no read past the block) for every cut of LZ4 blocks and for a block ending right after a match length (R7)", () =>
            {
                string Outcome(byte[] src, long count, int size)
                {
                    try
                    {
                        return LocalisationBundle.Lz4DecodeExact(src, 0, count, new byte[size], size) ? "true" : "false";
                    }
                    catch (Exception e)
                    {
                        return e.GetType().Name;
                    }
                }
                // the review's block: 1 literal, then a match (offset 1) whose length bytes run to the end of the input
                var r7 = new byte[] { 0x1F, 0x61, 0x01, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x01 };
                T.Eq("false", Outcome(r7, r7.Length, 2000), "a block ending right after a match length, at the end of the array");
                T.Eq("false", Outcome(r7.Concat(new byte[] { 0x50, 1, 2, 3, 4, 5 }).ToArray(), r7.Length, 2000), "the same block followed by more data");
                T.Eq("false", Outcome(new byte[] { 0x1F, 0x61, 0x01, 0x00, 0xFF }, 5, 2000), "a block ending inside a match length");
                T.Eq("false", Outcome(new byte[] { 0x10, 0x61 }, 1, 20), "a block ending inside its literals");
                // blocks with long literal runs and long matches (length bytes), cut at every length
                var rng = new Random(13);
                var random = new byte[700];
                rng.NextBytes(random);
                byte[] runs = Enumerable.Repeat((byte)'a', 3000).Concat(random.Take(300)).Concat(Enumerable.Repeat((byte)'b', 1000))
                    .Concat(Encoding.UTF8.GetBytes(want["I2LS_UI"]).Take(2000)).ToArray();
                var bad = new List<string>();
                int cuts = 0;
                foreach (var data in new[] { random, runs })
                {
                    foreach (var enc in encoders)
                    {
                        byte[] packed = enc.Value(data);
                        T.Eq("true", Outcome(packed, packed.Length, data.Length), $"{enc.Key} of {data.Length} bytes: control");
                        for (int n = 1; n < packed.Length; n++)
                        {
                            // the cut block alone in an array (a read past it throws) and followed by the rest of the block
                            string alone = Outcome(packed.Take(n).ToArray(), n, data.Length), followed = Outcome(packed, n, data.Length);
                            if (alone != "false" || followed != "false")
                                bad.Add($"{enc.Key} of {data.Length} bytes cut to {n} bytes: {alone} / {followed}");
                            cuts++;
                        }
                    }
                }
                r.Note($"{cuts} cut blocks");
                T.True(bad.Count == 0, string.Join("; ", bad.Take(5)));
            });

            r.Test("Bundle strict: the last data block ending right after a match length is reported as a damaged block, not as an unexpected exception (R7)", () =>
            {
                var h = Forge.Parse(o);
                int last = h.Blocks.Count - 1;
                int size = (int)h.Blocks[last].Decompressed, before = (int)h.Blocks.Take(last).Sum(b => (long)b.Compressed);
                // 1 literal, then a match (offset 1) up to 5 bytes before the end, whose length bytes end the block
                int extra = size - 1 - 5 - 4 - 15;
                var block = new List<byte> { 0x1F, 0x61, 0x01, 0x00 };
                block.AddRange(Enumerable.Repeat((byte)0xFF, extra / 255));
                block.Add((byte)(extra % 255));
                h.Blocks[last].Compressed = (uint)block.Count;
                h.Data = h.Data.Take(before).Concat(block).ToArray();
                Expect(Forge.Write(h), $"last block ({block.Count} bytes for {size}) ending after a match length", $"データブロックが壊れています（{last + 1} / {last + 1}）");
            });

            r.Test("Bundle strict: block info with trailing or missing input (compressed size 130 -> 129/131/132/134) or a wrong size is rejected (hand repro A)", () =>
            {
                int at = Forge.SizeFieldPos(o) + 8;
                T.Eq(130u, Forge.U32(o, at), "compressed block info size of the official file");
                foreach (uint csz in new uint[] { 129, 131, 132, 134 })
                    Expect(Forge.SetU32(o, at, csz), $"compressed block info size {csz}", "ブロック情報が壊れています");
                foreach (uint usz in new uint[] { 210, 212, 0x7FFFFFF0 })
                    Expect(Forge.SetU32(o, at + 4, usz), $"declared block info size {usz}", "ブロック情報が壊れています",
                        "ファイルが壊れている可能性があります"); // a smaller size already makes AssetsTools.NET fail
            });

            r.Test("Bundle strict: a data block with trailing input or a wrong decompressed size is rejected", () =>
            {
                var h = Forge.Parse(o);
                int first = (int)h.Blocks[0].Compressed;
                h.Blocks[0].Compressed += 2;
                h.Data = h.Data.Take(first).Concat(new byte[2]).Concat(h.Data.Skip(first)).ToArray();
                Expect(Forge.Write(h), "block 1 with 2 trailing bytes", "データブロックが壊れています（1 / 13）");
                foreach (int delta in new[] { -1, 1 })
                {
                    var hs = Forge.Parse(o);
                    hs.Blocks[hs.Blocks.Count - 1].Decompressed = (uint)(hs.Blocks[hs.Blocks.Count - 1].Decompressed + delta);
                    hs.Dirs[0].Size += delta;
                    Expect(Forge.Write(hs), $"last block decompressed size {delta:+0;-0}", "データブロックが壊れています（13 / 13）");
                }
            });

            r.Test("Bundle strict: a header format version / header flags / block flags / directory flags that Unity does not write are rejected", () =>
            {
                var hv = Forge.Parse(o);
                hv.Version = 9;
                Unsupported(Forge.Write(hv), "format version 9", "対応していないバンドルの形式です（版 9）");
                foreach (uint bit in new uint[] { 0x100, 0x400, 0x1000, 0x10000 })
                {
                    var hf = Forge.Parse(o);
                    hf.Flags |= bit;
                    Expect(Forge.Write(hf), $"header flag 0x{bit:X}", "ヘッダーのフラグが正しくありません");
                }
                var hb = Forge.Parse(o);
                hb.Blocks[2].Flags = 0x83;
                Expect(Forge.Write(hb), "block flags 0x83", "データブロックが壊れています（3 / 13）");
                foreach (uint flags in new uint[] { 0, 5, 6 })
                {
                    var hd = Forge.Parse(o);
                    hd.Dirs[0].Flags = flags;
                    Expect(Forge.Write(hd), $"directory flags {flags}", "バンドル内のファイルの種類が正しくありません");
                }
            });

            var sf = new SfForge(o);
            var texts = sf.Infos.Where(i => sf.ClassId(i) == 49).ToList();
            var assetBundle = sf.Infos.Single(i => sf.ClassId(i) == 142);

            r.Test("Bundle strict: the serialized file's header: format version, endianness, metadata size, data offset, file size", () =>
            {
                Util.AssertTexts(want, Util.Texts(sf.Bundle), "control: the uncompressed copy");
                Unsupported(sf.Edit(d => Forge.PutBE(d, sf.At + 8, 21, 4)), "format version 21", "アセットファイルの形式に対応していません（版 21）");
                Unsupported(sf.Edit(d => d[sf.At + 16] = 1), "big-endian", "アセットファイルの形式に対応していません（ビッグエンディアン）");
                Expect(sf.Edit(d => Forge.PutBE(d, sf.At + 20, sf.MetadataSize + 4, 4)), "metadata size +4", "アセットファイルの管理情報が壊れています");
                Expect(sf.Edit(d => Forge.PutBE(d, sf.At + 32, 48 + sf.MetadataSize - 1, 8)), "data offset inside the metadata", "アセットファイル");
                Expect(sf.Edit(d => Forge.PutBE(d, sf.At + 24, sf.FileSize + 1, 8)), "file size +1", "アセットファイル");
            });

            r.Test("Bundle strict: the object table: type index (fuzz type_index=1), overlapping / outside objects, a changed path ID", () =>
            {
                int abType = assetBundle.TypeIdOrIndex;
                Expect(sf.Edit(d => Forge.PutLE(d, sf.EntryPos(texts[3]) + 20, abType, 4)), "a TextAsset typed as the AssetBundle",
                    "オブジェクトのデータが型情報と一致しません", "AssetBundle の情報が壊れています");
                Expect(sf.Edit(d => Forge.PutLE(d, sf.EntryPos(texts[3]) + 20, sf.Types.Count, 4)), "type index out of range", "オブジェクトの位置が正しくありません");
                Expect(sf.Edit(d => Forge.PutLE(d, sf.EntryPos(texts[4]) + 8, texts[3].ByteOffset, 8)), "two objects at one offset", "オブジェクトの位置が正しくありません");
                Expect(sf.Edit(d => Forge.PutLE(d, sf.EntryPos(texts[4]) + 8, sf.FileSize - sf.DataOffset, 8)), "an object after the end", "オブジェクトの位置が正しくありません");
                var last = sf.Infos.OrderBy(i => i.ByteOffset).Last();
                Expect(sf.Edit(d => Forge.PutLE(d, sf.EntryPos(last) + 16, last.ByteSize + 4, 4)), "last object 4 bytes longer", "オブジェクトの位置が正しくありません");
                Expect(sf.Edit(d => Forge.PutLE(d, sf.EntryPos(texts[5]), 0x7123456789ABCDEF, 8)), "a TextAsset's path ID changed (the container still names the old one)", "AssetBundle の情報が壊れています");
            });

            r.Test("Bundle strict: object data must be exactly what the type tree describes (fuzz name_len 12 -> 11: padding not zero, lengths)", () =>
            {
                var ta = texts.First(i => BitConverter.ToInt32(sf.Bundle, sf.ObjectPos(i)) % 4 != 1);
                int len = BitConverter.ToInt32(sf.Bundle, sf.ObjectPos(ta));
                Expect(sf.Edit(d => Forge.PutLE(d, sf.ObjectPos(ta), len - 1, 4)), $"TextAsset name length {len} -> {len - 1}", "オブジェクトのデータが型情報と一致しません");
                Expect(sf.Edit(d => Forge.PutLE(d, sf.ObjectPos(ta), -1, 4)), "TextAsset name length -1", "オブジェクトのデータが型情報と一致しません");
                int scriptAt = sf.ObjectPos(ta) + ((4 + len + 3) & ~3);
                Expect(sf.Edit(d => Forge.PutLE(d, scriptAt, 0x7FFFFFF0, 4)), "TextAsset text length 2 GB", "オブジェクトのデータが型情報と一致しません");
                Expect(sf.Edit(d => Forge.PutLE(d, scriptAt, BitConverter.ToInt32(sf.Bundle, scriptAt) - 4, 4)), "TextAsset text 4 bytes shorter", "オブジェクトのデータが型情報と一致しません");
            });

            r.Test("Bundle strict: type tree damage: name not UTF-8 / not terminated, level jump, array flag, primitive size, managed reference flag", () =>
            {
                var taType = sf.Types[texts[0].TypeIdOrIndex];
                var abType = sf.Types[assetBundle.TypeIdOrIndex];
                int strings = sf.Find(abType.StringBufferBytes, sf.At + 48, sf.At + 48 + (int)sf.MetadataSize);
                int runtime = sf.Find(Encoding.ASCII.GetBytes("m_RuntimeCompatibility\0"), strings, strings + abType.StringBufferBytes.Length);
                Expect(sf.Edit(d => d[runtime + 2] = 0xC1), "a field name with a byte that is not UTF-8", "アセットファイルの型情報が壊れています");
                Expect(sf.Edit(d => d[runtime + 2] = 0x1F), "a field name with a control character", "アセットファイルの型情報が壊れています");
                Expect(sf.Edit(d => d[strings + abType.StringBufferBytes.Length - 1] = (byte)'x'), "the last name not terminated", "アセットファイルの型情報が壊れています");
                Expect(sf.Edit(d => d[sf.NodePos(taType, 3) + 2] = 4), "TextAsset node level 3 -> 4 (a jump)", "アセットファイルの型情報が壊れています");
                Expect(sf.Edit(d => d[sf.NodePos(taType, 2) + 3] = 0), "array flag of an Array node cleared", "アセットファイルの型情報が壊れています");
                Expect(sf.Edit(d => d[sf.NodePos(taType, 4) + 3] = 1), "array flag on a char node", "アセットファイルの型情報が壊れています");
                Expect(sf.Edit(d => Forge.PutLE(d, sf.NodePos(taType, 3) + 12, 8, 4)), "int with size 8", "アセットファイルの型情報が壊れています");
                Expect(sf.Edit(d => d[sf.NodePos(abType, 1) + 3] = 2), "managed reference flag on a string", "アセットファイルの型情報が壊れています");
                Expect(sf.Edit(d => d[sf.NodePos(taType, 0) + 3] = 8), "array-of-references flag on the TextAsset", "アセットファイルの型情報が壊れています");
                Expect(sf.Edit(d => Forge.PutLE(d, sf.NodePos(taType, 1) + 4, 0x80000000u | 1, 4)), "TextAsset m_Name typed as an unknown common string",
                    "TextAsset の型情報が想定と異なります");
            });

            r.Test("Bundle strict: the AssetBundle container: missing object, TextAsset not listed, preload range, file ID, renamed field", () =>
            {
                int abStart = sf.ObjectPos(assetBundle), abEnd = abStart + (int)assetBundle.ByteSize;
                var ta = texts[2];
                var pid = BitConverter.GetBytes(ta.PathId);
                var hits = new List<int>();
                for (int p = sf.Find(pid, abStart, abEnd); p >= 0; p = sf.Find(pid, p + 1, abEnd))
                    hits.Add(p);
                T.Eq(2, hits.Count, "the TextAsset is named twice in the AssetBundle (preload table, container)");
                T.Eq(0, BitConverter.ToInt32(sf.Bundle, hits[1] - 4), "container entry: file ID 0");
                T.Eq(1, BitConverter.ToInt32(sf.Bundle, hits[1] - 8), "container entry: preload size 1");
                Expect(sf.Edit(d => hits.ForEach(p => Forge.PutLE(d, p, 0x0123456789, 8))), "references to a missing object", "AssetBundle の情報が壊れています");
                Expect(sf.Edit(d => hits.ForEach(p => Forge.PutLE(d, p, texts[3].PathId, 8))), "a TextAsset not in the container", "TextAsset が AssetBundle の一覧にありません");
                Expect(sf.Edit(d => Forge.PutLE(d, hits[1] - 12, 1000, 4)), "container preload index 1000", "AssetBundle の情報が壊れています");
                Expect(sf.Edit(d => Forge.PutLE(d, hits[1] - 4, 5, 4)), "container file ID 5 (no external files)", "AssetBundle の情報が壊れています");
                var abType = sf.Types[assetBundle.TypeIdOrIndex];
                int strings = sf.Find(abType.StringBufferBytes, sf.At + 48, sf.At + 48 + (int)sf.MetadataSize);
                int container = sf.Find(Encoding.ASCII.GetBytes("m_Container\0"), strings, strings + abType.StringBufferBytes.Length);
                Expect(sf.Edit(d => d[container + 10] = (byte)'x'), "m_Container renamed m_Containex", "AssetBundle の情報が壊れています");
                Expect(sf.Edit(d => Forge.PutLE(d, sf.EntryPos(assetBundle) + 20, texts[0].TypeIdOrIndex, 4)), "no AssetBundle object",
                    "AssetBundle の情報が壊れています", "オブジェクトのデータが型情報と一致しません");
            });

            r.Test("Bundle strict: forged counts and lengths in the serialized file (2 GB) give InvalidDataException without a huge allocation", () =>
            {
                // the object count is followed by padding to a multiple of 4, then the first entry
                int first = sf.EntryPos(sf.Infos[0]);
                int objects = Enumerable.Range(4, 4).Select(k => first - k)
                    .FirstOrDefault(q => ((q + 4 - sf.At + 3) & ~3) == first - sf.At && BitConverter.ToInt32(sf.Bundle, q) == sf.Infos.Count);
                int types = sf.At + 48 + 6 + 4 + 1; // after the Unity version "0.0.0", the platform and the type tree flag
                T.Eq(sf.Infos.Count, BitConverter.ToInt32(sf.Bundle, objects), "object count position");
                T.Eq(sf.Types.Count, BitConverter.ToInt32(sf.Bundle, types), "type count position");
                T.Eq(sf.Types[0].Nodes.Count, BitConverter.ToInt32(sf.Bundle, types + 4 + 23), "node count position");
                int abStart = sf.ObjectPos(assetBundle);
                int preload = abStart + ((4 + BitConverter.ToInt32(sf.Bundle, abStart) + 3) & ~3);
                T.Eq(texts.Count, BitConverter.ToInt32(sf.Bundle, preload), "preload table count position");
                var cases = new Dictionary<string, byte[]>
                {
                    ["object count"] = sf.Edit(d => Forge.PutLE(d, objects, int.MaxValue, 4)),
                    ["type count"] = sf.Edit(d => Forge.PutLE(d, types, int.MaxValue, 4)),
                    ["node count"] = sf.Edit(d => Forge.PutLE(d, types + 4 + 23, int.MaxValue, 4)),
                    ["string buffer size"] = sf.Edit(d => Forge.PutLE(d, types + 4 + 23 + 4, int.MaxValue, 4)),
                    ["preload table count"] = sf.Edit(d => Forge.PutLE(d, preload, int.MaxValue, 4)),
                };
                foreach (var c in cases)
                {
                    var sw = System.Diagnostics.Stopwatch.StartNew();
                    var e = T.Throws<InvalidDataException>(() => LocalisationBundle.Load(c.Value, "case.bundle").Dispose(), c.Key);
                    T.True(!(e.InnerException is OutOfMemoryException) && sw.ElapsedMilliseconds < 5000, $"{c.Key}: {e.Message} ({sw.ElapsedMilliseconds} ms)");
                }
            });

            r.Test("Bundle strict: forged type trees that make the strict reading quadratic end quickly with InvalidDataException (R8: 10^6 elements of a class with 1,000 empty fields; 20,000 objects sharing 20,000 empty fields; 3,000 nodes naming one 1 MB string)", () =>
            {
                SfBuild.Node N(int level, string type, string name, int size) => new SfBuild.Node { Level = level, Type = type, Name = name, Size = size };
                // a vector of `count` elements, each one UInt8 and `empty` empty classes (the review's slowsf.py; 25 s for 1,000 x 10^6)
                byte[] Vector(int empty, int count)
                {
                    var nodes = new List<SfBuild.Node> { N(0, "X", "Base", -1), N(1, "vector", "v", -1), N(2, "Array", "Array", -1), N(3, "int", "size", 4), N(3, "E", "data", -1), N(4, "UInt8", "b", 1) };
                    nodes.AddRange(Enumerable.Repeat(N(4, "Y", "y", 0), empty));
                    var data = new byte[(4 + count + 3) & ~3];
                    BitConverter.GetBytes(count).CopyTo(data, 0);
                    return SfBuild.Bundle(nodes, new[] { data });
                }
                // `objects` objects of size 0 sharing one type of `empty` empty fields (6.7 s for 20,000 x 20,000)
                byte[] Objects(int empty, int objects)
                {
                    var nodes = new List<SfBuild.Node> { N(0, "R", "Base", -1) };
                    nodes.AddRange(Enumerable.Repeat(N(1, "Y", "y", 0), empty));
                    return SfBuild.Bundle(nodes, Enumerable.Repeat(new byte[0], objects).ToList());
                }
                // `count` nodes whose type and name are one string of `length` bytes (7 s for 3,000 x 1 MB)
                byte[] Names(int count, int length)
                {
                    var name = new string('A', length);
                    var nodes = new List<SfBuild.Node> { N(0, "R", "Base", -1) };
                    nodes.AddRange(Enumerable.Repeat(N(1, name, name, 0), count));
                    return SfBuild.Bundle(nodes, new[] { new byte[0] });
                }
                // control: small files of the same shapes pass the strict reading and fail only for the missing AssetBundle object
                foreach (var small in new Dictionary<string, byte[]> { ["vector"] = Vector(10, 1000), ["objects"] = Objects(10, 10), ["names"] = Names(3, 100) })
                    T.Contains(Invalid(small.Value, "small " + small.Key), "AssetBundle の情報が壊れています", "small " + small.Key);
                var cases = new[]
                {
                    Tuple.Create("vector", Vector(1000, 1000000), "オブジェクトのデータが型情報と一致しません"),
                    Tuple.Create("objects", Objects(20000, 20000), "オブジェクトのデータが型情報と一致しません"),
                    Tuple.Create("names", Names(3000, 1 << 20), "アセットファイルの型情報が壊れています"),
                };
                foreach (var c in cases)
                {
                    var sw = System.Diagnostics.Stopwatch.StartNew();
                    string msg = Invalid(c.Item2, c.Item1);
                    sw.Stop();
                    r.Note($"{c.Item1}: {c.Item2.Length:N0} bytes, {sw.ElapsedMilliseconds} ms: {msg}");
                    T.Contains(msg, c.Item3, c.Item1 + ": message");
                    T.True(sw.ElapsedMilliseconds < 3000, $"{c.Item1}: Load took {sw.ElapsedMilliseconds} ms");
                }
            });

            r.Test("Bundle strict: an LZMA bundle compressed far better than LZ4 can (more than 255:1) loads with exactly its texts (R9)", () =>
            {
                var exp = want.Keys.ToDictionary(n => n, n => n + "\n" + new string('あ', 20000), StringComparer.Ordinal);
                byte[] saved;
                using (var b = LocalisationBundle.Load(o, "x.bundle"))
                {
                    foreach (var kv in exp)
                        b.SetText(kv.Key, kv.Value);
                    saved = b.Save();
                }
                byte[] lzma = Forge.Packed(Forge.Unpacked(saved), false, AssetsTools.NET.AssetBundleCompressionType.LZMA);
                var file = new AssetsTools.NET.AssetBundleFile();
                file.Read(new AssetsTools.NET.AssetsFileReader(new MemoryStream(lzma, false)));
                var blocks = file.BlockAndDirInfo.BlockInfos;
                r.Note($"{lzma.Length:N0} bytes, blocks (decompressed / compressed, flags): " +
                       string.Join(", ", blocks.Select(x => $"{x.DecompressedSize:N0} / {x.CompressedSize:N0}, 0x{x.Flags:X}")));
                T.True(blocks.All(x => (x.Flags & 0x3F) == 1), "LZMA blocks");
                T.True(blocks.Any(x => x.DecompressedSize > 255L * x.CompressedSize + 64), "a block beyond the LZ4 expansion limit");
                Util.AssertTexts(exp, Util.Texts(lzma), "texts");
            });

            r.Test("Bundle strict: a format this patcher does not read is reported as unsupported, not as damage (R10): UnityFS 5 / 9 / 16 / 99, serialized file 21 / 23 / 99 / big endian; version 0 or above 99 is damage", () =>
            {
                foreach (uint v in new uint[] { 5, 9, 16, 99 })
                {
                    var h = Forge.Parse(o);
                    h.Version = v;
                    Unsupported(Forge.Write(h), $"UnityFS {v}", $"対応していないバンドルの形式です（版 {v}）");
                }
                foreach (uint v in new uint[] { 0, 100, 0x01000008 })
                {
                    var h = Forge.Parse(o);
                    h.Version = v;
                    T.Contains(Invalid(Forge.Write(h), $"UnityFS {v}"), $"対応していないバンドルの形式です（版 {v}）", $"UnityFS {v} (damage): message");
                }
                var h9 = Forge.Parse(o);
                h9.Version = 9;
                // checked before AssetsTools.NET reads the header: the rest may have another layout
                Unsupported(Forge.Write(h9).Take(40).ToArray(), "UnityFS 9, cut after 40 bytes", "対応していないバンドルの形式です（版 9）");
                Unsupported(Forge.SetU32(o, 8, 9), "UnityFS 9 in the original compressed file", "対応していないバンドルの形式です（版 9）");
                foreach (uint v in new uint[] { 21, 23, 99 })
                    Unsupported(sf.Edit(d => Forge.PutBE(d, sf.At + 8, v, 4)), $"serialized file format {v}", $"アセットファイルの形式に対応していません（版 {v}）");
                Unsupported(sf.Edit(d => d[sf.At + 16] = 1), "big-endian serialized file", "アセットファイルの形式に対応していません（ビッグエンディアン）");
                foreach (uint v in new uint[] { 0, 100, 0x02020016 })
                    T.Contains(Invalid(sf.Edit(d => Forge.PutBE(d, sf.At + 8, v, 4)), $"serialized file format {v}"), $"アセットファイルの形式に対応していません（版 {v}）",
                        $"serialized file format {v} (damage): message");
                T.Contains(Invalid(sf.Edit(d => d[sf.At + 16] = 2), "serialized file endianness byte 2"), "ビッグエンディアン", "endianness byte 2 (damage): message");
                // the official serialized file with the header of format 21 (32-bit sizes, metadata at 20), which
                // AssetsTools.NET also reads, and its control: the same file wrapped with its own format 22 header
                byte[] own = sf.Bundle.Skip(sf.At).Take((int)sf.FileSize).ToArray();
                Util.AssertTexts(want, Util.Texts(SfBuild.Wrap(own)), "control: the serialized file in a bundle of one uncompressed block");
                var old = new byte[own.Length - 28];
                Forge.PutBE(old, 0, sf.MetadataSize, 4);
                Forge.PutBE(old, 4, old.Length, 4);
                Forge.PutBE(old, 8, 21, 4);
                Forge.PutBE(old, 12, sf.DataOffset - 28, 4);
                Buffer.BlockCopy(own, 48, old, 20, own.Length - 48);
                var am = new AssetsTools.NET.Extra.AssetsManager();
                try
                {
                    var bun = am.LoadBundleFile(new MemoryStream(SfBuild.Wrap(old), false), "old.bundle", true);
                    T.True(bun.file.IsAssetsFile(0), "AssetsTools.NET recognises the format 21 file");
                }
                finally
                {
                    am.UnloadAll(true);
                }
                Unsupported(SfBuild.Wrap(old), "serialized file with the header of format 21", "アセットファイルの形式に対応していません（版 21）");
                // damage next to these fields stays damage (Invalid asserts that), and UnityFS 7 still loads
                var sig = Forge.SetU32(o, 8, 9);
                sig[5] = (byte)'X';
                Invalid(sig, "signature UnityXS with version 9");
                Invalid(o.Take(11).ToArray(), "cut inside the version field");
                Invalid(sf.Edit(d => Forge.PutBE(d, sf.At + 20, sf.MetadataSize + 4, 4)), "serialized file metadata size +4");
                var h7 = Forge.Parse(o);
                h7.Version = 7;
                Util.AssertTexts(want, Util.Texts(Forge.Write(h7)), "UnityFS 7");
            });

            r.Test("Bundle strict: bundles written by UnityPy (gen_vectors.py: block info at the end 0x80, LZ4HC, uncompressed, LZMA, flags 0x243, padded, LZMA beyond 255:1) load with exactly their texts", () =>
            {
                string tsv = Path.Combine(ctx.VectorsDir, "unitypy_texts.tsv");
                T.True(File.Exists(tsv), "unitypy_texts.tsv was generated");
                var labels = File.ReadAllLines(tsv).Where(l => l.Length > 0).Select(l => l.Split('\t')).GroupBy(f => f[0]).ToList();
                T.Eq(7, labels.Count, "UnityPy bundles");
                // R9: the repetitive LZMA bundle really is compressed beyond what LZ4 could expand
                var repetitive = new AssetsTools.NET.AssetBundleFile();
                repetitive.Read(new AssetsTools.NET.AssetsFileReader(new MemoryStream(File.ReadAllBytes(Path.Combine(ctx.VectorsDir, "unitypy_lzma_repetitive.bundle")), false)));
                var blocks = repetitive.BlockAndDirInfo.BlockInfos;
                T.True(blocks.All(x => (x.Flags & 0x3F) == 1) && blocks.Any(x => x.DecompressedSize > 255L * x.CompressedSize + 64),
                    "lzma_repetitive: LZMA beyond 255:1 (" + string.Join(", ", blocks.Select(x => $"{x.DecompressedSize:N0} / {x.CompressedSize:N0}")) + ")");
                foreach (var g in labels)
                {
                    var got = Util.Texts(File.ReadAllBytes(Path.Combine(ctx.VectorsDir, $"unitypy_{g.Key}.bundle")));
                    T.Eq(string.Join(",", g.Select(f => f[1]).OrderBy(n => n, StringComparer.Ordinal)), string.Join(",", got.Keys.OrderBy(n => n, StringComparer.Ordinal)), g.Key + ": names");
                    foreach (var f in g)
                        T.Eq(f[2], Util.Sha256(got[f[1]]), $"{g.Key}: {f[1]}");
                    using (var b = LocalisationBundle.Load(File.ReadAllBytes(Path.Combine(ctx.VectorsDir, $"unitypy_{g.Key}.bundle")), "u.bundle"))
                        Util.AssertTexts(got, Util.Texts(b.Save()), g.Key + ": saved again by the patcher");
                }
            });

            r.Test("Bundle strict: Save() after SetText of long multi-byte texts in several TextAssets reloads (AssetsTools.NET's LZ4 passes the strict decoder)", () =>
            {
                var rng = new Random(12);
                var exp = new Dictionary<string, string>(want);
                byte[] saved;
                using (var b = LocalisationBundle.Load(o, "x.bundle"))
                {
                    foreach (var name in want.Keys.Take(6))
                    {
                        var sb = new StringBuilder();
                        for (int i = rng.Next(1, 200000); i > 0; i--)
                            sb.Append(rng.Next(4) == 0 ? (char)rng.Next(0x20, 0x7F) : (char)rng.Next(0x3041, 0x30FF));
                        exp[name] = sb.ToString();
                        b.SetText(name, exp[name]);
                    }
                    saved = b.Save();
                }
                Util.AssertTexts(exp, Util.Texts(saved), "reloaded");
                r.Note($"{saved.Length:N0} bytes");
            });

            r.Test("Bundle strict: a bundle without type trees (as Unity writes with DisableWriteTypeTree) still loads", () =>
            {
                var noTrees = Forge.Rebuild(o, (am, inst) => inst.file.Metadata.TypeTreeEnabled = false);
                Util.AssertTexts(want, Util.Texts(noTrees), "texts");
            });

            r.Test("Bundle strict: the genuine bundles of the other languages load; localisation_assets_localisation.bundle passes every structure check (no TextAsset)", () =>
            {
                string dir = Environment.GetEnvironmentVariable("TPCJP_REAL_BUNDLES") ?? Path.Combine(ctx.ProjectRoot, "work", "localtest", "real_bundles");
                if (!Directory.Exists(dir))
                    throw new SkipException("copies of the game's other localisation bundles not found: set TPCJP_REAL_BUNDLES or use " + dir);
                int n = 0;
                long work = 0;
                foreach (var f in Directory.GetFiles(dir, "localisation_assets_localisation*.bundle"))
                {
                    var data = File.ReadAllBytes(f);
                    if (Path.GetFileName(f) == "localisation_assets_localisation.bundle")
                    {
                        // MonoBehaviour language sources, no TextAsset: rejected only by the last check of Load
                        T.Contains(Invalid(data, Path.GetFileName(f)), "TextAsset が見つかりません", Path.GetFileName(f));
                        continue;
                    }
                    using (var b = LocalisationBundle.Load(data, Path.GetFileName(f)))
                    {
                        T.Eq(25, b.Names.Count(), Path.GetFileName(f) + ": TextAssets");
                        Util.AssertTexts(Util.Texts(b), Util.Texts(b.Save()), Path.GetFileName(f) + ": saved by the patcher");
                        // R8: the work limit of the strict reading leaves genuine bundles a wide margin
                        T.True(b.CheckWork * 100 < b.CheckBudget, $"{Path.GetFileName(f)}: work {b.CheckWork:N0} of {b.CheckBudget:N0}");
                        work = Math.Max(work, b.CheckWork);
                    }
                    n++;
                }
                T.True(n >= 12, $"{n} language bundles in {dir}");
                r.Note($"{n} language bundles loaded and saved (strict reading: at most {work:N0} work units)");
            });

            r.Test("Bundle strict: Load time of the official bundle with the structure checks", () =>
            {
                long work, budget;
                using (var b = LocalisationBundle.Load(o, "w.bundle"))
                {
                    work = b.CheckWork;
                    budget = b.CheckBudget;
                }
                var sw = System.Diagnostics.Stopwatch.StartNew();
                for (int i = 0; i < 20; i++)
                    using (LocalisationBundle.Load(o, "t.bundle")) { }
                double ms = sw.Elapsed.TotalMilliseconds / 20;
                r.Note($"{ms:F1} ms per Load; strict reading {work:N0} of {budget:N0} work units");
                T.True(ms < 200, $"{ms:F1} ms per Load");
                T.True(work * 100 < budget, $"work {work:N0} of {budget:N0} (R8: wide margin for genuine bundles)");
            });
        }
    }

    /// <summary>
    /// The official bundle unpacked (stored blocks), with the position of its serialized file and the header, object
    /// table and type trees as AssetsTools.NET reads them, so that bytes of the serialized file can be changed in
    /// place while the container stays consistent.
    /// </summary>
    sealed class SfForge
    {
        public readonly byte[] Bundle;
        public readonly int At;
        public readonly long MetadataSize, FileSize, DataOffset;
        public readonly List<AssetsTools.NET.AssetFileInfo> Infos;
        public readonly List<AssetsTools.NET.TypeTreeType> Types;

        public SfForge(byte[] bundle)
        {
            Bundle = Forge.Unpacked(bundle);
            var am = new AssetsTools.NET.Extra.AssetsManager();
            try
            {
                var bun = am.LoadBundleFile(new MemoryStream(Bundle, false), "sf.bundle", false);
                At = (int)(bun.file.Header.GetFileDataOffset() + bun.file.BlockAndDirInfo.DirectoryInfos[0].Offset);
                var inst = am.LoadAssetsFileFromBundle(bun, 0, false);
                MetadataSize = inst.file.Header.MetadataSize;
                FileSize = inst.file.Header.FileSize;
                DataOffset = inst.file.Header.DataOffset;
                Infos = inst.file.AssetInfos.ToList();
                Types = inst.file.Metadata.TypeTreeTypes.ToList();
            }
            finally
            {
                am.UnloadAll(true);
            }
            T.Eq(22L, (long)Forge.U32(Bundle, At + 8), "SfForge: serialized file format version at its position");
        }

        public int ClassId(AssetsTools.NET.AssetFileInfo info) => Types[info.TypeIdOrIndex].TypeId;

        public byte[] Edit(Action<byte[]> edit)
        {
            var d = (byte[])Bundle.Clone();
            edit(d);
            return d;
        }

        /// <summary>Position of the pattern in Bundle within [from, to), or -1.</summary>
        public int Find(byte[] pattern, int from, int to)
        {
            for (int i = from; i + pattern.Length <= to; i++)
            {
                int k = 0;
                while (k < pattern.Length && Bundle[i + k] == pattern[k])
                    k++;
                if (k == pattern.Length)
                    return i;
            }
            return -1;
        }

        int InMetadata(byte[] pattern, string what)
        {
            int p = Find(pattern, At + 48, At + 48 + (int)MetadataSize);
            T.True(p >= 0, "SfForge: " + what + " not found");
            return p;
        }

        /// <summary>The object table entry: path ID, offset from the data, size, type index.</summary>
        public int EntryPos(AssetsTools.NET.AssetFileInfo info) =>
            InMetadata(BitConverter.GetBytes(info.PathId).Concat(BitConverter.GetBytes(info.ByteOffset)).Concat(BitConverter.GetBytes(info.ByteSize)).ToArray(), "object entry");

        public int ObjectPos(AssetsTools.NET.AssetFileInfo info) => At + (int)(DataOffset + info.ByteOffset);

        /// <summary>A type tree node record (32 bytes); the first match in the metadata.</summary>
        public int NodePos(AssetsTools.NET.TypeTreeType type, int index)
        {
            var n = type.Nodes[index];
            var rec = BitConverter.GetBytes(n.Version).Concat(new[] { n.Level, (byte)n.TypeFlags }).Concat(BitConverter.GetBytes(n.TypeStrOffset))
                .Concat(BitConverter.GetBytes(n.NameStrOffset)).Concat(BitConverter.GetBytes(n.ByteSize)).Concat(BitConverter.GetBytes(n.Index))
                .Concat(BitConverter.GetBytes(n.MetaFlags)).Concat(BitConverter.GetBytes(n.RefTypeHash)).ToArray();
            return InMetadata(rec, "type tree node");
        }
    }

    /// <summary>
    /// R8: a minimal serialized file (format 22; one type, class 1, with a type tree; objects of that type) in an
    /// uncompressed bundle, for forged files that are expensive to check.
    /// </summary>
    static class SfBuild
    {
        public sealed class Node { public int Level, Size; public string Type, Name; }

        public static byte[] Bundle(IList<Node> nodes, IList<byte[]> objects)
        {
            var strings = new MemoryStream();
            var offsets = new Dictionary<string, int>(StringComparer.Ordinal);
            int Str(string s)
            {
                if (!offsets.TryGetValue(s, out int at))
                {
                    at = (int)strings.Length;
                    var b = Encoding.UTF8.GetBytes(s);
                    strings.Write(b, 0, b.Length);
                    strings.WriteByte(0);
                    offsets.Add(s, at);
                }
                return at;
            }
            var records = new MemoryStream();
            var rw = new BinaryWriter(records);
            for (int i = 0; i < nodes.Count; i++)
            {
                // version, level, flags (array), type and name offsets, byte size, index, meta flags, reference hash
                var n = nodes[i];
                rw.Write((ushort)1);
                rw.Write((byte)n.Level);
                rw.Write((byte)(n.Type == "Array" ? 1 : 0));
                rw.Write(Str(n.Type));
                rw.Write(Str(n.Name));
                rw.Write(n.Size);
                rw.Write(i);
                rw.Write(0);
                rw.Write(0L);
            }
            rw.Flush();
            var meta = new MemoryStream();
            var w = new BinaryWriter(meta);
            w.Write(Encoding.ASCII.GetBytes("2020.3.47f1\0"));
            w.Write(19); // target platform
            w.Write((byte)1); // type trees
            w.Write(1); // one type: class 1, not stripped, no script, hash, type tree, no dependencies
            w.Write(1);
            w.Write((byte)0);
            w.Write((short)-1);
            w.Write(new byte[16]);
            w.Write(nodes.Count);
            w.Write((int)strings.Length);
            w.Write(records.ToArray());
            w.Write(strings.ToArray());
            w.Write(0);
            w.Write(objects.Count);
            while ((48 + meta.Length) % 4 != 0)
                w.Write((byte)0);
            var starts = new List<long>();
            long size = 0;
            foreach (var obj in objects)
            {
                starts.Add(size);
                size += (obj.Length + 7) & ~7;
            }
            for (int i = 0; i < objects.Count; i++)
            {
                w.Write((long)(i + 1)); // path ID
                w.Write(starts[i]);
                w.Write((uint)objects[i].Length);
                w.Write(0); // type index
            }
            w.Write(0); // script types
            w.Write(0); // external files
            w.Write(0); // reference types
            w.Write((byte)0); // user information
            w.Flush();
            byte[] metadata = meta.ToArray();
            int dataOffset = (48 + metadata.Length + 15) & ~15;
            var sf = new byte[dataOffset + size];
            Forge.PutBE(sf, 8, 22, 4);
            Forge.PutBE(sf, 20, metadata.Length, 4);
            Forge.PutBE(sf, 24, sf.Length, 8);
            Forge.PutBE(sf, 32, dataOffset, 8);
            metadata.CopyTo(sf, 48);
            for (int i = 0; i < objects.Count; i++)
                objects[i].CopyTo(sf, dataOffset + starts[i]);
            return Wrap(sf);
        }

        /// <summary>A UnityFS 8 bundle with the serialized file as its only entry, in one uncompressed block.</summary>
        public static byte[] Wrap(byte[] sf)
        {
            var h = new Forge.Header { Sig = "UnityFS", Version = 8, Gen = "5.x.x", Eng = "2020.3.47f1", Flags = 0x240, Data = sf };
            h.Blocks.Add(new Forge.Block { Decompressed = (uint)sf.Length, Compressed = (uint)sf.Length, Flags = 0x40 });
            h.Dirs.Add(new Forge.Dir { Offset = 0, Size = sf.Length, Flags = 4, Name = "CAB-0123456789abcdef0123456789abcdef" });
            return Forge.Write(h);
        }
    }

    /// <summary>
    /// Builds forged variants of a UnityFS bundle: the header and the block/directory info are re-serialized
    /// (block info stored uncompressed, data blocks copied unchanged), so single fields can be changed.
    /// </summary>
    static class Forge
    {
        public sealed class Block { public uint Decompressed, Compressed; public ushort Flags; }
        public sealed class Dir { public long Offset, Size; public uint Flags; public string Name; }

        public sealed class Header
        {
            public string Sig, Gen, Eng;
            public uint Version, Flags;
            public List<Block> Blocks = new List<Block>();
            public List<Dir> Dirs = new List<Dir>();
            public byte[] Data; // the data blocks as stored
        }

        public static uint U32(byte[] b, int p) => (uint)(b[p] << 24 | b[p + 1] << 16 | b[p + 2] << 8 | b[p + 3]);

        public static byte[] SetU32(byte[] bundle, int p, uint v)
        {
            var d = (byte[])bundle.Clone();
            PutBE(d, p, v, 4);
            return d;
        }

        public static void PutBE(byte[] d, int p, long v, int bytes)
        {
            for (int i = 0; i < bytes; i++)
                d[p + i] = (byte)(v >> (8 * (bytes - 1 - i)));
        }

        public static void PutLE(byte[] d, int p, long v, int bytes)
        {
            for (int i = 0; i < bytes; i++)
                d[p + i] = (byte)(v >> (8 * i));
        }

        static string CStr(byte[] b, ref int p)
        {
            int e = Array.IndexOf(b, (byte)0, p);
            string s = Encoding.UTF8.GetString(b, p, e - p);
            p = e + 1;
            return s;
        }

        /// <summary>Position of the 8-byte total size field (followed by the block info sizes and the flags).</summary>
        public static int SizeFieldPos(byte[] b)
        {
            int p = 0;
            CStr(b, ref p);
            p += 4;
            CStr(b, ref p);
            CStr(b, ref p);
            return p;
        }

        public static byte[] SetTotalSize(byte[] bundle, long size)
        {
            var d = (byte[])bundle.Clone();
            int p = SizeFieldPos(d);
            for (int i = 0; i < 8; i++)
                d[p + i] = (byte)(size >> (56 - 8 * i));
            return d;
        }

        public static Header Parse(byte[] bundle)
        {
            var h = new Header();
            int p = 0;
            h.Sig = CStr(bundle, ref p);
            h.Version = U32(bundle, p);
            p += 4;
            h.Gen = CStr(bundle, ref p);
            h.Eng = CStr(bundle, ref p);
            p += 8; // total size
            uint packedInfo = U32(bundle, p);
            h.Flags = U32(bundle, p + 8);
            p += 12;
            if ((h.Flags & 0x80) != 0)
                throw new InvalidOperationException("block info at the end: not supported by the forging helper");
            if ((h.Flags & 0x200) != 0)
                p = (p + 15) & ~15;
            var file = new AssetsTools.NET.AssetBundleFile();
            file.Read(new AssetsTools.NET.AssetsFileReader(new MemoryStream(bundle, false)));
            foreach (var b in file.BlockAndDirInfo.BlockInfos)
                h.Blocks.Add(new Block { Decompressed = b.DecompressedSize, Compressed = b.CompressedSize, Flags = b.Flags });
            foreach (var d in file.BlockAndDirInfo.DirectoryInfos)
                h.Dirs.Add(new Dir { Offset = d.Offset, Size = d.DecompressedSize, Flags = d.Flags, Name = d.Name });
            int data = p + (int)packedInfo;
            if ((h.Flags & 0x200) != 0)
                data = (data + 15) & ~15; // the data blocks are aligned as well
            T.Eq(file.Header.GetFileDataOffset(), (long)data, "forging helper: data offset agrees with AssetsTools.NET");
            h.Data = bundle.Skip(data).ToArray();
            return h;
        }

        static void BE(Stream s, ulong v, int bytes)
        {
            for (int i = bytes - 1; i >= 0; i--)
                s.WriteByte((byte)(v >> (8 * i)));
        }

        static void CStr(Stream s, string v)
        {
            var b = Encoding.UTF8.GetBytes(v);
            s.Write(b, 0, b.Length);
            s.WriteByte(0);
        }

        /// <summary>Header + uncompressed block info + the data; the total size field is set to the result's length.</summary>
        public static byte[] Write(Header h)
        {
            var info = new MemoryStream();
            info.Write(new byte[16], 0, 16); // hash (not checked)
            BE(info, (ulong)h.Blocks.Count, 4);
            foreach (var b in h.Blocks)
            {
                BE(info, b.Decompressed, 4);
                BE(info, b.Compressed, 4);
                BE(info, b.Flags, 2);
            }
            BE(info, (ulong)h.Dirs.Count, 4);
            foreach (var d in h.Dirs)
            {
                BE(info, (ulong)d.Offset, 8);
                BE(info, (ulong)d.Size, 8);
                BE(info, d.Flags, 4);
                CStr(info, d.Name);
            }
            var o = new MemoryStream();
            CStr(o, h.Sig);
            BE(o, h.Version, 4);
            CStr(o, h.Gen);
            CStr(o, h.Eng);
            long sizePos = o.Position;
            BE(o, 0, 8);
            BE(o, (ulong)info.Length, 4);
            BE(o, (ulong)info.Length, 4);
            BE(o, h.Flags & ~0x3Fu, 4); // block info not compressed
            if ((h.Flags & 0x200) != 0)
                while (o.Position % 16 != 0)
                    o.WriteByte(0);
            info.WriteTo(o);
            if ((h.Flags & 0x200) != 0)
                while (o.Position % 16 != 0)
                    o.WriteByte(0);
            o.Write(h.Data, 0, h.Data.Length);
            var result = o.ToArray();
            return SetTotalSize(result, result.Length);
        }

        public static byte[] Unpacked(byte[] bundle)
        {
            var file = new AssetsTools.NET.AssetBundleFile();
            file.Read(new AssetsTools.NET.AssetsFileReader(new MemoryStream(bundle, false)));
            var ms = new MemoryStream();
            var w = new AssetsTools.NET.AssetsFileWriter(ms);
            file.Unpack(w);
            w.Flush();
            return ms.ToArray();
        }

        public static byte[] Packed(byte[] unpacked, bool blockInfoAtEnd,
            AssetsTools.NET.AssetBundleCompressionType compression = AssetsTools.NET.AssetBundleCompressionType.LZ4)
        {
            var file = new AssetsTools.NET.AssetBundleFile();
            file.Read(new AssetsTools.NET.AssetsFileReader(new MemoryStream(unpacked, false)));
            var ms = new MemoryStream();
            var w = new AssetsTools.NET.AssetsFileWriter(ms);
            file.Pack(w, compression, blockInfoAtEnd, null);
            w.Flush();
            return ms.ToArray();
        }

        /// <summary>Loads the bundle with AssetsTools.NET, lets `mutate` change the assets file and saves it like LocalisationBundle.Save.</summary>
        public static byte[] Rebuild(byte[] bundle, Action<AssetsTools.NET.Extra.AssetsManager, AssetsTools.NET.Extra.AssetsFileInstance> mutate)
        {
            var am = new AssetsTools.NET.Extra.AssetsManager();
            try
            {
                var bun = am.LoadBundleFile(new MemoryStream(bundle, false), "forge.bundle", true);
                int idx = Enumerable.Range(0, bun.file.BlockAndDirInfo.DirectoryInfos.Count).First(i => bun.file.IsAssetsFile(i));
                var inst = am.LoadAssetsFileFromBundle(bun, idx, false);
                mutate(am, inst);
                bun.file.BlockAndDirInfo.DirectoryInfos[idx].SetNewData(inst.file);
                var plain = new MemoryStream();
                var w = new AssetsTools.NET.AssetsFileWriter(plain);
                bun.file.Write(w, 0);
                w.Flush();
                return Packed(plain.ToArray(), false);
            }
            finally
            {
                am.UnloadAll(true);
            }
        }

        public static string TextAssetName(AssetsTools.NET.Extra.AssetsFileInstance inst, AssetsTools.NET.AssetFileInfo info)
        {
            var reader = inst.file.Reader;
            reader.Position = info.GetAbsoluteByteOffset(inst.file);
            var raw = reader.ReadBytes((int)info.ByteSize);
            return Encoding.UTF8.GetString(raw, 4, BitConverter.ToInt32(raw, 0));
        }

        /// <summary>Serialized TextAsset (Unity 2020.3): m_Name, m_Script, each int32 length + UTF-8, aligned to 4.</summary>
        public static byte[] TextAssetRaw(string name, string script)
        {
            var ms = new MemoryStream();
            foreach (var s in new[] { name, script })
            {
                var b = Encoding.UTF8.GetBytes(s);
                ms.Write(BitConverter.GetBytes(b.Length), 0, 4);
                ms.Write(b, 0, b.Length);
                while (ms.Length % 4 != 0)
                    ms.WriteByte(0);
            }
            return ms.ToArray();
        }
    }
}
