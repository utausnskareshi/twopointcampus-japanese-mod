using System;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

namespace TpcJpPatch
{
    static class Program
    {
        public static string Version => Assembly.GetExecutingAssembly().GetName().Version.ToString(3);

        [DllImport("kernel32.dll")]
        static extern bool AttachConsole(int processId);

        [DllImport("kernel32.dll")]
        static extern IntPtr GetStdHandle(int stdHandle);

        /// <summary>The library the patcher needs. It ships next to the exe: 1.0.0 embedded it and loaded it from
        /// memory, which antivirus heuristics can mistake for malware.</summary>
        const string Library = "AssetsTools.NET.dll";

        [STAThread]
        static int Main(string[] args)
        {
            // Checked before any code that uses the library runs (Main itself never needs it).
            string missingLibrary = MissingLibraryMessage();
            if (args.Length > 0)
            {
                // A GUI-subsystem exe started from cmd/PowerShell has no stdout handle (Console.IsOutputRedirected
                // wrongly says "redirected" then): attach to that console. A pipe or file gets UTF-8.
                IntPtr stdout = GetStdHandle(-11);
                if (stdout == IntPtr.Zero || stdout == new IntPtr(-1))
                    AttachConsole(-1);
                else
                    Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true });
                if (missingLibrary != null)
                {
                    Console.WriteLine();
                    Console.WriteLine($"Two Point Campus 日本語化パッチ {Version}");
                    Console.WriteLine("エラー: " + missingLibrary);
                    WriteLogFile("エラー: " + missingLibrary);
                    return 1;
                }
                return Cli.Run(args);
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            if (missingLibrary != null)
            {
                WriteLogFile(Library + " が見つからないため終了しました");
                MessageBox.Show(missingLibrary, MainForm.Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
            // The game folder is detected from Steam at startup; without an installed game there is nothing to patch.
            string gameDir = GameLocator.FindGameDir();
            if (gameDir == null)
            {
                WriteLogFile("Two Point Campus（Steam版）が見つからないため終了しました");
                MessageBox.Show(
                    "Two Point Campus（Steam版）がこのPCにインストールされていません。\n\n" +
                    "Steam でゲームをインストールしてから、もう一度実行してください。",
                    MainForm.Title, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return 1;
            }
            Application.Run(new MainForm(gameDir));
            return 0;
        }

        /// <summary>Null when AssetsTools.NET.dll is next to the exe (where the runtime looks for it), otherwise the
        /// message to show: typically the exe was started from inside the ZIP or copied out of it alone.</summary>
        static string MissingLibraryMessage()
        {
            if (File.Exists(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, Library)))
                return null;
            return Library + " が TpcJpPatch.exe と同じフォルダーにありません。" +
                "ZIP ファイルをすべて展開してから、展開したフォルダーにある TpcJpPatch.exe を実行してください。";
        }

        /// <summary>Appends to %LOCALAPPDATA%\TpcJpPatch\TpcJpPatch.log (best effort).</summary>
        public static void WriteLogFile(string line)
        {
            try
            {
                Directory.CreateDirectory(Patcher.AppDataDir);
                File.AppendAllText(Path.Combine(Patcher.AppDataDir, "TpcJpPatch.log"),
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss ", CultureInfo.InvariantCulture) + line + Environment.NewLine,
                    new UTF8Encoding(false));
            }
            catch (Exception)
            {
                // logging must never break the patcher
            }
        }
    }
}
