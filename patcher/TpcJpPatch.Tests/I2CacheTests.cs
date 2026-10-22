using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace TpcJpPatch.Tests
{
    static class I2CacheTests
    {
        public static void Run(Runner r, Ctx ctx)
        {
            r.Test("I2Cache: ToString() is the exact identity on every TextAsset of the official bundle", () =>
            {
                var texts = Util.Texts(ctx.Official);
                T.Eq(25, texts.Count, "TextAsset count");
                foreach (var kv in texts)
                {
                    var c = new I2Cache(kv.Value);
                    T.True(c.ToString() == kv.Value, "identity broken for " + kv.Key);
                    T.Eq(Util.ParseI2(kv.Value).Count, c.Count, "entry count of " + kv.Key);
                }
            });

            r.Test("I2Cache: lookups agree with tools/tpcloc/i2cache.parse on the official bundle (Python vectors)", () =>
            {
                var texts = Util.Texts(ctx.Official);
                var caches = texts.ToDictionary(kv => kv.Key, kv => new I2Cache(kv.Value));
                int keys = 0, dups = 0, textsSeen = 0;
                foreach (var line in File.ReadAllLines(Path.Combine(ctx.VectorsDir, "official_texts.tsv")))
                {
                    var f = line.Split('\t');
                    if (f[0] == "T")
                    {
                        textsSeen++;
                        T.Eq(f[2], Util.Sha256(texts[f[1]]), "text sha256 of " + f[1] + " (UnityPy vs AssetsTools)");
                        T.Eq(int.Parse(f[3]), caches[f[1]].Count, "pair count of " + f[1]);
                    }
                    else if (f[0] == "K")
                    {
                        string key = Util.B64(f[2]);
                        T.True(caches[f[1]].TryGetValue(key, out var v), "key missing in C#: " + f[1] + "|" + key);
                        T.Eq(f[3], Util.Sha256(v), "last value of " + f[1] + "|" + key);
                        keys++;
                        if (f[4] != "1")
                            dups++;
                    }
                }
                T.Eq(25, textsSeen, "texts in vectors");
                T.True(dups > 0, "the official data has duplicate keys (test precondition)");
                r.Note($"{keys} distinct keys compared, {dups} duplicated keys");
            });

            r.Test("I2Cache: duplicate keys: TryGetValue returns the last occurrence, SetValue changes all", () =>
            {
                var c = new I2Cache("a=1[i2t]b=2[i2t]a=3[i2t]c=[i2t]a=5");
                T.True(c.TryGetValue("a", out var v) && v == "5", "last value");
                T.Eq(5, c.Count, "count");
                c.SetValue("a", "X");
                T.Eq("a=X[i2t]b=2[i2t]a=X[i2t]c=[i2t]a=X", c.ToString(), "all occurrences replaced");
                T.True(c.TryGetValue("c", out var e) && e == "", "empty value");
                T.True(!c.TryGetValue("d", out var none) && none == null, "missing key");
            });

            r.Test("I2Cache: duplicate keys in the official data are all replaced by SetValue", () =>
            {
                var texts = Util.Texts(ctx.Official);
                var src = texts["I2LS_Characters"];
                var dupKey = Util.ParseI2(src).GroupBy(p => p.Key).First(g => g.Count() > 1).Key;
                var c = new I2Cache(src);
                c.SetValue(dupKey, "置換テスト=値");
                var after = Util.ParseI2(c.ToString());
                T.True(after.Where(p => p.Key == dupKey).All(p => p.Value == "置換テスト=値"), "every occurrence replaced");
                T.Eq(Util.ParseI2(src).Count(p => p.Key == dupKey), after.Count(p => p.Key == dupKey), "occurrence count");
                var others = Util.ParseI2(src).Where(p => p.Key != dupKey).ToList();
                T.True(others.SequenceEqual(after.Where(p => p.Key != dupKey)), "other entries unchanged");
            });

            r.Test("I2Cache: values containing '=' split at the first '=' and survive a rewrite", () =>
            {
                string text = "k=a=b[i2t]x==[i2t]=nokey[i2t]plain[i2t]y=v";
                var c = new I2Cache(text);
                T.Eq(text, c.ToString(), "identity");
                T.True(c.TryGetValue("k", out var v1) && v1 == "a=b", "k");
                T.True(c.TryGetValue("x", out var v2) && v2 == "=", "x");
                T.True(c.TryGetValue("", out var v3) && v3 == "nokey", "empty key");
                T.True(c.TryGetValue("plain", out var v4) && v4 == "", "entry without '='");
                c.SetValue("k", "c=d=e");
                c.SetValue("y", "");
                T.Eq("k=c=d=e[i2t]x==[i2t]=nokey[i2t]plain[i2t]y=", c.ToString(), "rewritten");
                var again = new I2Cache(c.ToString());
                T.True(again.TryGetValue("k", out var v5) && v5 == "c=d=e", "re-parsed value with '='");
            });

            r.Test("I2Cache: edge texts (empty, separators only, LF/CR in values, separator-like text) are identities", () =>
            {
                foreach (var text in new[] { "", "[i2t]", "[i2t][i2t]", "a=1[i2t]", "[i2t]a=1", "a=line1\nline2\r\n[i2t]b=\n",
                             "a=[i2t[i2t]b=i2t]", "a=[I2T][i2t]b=2", "日本=語[i2t]😀=𠮷", "a" })
                {
                    var c = new I2Cache(text);
                    T.Eq(text, c.ToString(), "identity of " + T.Show(text));
                    T.Eq(Util.ParseI2(text).Count, c.Count, "count of " + T.Show(text));
                }
                T.Eq(0, new I2Cache("").Count, "empty text has no entries");
            });

            r.Test("I2Cache: only modified entries are re-serialized (raw text of others kept)", () =>
            {
                var c = new I2Cache("noeq[i2t]k=v[i2t]other=1");
                c.SetValue("k", "new");
                T.Eq("noeq[i2t]k=new[i2t]other=1", c.ToString(), "entry without '=' kept raw");
                c.SetValue("noeq", "x");
                T.Eq("noeq=x[i2t]k=new[i2t]other=1", c.ToString(), "entry without '=' gets one when set");
            });

            r.Test("I2Cache: SetValue of an unknown key throws (callers check TryGetValue first)", () =>
            {
                var c = new I2Cache("a=1");
                T.Throws<KeyNotFoundException>(() => c.SetValue("b", "x"), "unknown key");
                T.Eq("a=1", c.ToString(), "unchanged");
            });
        }
    }
}
