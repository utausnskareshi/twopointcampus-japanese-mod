using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using AssetsTools.NET;
using AssetsTools.NET.Extra;

namespace TpcJpPatch
{
    /// <summary>
    /// A localisation_assets_localisation&lt;language&gt;.bundle: an Addressables AssetBundle holding one
    /// serialized file whose TextAssets (I2LS_*) contain the I2 cache text of each source.
    /// TextAssets are read and written as raw data (m_Name, m_Script), so no Unity class database is needed.
    /// </summary>
    sealed class LocalisationBundle : IDisposable
    {
        sealed class TextAssetData
        {
            public AssetFileInfo Info;
            public byte[] NameField; // serialized m_Name including its alignment padding
            public string Script;
        }

        readonly AssetsManager manager = new AssetsManager();
        readonly BundleFileInstance bundle;
        readonly AssetsFileInstance assets;
        readonly int directoryIndex;
        readonly Dictionary<string, TextAssetData> texts = new Dictionary<string, TextAssetData>(StringComparer.Ordinal);
        static readonly UTF8Encoding StrictUtf8 = new UTF8Encoding(false, true);

        /// <summary>Work units the strict reading of the serialized file used, and the limit it had (see SerializedFile).</summary>
        internal readonly long CheckWork, CheckBudget;

        LocalisationBundle(byte[] data, string name)
        {
            bundle = manager.LoadBundleFile(new MemoryStream(data, false), name, true);
            var dirs = bundle.file.BlockAndDirInfo.DirectoryInfos;
            var reader = bundle.file.DataReader;
            // a serialized file (flag 4) in a format this patcher does not read is not damage: checked before
            // AssetsTools.NET looks for the assets file, which does not recognise every format
            for (int i = 0; i < dirs.Count; i++)
            {
                bundle.file.GetFileRange(i, out long at, out long size);
                if ((dirs[i].Flags & 4) == 0 || size < 20)
                    continue;
                reader.Position = at;
                SerializedFile.CheckFormat(reader.ReadBytes(20));
            }
            directoryIndex = -1;
            for (int i = 0; i < dirs.Count; i++)
            {
                if (bundle.file.IsAssetsFile(i))
                {
                    if (directoryIndex >= 0)
                        throw new InvalidDataException("バンドル内に複数のアセットファイルがあります（想定外の形式）");
                    directoryIndex = i;
                }
            }
            if (directoryIndex < 0)
                throw new InvalidDataException("バンドル内にアセットファイルが見つかりません");
            // Unity loads only an entry flagged as a serialized file (4)
            for (int i = 0; i < dirs.Count; i++)
            {
                if (i == directoryIndex ? dirs[i].Flags != 4 : (dirs[i].Flags & 4) != 0)
                    throw new InvalidDataException("バンドル内のファイルの種類が正しくありません");
            }
            // the serialized file is checked strictly before AssetsTools.NET (lenient) reads its metadata
            bundle.file.GetFileRange(directoryIndex, out long offset, out long length);
            reader.Position = offset;
            var layout = SerializedFile.Check(reader.ReadBytes((int)length));
            CheckWork = layout.WorkUsed;
            CheckBudget = layout.WorkBudget;
            assets = manager.LoadAssetsFileFromBundle(bundle, directoryIndex, false);
            if (assets.file.Header.FileSize != dirs[directoryIndex].DecompressedSize)
                throw new InvalidDataException("アセットファイルの大きさが一致しません");
            layout.CompareWith(assets.file);
            var pathIds = new HashSet<long>();
            foreach (var info in assets.file.AssetInfos)
            {
                if (!pathIds.Add(info.PathId))
                    throw new InvalidDataException("アセットの番号が重複しています");
            }

            foreach (var info in assets.file.GetAssetsOfType(AssetClassID.TextAsset))
            {
                var t = ReadTextAsset(info);
                if (texts.ContainsKey(t.Item1))
                    throw new InvalidDataException($"同名の TextAsset が複数あります: {t.Item1}");
                texts.Add(t.Item1, t.Item2);
            }
            if (texts.Count == 0)
                throw new InvalidDataException("TextAsset が見つかりません（ローカライズ用のバンドルではありません）");
        }

        public static LocalisationBundle Load(string path) => Load(File.ReadAllBytes(path), Path.GetFileName(path));

        /// <summary>
        /// Loads a bundle; damaged or unexpected data gives an InvalidDataException. For a format this patcher does not
        /// read (UnityFS format version 1..99 other than 6..8; serialized file format version 1..99 other than 22, or big
        /// endian) its inner exceptions include an UnsupportedFormatException (UnsupportedFormatException.In).
        /// </summary>
        public static LocalisationBundle Load(byte[] data, string name)
        {
            try
            {
                CheckStructure(data);
                return new LocalisationBundle(data, name);
            }
            catch (InvalidDataException e)
            {
                throw new InvalidDataException($"{name} を読み込めません（{e.Message}）", e);
            }
            catch (Exception e)
            {
                // AssetsTools.NET reports damaged data with many exception types (EndOfStream, IndexOutOfRange,
                // and OutOfMemory for a forged size: a real localisation bundle is about 0.5 MB)
                throw new InvalidDataException($"{name} を読み込めません（ファイルが壊れている可能性があります）", e);
            }
        }

