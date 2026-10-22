using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace TpcJpPatch
{
    /// <summary>
    /// Command line:
    ///   TpcJpPatch.exe status   [--game DIR]
    ///   TpcJpPatch.exe apply    [--game DIR] [--data FILE] [--no-language]
    ///   TpcJpPatch.exe restore  [--game DIR] [--data FILE] [--keep-language]
    ///   TpcJpPatch.exe language ja|en|previous
    /// Exit code: 0 success, 1 error (including a failed language switch), 2 usage.
    /// </summary>
    static class Cli
    {
        const string Usage =
            "使い方:\n" +
            "  TpcJpPatch.exe status   [--game フォルダー]\n" +
            "  TpcJpPatch.exe apply    [--game フォルダー] [--data 翻訳データ] [--no-language]\n" +
            "  TpcJpPatch.exe restore  [--game フォルダー] [--data 翻訳データ] [--keep-language]\n" +
            "  TpcJpPatch.exe language ja|en|previous";

        static readonly Dictionary<string, string[]> Options = new Dictionary<string, string[]>
        {
            ["status"] = new[] { "--game" },
            ["apply"] = new[] { "--game", "--data", "--no-language" },
            ["restore"] = new[] { "--game", "--data", "--keep-language" },
            ["language"] = new string[0],
        };
        static readonly HashSet<string> TakesValue = new HashSet<string> { "--game", "--data" };

        sealed class Args
        {
            public string Command;
            public readonly Dictionary<string, string> Values = new Dictionary<string, string>();
            public readonly HashSet<string> Flags = new HashSet<string>();
            public readonly List<string> Positional = new List<string>();
            public string Value(string name) => Values.TryGetValue(name, out var v) ? v : null;
        }

        static void Log(string line)
        {
            Console.WriteLine(line);
            Program.WriteLogFile(line);
        }

        static int UsageError(string message)
        {
            if (message != null)
                Console.WriteLine("エラー: " + message);
            Console.WriteLine(Usage);
            return 2;
        }

        public static int Run(string[] argv)
        {
            Console.WriteLine();
            Console.WriteLine($"Two Point Campus 日本語化パッチ {Program.Version}");
            if (!TryParse(argv, out var args, out var error))
                return UsageError(error);
            try
            {
                string game = args.Value("--game");
                if (game != null)
                {
                    // Windows PowerShell passes 'C:\a b\' as C:\a b" (the backslash escapes the closing quote).
                    game = game.Trim().TrimEnd('"').Trim();
                    try
                    {
                        game = Path.GetFullPath(game);
                    }
                    catch (Exception e) when (e is ArgumentException || e is NotSupportedException || e is PathTooLongException)
                    {
                        return UsageError("フォルダーの指定が正しくありません: " + args.Value("--game"));
                    }
                }
                else if (args.Command != "language")
                    game = GameLocator.FindGameDir();
                if (args.Command != "language" && !GameLocator.IsGameDir(game))
                {
                    Log(args.Value("--game") == null
                        ? "Two Point Campus（Steam版）がこのPCにインストールされていません"
                        : "指定されたフォルダーに Two Point Campus（TPC.exe）が見つかりません: " + game);
                    return 1;
                }
                var patcher = new Patcher(game, Log);
                using (args.Command == "status" ? null : Patcher.Lock())
                switch (args.Command)
                {
                    case "status":
                        PrintStatus(patcher);
                        return 0;
                    case "apply":
                    {
                        var data = LoadData(args.Value("--data"));
                        Log($"翻訳データ: {data.PatchVersion}（{data.Count} 件、対象 {data.GameVersion}）{data.Origin}");
                        patcher.Apply(data);
                        if (args.Flags.Contains("--no-language"))
                            return 0;
                        return LanguageExit(patcher.SetLanguage(Preferences.Japanese), "日本語データは適用しましたが、表示言語を日本語に切り替えられませんでした");
                    }
                    case "restore":
                    {
                        PatchData data = null;
                        try
                        {
                            data = LoadData(args.Value("--data"));
                        }
                        catch (InvalidDataException e)
                        {
                            Log("注意: 翻訳データを読み込めないため、状態ファイルだけで判断します（" + e.Message + "）");
                        }
                        patcher.Restore(data);
                        if (args.Flags.Contains("--keep-language"))
                            return 0;
                        return LanguageExit(patcher.RestoreLanguage(), "日本語データは元に戻しましたが、表示言語を戻せませんでした");
                    }
                    default: // language
                        switch (args.Positional[0])
                        {
                            case "ja": return LanguageExit(patcher.SetLanguage(Preferences.Japanese), null);
                            case "en": return LanguageExit(patcher.SetLanguage(Preferences.English), null);
                            default: return LanguageExit(patcher.RestoreLanguage(), null);
                        }
                }
            }
            catch (PatchException e)
            {
                Log("エラー: " + e.Message);
                return 1;
            }
            catch (InvalidDataException e)
            {
                Log("エラー: " + e.Message);
                return 1;
            }
            catch (Exception e) when (e is UnauthorizedAccessException || e is IOException)
            {
                Log(FileErrorLine(e));
                return 1;
            }
            catch (Exception e)
            {
                Log("予期しないエラー: " + e);
                return 1;
            }
        }

        /// <summary>
        /// The error line for a file error: a failed replacement by SafeFile as it is (it names the file and the Windows
        /// reason, and says when another program may be using it); the advice about the game or Steam using the file only
        /// for a file in use (S10).
        /// </summary>
        internal static string FileErrorLine(Exception e) =>
            SafeFile.IsReplaceError(e) ? "エラー: " + e.Message
            : e is UnauthorizedAccessException ? "エラー: ファイルにアクセスできません（" + e.Message + "）"
            : e is IOException io && SafeFile.IsInUse(io) ? "エラー: ファイルを読み書きできません。ゲームや Steam がファイルを使っていないか確認してください（" + e.Message + "）"
            : "エラー: ファイルを読み書きできません（" + e.Message + "）";

        /// <summary>Any problem reading the translation data is reported as a data problem (not as a game/Steam file problem).</summary>
        static PatchData LoadData(string path)
        {
            try
            {
                return PatchData.Load(path);
            }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
            {
                throw new InvalidDataException($"翻訳データを読み込めません: {path ?? PatchData.FileName}（{e.Message}）", e);
            }
        }

        static bool TryParse(string[] argv, out Args args, out string error)
        {
            args = new Args { Command = argv[0].ToLowerInvariant() };
            error = null;
            if (!Options.TryGetValue(args.Command, out var allowed))
            {
                error = "不明なコマンドです: " + argv[0];
                return false;
            }
            for (int i = 1; i < argv.Length; i++)
            {
                string a = argv[i];
                if (!a.StartsWith("--", StringComparison.Ordinal))
                {
                    args.Positional.Add(a.ToLowerInvariant());
                    continue;
                }
                if (!allowed.Contains(a))
                {
                    error = "不明なオプションです: " + a;
                    return false;
                }
                if (!TakesValue.Contains(a))
                {
                    args.Flags.Add(a);
                    continue;
                }
                if (i + 1 >= argv.Length || argv[i + 1].StartsWith("--", StringComparison.Ordinal) || argv[i + 1].Trim().Length == 0)
                {
                    error = a + " の後に値を指定してください";
                    return false;
                }
                args.Values[a] = argv[++i];
            }
            if (args.Command == "language")
            {
                if (args.Positional.Count != 1 || !new[] { "ja", "en", "previous" }.Contains(args.Positional[0]))
                {
                    error = "language の後に ja、en、previous のどれかを指定してください";
                    return false;
                }
            }
            else if (args.Positional.Count > 0)
            {
                error = "余分な引数があります: " + string.Join(" ", args.Positional);
                return false;
            }
            return true;
        }

        static int LanguageExit(Patcher.LanguageResult result, string summary)
        {
            if (result.Failures.Count == 0)
                return 0;
            if (summary != null)
                Log("エラー: " + summary);
            return 1;
        }

        static void PrintStatus(Patcher patcher)
        {
            PatchData data = null;
            try
            {
                data = LoadData(null);
            }
            catch (InvalidDataException)
            {
                // without data the status is decided from the state file and the backup only
            }
            var st = patcher.GetStatus(data);
            Console.WriteLine("ゲームのフォルダー: " + patcher.GameDir);
            Console.WriteLine("ゲームのバージョン: " + st.GameVersion);
            Console.WriteLine("日本語データ: " + MainForm.DescribeState(st));
            foreach (var l in st.Languages)
                Console.WriteLine($"表示言語（{l.Item1}）: {l.Item2}");
            if (st.Languages.Count == 0)
                Console.WriteLine("表示言語: 設定ファイルがありません（ゲームを一度起動してください）");
        }
    }
}
