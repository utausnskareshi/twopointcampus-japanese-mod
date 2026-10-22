using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace TpcJpPatch
{
    /// <summary>
    /// File replacement that never leaves a half-written or missing target: the new content goes to
    /// "&lt;target&gt;.tpcjp-tmp", is flushed to disk, and is then renamed over the target in one step
    /// (MoveFileEx with MOVEFILE_REPLACE_EXISTING; File.Replace renames the old file aside first and can leave the
    /// target missing when the process is killed in between). MoveFileEx refuses a target that another program
    /// has open, even with FILE_SHARE_DELETE (a virus scanner, an indexer, Patcher.Sha256); such a target is
    /// replaced by a rename with POSIX semantics (also one step), and a rename that is still refused is retried
    /// for about a second. A read-only attribute on the target is cleared first, and the temp file is removed
    /// whatever happens.
    /// </summary>
    static class SafeFile
    {
        public const string TempSuffix = ".tpcjp-tmp";

        const int MoveFileReplaceExisting = 0x1;
        const int MoveFileWriteThrough = 0x8;
        const int AccessDelete = 0x10000, AccessSynchronize = 0x100000;
        const int FileRenameInfoEx = 22;
        const int RenameReplaceIfExists = 0x1, RenamePosixSemantics = 0x2;
        const int ErrorAccessDenied = 5, ErrorGenFailure = 31, ErrorSharingViolation = 32, ErrorLockViolation = 33, ErrorInvalidParameter = 87;
        const int RenameAttempts = 10, RenameFirstDelayMs = 10, RenameMaxDelayMs = 200; // waits 10, 20, 40 … 200 ms: 1.1 s in all

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern bool MoveFileEx(string existingFileName, string newFileName, int flags);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern SafeFileHandle CreateFile(string fileName, int desiredAccess, FileShare shareMode, IntPtr securityAttributes,
            FileMode creationDisposition, int flagsAndAttributes, IntPtr templateFile);

        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool SetFileInformationByHandle(SafeFileHandle file, int fileInformationClass, IntPtr fileInformation, int bufferSize);

        /// <summary>
        /// FILE_RENAME_INFO up to the first character of the name: Flags (a union with ReplaceIfExists), RootDirectory,
        /// FileNameLength (in bytes, without the terminating NUL) and the name, at 0 / 8 / 16 / 20 in a 64-bit process
        /// and 0 / 4 / 8 / 12 in a 32-bit one.
        /// </summary>
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        struct RenameInfo
        {
            public int Flags;
            public IntPtr RootDirectory;
            public int FileNameLength;
            public char FileName;
        }

#if DEBUG
        /// <summary>Test hook (debug builds only): called with the temp file's path once it is written and closed, just
        /// before the rename (another program opening it at that moment, R13).</summary>
        internal static Action<string> BeforeRename;
#endif

        public static void Write(string target, byte[] content)
        {
            string tmp = target + TempSuffix;
            try
            {
                Delete(tmp);
                using (var fs = new FileStream(tmp, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1 << 16, FileOptions.WriteThrough))
                {
                    fs.Write(content, 0, content.Length);
                    fs.Flush(true);
                }
                if (File.Exists(target))
                    ClearReadOnly(target);
#if DEBUG
                BeforeRename?.Invoke(tmp);
#endif
                Replace(tmp, target);
            }
            finally
            {
                try
                {
                    Delete(tmp);
                }
                catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
                {
                    // the original error (if any) matters more than a leftover temp file
                }
            }
        }

        /// <summary>
        /// Renames `tmp` over `target` in one step; retries for about a second while another program has either open.
        /// Every failure is thrown by RenameError; the errors of the POSIX rename are never thrown themselves (in-use ones
        /// replace MoveFileEx's, any other turns the POSIX rename off).
        /// </summary>
        static void Replace(string tmp, string target)
        {
            bool posix = true;
            for (int attempt = 1; ; attempt++)
            {
                int error = MoveFile(tmp, target);
                if (error == 0)
                    return;
                if (!InUse(error))
                    throw RenameError(target, error);
                if (posix)
                {
                    int e = RenamePosix(tmp, target);
                    if (e == 0)
                        return;
                    if (InUse(e))
                        error = e; // more precise: ERROR_SHARING_VIOLATION when the target is open without FILE_SHARE_DELETE
                    else
                        posix = false; // not supported here (FAT / exFAT, network drives, Windows before 10 1709 answer
                                       // ERROR_INVALID_PARAMETER or ERROR_NOT_SUPPORTED): MoveFileEx alone from now on
                }
                if (attempt == RenameAttempts)
                    throw RenameError(target, error);
                Thread.Sleep(Math.Min(RenameFirstDelayMs << (attempt - 1), RenameMaxDelayMs));
            }
        }

        /// <summary>MoveFileEx(MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH). Returns 0 or the Windows error.</summary>
        static int MoveFile(string tmp, string target)
        {
#if DEBUG
            // Test hook (debug builds only): MoveFileEx fails with this Windows error (ERROR_DISK_FULL, ERROR_IO_DEVICE …).
            if (int.TryParse(Environment.GetEnvironmentVariable("TPCJP_SIMULATE_MOVEFILE_ERROR"), out int simulated) && simulated != 0)
                return simulated;
#endif
            return MoveFileEx(tmp, target, MoveFileReplaceExisting | MoveFileWriteThrough) ? 0 : LastError();
        }

        /// <summary>
        /// SetFileInformationByHandle(FileRenameInfoEx) with FILE_RENAME_FLAG_POSIX_SEMANTICS: replaces a target that
        /// is open with FILE_SHARE_DELETE in one step (the open handles keep the old file). Returns 0 or the Windows error.
        /// </summary>
        static int RenamePosix(string tmp, string target)
        {
#if DEBUG
            // Test hook (debug builds only): behave as on a volume or a Windows version without POSIX renames.
            if (Environment.GetEnvironmentVariable("TPCJP_SIMULATE_NO_POSIX_RENAME") == "1")
                return ErrorInvalidParameter;
#endif
            using (var h = CreateFile(tmp, AccessDelete | AccessSynchronize, FileShare.ReadWrite | FileShare.Delete, IntPtr.Zero, FileMode.Open, 0, IntPtr.Zero))
            {
                if (h.IsInvalid)
                    return LastError(); // ERROR_SHARING_VIOLATION when another program has the temp file open without FILE_SHARE_DELETE
                string name = Path.GetFullPath(target);
                int nameOffset = (int)Marshal.OffsetOf(typeof(RenameInfo), nameof(RenameInfo.FileName));
                int size = Math.Max(Marshal.SizeOf(typeof(RenameInfo)), nameOffset + (name.Length + 1) * 2); // with the NUL
                IntPtr info = Marshal.AllocHGlobal(size);
                try
                {
                    Marshal.StructureToPtr(new RenameInfo { Flags = RenameReplaceIfExists | RenamePosixSemantics, FileNameLength = name.Length * 2 }, info, false);
                    Marshal.Copy((name + "\0").ToCharArray(), 0, info + nameOffset, name.Length + 1);
                    return SetFileInformationByHandle(h, FileRenameInfoEx, info, size) ? 0 : LastError();
                }
                finally
                {
                    Marshal.FreeHGlobal(info);
                }
            }
        }

        static bool InUse(int error) => error == ErrorAccessDenied || error == ErrorSharingViolation || error == ErrorLockViolation;

        static int HResult(int error) => unchecked((int)0x80070000 | (error & 0xFFFF));

        /// <summary>The Windows error of the call that just failed; never 0, which the callers would take for success.</summary>
        static int LastError()
        {
            int error = Marshal.GetLastWin32Error();
            return error != 0 ? error : ErrorGenFailure;
        }

        /// <summary>
        /// The exception for a rename that failed with `error`. The callers catch only IOException and
        /// UnauthorizedAccessException (anything else is reported as 予期しないエラー), so every Windows error becomes one of
        /// them, naming the file and giving the Windows reason: UnauthorizedAccessException for ERROR_ACCESS_DENIED, otherwise
        /// IOException with the HRESULT. Marshal.GetExceptionForHR is not used: it gives COMException, ArgumentException,
        /// BadImageFormatException or OutOfMemoryException for errors such as ERROR_DISK_FULL, ERROR_IO_DEVICE,
        /// ERROR_INVALID_PARAMETER, ERROR_FILE_CORRUPT or ERROR_OUTOFMEMORY (R11).
        /// </summary>
        static Exception RenameError(string target, int error)
        {
            string reason = new Win32Exception(error).Message;
            string message = Path.GetFileName(target) + (InUse(error)
                ? " を置き換えられません。別のプログラム（ウイルス対策ソフトなど）が使用中の可能性があります: " + reason
                : " を置き換えられません（" + reason + "）");
            var e = error == ErrorAccessDenied ? new UnauthorizedAccessException(message) : (Exception)new IOException(message, HResult(error));
            e.Data[ReplaceErrorKey] = true;
            return e;
        }

        const string ReplaceErrorKey = "TpcJpPatch.SafeFile.ReplaceError";

        /// <summary>
        /// Whether e is a failed replacement thrown by Write: its message names the file and gives the Windows reason (and,
        /// when the file is in use, says that another program may be using it), so it is shown as it is.
        /// </summary>
        public static bool IsReplaceError(Exception e) => e.Data.Contains(ReplaceErrorKey);

        /// <summary>Whether an IOException reports a file in use by another program (ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION).</summary>
        public static bool IsInUse(IOException e) => e.HResult == HResult(ErrorSharingViolation) || e.HResult == HResult(ErrorLockViolation);

        public static void Delete(string path)
        {
            if (!File.Exists(path))
                return;
            ClearReadOnly(path);
            File.Delete(path);
        }

        /// <summary>Removes the read-only attribute; returns true when it was set.</summary>
        public static bool ClearReadOnly(string path)
        {
            var attributes = File.GetAttributes(path);
            if ((attributes & FileAttributes.ReadOnly) == 0)
                return false;
            File.SetAttributes(path, attributes & ~FileAttributes.ReadOnly);
            return true;
        }

        /// <summary>
        /// Deletes leftovers of an interrupted run for a file: "&lt;name&gt;.tpcjp-tmp" and the "&lt;name&gt;~RF*.TMP"
        /// copies that File.Replace (used by version 1.0.0) could leave. Call while holding Patcher.Lock().
        /// </summary>
        public static int RemoveLeftovers(string path)
        {
            string dir = Path.GetDirectoryName(path), name = Path.GetFileName(path);
            if (!Directory.Exists(dir))
                return 0;
            int n = 0;
            foreach (var f in Directory.GetFiles(dir, name + "*"))
            {
                string rest = Path.GetFileName(f).Substring(name.Length);
                bool leftover = rest == TempSuffix
                    || (rest.StartsWith("~RF", StringComparison.OrdinalIgnoreCase) && rest.EndsWith(".TMP", StringComparison.OrdinalIgnoreCase));
                if (!leftover)
                    continue;
                try
                {
                    Delete(f);
                    n++;
                }
                catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
                {
                    // still in use: left for the next run
                }
            }
            return n;
        }
    }
}
