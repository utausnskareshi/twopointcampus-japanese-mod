using System;
using System.Collections.Generic;

namespace TpcJpPatch
{
    /// <summary>
    /// The game's I2 Localization cache text: "Term=Value[i2t]Term=Value...".
    /// Untouched entries are written back byte-for-byte; only replaced values are re-serialized.
    /// A few terms occur twice; like the extraction tools, lookups use the last occurrence and
    /// replacements are applied to every occurrence.
    /// </summary>
    sealed class I2Cache
    {
        const string Separator = "[i2t]";

        sealed class Entry
        {
            public string Raw;
            public string Key;
            public string Value;
            public bool Modified;
        }

        readonly List<Entry> entries = new List<Entry>();
        readonly Dictionary<string, List<Entry>> byKey = new Dictionary<string, List<Entry>>(StringComparer.Ordinal);

        public I2Cache(string text)
        {
            if (text.Length == 0)
                return;
            foreach (var part in text.Split(new[] { Separator }, StringSplitOptions.None))
            {
                int eq = part.IndexOf('=');
                var e = new Entry
                {
                    Raw = part,
                    Key = eq < 0 ? part : part.Substring(0, eq),
                    Value = eq < 0 ? "" : part.Substring(eq + 1),
                };
                entries.Add(e);
                if (!byKey.TryGetValue(e.Key, out var list))
                    byKey.Add(e.Key, list = new List<Entry>(1));
                list.Add(e);
            }
        }

        public int Count => entries.Count;

        public bool TryGetValue(string key, out string value)
        {
            if (byKey.TryGetValue(key, out var list))
            {
                value = list[list.Count - 1].Value;
                return true;
            }
            value = null;
            return false;
        }

        public void SetValue(string key, string value)
        {
            foreach (var e in byKey[key])
            {
                e.Value = value;
                e.Modified = true;
            }
        }

        public override string ToString()
        {
            var parts = new string[entries.Count];
            for (int i = 0; i < entries.Count; i++)
            {
                var e = entries[i];
                parts[i] = e.Modified ? e.Key + "=" + e.Value : e.Raw;
            }
            return string.Join(Separator, parts);
        }
    }
}