        static readonly Regex VersionRx = new Regex(@"^\d+\.[0-9A-Za-z]+\.[0-9A-Za-z]+$");

        /// <summary>
        /// Header checks before any block is decompressed: a truncated, padded or partly overwritten file is
        /// rejected (AssetsTools.NET itself zero-fills a short block instead of failing).
        /// </summary>
        static void CheckStructure(byte[] data)
        {
            // another UnityFS version (e.g. from a newer Unity, perhaps with another header layout) is not damage: checked
            // before AssetsTools.NET reads the header
            if (data.Length >= 12 && Encoding.ASCII.GetString(data, 0, 8) == "UnityFS\0")
            {
                uint version = (uint)(data[8] << 24 | data[9] << 16 | data[10] << 8 | data[11]);
                if (version < 6 || version > 8)
                    throw Unsupported($"対応していないバンドルの形式です（版 {version}）", version > 0 && version < 100);
            }
            var file = new AssetBundleFile();
            file.Read(new AssetsFileReader(new MemoryStream(data, false)));
            var h = file.Header;
            if (h.Signature != "UnityFS")
                throw new InvalidDataException("Unity のバンドルではありません");
            if (!VersionRx.IsMatch(h.GenerationVersion ?? "") || !VersionRx.IsMatch(h.EngineVersion ?? ""))
                throw new InvalidDataException("ヘッダーの版の情報が正しくありません");
            long declared = h.FileStreamHeader.TotalFileSize;
            if (declared != data.Length)
                throw new InvalidDataException($"ファイルの大きさが一致しません（{data.Length:N0} / {declared:N0} バイト）");
            // compression of the block info (none, LZMA, LZ4, LZ4HC), 0x40 block and directory info combined (always
            // set by Unity), 0x80 block info at the end, 0x200 padding before the data blocks
            uint flags = (uint)h.FileStreamHeader.Flags;
            if ((flags & ~0x2FFu) != 0 || (flags & 0x40) == 0 || (flags & 0x3F) > 3)
                throw new InvalidDataException($"ヘッダーのフラグが正しくありません（0x{flags:X}）");
            long packed = 0, unpacked = 0;
            foreach (var block in file.BlockAndDirInfo.BlockInfos)
            {
                packed += block.CompressedSize;
                unpacked += block.DecompressedSize;
            }
            // with the block info at the end of the file (flag 0x80, as UnityPy writes it) it follows the data
            long trailer = ((uint)h.FileStreamHeader.Flags & 0x80) != 0 ? h.FileStreamHeader.CompressedSize : 0;
            if (h.GetFileDataOffset() + packed + trailer != data.Length)
                throw new InvalidDataException("データブロックの大きさが一致しません");
            foreach (var dir in file.BlockAndDirInfo.DirectoryInfos)
            {
                if (dir.Offset < 0 || dir.DecompressedSize < 0 || dir.Offset + dir.DecompressedSize > unpacked)
                    throw new InvalidDataException("バンドル内のファイルの位置が正しくありません");
            }
            if (unpacked > int.MaxValue)
                throw new InvalidDataException("データブロックの大きさが一致しません");
            CheckBlocks(data, file);
        }

        /// <summary>
        /// A format this patcher does not read (not necessarily damaged) is marked by an inner UnsupportedFormatException,
        /// unless the value can only be damage (<paramref name="plausible"/> false: e.g. a format version 0 or above 99).
        /// </summary>
        static InvalidDataException Unsupported(string message, bool plausible = true) =>
            plausible ? new InvalidDataException(message, new UnsupportedFormatException(message)) : new InvalidDataException(message);

