using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;

namespace TpcJpPatch.Tests
{
    /// <summary>
    /// MainForm layout at 100/125/150/200 % on an STA thread. The form is never shown: its handles are
    /// created while it is invisible and DrawToBitmap renders it. Scaling is emulated the way WinForms does it
    /// for a DPI-aware process (fonts in points grow by the factor, then Scale(factor) scales sizes,
    /// margins and paddings), because this test process itself runs at 96 DPI.
    /// </summary>
    static class LayoutTests
    {
        static readonly BindingFlags Any = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;

        sealed class Layout
        {
            public Form Form;
            public readonly List<Tuple<string, Control>> Items = new List<Tuple<string, Control>>();
            public TextBox LogBox;
            public Label Notes;
        }

        static TV F<TV>(Form f, string name) => (TV)typeof(MainForm).GetField(name, Any).GetValue(f);

        /// <summary>Location of a control in the form's client coordinates (no handles needed).</summary>
        static Rectangle Bounds(Control c, Form form)
        {
            var r = c.Bounds;
            for (var p = c.Parent; p != null && p != form; p = p.Parent)
                r.Offset(p.Location); // child bounds are relative to the parent's client area
            return r;
        }

        /// <summary>Scales every font that is set on a control itself (inherited fonts follow their parent).</summary>
        static void ScaleFonts(Control root, float f)
        {
            var own = new List<Control>();
            void Walk(Control c)
            {
                if (c.Parent == null || !ReferenceEquals(c.Font, c.Parent.Font))
                    own.Add(c);
                foreach (Control child in c.Controls)
                    Walk(child);
            }
            Walk(root);
            var fonts = own.Select(c => c.Font).ToList();
            for (int i = 0; i < own.Count; i++)
                own[i].Font = new Font(fonts[i].FontFamily, fonts[i].SizeInPoints * f, fonts[i].Style, GraphicsUnit.Point, fonts[i].GdiCharSet);
        }

        static void CreateHandles(Control c)
        {
            var h = c.Handle;
            foreach (Control child in c.Controls)
                CreateHandles(child);
        }

        /// <summary>A sandbox where every label gets a realistic long value: patched state, two accounts, long path.</summary>
        static string Sandbox(Ctx ctx, out string prefsRoot)
        {
            string deep = Path.Combine("layout", "SteamLibrary", "steamapps", "common", "Two Point Campus");
            var g = ctx.NewGame(deep, ctx.Official);
            Util.Retry(() => g.Patcher().Apply(ctx.Data));
            prefsRoot = ctx.Dir("layout_prefs");
            foreach (var kv in new Dictionary<string, int> { ["1000000000"] = 12, ["76561198000000000"] = 1 })
            {
                Directory.CreateDirectory(Path.Combine(prefsRoot, kv.Key));
                File.WriteAllBytes(Path.Combine(prefsRoot, kv.Key, "preferences.json"), Mp.Prefs(Mp.Int(kv.Value)));
            }
            return g.Dir;
        }

        static Layout Build(string gameDir, float f, Size? clientSize = null, Action<Form> customize = null)
        {
            var form = new MainForm(gameDir);
            typeof(MainForm).GetMethod("Initialize", Any).Invoke(form, null); // what the Shown event does
            customize?.Invoke(form);
            var log = F<TextBox>(form, "logBox");
            log.Text = ("--- 日本語化（翻訳データ 1.0.0）\r\n公式の日本語データにパッチを適用します\r\nバックアップを作成しました: localisation_assets_localisationjapanese.bundle.tpcjp-original\r\n" +
                           "日本語データを書き出しています…\r\n日本語データにパッチを適用しました（翻訳 2391 件）\r\n表示言語を英語から日本語に変更しました（1000000000）\r\n");
            if (f != 1f)
            {
                form.SuspendLayout();
                ScaleFonts(form, f);
                form.Scale(new SizeF(f, f));
                form.ResumeLayout(false);
            }
            if (clientSize != null)
                form.ClientSize = clientSize.Value;
            form.PerformLayout();
            var l = new Layout { Form = form, LogBox = log };
            var table = (TableLayoutPanel)form.Controls[0];
            foreach (Control c in table.Controls)
            {
                if (c is FlowLayoutPanel flow)
                {
                    foreach (Control b in flow.Controls)
                        l.Items.Add(Tuple.Create(b.GetType().Name + " " + b.Text, b));
                }
                else
                    l.Items.Add(Tuple.Create(c.GetType().Name + " " + Short(c.Text), c));
                if (c is Label lab && lab.Text.StartsWith("・", StringComparison.Ordinal))
                    l.Notes = lab;
            }
            return l;
        }

