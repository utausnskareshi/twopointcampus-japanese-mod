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

        [STAThread]
        static int Main(string[] args)
        {
            // AssetsTools.NET.dll is embedded so the tool can be distributed as a single exe.
            AppDomain.CurrentDomain.AssemblyResolve += ResolveEmbeddedAssembly;
            if (args.Length > 0)
            {
                // A GUI-subsystem exe started from cmd/PowerShell has no stdout handle (Console.IsOutputRedirected
                // wrongly says "redirected" then): attach to that console. A pipe or file gets UTF-8.
                IntPtr stdout = GetStdHandle(-11);
                if (stdout == IntPtr.Zero || stdout == new IntPtr(-1))
                    AttachConsole(-1);
                else
                    Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true });
                return Cli.Run(args);
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
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

        static Assembly ResolveEmbeddedAssembly(object sender, ResolveEventArgs e)
        {
            string name = new AssemblyName(e.Name).Name + ".dll";
            using (var s = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
            {
                if (s == null)
                    return null;
                var ms = new MemoryStream();
                s.CopyTo(ms);
                return Assembly.Load(ms.ToArray());
            }
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