        /// <summary>
        /// The block info and every data block must decode the way Unity and UnityPy decode them
        /// (LZ4_decompress_safe): the compressed bytes are consumed exactly and give exactly the declared size.
        /// AssetsTools.NET ignores trailing input and zero-fills a short result. LZMA is left to AssetsTools.NET.
        /// </summary>
        static void CheckBlocks(byte[] data, AssetBundleFile file)
        {
            var h = file.Header;
            var fs = h.FileStreamHeader;
            uint flags = (uint)fs.Flags;
            // the header: signature, format version, two version strings, then sizes and flags (20 bytes)
            int p = Array.IndexOf(data, (byte)0) + 1 + 4;
            p = Array.IndexOf(data, (byte)0, p) + 1;
            p = Array.IndexOf(data, (byte)0, p) + 1 + 20;
            if (h.Version >= 7)
                p = (p + 15) & ~15;
            long infoAt = (flags & 0x80) != 0 ? data.Length - (long)fs.CompressedSize : p;
            long dataAt = (flags & 0x80) != 0 ? p : p + (long)fs.CompressedSize;
            if ((flags & 0x200) != 0)
                dataAt = (dataAt + 15) & ~15L;
            // hash, block count, blocks (10 bytes each), directory count, entries (20 bytes and a name each)
            var info = file.BlockAndDirInfo;
            long infoSize = 16 + 4 + 10L * info.BlockInfos.Length + 4;
            foreach (var dir in info.DirectoryInfos)
                infoSize += 20 + Encoding.UTF8.GetByteCount(dir.Name ?? "") + 1;
            // a name that is not UTF-8 was read with U+FFFD
            bool names = info.DirectoryInfos.TrueForAll(dir => dir.Name != null && dir.Name.IndexOf((char)0xFFFD) < 0);
            byte[] buffer = null;
            if (!names || infoAt != h.GetBundleInfoOffset() || dataAt != h.GetFileDataOffset() || infoSize != fs.DecompressedSize
                || !DecodesExactly(data, infoAt, fs.CompressedSize, fs.DecompressedSize, (int)(flags & 0x3F), ref buffer))
                throw new InvalidDataException("ブロック情報が壊れています");
            long pos = dataAt;
            for (int i = 0; i < info.BlockInfos.Length; i++)
            {
                // block flags: compression and 0x40 (streamed)
                var block = info.BlockInfos[i];
                if ((block.Flags & ~0x7F) != 0 || !DecodesExactly(data, pos, block.CompressedSize, block.DecompressedSize, block.Flags & 0x3F, ref buffer))
                    throw new InvalidDataException($"データブロックが壊れています（{i + 1} / {info.BlockInfos.Length}）");
                pos += block.CompressedSize;
            }
        }

        static bool DecodesExactly(byte[] data, long at, uint packed, uint size, int compression, ref byte[] buffer)
        {
            // more than LZ4 can expand (255 bytes per input byte): a forged size, not allocated. LZMA has no such ratio
            // limit (its blocks are bounded by the total size, at most int.MaxValue, checked in CheckStructure).
            if (((compression == 2 || compression == 3) && size > 255L * packed + 64) || size > 0x7FFFFFC7)
                return false;
            switch (compression)
            {
                case 0:
                    return packed == size;
                case 1:
                    return true; // LZMA (not used for these bundles): decoded by AssetsTools.NET
                case 2:
                case 3:
                    if (buffer == null || buffer.Length < size)
                        buffer = new byte[size];
                    return Lz4DecodeExact(data, at, packed, buffer, (int)size);
                default:
                    return false;
            }
        }

        /// <summary>
        /// Decodes an LZ4 block like LZ4_decompress_safe into exactly <paramref name="size"/> bytes of
        /// <paramref name="dst"/>: false unless the input is consumed exactly and gives exactly that size, with the
        /// format's end rules (the last 5 bytes are literals, the last match starts 12 bytes before the end).
        /// Never reads outside src[at .. at + count): damaged input of any length gives false, not an exception.
        /// </summary>
        internal static bool Lz4DecodeExact(byte[] src, long at, long count, byte[] dst, int size)
        {
            if (at < 0 || count < 1 || count > src.Length - at || size < 0 || size > dst.Length)
                return false;
            int ip = (int)at, end = (int)(at + count), op = 0;
            while (true)
            {
                if (ip >= end)
                    return false; // the input ends after a match (e.g. inside or right after its length bytes)
                int token = src[ip++];
                int length = token >> 4;
                if (length == 15 && !Lz4Length(src, ref ip, end, ref length))
                    return false;
                if (length > end - ip || length > size - op)
                    return false;
                Buffer.BlockCopy(src, ip, dst, op, length);
                ip += length;
                op += length;
                if (ip == end)
                    return op == size; // the last sequence has literals only
                if (op > size - 12 || ip > end - 8)
                    return false;
                int offset = src[ip] | src[ip + 1] << 8;
                ip += 2;
                if (offset == 0 || offset > op)
                    return false;
                length = token & 15;
                if (length == 15 && !Lz4Length(src, ref ip, end, ref length))
                    return false;
                length += 4;
                if (length > size - 5 - op)
                    return false;
                if (offset >= length)
                    Buffer.BlockCopy(dst, op - offset, dst, op, length);
                else
                    for (int k = 0; k < length; k++)
                        dst[op + k] = dst[op - offset + k];
                op += length;
            }
        }

        static bool Lz4Length(byte[] src, ref int ip, int end, ref int length)
        {
            int s;
            do
            {
                if (ip >= end || length > 0x3FFFFFFF)
                    return false;
                s = src[ip++];
                length += s;
            } while (s == 255);
            return true;
        }

        public IEnumerable<string> Names => texts.Keys;

        public string GetText(string name) => texts[name].Script;

        public bool Contains(string name) => texts.ContainsKey(name);