        static string Short(string s) => s.Length > 18 ? s.Substring(0, 18) + "…" : s;

        /// <summary>Layout problems of one rendering; empty when the layout is fine.</summary>
        static List<string> Problems(Layout l, float f, int logLines)
        {
            var form = l.Form;
            var client = new Rectangle(Point.Empty, form.ClientSize);
            var table = (TableLayoutPanel)form.Controls[0];
            var inner = new Rectangle(table.Padding.Left, table.Padding.Top,
                client.Width - table.Padding.Horizontal, client.Height - table.Padding.Vertical);
            var problems = new List<string>();
            var rects = l.Items.Select(t => Tuple.Create(t.Item1, t.Item2, Bounds(t.Item2, form))).ToList();
            for (int i = 0; i < rects.Count; i++)
            {
                var a = rects[i];
                if (!client.Contains(a.Item3))
                    problems.Add($"{a.Item1} {a.Item3} is cut off by the window (client {client.Size})");
                else if (!inner.Contains(a.Item3))
                    problems.Add($"{a.Item1} {a.Item3} reaches into the window's padding (inner {inner})");
                for (int j = i + 1; j < rects.Count; j++)
                    if (a.Item3.IntersectsWith(rects[j].Item3))
                        problems.Add($"{a.Item1} {a.Item3} overlaps {rects[j].Item1} {rects[j].Item3}");
                var c = a.Item2;
                if (c is Label || c is CheckBox || c is Button)
                {
                    var pref = c.GetPreferredSize(Size.Empty);
                    // a label may wrap its text when the table gives it less width: then all wrapped lines must fit
                    if (c is Label && c.Width < pref.Width)
                        pref = c.GetPreferredSize(new Size(c.Width, int.MaxValue));
                    if (c.Width < pref.Width || c.Height < pref.Height)
                        problems.Add($"{a.Item1}: text needs {pref}, has {c.Size}");
                }
            }
            int minLog = l.LogBox.Font.Height * logLines + 6;
            if (l.LogBox.Height < minLog)
                problems.Add($"log box only {l.LogBox.Height} px high (want >= {minLog}: {logLines} lines of {l.LogBox.Font.Height} px)");
            return problems;
        }

        static void Save(Layout l, string path)
        {
            CreateHandles(l.Form); // the form stays invisible: no window is shown
            using (var bmp = new Bitmap(l.Form.Width, l.Form.Height, PixelFormat.Format32bppArgb))
            {
                l.Form.DrawToBitmap(bmp, new Rectangle(Point.Empty, bmp.Size));
                bmp.Save(path, ImageFormat.Png);
            }
        }

        static void Sta(Action a)
        {
            Exception error = null;
            var t = new Thread(() =>
            {
                try
                {
                    a();
                }
                catch (Exception e)
                {
                    error = e;
                }
            });
            t.SetApartmentState(ApartmentState.STA);
            t.Start();
            t.Join();
            if (error is TargetInvocationException tie && tie.InnerException != null)
                error = tie.InnerException;
            if (error != null)
                throw error is AssertException ? error : new AssertException(error.GetType().Name + ": " + error.Message + "\n" + error.StackTrace);
        }

