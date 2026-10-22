using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace TpcJpPatch.Tests
{
    static class SafeFileTests
    {
        static readonly byte[] Old = Encoding.UTF8.GetBytes("old content 古い");
        static readonly byte[] New = Encoding.UTF8.GetBytes("new content 新しい内容");

        /// <summary>What SafeFile says when the rename is still refused after its retries (CS-11), after the file name.</summary>
        const string InUseText = " を置き換えられません。別のプログラム（ウイルス対策ソフトなど）が使用中の可能性があります: ";

        static void NoTemp(string target, string what) =>
            T.True(!File.Exists(target + SafeFile.TempSuffix), what + ": temp file left behind");

        static byte[] ReadAll(FileStream fs)
        {
            fs.Position = 0;
            var ms = new MemoryStream();
            fs.CopyTo(ms);
            return ms.ToArray();
        }

        /// <summary>SafeFile.Write that must fail after its retries: checks the exception (type, message naming the file),
        /// the time it took (retried for about a second, never much longer) and the temp file. The caller checks the
        /// target once the holder let go.</summary>
        static Exception RefusedAfterRetries<TEx>(string p, string what) where TEx : Exception
        {
            var sw = Stopwatch.StartNew();
            var e = T.Throws<TEx>(() => SafeFile.Write(p, New), what);
            long ms = sw.ElapsedMilliseconds;
            T.Eq(typeof(TEx), e.GetType(), what + ": exception type (callers catch IOException / UnauthorizedAccessException)");
            T.True(e.Message.StartsWith(Path.GetFileName(p) + InUseText, StringComparison.Ordinal) && e.Message.Length > Path.GetFileName(p).Length + InUseText.Length,
                what + ": message names the file, says it may be in use and gives the Windows reason: " + e.Message);
            T.True(ms >= 700 && ms <= 3000, what + ": refused after about 1 s of retries (" + ms + " ms)");
            NoTemp(p, what);
            return e;
        }

        /// <summary>Runs `body` as on a volume / Windows without POSIX renames (debug hook of SafeFile).</summary>
        static void WithoutPosixRename(Action body)
        {
            Environment.SetEnvironmentVariable("TPCJP_SIMULATE_NO_POSIX_RENAME", "1");
            try
            {
                body();
            }
            finally
            {
                Environment.SetEnvironmentVariable("TPCJP_SIMULATE_NO_POSIX_RENAME", null);
            }
        }

        /// <summary>Runs `body` while MoveFileEx fails with the Windows error `code` (debug hook of SafeFile).</summary>
        static void WithMoveFileError(int code, Action body)
        {
            Environment.SetEnvironmentVariable("TPCJP_SIMULATE_MOVEFILE_ERROR", code.ToString());
            try
            {
                body();
            }
            finally
            {
                Environment.SetEnvironmentVariable("TPCJP_SIMULATE_MOVEFILE_ERROR", null);
            }
        }

        /// <summary>Runs `body` while SafeFile.Write calls `beforeRename` with the temp file's path once it is written and
        /// closed, just before the rename (debug hook of SafeFile; looked up by reflection so that the tests still compile
        /// against a Release build, which has no hooks).</summary>
        internal static void WithBeforeRename(Action<string> beforeRename, Action body)
        {
            var hook = typeof(SafeFile).GetField("BeforeRename", BindingFlags.NonPublic | BindingFlags.Static);
            T.True(hook != null, "SafeFile.BeforeRename (debug builds only) is missing");
            hook.SetValue(null, beforeRename);
            try
            {
                body();
            }
            finally
            {
                hook.SetValue(null, null);
            }
        }

        /// <summary>SafeFile.Replace (private): the rename alone, for targets that SafeFile.Write cannot reach (its temp
        /// file would fail first).</summary>
        static void Replace(string tmp, string target)
        {
            try
            {
                typeof(SafeFile).GetMethod("Replace", BindingFlags.NonPublic | BindingFlags.Static).Invoke(null, new object[] { tmp, target });
            }
            catch (TargetInvocationException e)
            {
                ExceptionDispatchInfo.Capture(e.InnerException).Throw();
            }
        }

        static int HResult(int code) => unchecked((int)0x80070000 | code);

        /// <summary>A rename refused for another reason than "in use" (R11): exactly IOException (never COMException,
        /// ArgumentException, BadImageFormatException …, which the callers report as 予期しないエラー) with the HRESULT of the
        /// Windows error and the message 「&lt;name&gt; を置き換えられません（&lt;Windows reason&gt;）」. Returns the Windows error.</summary>
        static int OtherRenameError(Exception e, string name, string what)
        {
            T.Eq(typeof(IOException), e.GetType(), what + ": exception type (callers catch IOException / UnauthorizedAccessException): " + e.Message);
            T.Eq(0x8007, (int)((uint)e.HResult >> 16), what + ": HRESULT from a Windows error (0x" + e.HResult.ToString("X8") + ")");
            int code = e.HResult & 0xFFFF;
            T.Eq(name + " を置き換えられません（" + new Win32Exception(code).Message + "）", e.Message, what + ": message names the file and gives the Windows reason");
            return code;
        }

        /// <summary>What Marshal.GetExceptionForHR (used for these errors before R11) makes of a Windows error.</summary>
        static string BeforeR11(int code) => Marshal.GetExceptionForHR(HResult(code))?.GetType().Name ?? "null";

        /// <summary>Starts a thread that opens `p` with `share`, keeps it open for `ms`, and returns once it is open.</summary>
        static Thread HoldFor(string p, FileShare share, int ms)
        {
            var opened = new ManualResetEventSlim();
            var t = new Thread(() =>
            {
                using (new FileStream(p, FileMode.Open, FileAccess.Read, share))
                {
                    opened.Set();
                    Thread.Sleep(ms);
                }
            });
            t.Start();
            opened.Wait();
            return t;
        }

        sealed class PollResult
        {
            public int Polls, Missing, Reads, Partial, ReadErrors, Writes, WriteErrors, Held;
            public override string ToString() =>
                $"{Writes} writes ({WriteErrors} refused while the poller had the file open), {Polls} polls, {Missing} found the target missing, {Reads} reads ({Partial} partial, {ReadErrors} errors)"
                + (Held > 0 ? $", {Held} writes while a reader kept one handle open" : "");
        }

        /// <summary>Replaces `p` until `count` writes succeeded (alternating b / a; at most 10 x count attempts) while another thread polls File.Exists and reads it now and then.</summary>
        static PollResult Poll(string p, byte[] a, byte[] b, Action<string, byte[]> write, int count)
        {
            var res = new PollResult();
            bool done = false;
            var reader = new Thread(() =>
            {
                while (!Volatile.Read(ref done))
                {
                    res.Polls++;
                    if (!File.Exists(p))
                    {
                        res.Missing++;
                        continue;
                    }
                    if (res.Polls % 50 != 0)
                        continue;
                    try
                    {
                        var got = File.ReadAllBytes(p);
                        res.Reads++;
                        if (!got.SequenceEqual(a) && !got.SequenceEqual(b))
                            res.Partial++;
                    }
                    catch (FileNotFoundException)
                    {
                        res.Missing++;
                    }
                    catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
                    {
                        res.ReadErrors++;
                    }
                }
            });
            reader.Start();
            for (int i = 0; res.Writes < count && i < count * 10; i++)
            {
                try
                {
                    write(p, res.Writes % 2 == 0 ? b : a);
                    res.Writes++;
                }
                catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
                {
                    res.WriteErrors++;
                }
            }
            Volatile.Write(ref done, true);
            reader.Join();
            return res;
        }

        /// <summary>
        /// Like Poll, but the reader opens `p` with FileShare.ReadWrite|Delete (as a virus scanner or Patcher.Sha256 does),
        /// reads it, keeps the handle for 10 ms while it goes on polling File.Exists, then reads it again through the same
        /// handle (it must still give the complete content it gave first) and closes it; every 50 polls it opens it again.
        /// </summary>
        static PollResult PollHeld(string p, byte[] a, byte[] b, int count)
        {
            var res = new PollResult();
            bool done = false;
            int generation = 0; // odd while the reader holds a handle
            var reader = new Thread(() =>
            {
                FileStream held = null;
                byte[] first = null;
                var since = new Stopwatch();
                try
                {
                    while (!Volatile.Read(ref done))
                    {
                        res.Polls++;
                        if (!File.Exists(p))
                            res.Missing++;
                        if (held != null)
                        {
                            if (since.ElapsedMilliseconds < 10)
                                continue;
                            res.Reads++;
                            if (!ReadAll(held).SequenceEqual(first))
                                res.Partial++;
                            Interlocked.Increment(ref generation);
                            held.Dispose();
                            held = null;
                        }
                        else if (res.Polls % 50 == 0)
                        {
                            try
                            {
                                held = new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
                                Interlocked.Increment(ref generation);
                                since.Restart();
                                first = ReadAll(held);
                                res.Reads++;
                                if (!first.SequenceEqual(a) && !first.SequenceEqual(b))
                                    res.Partial++;
                            }
                            catch (FileNotFoundException)
                            {
                                res.Missing++;
                            }
                            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
                            {
                                res.ReadErrors++;
                            }
                        }
                    }
                }
                finally
                {
                    held?.Dispose();
                }
            });
            reader.Start();
            for (int i = 0; res.Writes < count && i < count * 10; i++)
            {
                int g = Volatile.Read(ref generation);
                try
                {
                    SafeFile.Write(p, res.Writes % 2 == 0 ? b : a);
                    res.Writes++;
                    if (g % 2 == 1 && Volatile.Read(ref generation) == g)
                        res.Held++;
                }
                catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
                {
                    res.WriteErrors++;
                }
            }
            Volatile.Write(ref done, true);
            reader.Join();
            return res;
        }

        public static void Run(Runner r, Ctx ctx)
        {
            r.Test("SafeFile: creates a new file (no temp file left)", () =>
            {
                string dir = ctx.Dir("safefile_create");
                string p = Path.Combine(dir, "new.bin");
                SafeFile.Write(p, New);
                T.Bytes(New, File.ReadAllBytes(p), "content");
                NoTemp(p, "create");
                T.Eq(1, Directory.GetFiles(dir).Length, "only the target in the folder");
            });

            r.Test("SafeFile: replaces an existing file; empty and 20 MB contents", () =>
            {
                string dir = ctx.Dir("safefile_replace");
                string p = Path.Combine(dir, "t.bin");
                File.WriteAllBytes(p, Old);
                SafeFile.Write(p, New);
                T.Bytes(New, File.ReadAllBytes(p), "replaced");
                SafeFile.Write(p, new byte[0]);
                T.Eq(0L, new FileInfo(p).Length, "empty content");
                var big = new byte[20 << 20];
                new Random(5).NextBytes(big);
                SafeFile.Write(p, big);
                T.Bytes(big, File.ReadAllBytes(p), "20 MB");
                NoTemp(p, "replace");
                T.Eq(1, Directory.GetFiles(dir).Length, "no backup / temp files");
            });

            r.Test("SafeFile: a read-only target is replaced (attribute cleared first)", () =>
            {
                string dir = ctx.Dir("safefile_ro");
                string p = Path.Combine(dir, "t.bin");
                File.WriteAllBytes(p, Old);
                File.SetAttributes(p, FileAttributes.ReadOnly);
                SafeFile.Write(p, New);
                T.Bytes(New, File.ReadAllBytes(p), "replaced");
                NoTemp(p, "read-only");
                r.Note("attributes after the write: " + File.GetAttributes(p));
                File.SetAttributes(p, FileAttributes.Normal);
            });

            r.Test("SafeFile: a hidden / system target is replaced", () =>
            {
                string dir = ctx.Dir("safefile_hidden");
                string p = Path.Combine(dir, "t.bin");
                File.WriteAllBytes(p, Old);
                File.SetAttributes(p, FileAttributes.Hidden);
                SafeFile.Write(p, New);
                T.Bytes(New, File.ReadAllBytes(p), "hidden replaced");
                NoTemp(p, "hidden");
                File.SetAttributes(p, FileAttributes.Hidden | FileAttributes.ReadOnly);
                SafeFile.Write(p, Old);
                T.Bytes(Old, File.ReadAllBytes(p), "hidden + read-only replaced");
                NoTemp(p, "hidden + read-only");
                File.SetAttributes(p, FileAttributes.Normal);
            });

            foreach (var share in new[] { FileShare.None, FileShare.Read, FileShare.ReadWrite })
            {
                r.Test($"SafeFile: target held open by another FileStream without FileShare.Delete (FileShare.{share}) -> IOException naming the file after about 1 s of retries, target intact, no temp file (CS-11)", () =>
                {
                    string dir = ctx.Dir("safefile_locked_" + share);
                    string p = Path.Combine(dir, "t.bin");
                    File.WriteAllBytes(p, Old);
                    Exception caught;
                    using (var holder = new FileStream(p, FileMode.Open, FileAccess.Read, share))
                    {
                        // MoveFileEx: ERROR_ACCESS_DENIED; the POSIX rename: ERROR_SHARING_VIOLATION (an IOException)
                        caught = RefusedAfterRetries<IOException>(p, "locked target");
                        T.Bytes(Old, ReadAll(holder), "the holder still reads the old content");
                    }
                    T.Bytes(Old, File.ReadAllBytes(p), "target intact after the holder let go");
                    T.Eq(1, Directory.GetFiles(dir).Length, "only the target in the folder");
                    r.Note(caught.GetType().Name + ": " + caught.Message.Trim());
                });
            }

            foreach (var holder in new[] { "FileShare.Delete", "FileShare.Read|Delete", "FileShare.ReadWrite|Delete", "a writer with FileShare.ReadWrite|Delete" })
            {
                r.Test($"SafeFile: target held open with {holder} for the whole write (virus scanner, indexer, Sha256) -> replaced at once; the open handle still reads the old content, a new open the new one (CS-11)", () =>
                {
                    string dir = ctx.Dir("safefile_sharedelete_" + holder.Replace(' ', '_').Replace('|', '_').Replace('.', '_'));
                    string p = Path.Combine(dir, "t.bin");
                    File.WriteAllBytes(p, Old);
                    var share = holder.Contains("ReadWrite") ? FileShare.ReadWrite | FileShare.Delete
                        : holder.Contains("Read") ? FileShare.Read | FileShare.Delete : FileShare.Delete;
                    long ms;
                    using (var h = new FileStream(p, FileMode.Open, holder.StartsWith("a writer", StringComparison.Ordinal) ? FileAccess.ReadWrite : FileAccess.Read, share))
                    {
                        var sw = Stopwatch.StartNew();
                        SafeFile.Write(p, New);
                        ms = sw.ElapsedMilliseconds;
                        T.Bytes(New, File.ReadAllBytes(p), "a new open reads the new content");
                        T.Bytes(Old, ReadAll(h), "the open handle still reads the old content");
                        NoTemp(p, "share-delete");
                        T.Eq(1, Directory.GetFiles(dir).Length, "only the target in the folder while the old file is still open");
                    }
                    T.True(ms < 500, "replaced without waiting for retries (" + ms + " ms)");
                    T.Bytes(New, File.ReadAllBytes(p), "new content after the holder let go");
                    T.Eq(1, Directory.GetFiles(dir).Length, "only the target in the folder");
                });
            }

            r.Test("SafeFile: a stale temp file (also read-only) from an earlier crash is replaced", () =>
            {
                string dir = ctx.Dir("safefile_stale");
                string p = Path.Combine(dir, "t.bin");
                File.WriteAllBytes(p, Old);
                File.WriteAllBytes(p + SafeFile.TempSuffix, Encoding.ASCII.GetBytes("stale half-written"));
                File.SetAttributes(p + SafeFile.TempSuffix, FileAttributes.ReadOnly);
                SafeFile.Write(p, New);
                T.Bytes(New, File.ReadAllBytes(p), "replaced");
                NoTemp(p, "stale");
            });

            r.Test("SafeFile: a temp file locked by someone else -> exception, target intact", () =>
            {
                string dir = ctx.Dir("safefile_tmplocked");
                string p = Path.Combine(dir, "t.bin");
                File.WriteAllBytes(p, Old);
                using (var holder = new FileStream(p + SafeFile.TempSuffix, FileMode.Create, FileAccess.Write, FileShare.None))
                {
                    var e = T.Throws<Exception>(() => SafeFile.Write(p, New), "locked temp");
                    T.True(e is IOException || e is UnauthorizedAccessException, "exception type " + e.GetType().FullName);
                }
                T.Bytes(Old, File.ReadAllBytes(p), "target intact");
                File.Delete(p + SafeFile.TempSuffix);
            });

            r.Test("SafeFile: missing folder / target is a folder -> exception, nothing created", () =>
            {
                string dir = ctx.Dir("safefile_paths");
                string missing = Path.Combine(dir, "no", "such", "t.bin");
                T.Throws<DirectoryNotFoundException>(() => SafeFile.Write(missing, New), "missing folder");
                T.True(!Directory.Exists(Path.Combine(dir, "no")), "no folder created");
                string sub = Path.Combine(dir, "sub");
                Directory.CreateDirectory(sub);
                File.WriteAllBytes(Path.Combine(sub, "inner.txt"), Old);
                var sw = Stopwatch.StartNew();
                var e = T.Throws<Exception>(() => SafeFile.Write(sub, New), "target is a folder");
                // MoveFileEx and the POSIX rename both answer ERROR_ACCESS_DENIED, retried like a file in use (R11)
                T.Eq(typeof(UnauthorizedAccessException), e.GetType(), "exception type (callers catch IOException / UnauthorizedAccessException): " + e.Message);
                T.True(e.Message.StartsWith("sub を置き換えられません", StringComparison.Ordinal) && e.Message.EndsWith(new Win32Exception(5).Message, StringComparison.Ordinal),
                    "message names the folder and gives the Windows reason: " + e.Message);
                T.True(sw.ElapsedMilliseconds <= 3000, "refused within the retry bound (" + sw.ElapsedMilliseconds + " ms)");
                T.True(Directory.Exists(sub) && File.ReadAllBytes(Path.Combine(sub, "inner.txt")).SequenceEqual(Old), "folder intact");
                NoTemp(sub, "folder target");
            });

            r.Test("SafeFile: Delete / ClearReadOnly", () =>
            {
                string dir = ctx.Dir("safefile_delete");
                string p = Path.Combine(dir, "t.bin");
                SafeFile.Delete(p); // missing: no error
                File.WriteAllBytes(p, Old);
                T.True(!SafeFile.ClearReadOnly(p), "not read-only -> false");
                File.SetAttributes(p, FileAttributes.ReadOnly | FileAttributes.Archive);
                T.True(SafeFile.ClearReadOnly(p), "read-only -> true");
                T.True((File.GetAttributes(p) & FileAttributes.ReadOnly) == 0, "cleared");
                T.True((File.GetAttributes(p) & FileAttributes.Archive) != 0, "other attributes kept");
                File.SetAttributes(p, FileAttributes.ReadOnly);
                SafeFile.Delete(p);
                T.True(!File.Exists(p), "read-only file deleted");
            });

            // ---- round 2 (F-SBP-7): one MoveFileEx rename instead of File.Replace, and clean-up of leftovers ----

            r.Test("SafeFile: a poller never finds the target missing while it is replaced 300 times (one rename, F-SBP-7)", () =>
            {
                string dir = ctx.Dir("safefile_atomic");
                string p = Path.Combine(dir, "t.bin");
                var a = new byte[256 << 10];
                var b = new byte[300 << 10];
                new Random(1).NextBytes(a);
                new Random(2).NextBytes(b);
                File.WriteAllBytes(p, a);
                var r1 = Poll(p, a, b, SafeFile.Write, 300);
                r.Note($"SafeFile.Write: {r1}");
                T.Eq(0, r1.Missing, "polls that found the target missing");
                T.Eq(0, r1.Partial, "reads of a partial / mixed file");
                T.Eq(300, r1.Writes, "successful replacements: " + r1);
                // File.Exists opens the file with FILE_SHARE_DELETE (POSIX rename); File.ReadAllBytes without it (retried) (CS-11)
                T.Eq(0, r1.WriteErrors, "writes refused while the poller had the file open");
                NoTemp(p, "atomic");
                T.Eq(1, Directory.GetFiles(dir).Length, "only the target in the folder (no ~RF*.TMP)");

                // control: the same poller against File.Replace (what 1.0.0 used) shows how sensitive the check is
                string dir2 = ctx.Dir("safefile_atomic_control");
                string p2 = Path.Combine(dir2, "t.bin");
                File.WriteAllBytes(p2, a);
                var r2 = Poll(p2, a, b, (target, content) =>
                {
                    File.WriteAllBytes(target + ".ctl-tmp", content);
                    File.Replace(target + ".ctl-tmp", target, null);
                }, 300);
                r.Note($"control with File.Replace: {r2}");
            });

            r.Test("SafeFile: a target that another program holds open with FileShare.ReadWrite|Delete (virus scanner, indexer) is replaced, as File.Replace did", () =>
            {
                // MoveFileEx cannot rename over a file that has an open handle, even one that allows deleting;
                // File.Replace (1.0.0) could. Readers such as Patcher.Sha256 or a virus scanner open files this way.
                var problems = new List<string>();
                string dir = ctx.Dir("safefile_open_reader");
                string p = Path.Combine(dir, "t.bin");
                File.WriteAllBytes(p, Old);
                using (new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                {
                    try
                    {
                        SafeFile.Write(p, New);
                        if (!File.ReadAllBytes(p).SequenceEqual(New))
                            problems.Add("held open for the whole write: no exception, but the target is not the new content");
                    }
                    catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
                    {
                        problems.Add("held open for the whole write: " + e.GetType().Name + ": " + e.Message.Trim());
                    }
                }
                // a reader that lets go after 150 ms (a scan): one short retry would be enough
                File.WriteAllBytes(p, Old);
                var t = HoldFor(p, FileShare.ReadWrite | FileShare.Delete, 150);
                try
                {
                    SafeFile.Write(p, New);
                    if (!File.ReadAllBytes(p).SequenceEqual(New))
                        problems.Add("open for 150 ms: no exception, but the target is not the new content");
                }
                catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
                {
                    problems.Add("open for 150 ms: " + e.GetType().Name + " at once (no retry)");
                }
                t.Join();
                NoTemp(p, "open reader");
                // control: File.Replace with the same reader
                File.WriteAllBytes(p, Old);
                using (new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                {
                    File.WriteAllBytes(p + ".ctl-tmp", New);
                    try
                    {
                        File.Replace(p + ".ctl-tmp", p, null);
                        r.Note("control: File.Replace replaced the target held open with FileShare.ReadWrite|Delete");
                    }
                    catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
                    {
                        r.Note("control: File.Replace refused as well: " + e.GetType().Name);
                    }
                }
                T.True(problems.Count == 0, string.Join("\n", problems));
            });

            // ---- round 3 (CS-11): targets that other programs have open ----

            r.Test("SafeFile: a reader without FileShare.Delete that lets go after 150 ms -> replaced after a retry (CS-11)", () =>
            {
                string dir = ctx.Dir("safefile_transient");
                string p = Path.Combine(dir, "t.bin");
                foreach (var share in new[] { FileShare.Read, FileShare.None })
                {
                    File.WriteAllBytes(p, Old);
                    var t = HoldFor(p, share, 150);
                    var sw = Stopwatch.StartNew();
                    SafeFile.Write(p, New);
                    long ms = sw.ElapsedMilliseconds;
                    t.Join();
                    T.Bytes(New, File.ReadAllBytes(p), share + ": replaced");
                    T.True(ms >= 50 && ms < 1000, share + ": replaced once the reader let go (" + ms + " ms)");
                    NoTemp(p, share.ToString());
                    r.Note($"FileShare.{share}: replaced after {ms} ms");
                }
                T.Eq(1, Directory.GetFiles(dir).Length, "only the target in the folder");
            });

            r.Test("SafeFile: a read-only target held open with FileShare.ReadWrite|Delete is replaced; held without FileShare.Delete it fails cleanly (CS-11)", () =>
            {
                string dir = ctx.Dir("safefile_ro_open");
                string p = Path.Combine(dir, "t.bin");
                File.WriteAllBytes(p, Old);
                File.SetAttributes(p, FileAttributes.ReadOnly);
                using (var h = new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                {
                    SafeFile.Write(p, New);
                    T.Bytes(New, File.ReadAllBytes(p), "replaced");
                    T.Bytes(Old, ReadAll(h), "the open handle still reads the old content");
                }
                T.True((File.GetAttributes(p) & FileAttributes.ReadOnly) == 0, "the new file is not read-only");
                NoTemp(p, "read-only + share-delete reader");
                File.WriteAllBytes(p, Old);
                File.SetAttributes(p, FileAttributes.ReadOnly);
                using (new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.Read))
                    RefusedAfterRetries<IOException>(p, "read-only + reader without FileShare.Delete");
                T.Bytes(Old, File.ReadAllBytes(p), "target intact");
                File.SetAttributes(p, FileAttributes.Normal);
            });

            r.Test("SafeFile: a missing target is created at once, also when another program deleted it while still holding it open (CS-11)", () =>
            {
                string dir = ctx.Dir("safefile_missing");
                string p = Path.Combine(dir, "t.bin");
                var sw = Stopwatch.StartNew();
                SafeFile.Write(p, New);
                T.True(sw.ElapsedMilliseconds < 500, "no retry delay (" + sw.ElapsedMilliseconds + " ms)");
                T.Bytes(New, File.ReadAllBytes(p), "created");
                NoTemp(p, "missing");
                File.WriteAllBytes(p, Old);
                using (var h = new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                {
                    File.Delete(p);
                    r.Note("after File.Delete with a handle open: File.Exists = " + File.Exists(p));
                    SafeFile.Write(p, New);
                    T.Bytes(New, File.ReadAllBytes(p), "created while the deleted file is still open");
                    T.Bytes(Old, ReadAll(h), "the open handle still reads the deleted file");
                }
                NoTemp(p, "deleted while open");
                T.Eq(1, Directory.GetFiles(dir).Length, "only the target in the folder");
            });

            r.Test("SafeFile: a poller never finds the target missing or partial while readers keep it open with FileShare.ReadWrite|Delete during 300 replacements; no write is refused (CS-11)", () =>
            {
                string dir = ctx.Dir("safefile_atomic_held");
                string p = Path.Combine(dir, "t.bin");
                var a = new byte[256 << 10];
                var b = new byte[300 << 10];
                new Random(3).NextBytes(a);
                new Random(4).NextBytes(b);
                File.WriteAllBytes(p, a);
                var res = PollHeld(p, a, b, 300);
                r.Note("SafeFile.Write: " + res);
                T.Eq(0, res.Missing, "polls that found the target missing");
                T.Eq(0, res.Partial, "reads of a partial / mixed file (also through a handle opened before the replacements)");
                T.Eq(300, res.Writes, "successful replacements: " + res);
                T.Eq(0, res.WriteErrors, "writes refused while a reader had the file open");
                T.True(res.Held >= 30, "at least 30 writes replaced the target while a reader kept one handle open the whole time: " + res);
                NoTemp(p, "atomic held");
                T.Eq(1, Directory.GetFiles(dir).Length, "only the target in the folder");
            });

            r.Test("SafeFile without POSIX renames (FAT / exFAT, network drives, older Windows; simulated): a reader with FileShare.ReadWrite|Delete is waited for about 1 s, then a clean failure naming the file (CS-11)", () =>
            {
                string dir = ctx.Dir("safefile_noposix");
                string p = Path.Combine(dir, "t.bin");
                WithoutPosixRename(() =>
                {
                    File.WriteAllBytes(p, Old);
                    var t = HoldFor(p, FileShare.ReadWrite | FileShare.Delete, 150);
                    var sw = Stopwatch.StartNew();
                    SafeFile.Write(p, New);
                    long ms = sw.ElapsedMilliseconds;
                    t.Join();
                    T.Bytes(New, File.ReadAllBytes(p), "a reader that lets go after 150 ms: replaced");
                    T.True(ms >= 50 && ms < 1000, "replaced once the reader let go (" + ms + " ms)");
                    NoTemp(p, "transient reader");
                    r.Note($"reader for 150 ms: replaced after {ms} ms");

                    File.WriteAllBytes(p, Old);
                    Exception e;
                    using (new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                        e = RefusedAfterRetries<UnauthorizedAccessException>(p, "held for the whole write"); // MoveFileEx: ERROR_ACCESS_DENIED
                    T.Bytes(Old, File.ReadAllBytes(p), "target intact");
                    r.Note(e.GetType().Name + ": " + e.Message);
                    SafeFile.Write(p, New);
                    T.Bytes(New, File.ReadAllBytes(p), "replaced once nobody holds it");
                    NoTemp(p, "free");
                });
                T.Eq(1, Directory.GetFiles(dir).Length, "only the target in the folder");
            });

            // ---- round 3b: rename errors other than "in use" (R11) and a temp file held by another program (R13) ----

            r.Test("SafeFile: a rename refused with another Windows error (disk full, failing drive, corrupt file, invalid parameter, out of memory …; simulated) -> IOException naming the file with the Windows reason, at once; target intact, no temp file (R11)", () =>
            {
                string dir = ctx.Dir("safefile_other_error");
                string p = Path.Combine(dir, "t.bin");
                // ERROR_FILE_NOT_FOUND, ERROR_INVALID_HANDLE, ERROR_OUTOFMEMORY, ERROR_WRITE_PROTECT, ERROR_CRC, ERROR_NOT_SUPPORTED,
                // ERROR_INVALID_PARAMETER, ERROR_DISK_FULL, ERROR_ALREADY_EXISTS, ERROR_IO_DEVICE, ERROR_USER_MAPPED_FILE,
                // ERROR_FILE_CORRUPT, ERROR_DISK_CORRUPT
                foreach (int code in new[] { 2, 6, 14, 19, 23, 50, 87, 112, 183, 1117, 1224, 1392, 1393 })
                {
                    File.WriteAllBytes(p, Old);
                    Exception e = null;
                    long ms = 0;
                    WithMoveFileError(code, () =>
                    {
                        var sw = Stopwatch.StartNew();
                        e = T.Throws<Exception>(() => SafeFile.Write(p, New), "error " + code);
                        ms = sw.ElapsedMilliseconds;
                    });
                    T.Eq(code, OtherRenameError(e, "t.bin", "error " + code), "error " + code + ": Windows error in the HRESULT");
                    T.True(ms < 500, "error " + code + ": thrown at once, not retried (" + ms + " ms)");
                    T.Bytes(Old, File.ReadAllBytes(p), "error " + code + ": target intact");
                    NoTemp(p, "error " + code);
                    r.Note($"{code}: {e.Message} (before R11: {BeforeR11(code)})");
                }
                T.Eq(1, Directory.GetFiles(dir).Length, "only the target in the folder");

                // a target that does not exist yet stays missing
                string q = Path.Combine(dir, "new.bin");
                WithMoveFileError(112, () => OtherRenameError(T.Throws<Exception>(() => SafeFile.Write(q, New), "missing target"), "new.bin", "missing target"));
                T.True(!File.Exists(q), "missing target: not created");
                NoTemp(q, "missing target");
                SafeFile.Write(p, New);
                T.Bytes(New, File.ReadAllBytes(p), "replaced once the rename works again");
                T.Eq(1, Directory.GetFiles(dir).Length, "only the target in the folder");
            });

            r.Test("SafeFile: real rename errors other than in use (\\\\.\\NUL, a drive that does not exist, an invalid name) -> IOException naming the file with the Windows reason, at once; the temp file is untouched (R11)", () =>
            {
                string dir = ctx.Dir("safefile_other_error_real");
                string tmp = Path.Combine(dir, "t.bin" + SafeFile.TempSuffix);
                var used = new HashSet<char>(DriveInfo.GetDrives().Select(d => char.ToUpperInvariant(d.Name[0])));
                char free = "ZYXWVUTSRQPONMLKJIHGFED".FirstOrDefault(c => !used.Contains(c));
                var targets = new List<(string Target, string Name)> { (@"\\.\NUL", "NUL"), (Path.Combine(dir, "t?.bin"), "t?.bin") };
                if (free != default(char))
                    targets.Add((free + @":\tpcjp\t.bin", "t.bin"));
                else
                    r.Note("no free drive letter: the missing-drive case is skipped");
                foreach (var (target, name) in targets)
                {
                    File.WriteAllBytes(tmp, New);
                    var sw = Stopwatch.StartNew();
                    var e = T.Throws<Exception>(() => Replace(tmp, target), target);
                    long ms = sw.ElapsedMilliseconds;
                    int code = OtherRenameError(e, name, target);
                    T.True(ms < 500, target + ": thrown at once, not retried (" + ms + " ms)");
                    T.Bytes(New, File.ReadAllBytes(tmp), target + ": the temp file is untouched");
                    T.Eq(1, Directory.GetFiles(dir).Length, target + ": nothing else created");
                    r.Note($"{target}: Windows error {code}: {e.Message} (before R11: {BeforeR11(code)})");
                }
                File.Delete(tmp);
            });

            r.Test("SafeFile: a rename error other than in use reaches Patcher as a file error: the state file -> the apply completes with the warning; the bundle -> IOException naming it, bundle unchanged (R11)", () =>
            {
                // MoveFileEx fails with `code` only for the temp file of `file` (the other writes work)
                Action<string> failRenameOf(string file, int code) => tmp =>
                    Environment.SetEnvironmentVariable("TPCJP_SIMULATE_MOVEFILE_ERROR",
                        tmp.EndsWith(@"\" + Path.GetFileName(file) + SafeFile.TempSuffix, StringComparison.OrdinalIgnoreCase) ? code.ToString() : null);

                // before R11 the COMException passed the warning's catch: the apply ended with 予期しないエラー after the bundle was written
                var g = ctx.NewGame("safefile_r11_state", ctx.Official);
                try
                {
                    WithBeforeRename(failRenameOf(g.State, 112), () => Util.Retry(() => g.Patcher().Apply(ctx.Data)));
                }
                finally
                {
                    Environment.SetEnvironmentVariable("TPCJP_SIMULATE_MOVEFILE_ERROR", null);
                }
                string stateError = Path.GetFileName(g.State) + " を置き換えられません（" + new Win32Exception(112).Message + "）";
                T.True(g.Log.Any(l => l.EndsWith(stateError, StringComparison.Ordinal)), "the warning gives SafeFile's message: " + g.LogText);
                T.True(Util.Sha256(File.ReadAllBytes(g.Bundle)) != Util.Sha256(ctx.Official), "bundle patched");
                T.Bytes(ctx.Official, File.ReadAllBytes(g.Backup), "backup written");
                T.True(!File.Exists(g.State), "no state file");
                T.Eq(0, g.Leftovers().Length, "no temp files: " + string.Join(", ", g.Leftovers()));
                r.Note("state: " + g.Log.First(l => l.EndsWith(stateError, StringComparison.Ordinal)));

                // before R11: COMException -> CLI 「予期しないエラー: System.Runtime.InteropServices.COMException …」 with a stack trace
                var g2 = ctx.NewGame("safefile_r11_bundle", ctx.Official);
                Exception e = null;
                try
                {
                    WithBeforeRename(failRenameOf(g2.Bundle, 1117),
                        () => e = T.Throws<Exception>(() => Util.Retry(() => g2.Patcher().Apply(ctx.Data)), "bundle rename refused"));
                }
                finally
                {
                    Environment.SetEnvironmentVariable("TPCJP_SIMULATE_MOVEFILE_ERROR", null);
                }
                T.Eq(typeof(IOException), e.GetType(), "exception type (the CLI and the GUI show it as a file error, not 予期しないエラー): " + e.Message);
                T.Eq(Path.GetFileName(g2.Bundle) + " を置き換えられません（" + new Win32Exception(1117).Message + "）", e.Message, "message names the bundle and gives the Windows reason");
                T.Bytes(ctx.Official, File.ReadAllBytes(g2.Bundle), "bundle unchanged");
                T.True(!File.Exists(g2.State), "no state file");
                T.Eq(0, g2.Leftovers().Length, "no temp files: " + string.Join(", ", g2.Leftovers()));
                r.Note("bundle: " + e.GetType().Name + ": " + e.Message);
                // round 3c (S10): the CLI and the GUI show it as it is, without the 'in use' advice and without nested brackets
                T.Eq("エラー: " + e.Message, Cli.FileErrorLine(e), "CLI line");
                T.Eq(e.Message, MainForm.FileErrorText(e), "GUI dialog text");
                r.Note("CLI: " + Cli.FileErrorLine(e));
            });

            r.Test("SafeFile: file errors in the CLI line and the GUI dialog: SafeFile's own replacement errors as they are, the 'game or Steam' advice only for a file in use, the reason always, no nested '。））' (S10)", () =>
            {
                string dir = ctx.Dir("safefile_error_texts");
                string p = Path.Combine(dir, "t.bin");
                File.WriteAllBytes(p, Old);
                const string InUseAdviceCli = "エラー: ファイルを読み書きできません。ゲームや Steam がファイルを使っていないか確認してください（";
                const string InUseAdviceGui = "ファイルを読み書きできませんでした。ゲームや Steam がファイルを使っていないか確認してから、もう一度お試しください。\n\n";
                const string AdminGui = "ゲームのフォルダーのファイルにアクセスできませんでした。\nゲームと Steam のファイル操作が終わっているか確認し、うまくいかない場合はこのツールを右クリックして「管理者として実行」してください。\n\n";
                var cases = new List<Tuple<string, Exception, string, string>>();
                // SafeFile: a rename refused for another reason (disk full), and still refused as in use after its retries
                Exception diskFull = null, inUse = null, denied = null;
                WithMoveFileError(112, () => diskFull = T.Throws<IOException>(() => SafeFile.Write(p, New), "disk full"));
                WithoutPosixRename(() => WithMoveFileError(32, () => inUse = T.Throws<IOException>(() => SafeFile.Write(p, New), "in use")));
                WithoutPosixRename(() => WithMoveFileError(5, () => denied = T.Throws<UnauthorizedAccessException>(() => SafeFile.Write(p, New), "access denied")));
                T.Bytes(Old, File.ReadAllBytes(p), "target intact");
                cases.Add(Tuple.Create("SafeFile, disk full", diskFull, "エラー: t.bin を置き換えられません（" + new Win32Exception(112).Message + "）", "t.bin を置き換えられません（" + new Win32Exception(112).Message + "）"));
                cases.Add(Tuple.Create("SafeFile, in use", inUse, "エラー: t.bin" + InUseText + new Win32Exception(32).Message, "t.bin" + InUseText + new Win32Exception(32).Message));
                cases.Add(Tuple.Create("SafeFile, in use (access denied)", denied, "エラー: t.bin" + InUseText + new Win32Exception(5).Message, "t.bin" + InUseText + new Win32Exception(5).Message));
                // other file errors (from .NET): in use, another I/O error, access denied
                Exception netInUse, netOther, netDenied;
                using (new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.None))
                    netInUse = T.Throws<IOException>(() => File.ReadAllBytes(p), ".NET in use");
                netOther = T.Throws<IOException>(() => File.ReadAllBytes(Path.Combine(dir, "no", "such", "file")), ".NET missing folder");
                netDenied = T.Throws<UnauthorizedAccessException>(() => File.ReadAllBytes(dir), ".NET access denied (a folder)");
                cases.Add(Tuple.Create(".NET, in use", netInUse, InUseAdviceCli + netInUse.Message + "）", InUseAdviceGui + netInUse.Message));
                cases.Add(Tuple.Create(".NET, missing folder", netOther, "エラー: ファイルを読み書きできません（" + netOther.Message + "）", "ファイルを読み書きできませんでした。\n\n" + netOther.Message));
                cases.Add(Tuple.Create(".NET, access denied", netDenied, "エラー: ファイルにアクセスできません（" + netDenied.Message + "）", AdminGui + netDenied.Message));
                foreach (var c in cases)
                {
                    T.Eq(c.Item1.StartsWith("SafeFile", StringComparison.Ordinal), SafeFile.IsReplaceError(c.Item2), c.Item1 + ": IsReplaceError");
                    string line = Cli.FileErrorLine(c.Item2), text = MainForm.FileErrorText(c.Item2);
                    T.Eq(c.Item3, line, c.Item1 + ": CLI line");
                    T.Eq(c.Item4, text, c.Item1 + ": GUI dialog text");
                    T.True(!line.Contains("。））") && !line.Contains("））"), c.Item1 + ": no nested brackets at the end: " + line);
                    T.Eq(c.Item1.Contains("in use") && !c.Item1.StartsWith("SafeFile", StringComparison.Ordinal), line.Contains("ゲームや Steam"), c.Item1 + ": 'game or Steam' advice only for a .NET in-use error: " + line);
                    T.Contains(text, c.Item2.Message, c.Item1 + ": the dialog gives the reason");
                    r.Note(c.Item1 + ": " + line);
                }
                T.True(SafeFile.IsInUse((IOException)netInUse) && SafeFile.IsInUse((IOException)inUse) && !SafeFile.IsInUse((IOException)netOther) && !SafeFile.IsInUse((IOException)diskFull),
                    "IsInUse: sharing violation yes, missing folder / disk full no");
                T.Eq(1, Directory.GetFiles(dir).Length, "only the target in the folder");
            });

            foreach (var share in new[] { FileShare.Read, FileShare.None, FileShare.ReadWrite })
            {
                r.Test($"SafeFile: the temp file (the rename source) opened by another program without FileShare.Delete (FileShare.{share}) just before the rename and let go after 150 ms -> replaced after a retry, no temp file (R13)", () =>
                {
                    string dir = ctx.Dir("safefile_tmp_held_" + share);
                    string p = Path.Combine(dir, "t.bin");
                    File.WriteAllBytes(p, Old);
                    Thread holder = null;
                    long ms = 0;
                    // MoveFileEx: ERROR_SHARING_VIOLATION on the source; the POSIX rename cannot open it for DELETE either
                    WithBeforeRename(tmp => holder = HoldFor(tmp, share, 150), () =>
                    {
                        var sw = Stopwatch.StartNew();
                        SafeFile.Write(p, New);
                        ms = sw.ElapsedMilliseconds;
                    });
                    T.True(holder != null, "the temp file was opened before the rename");
                    holder.Join();
                    T.Bytes(New, File.ReadAllBytes(p), "replaced");
                    T.True(ms >= 100 && ms < 1000, "replaced once the holder let go (" + ms + " ms)");
                    NoTemp(p, "temp held for 150 ms");
                    T.Eq(1, Directory.GetFiles(dir).Length, "only the target in the folder");
                    r.Note($"replaced after {ms} ms");
                });
            }

            r.Test("SafeFile: the temp file held by another program without FileShare.Delete for the whole write -> IOException naming the target after about 1 s of retries, target intact; the next run removes the temp file once it is let go (R13)", () =>
            {
                string dir = ctx.Dir("safefile_tmp_held_whole");
                string p = Path.Combine(dir, "t.bin");
                string tmp = p + SafeFile.TempSuffix;
                File.WriteAllBytes(p, Old);
                FileStream holder = null;
                try
                {
                    Exception e = null;
                    long ms = 0;
                    WithBeforeRename(t => holder = new FileStream(t, FileMode.Open, FileAccess.Read, FileShare.Read), () =>
                    {
                        var sw = Stopwatch.StartNew();
                        e = T.Throws<Exception>(() => SafeFile.Write(p, New), "temp file held");
                        ms = sw.ElapsedMilliseconds;
                    });
                    T.True(holder != null, "the temp file was opened before the rename");
                    T.Eq(typeof(IOException), e.GetType(), "exception type (callers catch IOException / UnauthorizedAccessException): " + e.Message);
                    T.Eq("t.bin" + InUseText + new Win32Exception(32).Message, e.Message, "message names the target and gives the Windows reason");
                    T.Eq(HResult(32), e.HResult, "HRESULT of ERROR_SHARING_VIOLATION");
                    T.True(ms >= 700 && ms <= 3000, "refused after about 1 s of retries (" + ms + " ms)");
                    T.Bytes(Old, File.ReadAllBytes(p), "target intact while the temp file is held");
                    // a file open without FILE_SHARE_DELETE cannot be deleted: Write leaves it to the next run
                    T.True(File.Exists(tmp), "the temp file is still there while it is held");
                    T.Bytes(New, ReadAll(holder), "the holder reads the complete new content");
                    r.Note($"{e.GetType().Name} after {ms} ms: {e.Message}");
                }
                finally
                {
                    holder?.Dispose();
                }
                T.Eq(1, SafeFile.RemoveLeftovers(p), "the next run (Apply / Restore start) removes the temp file once it is let go");
                NoTemp(p, "after RemoveLeftovers");
                T.Bytes(Old, File.ReadAllBytes(p), "target still intact");
                SafeFile.Write(p, New);
                T.Bytes(New, File.ReadAllBytes(p), "replaced by the next write");
                NoTemp(p, "next write");
                T.Eq(1, Directory.GetFiles(dir).Length, "only the target in the folder");
            });

            r.Test("SafeFile: the temp file held by another program with FileShare.Read|Delete for the whole write -> replaced at once (the open file is renamed), no temp file (R13)", () =>
            {
                string dir = ctx.Dir("safefile_tmp_held_sharedelete");
                string p = Path.Combine(dir, "t.bin");
                File.WriteAllBytes(p, Old);
                FileStream holder = null;
                try
                {
                    long ms = 0;
                    WithBeforeRename(t => holder = new FileStream(t, FileMode.Open, FileAccess.Read, FileShare.Read | FileShare.Delete), () =>
                    {
                        var sw = Stopwatch.StartNew();
                        SafeFile.Write(p, New);
                        ms = sw.ElapsedMilliseconds;
                    });
                    T.True(holder != null, "the temp file was opened before the rename");
                    T.True(ms < 500, "replaced without waiting for retries (" + ms + " ms)");
                    T.Bytes(New, File.ReadAllBytes(p), "replaced");
                    T.Bytes(New, ReadAll(holder), "the holder's handle now reads the target");
                    NoTemp(p, "temp held with share-delete");
                }
                finally
                {
                    holder?.Dispose();
                }
                T.Bytes(New, File.ReadAllBytes(p), "new content after the holder let go");
                T.Eq(1, Directory.GetFiles(dir).Length, "only the target in the folder");
            });

            r.Test("SafeFile.RemoveLeftovers: removes <name>.tpcjp-tmp and <name>~RF*.TMP (any case, read-only); keeps everything else", () =>
            {
                string dir = ctx.Dir("safefile_leftovers");
                string name = "localisation_assets_localisationjapanese.bundle";
                string p = Path.Combine(dir, name);
                File.WriteAllBytes(p, Old);
                var remove = new[] { name + ".tpcjp-tmp", name + "~RF38b3fbc.TMP", name + "~rf1a.tmp", name + "~RF.TMP" };
                var keep = new[]
                {
                    name, name + ".tpcjp-original", name + ".tpcjp-state", name + ".tpcjp-original~RF38a219a.TMP", // another file's leftover
                    name + ".tpcjp-tmp.bak", name + "~RF1.TXT", name + "x~RF1.TMP", "other.bundle~RF1.TMP", "other.bundle.tpcjp-tmp",
                    name + ".TPCJP-TMP2", name + " ~RF1.TMP",
                };
                foreach (var f in remove.Concat(keep))
                    File.WriteAllBytes(Path.Combine(dir, f), Old);
                File.SetAttributes(Path.Combine(dir, remove[1]), FileAttributes.ReadOnly | FileAttributes.Hidden);
                Directory.CreateDirectory(Path.Combine(dir, name + "~RF2.TMP")); // a folder is never removed
                T.Eq(remove.Length, SafeFile.RemoveLeftovers(p), "removed count");
                foreach (var f in remove)
                    T.True(!File.Exists(Path.Combine(dir, f)), "removed: " + f);
                foreach (var f in keep)
                    T.True(File.Exists(Path.Combine(dir, f)), "kept: " + f);
                T.True(Directory.Exists(Path.Combine(dir, name + "~RF2.TMP")), "folder kept");
                T.Eq(0, SafeFile.RemoveLeftovers(p), "second call: nothing left");
                T.Eq(0, SafeFile.RemoveLeftovers(Path.Combine(dir, "no", "such", name)), "missing folder: 0, no exception");
                T.Eq(1, SafeFile.RemoveLeftovers(p + ".tpcjp-original"), "the backup's own ~RF leftover");
                T.Eq(0, SafeFile.RemoveLeftovers(Path.Combine(dir, "missing.bundle")), "a target that does not exist and has no leftovers");
            });

            r.Test("SafeFile.RemoveLeftovers: a leftover held open by another process is skipped without an exception", () =>
            {
                string dir = ctx.Dir("safefile_leftovers_locked");
                string p = Path.Combine(dir, "t.bin");
                File.WriteAllBytes(p, Old);
                File.WriteAllBytes(p + "~RF1.TMP", Old);
                File.WriteAllBytes(p + "~RF2.TMP", Old);
                using (new FileStream(p + "~RF1.TMP", FileMode.Open, FileAccess.Read, FileShare.None))
                    T.Eq(1, SafeFile.RemoveLeftovers(p), "only the free one is removed");
                T.True(File.Exists(p + "~RF1.TMP") && !File.Exists(p + "~RF2.TMP"), "locked one kept, free one removed");
                T.Eq(1, SafeFile.RemoveLeftovers(p), "removed on the next call");
                T.Bytes(Old, File.ReadAllBytes(p), "target untouched");
            });
        }
    }
}