        Tuple<string, TextAssetData> ReadTextAsset(AssetFileInfo info)
        {
            var reader = assets.file.Reader;
            long start = info.GetAbsoluteByteOffset(assets.file);
            reader.Position = start;
            byte[] raw = reader.ReadBytes((int)info.ByteSize);
            if (raw.Length != info.ByteSize)
                throw new InvalidDataException("TextAsset の読み込みに失敗しました");

            // TextAsset (Unity 2020.3): string m_Name; string m_Script — each int32 length + bytes, aligned to 4.
            int pos = 0;
            int nameLen = BitConverter.ToInt32(raw, pos);
            if (nameLen < 0 || 4 + nameLen > raw.Length)
                throw new InvalidDataException("TextAsset の形式が想定と異なります (m_Name)");
            string name = StrictUtf8.GetString(raw, 4, nameLen);
            pos = Align4(4 + nameLen);
            var nameField = new byte[pos];
            Buffer.BlockCopy(raw, 0, nameField, 0, pos);

            int scriptLen = BitConverter.ToInt32(raw, pos);
            if (scriptLen < 0 || pos + 4 + scriptLen > raw.Length)
                throw new InvalidDataException($"TextAsset の形式が想定と異なります (m_Script: {name})");
            string script = StrictUtf8.GetString(raw, pos + 4, scriptLen);
            if (script.IndexOf('\0') >= 0 || name.IndexOf('\0') >= 0)
                throw new InvalidDataException($"TextAsset に壊れたデータがあります ({name})");
            pos = Align4(pos + 4 + scriptLen);
            if (pos != raw.Length)
                throw new InvalidDataException($"TextAsset の形式が想定と異なります (末尾: {name})");

            return Tuple.Create(name, new TextAssetData { Info = info, NameField = nameField, Script = script });
        }

        static int Align4(int n) => (n + 3) & ~3;

        public void SetText(string name, string script)
        {
            var t = texts[name];
            byte[] body = StrictUtf8.GetBytes(script);
            var ms = new MemoryStream(t.NameField.Length + 8 + body.Length);
            ms.Write(t.NameField, 0, t.NameField.Length);
            ms.Write(BitConverter.GetBytes(body.Length), 0, 4);
            ms.Write(body, 0, body.Length);
            while (ms.Length % 4 != 0)
                ms.WriteByte(0);
            t.Info.SetNewData(ms.ToArray());
            t.Script = script;
        }

        /// <summary>Serializes the bundle (LZ4-compressed, block info at the start like the original).</summary>
        public byte[] Save()
        {
            bundle.file.BlockAndDirInfo.DirectoryInfos[directoryIndex].SetNewData(assets.file);
            var uncompressed = new MemoryStream();
            var w = new AssetsFileWriter(uncompressed);
            bundle.file.Write(w, 0);
            w.Flush();
            byte[] plain = uncompressed.ToArray();

            var unpacked = new AssetBundleFile();
            unpacked.Read(new AssetsFileReader(new MemoryStream(plain, false)));
            var packed = new MemoryStream();
            var pw = new AssetsFileWriter(packed);
            unpacked.Pack(pw, AssetBundleCompressionType.LZ4, false, null);
            pw.Flush();
            return packed.ToArray();
        }

        public void Dispose() => manager.UnloadAll(true);

        /// <summary>
        /// Strict reading of the serialized file (format 22, as Unity 2020.3 writes it) the way Unity and UnityPy read
        /// it: header and metadata sizes, the type trees, the object table, every object's data against its type tree
        /// and the references of the AssetBundle object. AssetsTools.NET reads damaged metadata leniently and must
        /// then see the same objects (CompareWith).
        /// </summary>
        sealed class SerializedFile
        {
            const string MetadataError = "アセットファイルの管理情報が壊れています";
            const string TypeTreeError = "アセットファイルの型情報が壊れています";
            const string ObjectError = "オブジェクトのデータが型情報と一致しません";
            const string AssetBundleError = "AssetBundle の情報が壊れています";
            const string TextAssetShape = "1 string m_Name;2 Array Array;3 int size;3 char data;1 string m_Script;2 Array Array;3 int size;3 char data;";
            const string PreloadTableShape = "0 vector m_PreloadTable;1 Array Array;2 int size;2 PPtr<Object> data;3 int m_FileID;3 SInt64 m_PathID;";
            const string ContainerShape = "0 map m_Container;1 Array Array;2 int size;2 pair data;3 string first;4 Array Array;5 int size;5 char data;" +
                                          "3 AssetInfo second;4 int preloadIndex;4 int preloadSize;4 PPtr<Object> asset;5 int m_FileID;5 SInt64 m_PathID;";

            static readonly Dictionary<string, int> Primitives = new Dictionary<string, int>(StringComparer.Ordinal)
            {
                ["SInt8"] = 1, ["UInt8"] = 1, ["char"] = 1, ["bool"] = 1, ["short"] = 2, ["SInt16"] = 2, ["unsigned short"] = 2,
                ["UInt16"] = 2, ["int"] = 4, ["SInt32"] = 4, ["unsigned int"] = 4, ["UInt32"] = 4, ["Type*"] = 4, ["float"] = 4,
                ["long long"] = 8, ["SInt64"] = 8, ["unsigned long long"] = 8, ["UInt64"] = 8, ["FileSize"] = 8, ["double"] = 8,
            };

            static Dictionary<int, string> commonStrings;

