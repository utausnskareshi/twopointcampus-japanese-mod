using System;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace TpcJpPatch
{
    sealed class MainForm : Form
    {
        readonly TextBox gameDirBox = new TextBox { ReadOnly = true, Dock = DockStyle.Fill, TabStop = false };
        readonly Button browseButton = new Button { Text = "参照…", AutoSize = true };
        readonly Label versionLabel = ValueLabel();
        readonly Label dataLabel = ValueLabel();
        readonly Label stateLabel = ValueLabel();
        readonly Label languageLabel = ValueLabel();
        readonly CheckBox languageCheck = new CheckBox
        {
            Text = "ゲームの表示言語も切り替える（おすすめ）", Checked = true, AutoSize = true, Margin = new Padding(3, 8, 3, 3),
        };
        readonly Button applyButton = new Button { Text = "日本語化する", AutoSize = true, Padding = new Padding(16, 4, 16, 4) };
        readonly Button restoreButton = new Button { Text = "元に戻す", AutoSize = true, Padding = new Padding(16, 4, 16, 4) };
        readonly TextBox logBox = new TextBox
        {
            Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill, TabStop = false,
        };

        PatchData data;
        string dataError;
        string gameDir;
        bool busy;

        public static string Title => $"Two Point Campus 日本語化パッチ {Program.Version}";

        public MainForm(string gameDir)
        {
            this.gameDir = gameDir;
            // Designer order: the sizes below are 96-DPI values that WinForms scales to the monitor's DPI.
            SuspendLayout();
            AutoScaleDimensions = new SizeF(96F, 96F);
            AutoScaleMode = AutoScaleMode.Dpi;
            Text = Title;
            Font = SystemFonts.MessageBoxFont;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(680, 520);
            MinimumSize = new Size(560, 460);

            var table = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 3, Padding = new Padding(12) };
            table.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            table.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            table.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));

            AddRow(table, "ゲームのフォルダー", gameDirBox, browseButton);
            AddRow(table, "ゲームのバージョン", versionLabel);
            AddRow(table, "翻訳データ", dataLabel);
            AddRow(table, "日本語データ", stateLabel);
            AddRow(table, "表示言語", languageLabel);

            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(languageCheck, 0, table.RowCount);
            table.SetColumnSpan(languageCheck, 3);
            table.RowCount++;

            var buttons = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, Margin = new Padding(0, 6, 0, 6) };
            applyButton.Font = new Font(Font, FontStyle.Bold);
            buttons.Controls.Add(applyButton);
            buttons.Controls.Add(restoreButton);
            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(buttons, 0, table.RowCount);
            table.SetColumnSpan(buttons, 3);
            table.RowCount++;

            table.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            table.Controls.Add(logBox, 0, table.RowCount);
            table.SetColumnSpan(logBox, 3);
            table.RowCount++;

            var notes = new Label
            {
                AutoSize = true,
                Margin = new Padding(3, 8, 3, 0),
                Text = "・実行する前にゲームを終了してください。\n" +
                       "・ゲームの更新、ファイルの整合性確認、Steam の言語設定の変更の後は、もう一度「日本語化する」を押してください。\n" +
                       "・ゲームの設定画面の言語欄は「トルコ語」と表示されますが、日本語で動作します（言語欄は変更しないでください）。",
            };
            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(notes, 0, table.RowCount);
            table.SetColumnSpan(notes, 3);
            table.RowCount++;

            Controls.Add(table);
            AcceptButton = applyButton;

            browseButton.Click += OnBrowse;
            applyButton.Click += OnApply;
            restoreButton.Click += OnRestore;
            Shown += (s, e) => Initialize();
            FormClosing += OnFormClosing;
            ResumeLayout(false);
            PerformLayout();
        }

        void OnFormClosing(object sender, FormClosingEventArgs e)
        {
            // Closing would end the process in the middle of replacing the game's files.
            if (!busy)
                return;
            e.Cancel = true;
            MessageBox.Show(this, "処理中です。終わるまでお待ちください。", Text, MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        static Label ValueLabel() => new Label { AutoSize = true, Margin = new Padding(3, 6, 3, 6) };

        static void AddRow(TableLayoutPanel table, string caption, Control value, Control extra = null)
        {
            table.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            table.Controls.Add(new Label { Text = caption, AutoSize = true, Margin = new Padding(3, 6, 12, 6) }, 0, table.RowCount);
            table.Controls.Add(value, 1, table.RowCount);
            if (extra != null)
                table.Controls.Add(extra, 2, table.RowCount);
            else
                table.SetColumnSpan(value, 2);
            table.RowCount++;
        }

        void Initialize()
        {
            try
            {
                data = PatchData.Load();
                dataLabel.Text = $"{data.PatchVersion}（翻訳 {data.Count:N0} 件、対象バージョン {data.GameVersion}）";
            }
            catch (Exception e)
            {
                dataError = e.Message;
                dataLabel.Text = "読み込めません: " + e.Message;
            }
            RefreshStatus();
        }

        void RefreshStatus()
        {
            bool ok = GameLocator.IsGameDir(gameDir);
            gameDirBox.Text = gameDir ?? "";
            applyButton.Enabled = restoreButton.Enabled = ok && data != null;
            if (!ok)
            {
                versionLabel.Text = stateLabel.Text = languageLabel.Text = "－";
                if (data == null)
                    restoreButton.Enabled = false;
                return;
            }
            restoreButton.Enabled = true;
            try
            {
                var st = new Patcher(gameDir, null).GetStatus(data);
                versionLabel.Text = st.GameVersion;
                stateLabel.Text = DescribeState(st);
                languageLabel.Text = st.Languages.Count == 0
                    ? "設定ファイルがありません（ゲームを一度起動してください）"
                    : st.Languages.Count == 1
                        ? st.Languages[0].Item2
                        : string.Join("、", st.Languages.Select(l => $"{l.Item2}（{l.Item1}）"));
            }
            catch (Exception e)
            {
                stateLabel.Text = "状態を確認できません: " + e.Message;
            }
        }

        public static string DescribeState(Patcher.Status st)
        {
            switch (st.State)
            {
                case Patcher.BundleState.Missing:
                    return "見つかりません（ゲームのフォルダーを確認してください）";
                case Patcher.BundleState.Original:
                    return "公式のまま（パッチ未適用）";
                case Patcher.BundleState.Reverted:
                    return "公式のデータに戻っています（「日本語化する」で適用し直せます）";
                case Patcher.BundleState.PatchedNoState:
                    return "日本語化パッチ適用済み（状態の記録なし）";
                case Patcher.BundleState.PatchedNoBackup:
                    return "日本語化パッチ適用済み（バックアップなし。Steam の整合性の確認で戻せます）";
                case Patcher.BundleState.Damaged:
                    return "壊れているか読み込めません（「元に戻す」か Steam の整合性の確認で直せます）";
                case Patcher.BundleState.DamagedNoBackup:
                    return "壊れているか読み込めません（Steam の整合性の確認で直せます）";
                case Patcher.BundleState.Unsupported:
                    return "このパッチが対応していない形式です（パッチの更新をお待ちください）";
                case Patcher.BundleState.Patched:
                    var s = st.PatchState;
                    return $"日本語化パッチ適用済み（{Get(s, "patch_version")}、翻訳 {(int.TryParse(Get(s, "applied"), out var n) ? n.ToString("N0") : "?")} 件、{Get(s, "time")}）";
                default:
                    return "ゲーム側で更新されています（「日本語化する」で適用し直してください）";
            }
        }

        static string Get(System.Collections.Generic.Dictionary<string, string> d, string key) =>
            d != null && d.TryGetValue(key, out var v) ? v : "?";

        void Log(string line)
        {
            if (InvokeRequired)
            {
                BeginInvoke(new Action<string>(Log), line);
                return;
            }
            logBox.AppendText(line + Environment.NewLine);
            Program.WriteLogFile(line);
        }

        void OnBrowse(object sender, EventArgs e)
        {
            using (var dlg = new FolderBrowserDialog { Description = "Two Point Campus のフォルダー（TPC.exe があるフォルダー）を選んでください" })
            {
                if (GameLocator.IsGameDir(gameDir))
                    dlg.SelectedPath = gameDir;
                if (dlg.ShowDialog(this) != DialogResult.OK)
                    return;
                if (!GameLocator.IsGameDir(dlg.SelectedPath))
                {
                    MessageBox.Show(this, "TPC.exe があるフォルダーを選んでください。", Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }
                gameDir = dlg.SelectedPath;
                RefreshStatus();
            }
        }

        async void OnApply(object sender, EventArgs e)
        {
            if (data == null)
            {
                MessageBox.Show(this, "翻訳データを読み込めません: " + dataError, Text, MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            bool switchLanguage = languageCheck.Checked;
            Log($"--- 日本語化（翻訳データ {data.PatchVersion}）");
            await Run(p =>
            {
                p.Apply(data);
                return switchLanguage
                    ? LanguageWarning(p.SetLanguage(Preferences.Japanese), "日本語データの書き換えは完了しましたが、表示言語を日本語に切り替えられませんでした。")
                    : null;
            }, "日本語化が完了しました。ゲームを起動してください。");
        }

        async void OnRestore(object sender, EventArgs e)
        {
            if (MessageBox.Show(this, "日本語化パッチを取り除いて、公式のデータに戻しますか？", Text,
                    MessageBoxButtons.OKCancel, MessageBoxIcon.Question) != DialogResult.OK)
                return;
            bool switchLanguage = languageCheck.Checked;
            Log("--- 元に戻す");
            await Run(p =>
            {
                p.Restore(data);
                return switchLanguage
                    ? LanguageWarning(p.RestoreLanguage(), "日本語データは元に戻しましたが、表示言語を戻せませんでした。")
                    : null;
            }, "元に戻しました。");
        }

        /// <summary>
        /// The dialog text for a file error: a failed replacement by SafeFile as it is (it names the file and the Windows
        /// reason, and says when another program may be using it); the advice about the game or Steam using the file only
        /// for a file in use; the reason always (S10).
        /// </summary>
        internal static string FileErrorText(Exception ex) =>
            SafeFile.IsReplaceError(ex) ? ex.Message
            : ex is UnauthorizedAccessException
                ? "ゲームのフォルダーのファイルにアクセスできませんでした。\nゲームと Steam のファイル操作が終わっているか確認し、うまくいかない場合はこのツールを右クリックして「管理者として実行」してください。\n\n" + ex.Message
            : ex is IOException io && SafeFile.IsInUse(io)
                ? "ファイルを読み書きできませんでした。ゲームや Steam がファイルを使っていないか確認してから、もう一度お試しください。\n\n" + ex.Message
            : "ファイルを読み書きできませんでした。\n\n" + ex.Message;

        static string LanguageWarning(Patcher.LanguageResult result, string summary) =>
            result.Failures.Count == 0 ? null : summary + "\n\n" + string.Join("\n", result.Failures);

        /// <summary>Runs the work off the UI thread; the action returns a warning to show instead of the done message.</summary>
        async Task Run(Func<Patcher, string> action, string doneMessage)
        {
            applyButton.Enabled = restoreButton.Enabled = browseButton.Enabled = languageCheck.Enabled = false;
            UseWaitCursor = true;
            busy = true;
            var patcher = new Patcher(gameDir, Log);
            try
            {
                string warning = await Task.Run(() =>
                {
                    using (Patcher.Lock()) // released on the same thread
                        return action(patcher);
                });
                busy = false;
                if (warning != null)
                    MessageBox.Show(this, warning, Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                else
                    MessageBox.Show(this, doneMessage, Text, MessageBoxButtons.OK, MessageBoxIcon.Information);
            }
            catch (PatchException ex)
            {
                busy = false;
                Log("エラー: " + ex.Message);
                MessageBox.Show(this, ex.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
            catch (InvalidDataException ex)
            {
                busy = false;
                Log("エラー: " + ex.Message);
                MessageBox.Show(this, ex.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
            catch (Exception ex) when (ex is UnauthorizedAccessException || ex is IOException)
            {
                busy = false;
                Log("エラー: " + ex.Message);
                MessageBox.Show(this, FileErrorText(ex), Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
            catch (Exception ex)
            {
                busy = false;
                Log("予期しないエラー: " + ex);
                MessageBox.Show(this, "予期しないエラーが発生しました。\n" + ex.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            finally
            {
                busy = false;
                UseWaitCursor = false;
                browseButton.Enabled = languageCheck.Enabled = true;
                RefreshStatus();
            }
        }
    }
}