        public static void Run(Runner r, Ctx ctx)
        {
            string gameDir = null, prefsRoot = null;
            r.Test("Layout: setup (patched sandbox, two accounts, visual styles)", () =>
            {
                gameDir = Sandbox(ctx, out prefsRoot);
                try
                {
                    Application.EnableVisualStyles();
                    Application.SetCompatibleTextRenderingDefault(false);
                }
                catch (InvalidOperationException)
                {
                    // a window was created earlier in this process
                }
            });
            if (gameDir == null)
                return;
            string oldPrefs = Environment.GetEnvironmentVariable("TPCJP_PREFS_ROOT");
            Environment.SetEnvironmentVariable("TPCJP_PREFS_ROOT", prefsRoot);
            try
            {
                foreach (int pct in new[] { 100, 125, 150, 200 })
                {
                    float f = pct / 100f;
                    r.Test($"Layout: MainForm at {pct}% (default size): no overlap, nothing cut off, usable log box", () =>
                    {
                        List<string> problems = null;
                        Sta(() =>
                        {
                            var l = Build(gameDir, f);
                            using (l.Form)
                            {
                                Save(l, Path.Combine(ctx.ResultsDir, $"cstests_layout_{pct}.png"));
                                problems = Problems(l, f, 5);
                                var st = F<Label>(l.Form, "stateLabel").Text;
                                T.Contains(st, "適用済み", "state label filled (Initialize ran)");
                                r.Note($"client {l.Form.ClientSize}, font {l.Form.Font.Name} {l.Form.Font.SizeInPoints}pt, log box {l.LogBox.Size} ({(l.LogBox.Height - 6) / l.LogBox.Font.Height} lines), notes {l.Notes?.Bounds}");
                            }
                        });
                        T.True(problems.Count == 0, string.Join("\n", problems));
                    });
                }

                foreach (int pct in new[] { 100, 125, 150, 200 })
                {
                    float f = pct / 100f;
                    // The user may shrink the window to its minimum size; the notes then wrap and take height from the log box.
                    r.Test($"Layout: MainForm at {pct}% shrunk to its MinimumSize: no overlap, nothing cut off, log box >= 2 lines", () =>
                    {
                        List<string> problems = null;
                        Sta(() =>
                        {
                            var probe = Build(gameDir, f);
                            Size min = probe.Form.MinimumSize;
                            Size border = probe.Form.Size - probe.Form.ClientSize;
                            probe.Form.Dispose();
                            var l = Build(gameDir, f, min - border);
                            using (l.Form)
                            {
                                Save(l, Path.Combine(ctx.ResultsDir, $"cstests_layout_{pct}_min.png"));
                                problems = Problems(l, f, 2);
                                r.Note($"minimum size {min}, client {l.Form.ClientSize}, log box {l.LogBox.Size} ({(l.LogBox.Height - 6) / l.LogBox.Font.Height} lines), notes {l.Notes?.Bounds}");
                            }
                        });
                        T.True(problems.Count == 0, string.Join("\n", problems));
                    });
                }

                // State texts other than 'patched': I-2 'Reverted' (shortened by CS-08), the round-2 texts that
                // GetStatus(data) gives a bundle no state file describes (CS-07): 'PatchedNoState' and 'Damaged', and the
                // round-3 texts for a bundle without a (usable) backup (CS-09): 'PatchedNoBackup' (the longest state text)
                // and 'DamagedNoBackup'. The GUI passes its translation data to GetStatus, so the label shows them.
                var stateSandboxes = new List<Tuple<string, string, Patcher.BundleState, Action<Game>>>
                {
                    Tuple.Create<string, string, Patcher.BundleState, Action<Game>>("reverted", "公式のデータに戻っています（「日本語化する」で適用し直せます）",
                        Patcher.BundleState.Reverted, g => File.WriteAllBytes(g.Bundle, ctx.Official)),
                    Tuple.Create<string, string, Patcher.BundleState, Action<Game>>("nostate", "日本語化パッチ適用済み（状態の記録なし）",
                        Patcher.BundleState.PatchedNoState, g => File.Delete(g.State)),
                    Tuple.Create<string, string, Patcher.BundleState, Action<Game>>("damaged", "壊れているか読み込めません（「元に戻す」か Steam の整合性の確認で直せます）",
                        Patcher.BundleState.Damaged, g => File.WriteAllBytes(g.Bundle, ctx.Official.Take(ctx.Official.Length / 2).ToArray())),
                    Tuple.Create<string, string, Patcher.BundleState, Action<Game>>("nobackup", "日本語化パッチ適用済み（バックアップなし。Steam の整合性の確認で戻せます）",
                        Patcher.BundleState.PatchedNoBackup, g => { File.Delete(g.Backup); File.Delete(g.State); }),
                    Tuple.Create<string, string, Patcher.BundleState, Action<Game>>("damaged_nobackup", "壊れているか読み込めません（Steam の整合性の確認で直せます）",
                        Patcher.BundleState.DamagedNoBackup, g =>
                        {
                            File.Delete(g.Backup);
                            File.Delete(g.State);
                            File.WriteAllBytes(g.Bundle, ctx.Official.Take(ctx.Official.Length / 2).ToArray());
                        }),
                    // round 3c (S2/S8): a game update to a bundle format this patcher does not read (UnityFS 9)
                    Tuple.Create<string, string, Patcher.BundleState, Action<Game>>("unsupported", "このパッチが対応していない形式です（パッチの更新をお待ちください）",
                        Patcher.BundleState.Unsupported, g =>
                        {
                            byte[] v9 = (byte[])ctx.Official.Clone();
                            v9[11] = 9; // "UnityFS\0", then the format version as a big-endian uint32
                            g.Update(v9);
                        }),
                };
                foreach (var sb in stateSandboxes)
                {
                    string stateDir = null;
                    r.Test($"Layout: setup of a '{sb.Item1}' sandbox ({sb.Item3})", () =>
                    {
                        var g = ctx.NewGame(Path.Combine("layout_" + sb.Item1, "SteamLibrary", "steamapps", "common", "Two Point Campus"), ctx.Official);
                        Util.Retry(() => g.Patcher().Apply(ctx.Data));
                        sb.Item4(g);
                        T.Eq(sb.Item3, g.Patcher().GetStatus(ctx.Data).State, "status");
                        stateDir = g.Dir;
                    });
                    foreach (int pct in new[] { 100, 125, 150, 200 })
                    {
                        float f = pct / 100f;
                        foreach (bool min in new[] { false, true })
                        {
                            r.Test($"Layout: MainForm at {pct}%{(min ? " at its MinimumSize" : "")} with the '{sb.Item1}' state text: nothing cut off, log box >= {(min ? 2 : 5)} lines", () =>
                            {
                                if (stateDir == null)
                                    throw new SkipException("no sandbox");
                                List<string> problems = null;
                                Sta(() =>
                                {
                                    Size? client = null;
                                    if (min)
                                    {
                                        var probe = Build(stateDir, f);
                                        client = probe.Form.MinimumSize - (probe.Form.Size - probe.Form.ClientSize);
                                        probe.Form.Dispose();
                                    }
                                    var l = Build(stateDir, f, client);
                                    using (l.Form)
                                    {
                                        var st = F<Label>(l.Form, "stateLabel");
                                        T.Eq(sb.Item2, st.Text, "state label (MainForm passes its data to GetStatus)");
                                        Save(l, Path.Combine(ctx.ResultsDir, $"cstests_layout_{pct}{(min ? "_min" : "")}_{sb.Item1}.png"));
                                        problems = Problems(l, f, min ? 2 : 5);
                                        r.Note($"client {l.Form.ClientSize}, state label {st.Bounds} ({Math.Max(1, (int)Math.Round((double)st.Height / st.Font.Height))} lines), log box {l.LogBox.Size} ({(l.LogBox.Height - 6) / l.LogBox.Font.Height} lines)");
                                    }
                                });
                                T.True(problems.Count == 0, string.Join("\n", problems));
                            });
                        }
                    }
                }

                // Long values: an error text in the state label, many Steam accounts, a data error.
                foreach (int pct in new[] { 100, 200 })
                {
                    float f = pct / 100f;
                    r.Test($"Layout: MainForm at {pct}% with long status / language / data texts: they wrap, nothing cut off", () =>
                    {
                        List<string> problems = null;
                        Sta(() =>
                        {
                            var l = Build(gameDir, f, null, form =>
                            {
                                F<Label>(form, "stateLabel").Text = "状態を確認できません: ファイル 'C:\\Program Files (x86)\\Steam\\steamapps\\common\\Two Point Campus\\TPC_Data\\StreamingAssets\\aa\\StandaloneWindows64\\localisation_assets_localisationjapanese.bundle' は別のプロセスで使用されているため、プロセスはファイルにアクセスできません。";
                                F<Label>(form, "languageLabel").Text = "日本語（1000000000）、英語（76561198000000001）、フランス語（76561198000000002）、中国語（簡体字）（76561198000000003）、読み取れません（preferences.json が途中で切れています）（76561198000000004）";
                                // the longest data message after the fixes (CS-01 / CS-02)
                                F<Label>(form, "dataLabel").Text = "読み込めません: 翻訳データを UTF-8 として読めません（壊れているか、別の文字コードで保存されています）";
                            });
                            using (l.Form)
                            {
                                Save(l, Path.Combine(ctx.ResultsDir, $"cstests_layout_{pct}_long.png"));
                                problems = Problems(l, f, 3);
                                r.Note($"client {l.Form.ClientSize}, state label {F<Label>(l.Form, "stateLabel").Bounds}, log box {l.LogBox.Size} ({(l.LogBox.Height - 6) / l.LogBox.Font.Height} lines)");
                            }
                        });
                        T.True(problems.Count == 0, string.Join("\n", problems));
                    });
                }
            }
            finally
            {
                Environment.SetEnvironmentVariable("TPCJP_PREFS_ROOT", oldPrefs);
            }
        }
    }
}