            sealed class Node
            {
                public int Level, Flags, Size, Meta;
                public string Type, Name;
                public bool Checked; // the layout of this string node was checked
                public readonly List<Node> Children = new List<Node>();
            }

            sealed class Item
            {
                public long PathId, Start, Size; // Start: from the data offset
                public int TypeIndex;
            }

            long metadataSize, fileSize, dataOffset;
            readonly List<int> classIds = new List<int>();
            readonly List<Item> items = new List<Item>();

            // Work limit of the strict reading: one unit per byte of a type tree name and per node visited while walking
            // the objects. A forged file (a vector of a million elements of a class with a thousand empty fields, many
            // objects sharing such a tree, thousands of nodes naming one long string) would otherwise keep Load (and the
            // GUI's status check) busy for minutes; with the limit it ends within about 0.2 s. The game's localisation
            // bundles use 686 units of about 7 million, its MonoBehaviour language bundle 0.15 units per byte.
            const long WorkPerByte = 4, WorkBase = 1L << 20, WorkLimit = 8L << 20;
            long work;

            public long WorkBudget { get; private set; }

            public long WorkUsed => WorkBudget - work;

            void Spend(long units, string error)
            {
                work -= units;
                if (work < 0)
                    throw new InvalidDataException(error);
            }

            public static SerializedFile Check(byte[] sf)
            {
                var f = new SerializedFile();
                f.WorkBudget = f.work = Math.Min(WorkLimit, WorkBase + WorkPerByte * sf.Length);
                if (sf.Length < 48)
                    throw new InvalidDataException("アセットファイルのヘッダーが正しくありません");
                // header (big-endian): 16 legacy bytes (format version at 8), endianness, 3 reserved, metadata size u32,
                // file size i64, data offset i64, 8 reserved
                CheckFormat(sf);
                f.metadataSize = BigEndian(sf, 20, 4);
                f.fileSize = BigEndian(sf, 24, 8);
                f.dataOffset = BigEndian(sf, 32, 8);
                if (f.fileSize != sf.Length || f.dataOffset < 48 + f.metadataSize || f.dataOffset > f.fileSize)
                    throw new InvalidDataException("アセットファイルのヘッダーが正しくありません");

                var c = new Cursor(sf, 48, (int)(48 + f.metadataSize), MetadataError);
                c.CString(); // Unity version
                c.Skip(4); // target platform
                bool typeTrees = c.Byte() != 0;
                var trees = new List<Node>();
                for (int n = c.Count(23), i = 0; i < n; i++)
                    trees.Add(f.ReadType(c, typeTrees, false, f.classIds));
                for (int n = c.Count(24), i = 0; i < n; i++)
                {
                    c.Align4();
                    var item = new Item { PathId = c.Int64(), Start = c.Int64(), Size = c.UInt32(), TypeIndex = c.Int32() };
                    if (item.TypeIndex < 0 || item.TypeIndex >= trees.Count || item.Start < 0 || item.Start > f.fileSize - f.dataOffset - item.Size)
                        throw new InvalidDataException("オブジェクトの位置が正しくありません");
                    f.items.Add(item);
                }
                for (int n = c.Count(12), i = 0; i < n; i++)
                {
                    // script types: file index, aligned local id
                    c.Skip(4);
                    c.Align4();
                    c.Skip(8);
                }
                int externals = c.Count(22);
                for (int i = 0; i < externals; i++)
                {
                    c.CString();
                    c.Skip(20); // GUID, type
                    c.CString();
                }
                for (int n = c.Count(23), i = 0; i < n; i++)
                    f.ReadType(c, typeTrees, true, null); // reference types
                c.CString(); // user information
                if (c.Pos != 48 + f.metadataSize)
                    throw new InvalidDataException(MetadataError);

                var ids = new HashSet<long>();
                foreach (var item in f.items)
                {
                    if (!ids.Add(item.PathId))
                        throw new InvalidDataException("アセットの番号が重複しています");
                }
                var sorted = new List<Item>(f.items);
                sorted.Sort((a, b) => a.Start.CompareTo(b.Start));
                for (int i = 1; i < sorted.Count; i++)
                {
                    if (sorted[i].Start < sorted[i - 1].Start + sorted[i - 1].Size)
                        throw new InvalidDataException("オブジェクトの位置が正しくありません");
                }

                // every object's data must be exactly what its type tree describes (as UnityPy reads it)
                var preload = new List<long>();
                var container = new List<long>();
                var textAssets = new List<long>();
                int assetBundles = 0;
                var shapeChecked = new bool[trees.Count]; // the shape is checked once per type (objects may share one)
                foreach (var item in f.items)
                {
                    int classId = f.classIds[item.TypeIndex];
                    if (classId == 49)
                        textAssets.Add(item.PathId);
                    if (classId == 142)
                        assetBundles++;
                    var root = trees[item.TypeIndex];
                    if (root == null)
                        continue;
                    if (!shapeChecked[item.TypeIndex])
                    {
                        if (classId == 49 && Shape(root, 0, true) != TextAssetShape)
                            throw new InvalidDataException("TextAsset の型情報が想定と異なります");
                        if (classId == 142)
                        {
                            // the fields Unity finds the assets with must have their usual layout
                            foreach (var kv in new[] { Tuple.Create("m_PreloadTable", PreloadTableShape), Tuple.Create("m_Container", ContainerShape) })
                            {
                                var field = root.Children.FindAll(n => n.Name == kv.Item1);
                                if (field.Count != 1 || Shape(field[0], 1, false) != kv.Item2)
                                    throw new InvalidDataException(AssetBundleError);
                            }
                        }
                        shapeChecked[item.TypeIndex] = true;
                    }
                    var sinks = classId == 142 ? new Dictionary<string, List<long>> { ["m_PreloadTable"] = preload, ["m_Container"] = container } : null;
                    long start = f.dataOffset + item.Start;
                    var data = new Cursor(sf, (int)start, (int)(start + item.Size), ObjectError);
                    f.Walk(data, root, null, sinks);
                    if (data.Pos != start + item.Size)
                        throw new InvalidDataException(ObjectError);
                }

                // the AssetBundle object lists every asset Unity can load: references must exist, every TextAsset is listed
                if (assetBundles != 1)
                    throw new InvalidDataException(AssetBundleError);
                if (typeTrees)
                {
                    bool Missing(long fileId, long pathId) => fileId < 0 || fileId > externals || (fileId == 0 && pathId != 0 && !ids.Contains(pathId));
                    int preloadCount = preload.Count / 2;
                    for (int i = 0; i + 1 < preload.Count; i += 2)
                    {
                        if (Missing(preload[i], preload[i + 1]))
                            throw new InvalidDataException(AssetBundleError);
                    }
                    var listed = new HashSet<long>();
                    for (int i = 0; i + 3 < container.Count; i += 4)
                    {
                        long index = container[i], size = container[i + 1];
                        if (Missing(container[i + 2], container[i + 3]) || index < 0 || size < 0 || index + size > preloadCount)
                            throw new InvalidDataException(AssetBundleError);
                        if (container[i + 2] == 0)
                            listed.Add(container[i + 3]);
                    }
                    if (!textAssets.TrueForAll(listed.Contains))
                        throw new InvalidDataException("TextAsset が AssetBundle の一覧にありません");
                }
                return f;
            }

            /// <summary>
            /// Format version 22, little endian (in the first 20 bytes of the header, at the same places in every format
            /// version): another version below 100 or big endian is a format this patcher does not read (e.g. from
            /// another Unity), not damage.
            /// </summary>
            public static void CheckFormat(byte[] sf)
            {
                long version = BigEndian(sf, 8, 4);
                if (version != 22)
                    throw Unsupported($"アセットファイルの形式に対応していません（版 {version}）", version > 0 && version < 100);
                if (sf[16] != 0)
                    throw Unsupported("アセットファイルの形式に対応していません（ビッグエンディアン）", sf[16] == 1);
            }

            /// <summary>AssetsTools.NET must have read the same header, types and object table.</summary>
            public void CompareWith(AssetsFile file)
            {
                var h = file.Header;
                bool same = h.Version == 22 && !h.Endianness && h.MetadataSize == metadataSize && h.FileSize == fileSize &&
                            h.DataOffset == dataOffset && file.Metadata.TypeTreeTypes.Count == classIds.Count && file.AssetInfos.Count == items.Count;
                for (int i = 0; same && i < classIds.Count; i++)
                    same = file.Metadata.TypeTreeTypes[i].TypeId == classIds[i];
                for (int i = 0; same && i < items.Count; i++)
                {
                    var a = file.AssetInfos[i];
                    same = a.PathId == items[i].PathId && a.ByteOffset == items[i].Start && a.ByteSize == items[i].Size && a.TypeIdOrIndex == items[i].TypeIndex;
                }
                if (!same)
                    throw new InvalidDataException(MetadataError);
            }

            static long BigEndian(byte[] b, int at, int bytes)
            {
                ulong v = 0;
                for (int i = 0; i < bytes; i++)
                    v = v << 8 | b[at + i];
                return (long)v;
            }

            /// <summary>One entry of the type list; the type tree (null without type trees) is checked for consistency.</summary>
            Node ReadType(Cursor c, bool typeTrees, bool reference, List<int> classIds)
            {
                int classId = c.Int32();
                c.Skip(1); // stripped
                short scriptIndex = c.Int16();
                if ((reference && scriptIndex >= 0) || classId == 114)
                    c.Skip(16); // script id
                c.Skip(16); // type hash
                classIds?.Add(classId);
                if (!typeTrees)
                    return null;
                int count = c.Count(32), stringsSize = c.Int32();
                if (count == 0 || stringsSize < 0)
                    throw new InvalidDataException(TypeTreeError);
                int nodesAt = c.Pos;
                c.Skip(32L * count);
                int stringsAt = c.Pos;
                c.Skip(stringsSize);
                var nodes = new Node[count];
                var stack = new List<Node>();
                for (int i = 0; i < count; i++)
                {
                    // version u16, level u8, flags u8, type and name string offsets, byte size, index, meta flags, ref hash
                    int at = nodesAt + 32 * i;
                    var n = new Node
                    {
                        Level = c.Bytes[at + 2],
                        Flags = c.Bytes[at + 3],
                        Type = TypeTreeString(c.Bytes, stringsAt, stringsSize, BitConverter.ToUInt32(c.Bytes, at + 4)),
                        Name = TypeTreeString(c.Bytes, stringsAt, stringsSize, BitConverter.ToUInt32(c.Bytes, at + 8)),
                        Size = BitConverter.ToInt32(c.Bytes, at + 12),
                        Meta = BitConverter.ToInt32(c.Bytes, at + 20),
                    };
                    // one root, levels without jumps, the array flag exactly on the Array nodes, no managed references
                    // (flags 2, 4, 8: not used by these bundles)
                    if ((i == 0) != (n.Level == 0) || (i > 0 && n.Level > nodes[i - 1].Level + 1) || ((n.Flags & 1) != 0) != (n.Type == "Array") ||
                        (n.Flags & 0xE) != 0)
                        throw new InvalidDataException(TypeTreeError);
                    nodes[i] = n;
                    if (i > 0)
                    {
                        stack.RemoveRange(n.Level, stack.Count - n.Level);
                        stack[n.Level - 1].Children.Add(n);
                    }
                    stack.Add(n);
                }
                if (reference)
                {
                    c.CString(); // class name
                    c.CString(); // namespace
                    c.CString(); // assembly
                }
                else
                {
                    c.Skip(4L * c.Count(4)); // type dependencies
                }
                return nodes[0];
            }

            /// <summary>A name in the type tree: in the common table (unknown offsets read as numbers, as UnityPy does) or in the own buffer.</summary>
            string TypeTreeString(byte[] b, int stringsAt, int stringsSize, uint value)
            {
                if ((value & 0x80000000) != 0)
                {
                    int offset = (int)(value & 0x7FFFFFFF);
                    return CommonStrings().TryGetValue(offset, out var common) ? common : offset.ToString(System.Globalization.CultureInfo.InvariantCulture);
                }
                int end = value < stringsSize ? Array.IndexOf(b, (byte)0, stringsAt + (int)value, stringsSize - (int)value) : -1;
                if (end < 0)
                    throw new InvalidDataException(TypeTreeError);
                Spend(end - stringsAt - value + 1, TypeTreeError);
                string s;
                try
                {
                    s = StrictUtf8.GetString(b, stringsAt + (int)value, end - stringsAt - (int)value);
                }
                catch (ArgumentException)
                {
                    throw new InvalidDataException(TypeTreeError);
                }
                if (s.Length == 0)
                    throw new InvalidDataException(TypeTreeError);
                foreach (char ch in s)
                {
                    if (ch < 0x20 || ch == 0x7F)
                        throw new InvalidDataException(TypeTreeError);
                }
                return s;
            }

            static Dictionary<int, string> CommonStrings()
            {
                if (commonStrings == null)
                {
                    var table = TypeTreeType.COMMON_STRING_TABLE;
                    var d = new Dictionary<int, string>();
                    for (int p = 0; p < table.Length;)
                    {
                        int e = Array.IndexOf(table, (byte)0, p);
                        if (e < 0)
                            e = table.Length;
                        d[p] = Encoding.ASCII.GetString(table, p, e - p);
                        p = e + 1;
                    }
                    commonStrings = d;
                }
                return commonStrings;
            }

            /// <summary>"level type name;" of the node (or only of its children) and everything below it.</summary>
            static string Shape(Node node, int baseLevel, bool childrenOnly)
            {
                var sb = new StringBuilder();
                void Add(Node n)
                {
                    sb.Append(n.Level - baseLevel).Append(' ').Append(n.Type).Append(' ').Append(n.Name).Append(';');
                    n.Children.ForEach(Add);
                }
                if (childrenOnly)
                    node.Children.ForEach(Add);
                else
                    Add(node);
                return sb.ToString();
            }

            /// <summary>
            /// Reads one value as UnityPy does (primitives and strings by their type name, a node whose first child
            /// is an Array is a vector, other nodes are classes) and checks the type tree against it. The int and
            /// SInt64 values below a field named in <paramref name="sinks"/> are collected. Every visited node costs one
            /// work unit.
            /// </summary>
            void Walk(Cursor c, Node node, List<long> sink, Dictionary<string, List<long>> sinks)
            {
                Spend(1, ObjectError);
                bool align = (node.Meta & 0x4000) != 0;
                if (Primitives.TryGetValue(node.Type, out int size))
                {
                    if (node.Size != size || node.Children.Count != 0)
                        throw new InvalidDataException(TypeTreeError);
                    c.Skip(size);
                    if (sink != null && (node.Type == "int" || node.Type == "SInt64"))
                        sink.Add(size == 4 ? BitConverter.ToInt32(c.Bytes, c.Pos - 4) : BitConverter.ToInt64(c.Bytes, c.Pos - 8));
                }
                else if (node.Type == "string")
                {
                    // UnityPy always aligns a string, Unity by the flag of its Array node
                    var array = node.Children.Count == 1 ? node.Children[0] : null;
                    if (!node.Checked && (array == null || array.Type != "Array" || (array.Meta & 0x4000) == 0 || array.Children.Count != 2 ||
                                          !IsLeaf(array.Children[0], "int", 4) || !IsLeaf(array.Children[1], "char", 1)))
                        throw new InvalidDataException(TypeTreeError);
                    node.Checked = true;
                    c.Skip(c.Count(1));
                    c.Align4();
                }
                else if (node.Type == "TypelessData")
                {
                    c.Skip(c.Count(1));
                }
                else if (node.Children.Count > 0 && node.Children[0].Type == "Array")
                {
                    var array = node.Children[0];
                    if (node.Children.Count != 1 || array.Children.Count != 2 || !IsLeaf(array.Children[0], "int", 4))
                        throw new InvalidDataException(TypeTreeError);
                    align |= (array.Meta & 0x4000) != 0;
                    int count = c.Count(0);
                    var element = array.Children[1];
                    if (Primitives.TryGetValue(element.Type, out int elementSize) && element.Children.Count == 0)
                    {
                        if (element.Size != elementSize)
                            throw new InvalidDataException(TypeTreeError);
                        c.Skip((long)count * elementSize);
                        if ((element.Meta & 0x4000) != 0)
                            c.Align4();
                    }
                    else
                    {
                        for (int i = 0; i < count; i++)
                        {
                            int before = c.Pos;
                            Walk(c, element, sink, null);
                            if (c.Pos == before)
                                break; // an element without data: the others have none either
                        }
                    }
                }
                else if (node.Type == "pair" && node.Children.Count != 2)
                {
                    throw new InvalidDataException(TypeTreeError);
                }
                else if (node.Type == "ReferencedObject" || node.Type == "ManagedReferencesRegistry")
                {
                    throw new InvalidDataException(TypeTreeError); // managed references: not in these bundles
                }
                else
                {
                    foreach (var child in node.Children)
                        Walk(c, child, sink ?? (sinks != null && sinks.TryGetValue(child.Name, out var s) ? s : null), null);
                }
                if (align)
                    c.Align4();
            }

            static bool IsLeaf(Node n, string type, int size) => n.Type == type && n.Size == size && n.Children.Count == 0;

            /// <summary>A bounded little-endian reader; reading past the end throws InvalidDataException(error).</summary>
            sealed class Cursor
            {
                public readonly byte[] Bytes;
                public int Pos;
                readonly int end;
                readonly string error;

                public Cursor(byte[] bytes, int pos, int end, string error)
                {
                    Bytes = bytes;
                    Pos = pos;
                    this.end = end;
                    this.error = error;
                }

                public void Skip(long n)
                {
                    if (n < 0 || n > end - Pos)
                        throw new InvalidDataException(error);
                    Pos += (int)n;
                }

                public byte Byte()
                {
                    Skip(1);
                    return Bytes[Pos - 1];
                }

                public short Int16()
                {
                    Skip(2);
                    return BitConverter.ToInt16(Bytes, Pos - 2);
                }

                public int Int32()
                {
                    Skip(4);
                    return BitConverter.ToInt32(Bytes, Pos - 4);
                }

                public uint UInt32()
                {
                    Skip(4);
                    return BitConverter.ToUInt32(Bytes, Pos - 4);
                }

                public long Int64()
                {
                    Skip(8);
                    return BitConverter.ToInt64(Bytes, Pos - 8);
                }

                /// <summary>A count (or length) of items of at least minSize bytes each, which must fit in the rest.</summary>
                public int Count(int minSize)
                {
                    int n = Int32();
                    if (n < 0 || (long)n * minSize > end - Pos)
                        throw new InvalidDataException(error);
                    return n;
                }

                public void CString()
                {
                    int e = Array.IndexOf(Bytes, (byte)0, Pos, end - Pos);
                    if (e < 0)
                        throw new InvalidDataException(error);
                    Pos = e + 1;
                }

                /// <summary>Unity pads to a multiple of 4 (positions in the serialized file) with zeros.</summary>
                public void Align4()
                {
                    int pad = -Pos & 3;
                    Skip(pad);
                    for (int i = Pos - pad; i < Pos; i++)
                    {
                        if (Bytes[i] != 0)
                            throw new InvalidDataException(error);
                    }
                }
            }
        }
    }

    /// <summary>
    /// Inner exception of an InvalidDataException from LocalisationBundle.Load for a format this patcher does not read
    /// (e.g. written by a newer Unity): not necessarily damaged data. InvalidDataException itself is sealed.
    /// </summary>
    sealed class UnsupportedFormatException : Exception
    {
        public UnsupportedFormatException(string message) : base(message) { }

        /// <summary>Whether e (or one of its inner exceptions) reports an unsupported format.</summary>
        public static bool In(Exception e)
        {
            for (; e != null; e = e.InnerException)
            {
                if (e is UnsupportedFormatException)
                    return true;
            }
            return false;
        }
    }
}
