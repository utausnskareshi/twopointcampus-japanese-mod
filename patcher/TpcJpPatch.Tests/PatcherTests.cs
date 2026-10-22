using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace TpcJpPatch.Tests
{
    /// <summary>
    /// Patcher.Apply / Restore / GetStatus driven through the API on sandbox game folders with synthetic bundles:
    /// official, patched (with and without state), updated official (N identical / other Japanese / changed English),
    /// damaged. Expected results come from an independent model of the fill-only rules (Util.ExpectedPatched).
    /// </summary>
    static class PatcherTests
    {
        sealed class Model
        {
            public int Applied, SkippedOfficial, SkippedChanged, Missing;
            public Dictionary<string, string> Texts;
        }

        static Model Expect(byte[] bundle, PatchData data)
        {
            var texts = Util.Texts(bundle);
            var m = new Model { Texts = Util.ExpectedPatched(texts, data, out int applied) };
            var last = texts.ToDictionary(kv => kv.Key, kv =>
            {
                var d = new Dictionary<string, string>(StringComparer.Ordinal);
                foreach (var p in Util.ParseI2(kv.Value))
                    d[p.Key] = p.Value;
                return d;
            });
            foreach (var src in data.Sources)
            {
                if (!last.TryGetValue(src.Key, out var values))
                {
                    m.Missing += src.Value.Count;
                    continue;
                }
                foreach (var e in src.Value)
                {
                    if (!values.TryGetValue(e.Key, out var off))
                    {
                        m.Missing++;
                        continue;
                    }
                    if (e.RefSource != null && (!last.TryGetValue(e.RefSource, out var rv) || !rv.TryGetValue(e.RefKey, out var rj) || !Ctx.JapaneseRx.IsMatch(rj)))
                    {
                        m.Missing++;
                        continue;
                    }
                    if (PatchData.BaseHash(off) == e.Base)
                        m.Applied++;
                    else if (Ctx.JapaneseRx.IsMatch(off))
                        m.SkippedOfficial++;
                    else
                        m.SkippedChanged++;
                }
            }
            T.Eq(applied, m.Applied, "model self-check");
            return m;
        }

        static void Check(Patcher.ApplyResult res, Model m, string what)
        {
            T.Eq(m.Applied, res.Applied, what + ": applied");
            T.Eq(m.SkippedOfficial, res.SkippedOfficial, what + ": skipped (official)");
            T.Eq(m.SkippedChanged, res.SkippedChanged, what + ": skipped (changed English)");
            T.Eq(m.Missing, res.Missing, what + ": missing");
        }

        static Patcher.ApplyResult Apply(Game g, PatchData d) => Util.Retry(() => g.Patcher().Apply(d));
        static void Restore(Game g, PatchData d) => Util.Retry(() => g.Patcher().Restore(d));

        const string DamagedText = "壊れているか読み込めません（「元に戻す」か Steam の整合性の確認で直せます）";
        const string DamagedNoBackupText = "壊れているか読み込めません（Steam の整合性の確認で直せます）";
        const string PatchedNoBackupText = "日本語化パッチ適用済み（バックアップなし。Steam の整合性の確認で戻せます）";
        const string BackupMismatchText = "バックアップが記録と一致しません（壊れているか、別のファイルに置き換わっています）。バックアップは削除せずに残しました。";

        /// <summary>A state file as version 1.0.0 wrote it: no game_version.</summary>
        static void RemoveGameVersion(Game g) =>
            File.WriteAllLines(g.State, File.ReadAllLines(g.State).Where(l => !l.StartsWith("game_version=", StringComparison.Ordinal)));

        /// <summary>The call must refuse with a PatchException and leave every file next to the bundle as it was.</summary>
        static PatchException Refuses(Game g, Action a, string what)
        {
            var before = g.Snapshot();
            var e = T.Throws<PatchException>(() => Util.Retry(a), what);
            g.AssertUnchanged(before, what + " (nothing may change)");
            T.Eq(0, g.Leftovers().Length, what + ": temp files");
            return e;
        }

        /// <summary>Entries suitable for synthetic official updates: inline, Japanese, applicable, not a reference target.</summary>
        static List<Tuple<string, PatchData.Entry>> Candidates(Ctx ctx)
        {
            var texts = Util.Texts(ctx.Official);
            var refTargets = new HashSet<string>(ctx.Data.Sources.SelectMany(s => s.Value).Where(e => e.RefSource != null).Select(e => e.RefSource + "\n" + e.RefKey));
            var list = new List<Tuple<string, PatchData.Entry>>();
            foreach (var src in ctx.Data.Sources)
            {
                var values = new Dictionary<string, string>(StringComparer.Ordinal);
                foreach (var p in Util.ParseI2(texts[src.Key]))
                    values[p.Key] = p.Value;
                foreach (var e in src.Value)
                    if (e.Japanese != null && !e.IsNoOp && Ctx.JapaneseRx.IsMatch(e.Japanese) && !refTargets.Contains(src.Key + "\n" + e.Key)
                        && values.TryGetValue(e.Key, out var v) && PatchData.BaseHash(v) == e.Base)
                        list.Add(Tuple.Create(src.Key, e));
            }
            return list;
        }

        /// <summary>
        /// An "updated official" bundle: `identical` entries get exactly our translation, `otherJa` entries another
        /// Japanese text, `english` entries a changed English text. Entries are spread over all sources.
        /// </summary>
        static byte[] Updated(Ctx ctx, int identical, int otherJa, int english, int offset = 0)
        {
            var cand = Candidates(ctx);
            int total = identical + otherJa + english;
            T.True(total <= cand.Count, "not enough candidate entries");
            var changes = new Dictionary<string, Dictionary<string, string>>();
            for (int i = 0; i < total; i++)
            {
                var c = cand[(offset + (int)((long)i * cand.Count / total)) % cand.Count];
                string v = i < identical ? c.Item2.Japanese : i < identical + otherJa ? "公式の新しい訳" + i : "Updated English text " + i;
                if (!changes.TryGetValue(c.Item1, out var d))
                    changes[c.Item1] = d = new Dictionary<string, string>(StringComparer.Ordinal);
                d[c.Item2.Key] = v;
            }
            T.Eq(total, changes.Sum(kv => kv.Value.Count), "distinct changed entries");
            return Util.ModifyBundle(ctx.Official, changes);
        }

        public static void Run(Runner r, Ctx ctx)
        {
            var data = ctx.Data;
            byte[] official = ctx.Official;
            string officialSha = Util.Sha256(official);
            Model offModel = null;
            byte[] patched = null;

            r.Test("Patcher: fresh official bundle -> backup, state, fill-only output equal to the model", () =>
            {
                offModel = Expect(official, data);
                var g = ctx.NewGame("fresh", official);
                T.Eq(Patcher.BundleState.Original, g.Patcher().GetStatus().State, "status before");
                var res = Apply(g, data);
                Check(res, offModel, "apply");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), "backup = official");
                patched = File.ReadAllBytes(g.Bundle);
                Util.AssertTexts(offModel.Texts, Util.Texts(patched), "patched texts");
                var st = g.ReadState();
                T.Eq(officialSha, st["original_sha256"], "state original_sha256");
                T.Eq(Util.Sha256(patched), st["patched_sha256"], "state patched_sha256");
                T.Eq(res.Applied.ToString(), st["applied"], "state applied");
                T.Eq(data.PatchVersion, st["patch_version"], "state patch_version");
                T.Eq("10.3.169253+2024-12-06.1241", st["game_version"], "state game_version");
                T.Eq(Patcher.BundleState.Patched, g.Patcher().GetStatus().State, "status after");
                T.Contains(g.LogText, "バックアップを作成しました", "log");
                T.True(g.LogText.IndexOf("書き出しています", StringComparison.Ordinal) < g.LogText.IndexOf("バックアップを作成しました", StringComparison.Ordinal),
                    "the backup is written after the output was built and verified (FZ-09)");
                T.Eq(Util.ShippedPatchedSha, Util.Sha256(patched), "patched bundle hash for the shipped data (unchanged by the fixes)");
                T.Eq(0, g.Leftovers().Length, "temp files");
                r.Note($"applied {res.Applied}, skipped {res.SkippedOfficial}/{res.SkippedChanged}, missing {res.Missing}; patched sha {Util.Sha256(patched).Substring(0, 8)}");
            });

            if (patched == null)
            {
                r.Test("Patcher: remaining scenarios", () => throw new SkipException("the fresh apply failed"));
                return;
            }
            string patchedSha = Util.Sha256(patched);

            r.Test("Patcher: embedded data and --data file give the same output; output is deterministic", () =>
            {
                var g1 = ctx.NewGame("embedded", official);
                Apply(g1, PatchData.Load());
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g1.Bundle)), "embedded data output");
                using (var b = LocalisationBundle.Load(official, "m.bundle"))
                {
                    foreach (var kv in offModel.Texts)
                        if (kv.Value != b.GetText(kv.Key))
                            b.SetText(kv.Key, kv.Value);
                    r.Note("model-built bundle " + (Util.Sha256(b.Save()) == patchedSha ? "is byte-identical to" : "differs in bytes from") + " the patcher output");
                }
            });

            r.Test("Patcher: apply again (state describes the bundle) rebuilds from the backup, same bytes", () =>
            {
                var g = ctx.NewGame("reapply", official);
                Apply(g, data);
                var res = Apply(g, data);
                Check(res, offModel, "second apply");
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "same output");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), "backup unchanged");
                T.Contains(g.LogText, "作り直します", "log");
            });

            r.Test("Patcher: restore after apply gives the official bytes back and removes backup + state", () =>
            {
                var g = ctx.NewGame("restore", official);
                Apply(g, data);
                Restore(g, data);
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "official bytes");
                T.True(!File.Exists(g.Backup) && !File.Exists(g.State), "backup and state removed");
                T.Eq(Patcher.BundleState.Original, g.Patcher().GetStatus().State, "status");
                Restore(g, data); // second restore: nothing to do
                T.Contains(g.LogText, "パッチは適用されていません", "second restore log");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "still official");
            });

            r.Test("Patcher: restore without data (state only) and restore of a missing bundle", () =>
            {
                var g = ctx.NewGame("restore_nodata", official);
                Apply(g, data);
                Restore(g, null);
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "restored with state only");
                var g2 = ctx.NewGame("restore_missing", official);
                Apply(g2, data);
                File.Delete(g2.Bundle);
                T.Eq(Patcher.BundleState.Missing, g2.Patcher().GetStatus().State, "status missing");
                Restore(g2, data);
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g2.Bundle)), "missing bundle restored from backup");
                T.True(!File.Exists(g2.Backup), "backup removed");
            });

            r.Test("Patcher: Steam file verification after apply (bundle = backup) -> status Reverted, apply patches again / restore cleans up", () =>
            {
                var g = ctx.NewGame("verify", official);
                Apply(g, data);
                File.WriteAllBytes(g.Bundle, official);
                // I-2: backup and state are still there, so this is "back to official", not "never patched"
                var st = g.Patcher().GetStatus();
                T.Eq(Patcher.BundleState.Reverted, st.State, "status");
                T.Contains(MainForm.DescribeState(st), "公式のデータに戻っています", "status text");
                var res = Apply(g, data);
                Check(res, offModel, "apply after verify");
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "patched again");
                File.WriteAllBytes(g.Bundle, official);
                Restore(g, data);
                T.Contains(g.LogText, "公式のままです", "restore log");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "official kept");
                T.True(!File.Exists(g.Backup) && !File.Exists(g.State), "backup and state removed");
                T.Eq(Patcher.BundleState.Original, g.Patcher().GetStatus().State, "status after the clean-up");
            });

            r.Test("Patcher: status Original vs Reverted: a backup without a state file is Original; the recorded official file without a backup is Reverted", () =>
            {
                var g = ctx.NewGame("reverted_variants", official);
                Apply(g, data);
                File.WriteAllBytes(g.Bundle, official);
                File.Delete(g.State);
                T.Eq(Patcher.BundleState.Original, g.Patcher().GetStatus().State, "bundle = backup, no state");
                var g2 = ctx.NewGame("reverted_variants2", official);
                Apply(g2, data);
                File.WriteAllBytes(g2.Bundle, official);
                File.Delete(g2.Backup);
                // round 3: the bundle is the official file recorded in the state (original_sha256); apply backs it up again
                T.Eq(Patcher.BundleState.Reverted, g2.Patcher().GetStatus().State, "state but no backup, bundle = recorded official");
                T.Eq(Patcher.BundleState.Reverted, g2.Patcher().GetStatus(data).State, "the same with data");
                File.WriteAllBytes(g2.Bundle, Updated(ctx, 0, 0, 1, offset: 3));
                T.Eq(Patcher.BundleState.Unknown, g2.Patcher().GetStatus().State, "state but no backup, another valid bundle");
                var g3 = ctx.NewGame("reverted_variants3", official);
                Apply(g3, data);
                g3.Update(official); // reverted, and version.txt changed as well
                T.Eq(Patcher.BundleState.Reverted, g3.Patcher().GetStatus().State, "bundle = backup with state, other version.txt");
            });

            byte[] updated = Updated(ctx, 5, 7, 3);
            string updatedSha = Util.Sha256(updated);

            r.Test("Patcher: game update after apply (5 identical, 7 other official, 3 changed English) -> new backup, fill-only", () =>
            {
                var model = Expect(updated, data);
                T.Eq(offModel.Applied - 15, model.Applied, "model: 15 fewer applied");
                T.Eq(12, model.SkippedOfficial - offModel.SkippedOfficial, "model: 12 more official");
                T.Eq(3, model.SkippedChanged - offModel.SkippedChanged, "model: 3 more changed");
                var g = ctx.NewGame("update", official);
                Apply(g, data);
                g.Update(updated); // a real update also changes version.txt
                T.Eq(Patcher.BundleState.Unknown, g.Patcher().GetStatus().State, "status after the update");
                g.Log.Clear();
                var res = Apply(g, data);
                Check(res, model, "apply on the update");
                T.Contains(g.LogText, "日本語データが更新されています", "log");
                T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(g.Backup)), "backup = updated official");
                Util.AssertTexts(model.Texts, Util.Texts(File.ReadAllBytes(g.Bundle)), "patched texts");
                T.Eq(updatedSha, g.ReadState()["original_sha256"], "state original");
                T.Eq(Util.UpdatedVersion, g.ReadState()["game_version"], "state game_version = the new version");
                var log = g.LogText;
                T.True(log.IndexOf("書き出しています", StringComparison.Ordinal) < log.IndexOf("バックアップを作成しました", StringComparison.Ordinal),
                    "the new backup is written after the output was built and verified (FZ-09): " + T.Show(log));
                Restore(g, data);
                T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "restore gives the updated official");
            });

            r.Test("Patcher: game update after apply, then restore (no re-apply) keeps the newer official file", () =>
            {
                var g = ctx.NewGame("update_restore", official);
                Apply(g, data);
                g.Update(updated);
                Restore(g, data);
                T.Contains(g.LogText, "そのままにします", "log");
                T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "updated official kept");
                T.True(!File.Exists(g.Backup) && !File.Exists(g.State), "old backup and state removed");
            });

            r.Test("Patcher: same game version but a different bundle -> apply rebuilds from the backup, backup untouched (FZ-05/06)", () =>
            {
                // e.g. a file damaged in a way the format checks cannot see, or replaced by another tool
                foreach (var kv in new Dictionary<string, byte[]> { ["official-looking change"] = updated, ["one changed English text"] = Updated(ctx, 0, 0, 1, offset: 7) })
                {
                    var g = ctx.NewGame("samever_apply", official);
                    Apply(g, data);
                    File.WriteAllBytes(g.Bundle, kv.Value); // version.txt unchanged
                    var backupBefore = File.ReadAllBytes(g.Backup);
                    g.Log.Clear();
                    var res = Apply(g, data);
                    Check(res, offModel, kv.Key + ": result as for the official file");
                    T.Contains(g.LogText, "ゲームのバージョンは変わっていないため", kv.Key + ": log");
                    T.True(!g.LogText.Contains("バックアップを作成しました"), kv.Key + ": no new backup");
                    T.Bytes(backupBefore, File.ReadAllBytes(g.Backup), kv.Key + ": backup untouched");
                    T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), kv.Key + ": rebuilt from the backup");
                    T.Eq(officialSha, g.ReadState()["original_sha256"], kv.Key + ": state original");
                    T.Eq(Patcher.BundleState.Patched, g.Patcher().GetStatus().State, kv.Key + ": status");
                }
            });

            r.Test("Patcher: same game version but a different bundle -> restore restores the backup and cleans up (FZ-05/06)", () =>
            {
                foreach (bool withData in new[] { true, false })
                {
                    var g = ctx.NewGame("samever_restore", official);
                    Apply(g, data);
                    File.WriteAllBytes(g.Bundle, updated);
                    g.Log.Clear();
                    Restore(g, withData ? data : null);
                    string what = withData ? "with data" : "without data";
                    T.Contains(g.LogText, "ゲームのバージョンは変わっていないため、バックアップから戻します", what + ": log");
                    T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), what + ": official restored");
                    T.True(!File.Exists(g.Backup) && !File.Exists(g.State), what + ": backup and state removed");
                }
            });

            r.Test("Patcher: same version.txt but the state has no game_version (written by 1.0.0) -> a changed bundle is a new official file", () =>
            {
                var g = ctx.NewGame("samever_oldstate", official);
                Apply(g, data);
                File.WriteAllLines(g.State, File.ReadAllLines(g.State).Where(l => !l.StartsWith("game_version=", StringComparison.Ordinal)));
                File.WriteAllBytes(g.Bundle, updated);
                Apply(g, data);
                T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(g.Backup)), "new backup (the version rule needs a recorded game version)");
                var g2 = ctx.NewGame("samever_noversion", official);
                File.Delete(Path.Combine(g2.Dir, "version.txt"));
                Apply(g2, data);
                T.Eq(GameLocator.UnknownVersion, g2.ReadState()["game_version"], "unknown version recorded");
                File.WriteAllBytes(g2.Bundle, updated);
                Apply(g2, data);
                T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(g2.Backup)), "unknown version: a changed bundle is a new official file");
            });

            r.Test("Patcher: an update adding few identical translations (N = 1..3, 50, 500) is taken as official", () =>
            {
                foreach (int n in new[] { 1, 2, 3, 50, 500 })
                {
                    var u = Updated(ctx, n, 0, 0, offset: n);
                    var g = ctx.NewGame("ident_" + n, official);
                    Apply(g, data);
                    g.Update(u);
                    var res = Apply(g, data);
                    Check(res, Expect(u, data), $"N={n}");
                    T.Eq(Util.Sha256(u), Util.Sha256(File.ReadAllBytes(g.Backup)), $"N={n}: backup = the update");
                    var g2 = ctx.NewGame("ident_restore_" + n, official);
                    Apply(g2, data);
                    g2.Update(u);
                    Restore(g2, data);
                    T.Eq(Util.Sha256(u), Util.Sha256(File.ReadAllBytes(g2.Bundle)), $"N={n}: restore keeps the update");
                }
            });

            r.Test("Patcher: documents the classification threshold (identical translations > half of the comparable entries)", () =>
            {
                // Classify compares every inline, non-no-op entry; on the official bundle all of them count as "official"
                var offTexts = Util.Texts(official);
                int comparable = 0;
                foreach (var s in data.Sources)
                {
                    var last = new Dictionary<string, string>(StringComparer.Ordinal);
                    foreach (var p in Util.ParseI2(offTexts[s.Key]))
                        last[p.Key] = p.Value;
                    comparable += s.Value.Count(e => e.Japanese != null && !e.IsNoOp && last.TryGetValue(e.Key, out var v) && PatchData.BaseHash(v) == e.Base);
                }
                int nOfficial = comparable / 2, nPatched = comparable / 2 + 1;
                T.True(Candidates(ctx).Count >= nPatched, "enough candidates");
                r.Note($"{comparable} comparable entries: N={nOfficial} -> official, N={nPatched} -> our output");
                // version.txt changes too (a real update), so the content classification decides
                var below = Updated(ctx, nOfficial, 0, 0);
                var g = ctx.NewGame("threshold_below", official);
                Apply(g, data);
                g.Update(below);
                Apply(g, data);
                T.Eq(Util.Sha256(below), Util.Sha256(File.ReadAllBytes(g.Backup)), "N=half: new backup");
                var above = Updated(ctx, nPatched, 0, 0);
                var g2 = ctx.NewGame("threshold_above", official);
                Apply(g2, data);
                g2.Update(above);
                Apply(g2, data);
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g2.Backup)), "N>half: taken as our output, rebuilt from the old backup");
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g2.Bundle)), "N>half: output from the old backup");
            });

            r.Test("Patcher: patched bundle without state (older tool), backup present -> apply rebuilds, restore restores", () =>
            {
                var g = ctx.NewGame("nostate", official);
                Apply(g, data);
                File.Delete(g.State);
                T.Eq(Patcher.BundleState.Unknown, g.Patcher().GetStatus().State, "status");
                g.Log.Clear();
                Apply(g, data);
                T.Contains(g.LogText, "以前に適用したパッチを検出しました", "log");
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "same output");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), "backup kept");
                File.Delete(g.State);
                Restore(g, data);
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "restored");
            });

            r.Test("Patcher: patched bundle without state, restore without data -> refuses, backup kept", () =>
            {
                var g = ctx.NewGame("nostate_nodata", official);
                Apply(g, data);
                File.Delete(g.State);
                var e = Refuses(g, () => g.Patcher().Restore(null), "restore(null)");
                T.Contains(e.Message, "バックアップは削除せずに残しました", "message");
            });

            r.Test("Patcher: patched bundle without state and without backup -> apply and restore refuse", () =>
            {
                var g = ctx.NewGame("orphan", patched);
                T.Contains(Refuses(g, () => g.Patcher().Apply(data), "apply").Message, "バックアップが見つかりません", "apply message");
                T.Contains(Refuses(g, () => g.Patcher().Restore(data), "restore").Message, "バックアップが見つかりません", "restore message");
            });

            r.Test("Patcher: state describes the bundle but the backup is missing -> apply and restore refuse; status PatchedNoBackup (R2)", () =>
            {
                var g = ctx.NewGame("nobackup", official);
                Apply(g, data);
                File.Delete(g.Backup);
                Refuses(g, () => g.Patcher().Apply(data), "apply");
                Refuses(g, () => g.Patcher().Restore(data), "restore");
                Refuses(g, () => g.Patcher().Restore(null), "restore(null)");
                // round 3b (R2): the status does not show a plain 'patched' that restore cannot undo
                foreach (var d in new[] { data, null })
                {
                    var st = g.Patcher().GetStatus(d);
                    T.Eq(Patcher.BundleState.PatchedNoBackup, st.State, (d == null ? "without" : "with") + " data: status");
                    T.Eq(PatchedNoBackupText, MainForm.DescribeState(st), (d == null ? "without" : "with") + " data: text");
                }
            });

            r.Test("Patcher: state describes the bundle but the backup is not the recorded file (another valid / damaged / patched file) -> status PatchedNoBackup, apply and restore refuse; the Steam verification recovers (R2/R19)", () =>
            {
                var backups = new Dictionary<string, byte[]>
                {
                    ["another valid file"] = updated,
                    ["damaged"] = official.Take(official.Length / 2).ToArray(),
                    ["patched"] = patched,
                };
                foreach (var kv in backups)
                {
                    var g = ctx.NewGame("nobackup_swapped", official);
                    Apply(g, data);
                    File.WriteAllBytes(g.Backup, kv.Value);
                    foreach (var d in new[] { data, null })
                    {
                        var st = g.Patcher().GetStatus(d);
                        T.Eq(Patcher.BundleState.PatchedNoBackup, st.State, kv.Key + (d == null ? ", without" : ", with") + " data: status");
                        T.Eq(PatchedNoBackupText, MainForm.DescribeState(st), kv.Key + ": text");
                    }
                    // (a patched backup equals the installed bundle, so restore checks it as 'the official file' and names that)
                    string restoreText = kv.Key == "patched" ? "バックアップがパッチ適用後のデータになっています。バックアップは削除せずに残しました。" : BackupMismatchText;
                    T.Contains(Refuses(g, () => g.Patcher().Apply(data), kv.Key + ": apply").Message, BackupMismatchText, kv.Key + ": apply message");
                    T.Contains(Refuses(g, () => g.Patcher().Restore(data), kv.Key + ": restore").Message, restoreText, kv.Key + ": restore message");
                    if (kv.Key != "patched") // without data a backup equal to the bundle cannot be told from the official file (R30)
                        T.Contains(Refuses(g, () => g.Patcher().Restore(null), kv.Key + ": restore(null)").Message, BackupMismatchText, kv.Key + ": restore(null) message");
                    File.WriteAllBytes(g.Bundle, official); // "verify integrity of game files": the recorded official file
                    T.Eq(Patcher.BundleState.Reverted, g.Patcher().GetStatus(data).State, kv.Key + ": status after the verification");
                    Check(Apply(g, data), offModel, kv.Key + ": apply after the verification");
                    T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), kv.Key + ": backup repaired");
                    T.Eq(Patcher.BundleState.Patched, g.Patcher().GetStatus(data).State, kv.Key + ": status after the apply");
                }
            });

            byte[] damaged = official.Take(official.Length - 1000).ToArray();

            r.Test("Patcher: damaged game bundle, no backup -> apply and restore refuse with the Steam verification advice", () =>
            {
                var g = ctx.NewGame("damaged_nobackup", damaged);
                T.Contains(Refuses(g, () => g.Patcher().Apply(data), "apply").Message, "整合性を確認", "apply message");
                T.Contains(Refuses(g, () => g.Patcher().Restore(data), "restore").Message, "壊れていて", "restore message");
            });

            r.Test("Patcher: damaged game bundle with a good backup -> apply rebuilds, restore restores", () =>
            {
                var g = ctx.NewGame("damaged_backup", official);
                Apply(g, data);
                File.WriteAllBytes(g.Bundle, damaged);
                g.Log.Clear();
                Apply(g, data);
                T.Contains(g.LogText, "壊れているため", "apply log");
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "rebuilt");
                File.WriteAllBytes(g.Bundle, damaged);
                g.Log.Clear();
                Restore(g, data);
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "restored");
                // R20: the reason is logged once the backup passed its checks, right before the restore
                T.Eq("日本語データが壊れているため、バックアップから戻します\n日本語データを公式のものに戻しました", g.LogText, "restore log");
                foreach (var junk in new[] { new byte[0], new byte[official.Length], Encoding.ASCII.GetBytes("garbage") })
                {
                    var g2 = ctx.NewGame("damaged_backup_junk", official);
                    Apply(g2, data);
                    File.WriteAllBytes(g2.Bundle, junk);
                    Apply(g2, data);
                    T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g2.Bundle)), $"rebuilt from {junk.Length}-byte junk");
                }
            });

            r.Test("Patcher: damaged or replaced backup -> apply and restore refuse and keep everything", () =>
            {
                var g = ctx.NewGame("badbackup_state", official);
                Apply(g, data);
                File.WriteAllBytes(g.Backup, damaged);
                T.Contains(Refuses(g, () => g.Patcher().Apply(data), "apply, state current").Message, "記録と一致しません", "message");
                Refuses(g, () => g.Patcher().Restore(data), "restore, state current");

                var g2 = ctx.NewGame("badbackup_nostate", official);
                Apply(g2, data);
                File.Delete(g2.State);
                File.WriteAllBytes(g2.Backup, damaged);
                T.Contains(Refuses(g2, () => g2.Patcher().Apply(data), "apply, no state").Message, "バックアップが壊れています", "message");
                Refuses(g2, () => g2.Patcher().Restore(data), "restore, no state");

                var g3 = ctx.NewGame("backup_is_update", official);
                Apply(g3, data);
                File.WriteAllBytes(g3.Backup, updated); // a different official file than the one recorded
                T.Contains(Refuses(g3, () => g3.Patcher().Apply(data), "apply").Message, "記録と一致しません", "message");
                Refuses(g3, () => g3.Patcher().Restore(data), "restore");
            });

            r.Test("Patcher: backup that holds patched data -> apply and restore refuse", () =>
            {
                var g = ctx.NewGame("patched_backup", official);
                Apply(g, data);
                File.Delete(g.State);
                File.WriteAllBytes(g.Backup, patched); // bundle == backup == patched
                T.Contains(Refuses(g, () => g.Patcher().Apply(data), "apply").Message, "パッチ適用後のデータ", "apply message");
                T.Contains(Refuses(g, () => g.Patcher().Restore(data), "restore").Message, "パッチ適用後のデータ", "restore message");
                var g2 = ctx.NewGame("patched_backup2", official);
                Apply(g2, data);
                File.Delete(g2.State);
                File.WriteAllBytes(g2.Backup, patched);
                File.WriteAllBytes(g2.Bundle, official); // bundle official, backup patched
                Apply(g2, data); // the official bundle is a new official file: it replaces the patched backup
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g2.Backup)), "patched backup replaced by the official bundle");
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g2.Bundle)), "patched");
            });

            r.Test("Patcher: missing bundle -> apply refuses; status Missing", () =>
            {
                var g = ctx.NewGame("missing", null);
                T.Eq(Patcher.BundleState.Missing, g.Patcher().GetStatus().State, "status");
                T.Contains(Refuses(g, () => g.Patcher().Apply(data), "apply").Message, "見つかりません", "message");
                Restore(g, data); // nothing to restore: no error
                T.True(!File.Exists(g.Bundle), "nothing created");
            });

            r.Test("Patcher: read-only bundle / backup / state are handled", () =>
            {
                var g = ctx.NewGame("readonly", official);
                File.SetAttributes(g.Bundle, FileAttributes.ReadOnly);
                Apply(g, data);
                T.Contains(g.LogText, "読み取り専用属性を解除しました", "log");
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "patched");
                foreach (var f in new[] { g.Bundle, g.Backup, g.State })
                    File.SetAttributes(f, FileAttributes.ReadOnly);
                Apply(g, data);
                foreach (var f in new[] { g.Bundle, g.Backup, g.State })
                    File.SetAttributes(f, FileAttributes.ReadOnly);
                Restore(g, data);
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "restored");
                T.True(!File.Exists(g.Backup) && !File.Exists(g.State), "read-only backup and state removed");
            });

            r.Test("Patcher: bundle locked by another process -> apply fails with IOException, nothing half-written", () =>
            {
                var g = ctx.NewGame("locked", official);
                Exception caught;
                using (new FileStream(g.Bundle, FileMode.Open, FileAccess.Read, FileShare.Read))
                {
                    caught = T.Throws<Exception>(() => Apply(g, data), "apply on a locked bundle");
                }
                T.True(caught is IOException || caught is UnauthorizedAccessException || caught is PatchException,
                    "exception type " + caught.GetType().FullName + ": " + caught.Message);
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "bundle intact");
                T.Eq(0, g.Leftovers().Length, "temp files");
                T.True(!File.Exists(g.State), "no state");
                // the backup is written after verification, just before the bundle: if it is there it is the official file
                T.True(!File.Exists(g.Backup) || Util.Sha256(File.ReadAllBytes(g.Backup)) == officialSha, "a backup left by the failed run is the official file");
                T.Eq(Patcher.BundleState.Original, g.Patcher().GetStatus().State, "status after the failed run");
                r.Note(caught.GetType().Name + ": " + caught.Message + (File.Exists(g.Backup) ? " (backup of the official file kept)" : " (no backup)"));
                Apply(g, data);
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "apply works once the lock is gone");
            });

            r.Test("Patcher: data for another game version logs the notice; data that applies nothing refuses", () =>
            {
                string dir = ctx.Dir("otherdata");
                string text = File.ReadAllText(ctx.DataPath, Encoding.UTF8);
                string other = Path.Combine(dir, "other.tsv");
                File.WriteAllText(other, text.Replace("#bundle_sha256\t" + officialSha, "#bundle_sha256\t" + new string('1', 64))
                    .Replace("#game_version\t10.3.169253+2024-12-06.1241", "#game_version\t9.9"), new UTF8Encoding(false));
                var g = ctx.NewGame("otherdata", official);
                Apply(g, PatchData.Load(other));
                T.Contains(g.LogText, "別のバージョンのゲーム（9.9）", "notice");
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "same output");
                // round 3 (fuzz repro A): version.txt is the data's game version, only the official file differs
                string sameVersion = Path.Combine(dir, "same_version.tsv");
                File.WriteAllText(sameVersion, text.Replace("#bundle_sha256\t" + officialSha, "#bundle_sha256\t" + new string('1', 64)), new UTF8Encoding(false));
                var gs = ctx.NewGame("otherdata_sameversion", official);
                Apply(gs, PatchData.Load(sameVersion));
                T.Contains(gs.LogText, "注意: 公式の日本語データが、翻訳データを作ったときのものと異なります。公式訳が既にある項目は上書きしません", "notice (same version)");
                T.True(!gs.LogText.Contains("別のバージョンのゲーム"), "no 'another game version' notice for the same version: " + T.Show(gs.LogText));
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(gs.Bundle)), "same output (same version)");
                var gn = ctx.NewGame("otherdata_nonotice", official);
                Apply(gn, data);
                T.True(!gn.LogText.Contains("翻訳データを作ったとき") && !gn.LogText.Contains("別のバージョンのゲーム"),
                    "no notice for the data's own official file: " + T.Show(gn.LogText));

                var g2 = ctx.NewGame("nothing_applies", official);
                var fuzz = PatchData.Load(Path.Combine(ctx.VectorsDir, "escape_patch.tsv")); // source I2LS_Fuzz does not exist
                T.Contains(T.Throws<PatchException>(() => Apply(g2, fuzz), "nothing to apply").Message, "適用できる翻訳がありませんでした", "message");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g2.Bundle)), "bundle unchanged");
                T.True(!File.Exists(g2.State), "no state written");
                // FZ-09 (changed on purpose): the backup is written only after the output was built and verified
                T.True(!File.Exists(g2.Backup), "the failed apply created no backup");
                T.Eq(0, g2.Leftovers().Length, "temp files");
                T.Eq(Patcher.BundleState.Original, g2.Patcher().GetStatus().State, "status still official");
                Restore(g2, data);
                T.Contains(g2.LogText, "パッチは適用されていません", "restore: nothing to do");
            });

            r.Test("Patcher: a failed apply on a game update keeps the old backup and state; no new backup (FZ-09)", () =>
            {
                var g = ctx.NewGame("failed_update", official);
                Apply(g, data);
                g.Update(updated);
                var fuzz = PatchData.Load(Path.Combine(ctx.VectorsDir, "escape_patch.tsv"));
                T.Contains(Refuses(g, () => g.Patcher().Apply(fuzz), "apply that applies nothing").Message, "適用できる翻訳がありませんでした", "message");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), "old backup kept");
                // the next good run takes the update as the new official file
                g.Log.Clear();
                Apply(g, data);
                T.Contains(g.LogText, "日本語データが更新されています", "log of the next apply");
                T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(g.Backup)), "backup = the update after the good run");
            });

            r.Test("Patcher: the state file cannot be written -> warning, the apply still completes (FZ-10)", () =>
            {
                var g = ctx.NewGame("state_fail", official);
                Directory.CreateDirectory(g.State + SafeFile.TempSuffix); // a folder where SafeFile writes the new state
                Check(Apply(g, data), offModel, "result");
                T.Contains(g.LogText, "注意: 状態ファイルを書き込めませんでした（日本語データの書き換えは完了しています）", "warning");
                T.Contains(g.LogText, "日本語データにパッチを適用しました", "completion message");
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "bundle patched");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), "backup written");
                T.True(!File.Exists(g.State), "no state file");
                var st = g.Patcher().GetStatus();
                r.Note("status right after: " + st.State + " / " + MainForm.DescribeState(st));
                // without the state the patched bundle is recognised by its content
                g.Log.Clear();
                Apply(g, data);
                T.Contains(g.LogText, "以前に適用したパッチを検出しました", "re-apply recognises the output");
                Directory.Delete(g.State + SafeFile.TempSuffix);
                Restore(g, data);
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "restore works");
                T.True(!File.Exists(g.Backup), "backup removed");

                // an update applied while the state cannot be written leaves the old state: later runs still work
                var g2 = ctx.NewGame("state_fail_update", official);
                Apply(g2, data);
                g2.Update(updated);
                var oldState = File.ReadAllBytes(g2.State);
                Directory.CreateDirectory(g2.State + SafeFile.TempSuffix);
                Apply(g2, data);
                T.Contains(g2.LogText, "状態ファイルを書き込めませんでした", "warning (update)");
                T.Bytes(oldState, File.ReadAllBytes(g2.State), "the old state is kept");
                T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(g2.Backup)), "new backup written");
                Directory.Delete(g2.State + SafeFile.TempSuffix);
                g2.Log.Clear();
                Apply(g2, data);
                T.Contains(g2.LogText, "以前に適用したパッチを検出しました", "re-apply with the stale state");
                T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(g2.Backup)), "backup still the update");
                T.Eq(Patcher.BundleState.Patched, g2.Patcher().GetStatus().State, "status after a good run");
                Restore(g2, null);
                T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(g2.Bundle)), "restore gives the update back");
            });

            r.Test("Patcher: right after an apply whose state file could not be written, GetStatus(data) shows the patch as applied (CS-07)", () =>
            {
                // FZ-10 makes the apply succeed with a warning; GetStatus(data) (GUI label, CLI status) classifies the
                // bundle by its content when no state file describes it.
                var g = ctx.NewGame("state_fail_status", official);
                Directory.CreateDirectory(g.State + SafeFile.TempSuffix);
                Apply(g, data);
                var before = g.Snapshot();
                var st = g.Patcher().GetStatus(data);
                g.AssertUnchanged(before, "GetStatus(data)");
                T.Eq(Patcher.BundleState.PatchedNoState, st.State, "status with data");
                T.Eq("日本語化パッチ適用済み（状態の記録なし）", MainForm.DescribeState(st), "status text");
                T.Eq(Patcher.BundleState.Unknown, g.Patcher().GetStatus().State, "without data the old rule applies (Unknown)");
                // re-applying while the state is still unwritable: same warning, same status
                g.Log.Clear();
                Apply(g, data);
                T.Contains(g.LogText, "以前に適用したパッチを検出しました", "re-apply recognises the output");
                T.Eq(Patcher.BundleState.PatchedNoState, g.Patcher().GetStatus(data).State, "status after the re-apply");
                // once the state can be written again, the next apply records it
                Directory.Delete(g.State + SafeFile.TempSuffix);
                Apply(g, data);
                T.Eq(Patcher.BundleState.Patched, g.Patcher().GetStatus(data).State, "status after a good apply");

                // pytests-E2E1: an update applied while the state cannot be written keeps the stale state of the old output
                var g2 = ctx.NewGame("state_fail_status_update", official);
                Apply(g2, data);
                g2.Update(updated);
                T.Eq(Patcher.BundleState.Unknown, g2.Patcher().GetStatus(data).State, "a real update is still Unknown with data");
                T.Contains(MainForm.DescribeState(g2.Patcher().GetStatus(data)), "ゲーム側で更新されています", "update text");
                Directory.CreateDirectory(g2.State + SafeFile.TempSuffix);
                Apply(g2, data);
                T.Contains(g2.LogText, "状態ファイルを書き込めませんでした", "warning (update)");
                T.Eq(Patcher.BundleState.PatchedNoState, g2.Patcher().GetStatus(data).State, "stale state + our output of the update");
                Directory.Delete(g2.State + SafeFile.TempSuffix);
            });

            r.Test("Patcher: references copy the official text from the game data; unusable references are skipped", () =>
            {
                var refs = data.Sources.SelectMany(s => s.Value.Where(e => e.RefSource != null).Select(e => Tuple.Create(s.Key, e))).ToList();
                T.True(refs.Count > 0, "data has references");
                var first = refs[0];
                var texts = Util.Texts(official);
                var refValues = Util.ParseI2(texts[first.Item2.RefSource]).Where(p => p.Key == first.Item2.RefKey).ToList();
                string official1 = refValues.Last().Value;
                // in the patched output the referencing term holds the referenced official text
                var patchedValue = Util.ParseI2(Util.Texts(patched)[first.Item1]).Last(p => p.Key == first.Item2.Key).Value;
                T.Eq(official1, patchedValue, "reference resolved");
                // make the referenced official value English: the reference is skipped (counted as missing)
                var u = Util.ModifyBundle(official, new Dictionary<string, Dictionary<string, string>>
                {
                    [first.Item2.RefSource] = new Dictionary<string, string> { [first.Item2.RefKey] = "English only" },
                });
                var model = Expect(u, data);
                T.Eq(offModel.Missing + 1, model.Missing, "model: one more missing");
                var g = ctx.NewGame("refs", u);
                var res = Apply(g, data);
                Check(res, model, "apply");
                Util.AssertTexts(model.Texts, Util.Texts(File.ReadAllBytes(g.Bundle)), "texts");
            });

            r.Test("Patcher: GetStatus never changes files and reports a garbage state file as Unknown", () =>
            {
                var g = ctx.NewGame("status", official);
                Apply(g, data);
                File.WriteAllBytes(g.State, new byte[] { 0xFF, 0xFE, 0x00, 0x3D, 0x0A, 0x80 });
                var before = g.Snapshot();
                var st = g.Patcher().GetStatus();
                g.AssertUnchanged(before, "status");
                T.Eq(Patcher.BundleState.Unknown, st.State, "garbage state");
                Apply(g, data); // falls back to content classification
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "apply after a garbage state");
                T.Eq(Patcher.BundleState.Patched, g.Patcher().GetStatus().State, "state rewritten");
            });

            // ---- round 2: GetStatus(data) classifies by content (CS-07 / F-SBP-6 / pytests-E2E1) ----

            r.Test("Patcher: GetStatus(data) classifies a bundle no state file describes: our output -> PatchedNoState, damaged -> Damaged, update -> Unknown", () =>
            {
                var cases = new List<Tuple<string, Action<Game>, Patcher.BundleState, Patcher.BundleState>>
                {
                    // name, change after a normal apply, status with data, status without data
                    Tuple.Create<string, Action<Game>, Patcher.BundleState, Patcher.BundleState>("state file deleted", g => File.Delete(g.State),
                        Patcher.BundleState.PatchedNoState, Patcher.BundleState.Unknown),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState, Patcher.BundleState>("garbage state file", g => File.WriteAllBytes(g.State, new byte[] { 0xFF, 0x00, 0x3D }),
                        Patcher.BundleState.PatchedNoState, Patcher.BundleState.Unknown),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState, Patcher.BundleState>("state of another output", g => File.WriteAllText(g.State, "patched_sha256=" + new string('0', 64) + "\n"),
                        Patcher.BundleState.PatchedNoState, Patcher.BundleState.Unknown),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState, Patcher.BundleState>("half bundle (damaged)", g => File.WriteAllBytes(g.Bundle, official.Take(official.Length / 2).ToArray()),
                        Patcher.BundleState.Damaged, Patcher.BundleState.Unknown),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState, Patcher.BundleState>("zero-byte bundle", g => File.WriteAllBytes(g.Bundle, new byte[0]),
                        Patcher.BundleState.Damaged, Patcher.BundleState.Unknown),
                    // round 3c (S1): without a state file nothing vouches for the backup's game version, so restore refuses
                    Tuple.Create<string, Action<Game>, Patcher.BundleState, Patcher.BundleState>("damaged and state deleted", g => { File.Delete(g.State); File.WriteAllBytes(g.Bundle, new byte[1000]); },
                        Patcher.BundleState.DamagedNoBackup, Patcher.BundleState.Unknown),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState, Patcher.BundleState>("game update (other version)", g => g.Update(updated),
                        Patcher.BundleState.Unknown, Patcher.BundleState.Unknown),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState, Patcher.BundleState>("game update, state deleted", g => { g.Update(updated); File.Delete(g.State); },
                        Patcher.BundleState.Unknown, Patcher.BundleState.Unknown),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState, Patcher.BundleState>("Steam verify (= backup)", g => File.WriteAllBytes(g.Bundle, official),
                        Patcher.BundleState.Reverted, Patcher.BundleState.Reverted),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState, Patcher.BundleState>("bundle deleted", g => File.Delete(g.Bundle),
                        Patcher.BundleState.Missing, Patcher.BundleState.Missing),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState, Patcher.BundleState>("nothing changed", g => { },
                        Patcher.BundleState.Patched, Patcher.BundleState.Patched),
                };
                var texts = new Dictionary<Patcher.BundleState, string>
                {
                    [Patcher.BundleState.PatchedNoState] = "日本語化パッチ適用済み（状態の記録なし）",
                    [Patcher.BundleState.Damaged] = "壊れているか読み込めません（「元に戻す」か Steam の整合性の確認で直せます）",
                    [Patcher.BundleState.DamagedNoBackup] = DamagedNoBackupText,
                    [Patcher.BundleState.Unknown] = "ゲーム側で更新されています（「日本語化する」で適用し直してください）",
                    [Patcher.BundleState.Reverted] = "公式のデータに戻っています（「日本語化する」で適用し直せます）",
                    [Patcher.BundleState.Missing] = "見つかりません（ゲームのフォルダーを確認してください）",
                };
                int i = 0;
                foreach (var c in cases)
                {
                    var g = ctx.NewGame("status_data_" + i++, official);
                    Apply(g, data);
                    c.Item2(g);
                    var before = g.Snapshot();
                    var st = g.Patcher().GetStatus(data);
                    g.AssertUnchanged(before, c.Item1 + ": GetStatus(data)");
                    T.Eq(c.Item3, st.State, c.Item1 + ": with data");
                    T.Eq(c.Item4, g.Patcher().GetStatus().State, c.Item1 + ": without data");
                    if (texts.TryGetValue(st.State, out var text))
                        T.Eq(text, MainForm.DescribeState(st), c.Item1 + ": text");
                    // data that has nothing to do with this bundle never makes it 'patched'
                    var fuzz = PatchData.Load(Path.Combine(ctx.VectorsDir, "escape_patch.tsv"));
                    var sf = g.Patcher().GetStatus(fuzz).State;
                    T.True(sf != Patcher.BundleState.PatchedNoState, c.Item1 + ": unrelated data gives " + sf);
                }
            });

            r.Test("Patcher: GetStatus(data) without a backup and without a state file still reads the bundle: patched orphan -> PatchedNoBackup, damaged -> DamagedNoBackup, official -> Original (CS-09)", () =>
            {
                // round 3: with neither backup nor state GetStatus(data) classifies the bundle too; GetStatus() without
                // data still returns Original without reading it. Apply / restore agree with the status.
                var orphan = ctx.NewGame("status_orphan", patched);
                var before = orphan.Snapshot();
                var so = orphan.Patcher().GetStatus(data);
                orphan.AssertUnchanged(before, "GetStatus(data)");
                T.Eq(Patcher.BundleState.PatchedNoBackup, so.State, "patched orphan with data");
                T.Eq(PatchedNoBackupText, MainForm.DescribeState(so), "patched orphan text");
                T.Eq(Patcher.BundleState.Original, orphan.Patcher().GetStatus().State, "patched orphan without data (nothing read)");
                T.Contains(Refuses(orphan, () => orphan.Patcher().Apply(data), "apply on the orphan").Message,
                    "日本語データは以前のパッチで書き換えられていますが、バックアップが見つかりません", "apply message");
                T.Contains(Refuses(orphan, () => orphan.Patcher().Restore(data), "restore on the orphan").Message,
                    "バックアップが見つかりません", "restore message");

                foreach (var kv in new Dictionary<string, byte[]> { ["half"] = official.Take(official.Length / 2).ToArray(), ["empty"] = new byte[0], ["garbage"] = Encoding.ASCII.GetBytes("garbage") })
                {
                    var dmg = ctx.NewGame("status_damaged_nobackup", kv.Value);
                    var sd = dmg.Patcher().GetStatus(data);
                    T.Eq(Patcher.BundleState.DamagedNoBackup, sd.State, kv.Key + ": damaged without backup/state, with data");
                    T.Eq(DamagedNoBackupText, MainForm.DescribeState(sd), kv.Key + ": text (Steam verification only)");
                    T.True(!MainForm.DescribeState(sd).Contains("元に戻す"), kv.Key + ": restore is not offered");
                    T.Eq(Patcher.BundleState.Original, dmg.Patcher().GetStatus().State, kv.Key + ": without data (nothing read)");
                    T.Contains(Refuses(dmg, () => dmg.Patcher().Apply(data), kv.Key + ": apply").Message, "日本語データが壊れています", kv.Key + ": apply message");
                    T.Contains(Refuses(dmg, () => dmg.Patcher().Restore(data), kv.Key + ": restore").Message, "壊れていて、バックアップもありません", kv.Key + ": restore message");
                }

                var off = ctx.NewGame("status_official_nobackup", official);
                T.Eq(Patcher.BundleState.Original, off.Patcher().GetStatus(data).State, "official without backup/state, with data");
                T.Eq(Patcher.BundleState.Original, off.Patcher().GetStatus().State, "official without data");
                var fuzz = PatchData.Load(Path.Combine(ctx.VectorsDir, "escape_patch.tsv"));
                T.Eq(Patcher.BundleState.Original, orphan.Patcher().GetStatus(fuzz).State, "patched orphan with unrelated data: not ours by content");
            });

            r.Test("Patcher: GetStatus(data) offers 'restore' for a damaged bundle only when restore could use the backup; our output without a backup is PatchedNoBackup", () =>
            {
                byte[] half = official.Take(official.Length / 2).ToArray();
                var cases = new List<Tuple<string, Action<Game>, Patcher.BundleState>>
                {
                    Tuple.Create<string, Action<Game>, Patcher.BundleState>("good backup and state", g => { }, Patcher.BundleState.Damaged),
                    // round 3c (S1): neither a missing state nor one without game_version vouches for the backup's version
                    Tuple.Create<string, Action<Game>, Patcher.BundleState>("good backup, no state", g => File.Delete(g.State), Patcher.BundleState.DamagedNoBackup),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState>("state of version 1.0.0 (no game_version)", g => RemoveGameVersion(g), Patcher.BundleState.DamagedNoBackup),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState>("state, no backup", g => File.Delete(g.Backup), Patcher.BundleState.DamagedNoBackup),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState>("backup of another game version", g => g.SetVersion(Util.UpdatedVersion), Patcher.BundleState.DamagedNoBackup),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState>("no version.txt", g => File.Delete(Path.Combine(g.Dir, "version.txt")), Patcher.BundleState.DamagedNoBackup),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState>("swapped backup (valid, not recorded)", g => File.WriteAllBytes(g.Backup, updated), Patcher.BundleState.DamagedNoBackup),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState>("damaged backup", g => File.WriteAllBytes(g.Backup, official.Take(1000).ToArray()), Patcher.BundleState.DamagedNoBackup),
                    Tuple.Create<string, Action<Game>, Patcher.BundleState>("patched backup, no state", g => { File.Delete(g.State); File.WriteAllBytes(g.Backup, patched); }, Patcher.BundleState.DamagedNoBackup),
                };
                int i = 0;
                foreach (var c in cases)
                {
                    var g = ctx.NewGame("status_damaged_backup_" + i++, official);
                    Apply(g, data);
                    File.WriteAllBytes(g.Bundle, half);
                    c.Item2(g);
                    var before = g.Snapshot();
                    var st = g.Patcher().GetStatus(data);
                    g.AssertUnchanged(before, c.Item1 + ": GetStatus(data)");
                    T.Eq(c.Item3, st.State, c.Item1 + ": status");
                    T.Eq(c.Item3 == Patcher.BundleState.Damaged ? DamagedText : DamagedNoBackupText, MainForm.DescribeState(st), c.Item1 + ": text");
                    // the status tells the truth: restore works exactly when it offers restore
                    if (c.Item3 == Patcher.BundleState.Damaged)
                    {
                        Restore(g, data);
                        T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), c.Item1 + ": restore restored the official file");
                    }
                    else
                        Refuses(g, () => g.Patcher().Restore(data), c.Item1 + ": restore");
                }

                var g2 = ctx.NewGame("status_patched_state_nobackup", official);
                Apply(g2, data);
                File.Delete(g2.Backup);
                File.WriteAllText(g2.State, "patched_sha256=" + new string('0', 64) + "\n"); // a state that does not describe it
                var s2 = g2.Patcher().GetStatus(data);
                T.Eq(Patcher.BundleState.PatchedNoBackup, s2.State, "our output, a state, no backup");
                T.Eq(Patcher.BundleState.Unknown, g2.Patcher().GetStatus().State, "the same without data");
                Refuses(g2, () => g2.Patcher().Restore(data), "restore of our output without a backup");
            });

            // ---- round 2: F-SBP-7 (interrupted replacement) ----

            r.Test("Patcher: apply with the bundle missing but a good backup rebuilds the bundle from the backup (F-SBP-7); without a state file it refuses until a Steam verification (S1)", () =>
            {
                foreach (bool withState in new[] { true, false })
                {
                    var g = ctx.NewGame("missing_rebuild_" + withState, official);
                    Apply(g, data);
                    File.Delete(g.Bundle);
                    if (!withState)
                        File.Delete(g.State); // 1.0.0 killed inside the first apply: backup written, no state yet
                    T.Eq(Patcher.BundleState.Missing, g.Patcher().GetStatus(data).State, "status before");
                    g.Log.Clear();
                    if (!withState)
                    {
                        // round 3c (S1): nothing says which game version the backup is for
                        var e = Refuses(g, () => g.Patcher().Apply(data), "no state: apply");
                        T.Contains(e.Message, $"バックアップがどのバージョンのゲームのものか確認できません（記録: 不明、現在のゲームのバージョン: {Game.DefaultVersion}）。バックアップは削除せずに残しました。", "no state: message");
                        T.Contains(e.Message, "ゲームファイルの整合性を確認", "no state: advice");
                        T.Eq("", g.LogText, "no state: nothing announced");
                        File.WriteAllBytes(g.Bundle, official); // "verify integrity of game files"
                        Check(Apply(g, data), offModel, "no state: apply after the verification");
                        T.Contains(g.LogText, "公式の日本語データにパッチを適用します", "no state: the installed file is the backup");
                        T.Eq(Game.DefaultVersion, g.ReadState()["game_version"], "no state: the identified version is recorded");
                        T.Eq(Patcher.BundleState.Patched, g.Patcher().GetStatus(data).State, "no state: status after");
                        continue;
                    }
                    var res = Apply(g, data);
                    Check(res, offModel, "rebuild");
                    T.Contains(g.LogText, "日本語データが見つからないため、バックアップした公式データから作り直します", "log");
                    T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "bundle rebuilt");
                    T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), "backup unchanged");
                    T.Eq(patchedSha, g.ReadState()["patched_sha256"], "state describes the new bundle");
                    T.Eq(Game.DefaultVersion, g.ReadState()["game_version"], "game_version kept");
                    T.Eq(Patcher.BundleState.Patched, g.Patcher().GetStatus(data).State, "status after");
                    T.Eq(0, g.Leftovers().Length, "temp files");
                }
            });

            r.Test("Patcher: apply with the bundle missing refuses without a usable backup (none / damaged / patched), nothing changed", () =>
            {
                var g = ctx.NewGame("missing_nobackup", null);
                var e = Refuses(g, () => g.Patcher().Apply(data), "no bundle, no backup");
                T.Contains(e.Message, "日本語データ（localisation_assets_localisationjapanese.bundle）が見つかりません", "message");
                T.Contains(e.Message, "ゲームファイルの整合性を確認", "advice");

                var g2 = ctx.NewGame("missing_badbackup", official);
                Apply(g2, data);
                File.Delete(g2.Bundle);
                File.WriteAllBytes(g2.Backup, official.Take(1000).ToArray());
                T.Contains(Refuses(g2, () => g2.Patcher().Apply(data), "damaged backup").Message, "バックアップが壊れています", "damaged backup message");

                var g3 = ctx.NewGame("missing_patchedbackup", official);
                Apply(g3, data);
                File.Delete(g3.Bundle);
                File.WriteAllBytes(g3.Backup, patched);
                T.Contains(Refuses(g3, () => g3.Patcher().Apply(data), "patched backup").Message, "バックアップがパッチ適用後のデータになっています", "patched backup message");
                T.True(!File.Exists(g3.Bundle), "no bundle was made up");
            });

            // ---- round 3: only the state file vouches for the backup (CS-10 / F-SBP-8, fuzz backup_hash_not_checked) ----

            // Apply or Restore must refuse, change nothing and give the Steam verification advice.
            PatchException RefusesOp(Game g, string op, string what)
            {
                var e = Refuses(g, () => { if (op == "apply") g.Patcher().Apply(data); else g.Patcher().Restore(data); }, what + ": " + op);
                T.Contains(e.Message, "ゲームファイルの整合性を確認", what + ": " + op + ": advice");
                return e;
            }

            void Lose(Game g, string how, byte[] bundle)
            {
                if (how == "missing")
                    File.Delete(g.Bundle);
                else
                    File.WriteAllBytes(g.Bundle, bundle.Take(bundle.Length / 2).ToArray());
            }

            foreach (string how in new[] { "missing", "damaged" })
            {
                r.Test($"Patcher: bundle {how} after a game update: apply and restore refuse the old version's backup (nothing changed); after a Steam verification the new official file is adopted (CS-10)", () =>
                {
                    // apply -> game update (version.txt changed) -> bundle lost / damaged: the backup was taken for the old
                    // version, so it is not the official file of the installed version (before round 3 apply rebuilt from it
                    // and recorded the new version, and the real new official file was never adopted later)
                    foreach (bool restoreFirst in new[] { false, true })
                    {
                        string what = how + (restoreFirst ? ", restore after the verification" : ", apply after the verification");
                        var g = ctx.NewGame("lost_after_update_" + how + "_" + restoreFirst, official);
                        Apply(g, data);
                        g.Update(updated);
                        Lose(g, how, updated);
                        g.Log.Clear();
                        foreach (var op in new[] { "apply", "restore" })
                            T.Contains(RefusesOp(g, op, what).Message,
                                $"バックアップは別のバージョンのゲーム（{Game.DefaultVersion}）の公式データです（現在のゲームのバージョン: {Util.UpdatedVersion}）。バックアップは削除せずに残しました。",
                                what + ": " + op + ": message");
                        // R20: a refused restore is not announced as one ("…バックアップから戻します")
                        T.Eq("", g.LogText, what + ": nothing logged before the refusals");
                        T.Eq(Game.DefaultVersion, g.ReadState()["game_version"], what + ": state still records the backup's version");
                        var st = g.Patcher().GetStatus(data);
                        T.Eq(how == "missing" ? Patcher.BundleState.Missing : Patcher.BundleState.DamagedNoBackup, st.State, what + ": status");
                        File.WriteAllBytes(g.Bundle, updated); // "verify integrity of game files"
                        T.Eq(Patcher.BundleState.Unknown, g.Patcher().GetStatus(data).State, what + ": status after the verification (updated by the game)");
                        g.Log.Clear();
                        if (!restoreFirst)
                        {
                            Check(Apply(g, data), Expect(updated, data), what + ": apply");
                            T.Contains(g.LogText, "日本語データが更新されています（ゲームのアップデートなど）。新しいデータをバックアップし直します", what + ": log");
                            T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(g.Backup)), what + ": backup = the new official file");
                            T.Eq(Util.UpdatedVersion, g.ReadState()["game_version"], what + ": state game_version = the new version");
                            T.Eq(updatedSha, g.ReadState()["original_sha256"], what + ": state original");
                            Restore(g, data);
                            T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), what + ": restore gives the new official file");
                        }
                        else
                        {
                            Restore(g, data);
                            T.Contains(g.LogText, "そのままにします", what + ": log");
                            T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), what + ": the new official file is kept");
                        }
                        T.True(!File.Exists(g.Backup) && !File.Exists(g.State), what + ": backup and state removed");
                    }
                });
            }

            r.Test("Patcher: backup replaced by another valid bundle + bundle missing or damaged -> apply and restore refuse (not the recorded file); after a Steam verification apply backs the official file up again", () =>
            {
                // fuzz backup_hash_not_checked: the installed file cannot vouch for the backup, so the state file must
                foreach (string how in new[] { "missing", "damaged" })
                {
                    foreach (bool restoreFirst in new[] { false, true })
                    {
                        string what = how + (restoreFirst ? ", restore after the verification" : ", apply after the verification");
                        var g = ctx.NewGame("swapped_backup_" + how + "_" + restoreFirst, official);
                        Apply(g, data);
                        File.WriteAllBytes(g.Backup, updated); // valid and official-looking, but not the recorded file; version.txt unchanged
                        Lose(g, how, patched);
                        g.Log.Clear();
                        foreach (var op in new[] { "apply", "restore" })
                            T.Contains(RefusesOp(g, op, what).Message, BackupMismatchText, what + ": " + op + ": message");
                        T.Eq("", g.LogText, what + ": nothing logged before the refusals (R20)");
                        T.Eq(how == "missing" ? Patcher.BundleState.Missing : Patcher.BundleState.DamagedNoBackup, g.Patcher().GetStatus(data).State, what + ": status");
                        File.WriteAllBytes(g.Bundle, official); // "verify integrity of game files"
                        T.Eq(Patcher.BundleState.Reverted, g.Patcher().GetStatus(data).State, what + ": status after the verification (the recorded official file)");
                        g.Log.Clear();
                        if (!restoreFirst)
                        {
                            Check(Apply(g, data), offModel, what + ": apply");
                            T.Contains(g.LogText, "日本語データが記録されている公式データと一致するため、バックアップを作り直します", what + ": log");
                            T.Contains(g.LogText, "バックアップを作成しました", what + ": backup written");
                            T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), what + ": backup = the official file");
                            T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), what + ": patched");
                            var s = g.ReadState();
                            T.Eq(officialSha, s["original_sha256"], what + ": state original");
                            T.Eq(Game.DefaultVersion, s["game_version"], what + ": state game_version");
                            T.Eq(Patcher.BundleState.Patched, g.Patcher().GetStatus(data).State, what + ": status after apply");
                            Restore(g, data);
                        }
                        else
                        {
                            Restore(g, data);
                            T.Contains(g.LogText, "日本語データは公式のままです", what + ": log");
                        }
                        T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), what + ": official file after the restore");
                        T.True(!File.Exists(g.Backup) && !File.Exists(g.State), what + ": backup and state removed");
                        T.Eq(Patcher.BundleState.Original, g.Patcher().GetStatus(data).State, what + ": status at the end");
                    }
                }
            });

            r.Test("Patcher: the recorded official file is installed while the backup is missing / another file / damaged / patched -> apply backs it up again, restore leaves it", () =>
            {
                byte[] half = official.Take(official.Length / 2).ToArray();
                var backups = new Dictionary<string, Action<Game>>
                {
                    ["missing"] = g => File.Delete(g.Backup),
                    ["another valid file"] = g => File.WriteAllBytes(g.Backup, updated),
                    ["damaged"] = g => File.WriteAllBytes(g.Backup, half),
                    ["patched"] = g => File.WriteAllBytes(g.Backup, patched),
                };
                foreach (var kv in backups)
                {
                    foreach (string op in new[] { "Apply", "Restore", "Apply after a version change" })
                    {
                        string what = kv.Key + ": " + op;
                        var g = ctx.NewGame("recorded_original", official);
                        Apply(g, data);
                        File.WriteAllBytes(g.Bundle, official); // Steam verification
                        kv.Value(g);
                        if (op.EndsWith("change", StringComparison.Ordinal))
                            g.SetVersion(Util.UpdatedVersion); // an update that left this file as it is
                        T.Eq(Patcher.BundleState.Reverted, g.Patcher().GetStatus().State, what + ": status");
                        T.Eq(Patcher.BundleState.Reverted, g.Patcher().GetStatus(data).State, what + ": status with data");
                        g.Log.Clear();
                        if (op.StartsWith("Apply", StringComparison.Ordinal))
                        {
                            Check(Apply(g, data), offModel, what);
                            T.Contains(g.LogText, "日本語データが記録されている公式データと一致するため、バックアップを作り直します", what + ": log");
                            T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), what + ": backup repaired");
                            T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), what + ": patched");
                            T.Eq(op == "Apply" ? Game.DefaultVersion : Util.UpdatedVersion, g.ReadState()["game_version"], what + ": game_version = the installed version");
                            T.Eq(Patcher.BundleState.Patched, g.Patcher().GetStatus(data).State, what + ": status after");
                        }
                        else
                        {
                            Restore(g, data);
                            T.Contains(g.LogText, "日本語データは公式のままです", what + ": log");
                            T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), what + ": official file kept");
                            T.True(!File.Exists(g.Backup) && !File.Exists(g.State), what + ": backup and state removed");
                        }
                        T.Eq(0, g.Leftovers().Length, what + ": temp files");
                    }
                }
            });

            r.Test("Patcher: bundle missing or damaged with a state of version 1.0.0 (no game_version) or without a state -> apply and restore refuse (the backup's version cannot be confirmed), status without '元に戻す'; the Steam verification recovers and records the version (S1)", () =>
            {
                // round 3c (S1): before, the backup was used unchecked here, and a later new backup or re-apply recorded the
                // installed version for it (an old version's data after every Steam verification)
                foreach (string how in new[] { "missing", "damaged" })
                {
                    foreach (string stateKind in new[] { "1.0.0 state", "1.0.0 state, other version.txt", "no state" })
                    {
                        foreach (string after in new[] { "apply", "restore" })
                        {
                            string what = how + ", " + stateKind + ", " + after + " after the verification";
                            var g = ctx.NewGame("oldstate_" + how + "_" + after, official);
                            Apply(g, data);
                            if (stateKind == "no state")
                                File.Delete(g.State);
                            else
                                RemoveGameVersion(g);
                            string installed = Game.DefaultVersion;
                            if (stateKind.EndsWith("version.txt", StringComparison.Ordinal))
                                g.SetVersion(installed = Util.UpdatedVersion); // an update that did not touch the Japanese bundle
                            Lose(g, how, patched);
                            g.Log.Clear();
                            foreach (var op in new[] { "apply", "restore" })
                                T.Contains(RefusesOp(g, op, what).Message,
                                    $"バックアップがどのバージョンのゲームのものか確認できません（記録: 不明、現在のゲームのバージョン: {installed}）。バックアップは削除せずに残しました。",
                                    what + ": " + op + ": message");
                            T.Contains(Refuses(g, () => g.Patcher().Restore(null), what + ": restore(null)").Message, "確認できません（記録: 不明、", what + ": restore(null): message");
                            T.Eq("", g.LogText, what + ": nothing announced before the refusals (R20)");
                            var st = g.Patcher().GetStatus(data);
                            T.Eq(how == "missing" ? Patcher.BundleState.Missing : Patcher.BundleState.DamagedNoBackup, st.State, what + ": status");
                            T.True(!MainForm.DescribeState(st).Contains("元に戻す"), what + ": restore is not offered");
                            File.WriteAllBytes(g.Bundle, official); // "verify integrity of game files": the backup's file again
                            g.Log.Clear();
                            if (after == "apply")
                            {
                                Check(Apply(g, data), offModel, what + ": apply");
                                T.Contains(g.LogText, "公式の日本語データにパッチを適用します", what + ": log");
                                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), what + ": backup unchanged");
                                T.Eq(installed, g.ReadState()["game_version"], what + ": the identified version is recorded");
                                Lose(g, how, patched); // now the state vouches for the backup: a later loss is rebuilt
                                g.Log.Clear();
                                Check(Apply(g, data), offModel, what + ": rebuild of a later loss");
                                T.Contains(g.LogText, how == "missing" ? "日本語データが見つからないため" : "日本語データが壊れているため", what + ": rebuild log");
                                Restore(g, data);
                            }
                            else
                            {
                                Restore(g, data);
                                T.Contains(g.LogText, "日本語データは公式のままです", what + ": log");
                            }
                            T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), what + ": the official file at the end");
                            T.True(!File.Exists(g.Backup) && !File.Exists(g.State), what + ": backup and state removed");
                        }
                    }
                }
            });

            r.Test("Patcher: bundle missing while version.txt cannot be read (missing / a folder / empty), or with a backup recorded for an unknown or empty version -> apply and restore refuse saying the version cannot be confirmed (both versions must be known); the Steam verification recovers", () =>
            {
                // round 3b (R4/R14/R26): an unknown version is not called "another version"; the text says that the backup's
                // version cannot be confirmed (an empty version is shown as 不明)
                string Unconfirmed(string recorded, string current) =>
                    $"バックアップがどのバージョンのゲームのものか確認できません（記録: {recorded}、現在のゲームのバージョン: {current}）。バックアップは削除せずに残しました。";
                var g = ctx.NewGame("unknown_version_missing", official);
                Apply(g, data);
                File.Delete(Path.Combine(g.Dir, "version.txt"));
                File.Delete(g.Bundle);
                foreach (var op in new[] { "apply", "restore" })
                {
                    var e = RefusesOp(g, op, "unknown installed version");
                    T.Contains(e.Message, Unconfirmed(Game.DefaultVersion, GameLocator.UnknownVersion), op + ": message");
                    T.True(!e.Message.Contains("別のバージョン"), op + ": not called another version: " + T.Show(e.Message));
                }
                // version.txt that cannot be opened (a folder: UnauthorizedAccessException) is unknown as well, not an error
                Directory.CreateDirectory(Path.Combine(g.Dir, "version.txt"));
                T.Eq(GameLocator.UnknownVersion, g.Patcher().GetStatus(data).GameVersion, "version.txt is a folder: status version");
                foreach (var op in new[] { "apply", "restore" })
                    T.Contains(RefusesOp(g, op, "version.txt is a folder").Message, Unconfirmed(Game.DefaultVersion, GameLocator.UnknownVersion), op + ": message");
                Directory.Delete(Path.Combine(g.Dir, "version.txt"));
                // an empty version.txt is unknown too (R15/R27: it never matches, not even a recorded empty version)
                g.SetVersion("");
                foreach (var op in new[] { "apply", "restore" })
                    T.Contains(RefusesOp(g, op, "empty installed version").Message, Unconfirmed(Game.DefaultVersion, GameLocator.UnknownVersion), op + ": message");
                var ge = ctx.NewGame("empty_version_recorded", official);
                ge.SetVersion("");
                Apply(ge, data);
                T.Eq("", ge.ReadState()["game_version"], "empty version recorded with the new backup");
                File.Delete(ge.Bundle);
                foreach (var op in new[] { "apply", "restore" })
                    T.Contains(RefusesOp(ge, op, "empty recorded and installed version").Message, Unconfirmed(GameLocator.UnknownVersion, GameLocator.UnknownVersion), op + ": message");
                foreach (bool versionBack in new[] { false, true })
                {
                    string what = versionBack ? "recorded '不明', version.txt back" : "recorded '不明', still no version.txt";
                    var g2 = ctx.NewGame("unknown_version_recorded", official);
                    File.Delete(Path.Combine(g2.Dir, "version.txt"));
                    Apply(g2, data);
                    T.Eq(GameLocator.UnknownVersion, g2.ReadState()["game_version"], what + ": unknown version recorded");
                    if (versionBack)
                        g2.SetVersion(Game.DefaultVersion);
                    File.Delete(g2.Bundle);
                    foreach (var op in new[] { "apply", "restore" })
                        T.Contains(RefusesOp(g2, op, what).Message,
                            Unconfirmed(GameLocator.UnknownVersion, versionBack ? Game.DefaultVersion : GameLocator.UnknownVersion), what + ": " + op + ": message");
                    // the Steam verification brings the official file (and version.txt) back: no refuse loop
                    File.WriteAllBytes(g2.Bundle, official);
                    g2.SetVersion(Game.DefaultVersion);
                    Check(Apply(g2, data), offModel, what + ": apply after the verification");
                    T.Eq(Game.DefaultVersion, g2.ReadState()["game_version"], what + ": the identified version is recorded");
                    File.Delete(g2.Bundle);
                    Apply(g2, data);
                    T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g2.Bundle)), what + ": a later missing bundle is rebuilt");
                }
            });

            r.Test("Patcher: game_version follows the backup: recorded with a new backup or when the installed file is the backup; refreshed (never added) when it is the exact output of the backup; kept otherwise", () =>
            {
                // state lost, our output still installed: the rebuilt output equals it, but that output may itself have been
                // rebuilt from a backup nobody checked, so no version is added (round 3b, R1); the state is written without it
                var g = ctx.NewGame("version_nostate", official);
                Apply(g, data);
                File.Delete(g.State);
                g.SetVersion(Util.UpdatedVersion); // an update that did not touch the Japanese bundle
                Apply(g, data);
                T.Contains(g.LogText, "以前に適用したパッチを検出しました", "state lost: classified by content");
                T.True(!g.ReadState().ContainsKey("game_version"), "state lost: no game_version added");
                T.Eq(patchedSha, g.ReadState()["patched_sha256"], "state lost: the new state describes the bundle");
                Apply(g, data); // the state describes the bundle now: still nothing to refresh
                T.True(!g.ReadState().ContainsKey("game_version"), "state lost, re-apply: no game_version added");
                File.WriteAllBytes(g.Bundle, official); // the installed file is the backup itself: its version is recorded
                Apply(g, data);
                T.Eq(Util.UpdatedVersion, g.ReadState()["game_version"], "bundle = backup: the installed version");
                // the same with a stale state of the old version (FZ-10 / killed before the state was written): refreshed
                var g2 = ctx.NewGame("version_stale", official);
                Apply(g2, data);
                var stale = File.ReadAllText(g2.State).Replace(patchedSha, new string('0', 64));
                File.WriteAllText(g2.State, stale);
                g2.SetVersion(Util.UpdatedVersion);
                Apply(g2, data);
                T.Contains(g2.LogText, "以前に適用したパッチを検出しました", "stale state: classified by content");
                T.Eq(Util.UpdatedVersion, g2.ReadState()["game_version"], "stale state: the identified installed version");
                // our output that the backup does not reproduce (here: the patch of another official file): the old version is kept
                var gu = ctx.NewGame("version_other_output", updated);
                Apply(gu, data);
                byte[] otherOutput = File.ReadAllBytes(gu.Bundle);
                var g3 = ctx.NewGame("version_notidentified", official);
                Apply(g3, data);
                g3.Update(otherOutput);
                Apply(g3, data);
                T.Contains(g3.LogText, "以前に適用したパッチを検出しました", "classified as our output");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g3.Backup)), "rebuilt from the old backup");
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g3.Bundle)), "the output of the old backup");
                T.Eq(Game.DefaultVersion, g3.ReadState()["game_version"], "not identified: game_version of the backup kept");
                // a damaged bundle rebuilt from a backup of the same version keeps that version
                var g4 = ctx.NewGame("version_damaged", official);
                Apply(g4, data);
                Lose(g4, "damaged", patched);
                Apply(g4, data);
                T.Eq(Game.DefaultVersion, g4.ReadState()["game_version"], "damaged: kept (= installed)");
            });

            // ---- round 3b ----

            r.Test("Patcher: bundle lost after an update with a 1.0.0 state or no state: apply, restore and restore(null) refuse the old backup and change nothing; after a Steam verification apply backs up the new official file and restore keeps it (R1/R25, S1)", () =>
            {
                foreach (string how in new[] { "missing", "damaged" })
                {
                    foreach (string stateKind in new[] { "1.0.0 state", "no state" })
                    {
                        foreach (string after in new[] { "apply", "restore" })
                        {
                            string what = how + ", " + stateKind + ", " + after + " after the verification";
                            var g = ctx.NewGame("r1_" + how + "_" + after, official);
                            Apply(g, data);
                            if (stateKind == "no state")
                                File.Delete(g.State);
                            else
                                RemoveGameVersion(g);
                            g.Update(updated);
                            Lose(g, how, updated);
                            // round 3c (S1): nothing records the backup's version, so it is not used (before, the old backup was
                            // rebuilt or restored here, and a later new backup recorded the new version for it)
                            g.Log.Clear();
                            foreach (var op in new[] { "apply", "restore" })
                                T.Contains(RefusesOp(g, op, what).Message,
                                    $"バックアップがどのバージョンのゲームのものか確認できません（記録: 不明、現在のゲームのバージョン: {Util.UpdatedVersion}）。バックアップは削除せずに残しました。",
                                    what + ": " + op + ": message");
                            T.Contains(Refuses(g, () => g.Patcher().Restore(null), what + ": restore(null)").Message, "確認できません（記録: 不明、", what + ": restore(null): message");
                            T.Eq("", g.LogText, what + ": nothing announced before the refusals (R20)");
                            T.Eq(how == "missing" ? Patcher.BundleState.Missing : Patcher.BundleState.DamagedNoBackup, g.Patcher().GetStatus(data).State, what + ": status");
                            File.WriteAllBytes(g.Bundle, updated); // "verify integrity of game files"
                            T.Eq(Patcher.BundleState.Unknown, g.Patcher().GetStatus(data).State, what + ": status after the verification");
                            g.Log.Clear();
                            if (after == "apply")
                            {
                                Check(Apply(g, data), Expect(updated, data), what + ": apply");
                                T.Contains(g.LogText, "日本語データが更新されています（ゲームのアップデートなど）。新しいデータをバックアップし直します", what + ": apply log");
                                T.True(!g.LogText.Contains("ゲームのバージョンは変わっていないため"), what + ": not rebuilt from the old backup: " + T.Show(g.LogText));
                                T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(g.Backup)), what + ": backup = the new official file");
                                T.Eq(Util.UpdatedVersion, g.ReadState()["game_version"], what + ": the new backup's version");
                                Restore(g, data);
                            }
                            else
                            {
                                Restore(g, data);
                                T.Contains(g.LogText, "そのままにします", what + ": restore log");
                            }
                            T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), what + ": the new official file at the end");
                            T.True(!File.Exists(g.Backup) && !File.Exists(g.State), what + ": backup and state removed");
                        }
                    }
                }
            });

            r.Test("Patcher: an unreadable version.txt never replaces a recorded game_version (re-apply, bundle = backup, our output with a stale state); a recorded 不明 is refreshed once the version is known (R3)", () =>
            {
                string VersionFile(Game x) => Path.Combine(x.Dir, "version.txt");
                // re-apply (the state describes the bundle) while version.txt is held open by another program, missing or a folder
                foreach (string how in new[] { "held open", "missing", "a folder" })
                {
                    var g = ctx.NewGame("r3_reapply", official);
                    Apply(g, data);
                    if (how == "held open")
                    {
                        using (new FileStream(VersionFile(g), FileMode.Open, FileAccess.Read, FileShare.None))
                        {
                            T.Eq(GameLocator.UnknownVersion, GameLocator.GameVersion(g.Dir), how + ": precondition (version unknown)");
                            Apply(g, data);
                        }
                    }
                    else
                    {
                        File.Delete(VersionFile(g));
                        if (how == "a folder")
                            Directory.CreateDirectory(VersionFile(g));
                        T.Eq(GameLocator.UnknownVersion, GameLocator.GameVersion(g.Dir), how + ": precondition (version unknown)");
                        Apply(g, data);
                        if (how == "a folder")
                            Directory.Delete(VersionFile(g));
                        g.SetVersion(Game.DefaultVersion);
                    }
                    T.Eq(Game.DefaultVersion, g.ReadState()["game_version"], how + ": the recorded version is kept");
                    File.Delete(g.Bundle); // a later loss is rebuilt, not refused
                    g.Log.Clear();
                    Check(Apply(g, data), offModel, how + ": rebuild of a lost bundle");
                    T.Contains(g.LogText, "日本語データが見つからないため、バックアップした公式データから作り直します", how + ": rebuild log");
                }
                // the installed file is the backup (Steam verification) while version.txt is missing
                var g2 = ctx.NewGame("r3_backup", official);
                Apply(g2, data);
                File.WriteAllBytes(g2.Bundle, official);
                File.Delete(VersionFile(g2));
                g2.Log.Clear();
                Apply(g2, data);
                T.Contains(g2.LogText, "公式の日本語データにパッチを適用します", "bundle = backup: log");
                T.Eq(Game.DefaultVersion, g2.ReadState()["game_version"], "bundle = backup: the recorded version is kept");
                // our output with a stale state (classified by content, the rebuilt output equals it) while version.txt is missing
                var g3 = ctx.NewGame("r3_stale", official);
                Apply(g3, data);
                File.WriteAllText(g3.State, File.ReadAllText(g3.State).Replace(patchedSha, new string('0', 64)));
                File.Delete(VersionFile(g3));
                g3.Log.Clear();
                Apply(g3, data);
                T.Contains(g3.LogText, "以前に適用したパッチを検出しました", "stale state: log");
                T.Eq(Game.DefaultVersion, g3.ReadState()["game_version"], "stale state: the recorded version is kept");
                // a backup taken while version.txt could not be read records 不明; a re-apply with a readable version refreshes it
                var g4 = ctx.NewGame("r3_refresh", official);
                File.Delete(VersionFile(g4));
                Apply(g4, data);
                T.Eq(GameLocator.UnknownVersion, g4.ReadState()["game_version"], "new backup without version.txt: 不明 recorded");
                g4.SetVersion(Game.DefaultVersion);
                Apply(g4, data);
                T.Eq(Game.DefaultVersion, g4.ReadState()["game_version"], "re-apply with a readable version.txt: refreshed");
                // round 3c (S3): the recorded official file installed again while the backup is missing and version.txt is
                // missing or empty: the backup is taken again of the same file, so its recorded version is kept (a state
                // without one records what version.txt gives)
                foreach (string how in new[] { "missing", "empty", "missing, 1.0.0 state" })
                {
                    var g5 = ctx.NewGame("r3_recorded_original", official);
                    Apply(g5, data);
                    if (how.EndsWith("state", StringComparison.Ordinal))
                        RemoveGameVersion(g5);
                    File.WriteAllBytes(g5.Bundle, official); // Steam verification
                    File.Delete(g5.Backup);
                    if (how.StartsWith("missing", StringComparison.Ordinal))
                        File.Delete(VersionFile(g5));
                    else
                        g5.SetVersion("");
                    g5.Log.Clear();
                    Check(Apply(g5, data), offModel, how + ": recorded official file backed up again");
                    T.Contains(g5.LogText, "日本語データが記録されている公式データと一致するため、バックアップを作り直します", how + ": log");
                    T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g5.Backup)), how + ": backup repaired");
                    if (how.EndsWith("state", StringComparison.Ordinal))
                    {
                        T.Eq(GameLocator.UnknownVersion, g5.ReadState()["game_version"], how + ": nothing was recorded: 不明");
                        continue;
                    }
                    T.Eq(Game.DefaultVersion, g5.ReadState()["game_version"], how + ": the recorded version is kept");
                    g5.SetVersion(Game.DefaultVersion);
                    File.Delete(g5.Bundle); // a later loss is rebuilt, not refused
                    g5.Log.Clear();
                    Check(Apply(g5, data), offModel, how + ": rebuild of a lost bundle");
                    T.Contains(g5.LogText, "日本語データが見つからないため、バックアップした公式データから作り直します", how + ": rebuild log");
                }
            });

            r.Test("Patcher: same game version, another valid bundle installed and the backup swapped -> apply, restore and restore(null) refuse with 'バックアップが記録と一致しません' and change nothing (the same-version branches, R6/R28); the Steam verification recovers", () =>
            {
                byte[] other = Updated(ctx, 0, 0, 1, offset: 11); // valid, not our output, neither the backup nor the recorded file
                foreach (string after in new[] { "apply", "restore" })
                {
                    var g = ctx.NewGame("samever_swapped_" + after, official);
                    Apply(g, data);
                    File.WriteAllBytes(g.Bundle, other); // version.txt unchanged
                    File.WriteAllBytes(g.Backup, updated); // valid and official-looking, but not the recorded file
                    T.Eq(Patcher.BundleState.Unknown, g.Patcher().GetStatus(data).State, after + ": status");
                    g.Log.Clear();
                    T.Contains(RefusesOp(g, "apply", after).Message, BackupMismatchText, after + ": apply message");
                    T.Contains(RefusesOp(g, "restore", after).Message, BackupMismatchText, after + ": restore message");
                    var e = Refuses(g, () => g.Patcher().Restore(null), after + ": restore(null)");
                    T.Contains(e.Message, BackupMismatchText, after + ": restore(null) message");
                    T.Eq("", g.LogText, after + ": nothing announced before the refusals (R20)");
                    File.WriteAllBytes(g.Bundle, official); // "verify integrity of game files"
                    T.Eq(Patcher.BundleState.Reverted, g.Patcher().GetStatus(data).State, after + ": status after the verification");
                    g.Log.Clear();
                    if (after == "apply")
                    {
                        Check(Apply(g, data), offModel, after + ": apply after the verification");
                        T.Contains(g.LogText, "日本語データが記録されている公式データと一致するため、バックアップを作り直します", after + ": log");
                        T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), after + ": backup repaired");
                        Restore(g, data);
                    }
                    else
                    {
                        Restore(g, data);
                        T.Contains(g.LogText, "日本語データは公式のままです", after + ": log");
                    }
                    T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), after + ": the official file at the end");
                    T.True(!File.Exists(g.Backup) && !File.Exists(g.State), after + ": backup and state removed");
                }
                // a recorded version that cannot be compared (不明 / empty) never takes the same-version branches: the other
                // valid bundle is a new official file (apply backs it up, restore keeps it)
                foreach (string recorded in new[] { "不明", "empty" })
                {
                    foreach (string op in new[] { "apply", "restore" })
                    {
                        string what = "recorded " + recorded + ", " + op;
                        var g = ctx.NewGame("samever_unknown_" + op, official);
                        if (recorded == "empty")
                            g.SetVersion("");
                        else
                            File.Delete(Path.Combine(g.Dir, "version.txt"));
                        Apply(g, data);
                        T.Eq(recorded == "empty" ? "" : GameLocator.UnknownVersion, g.ReadState()["game_version"], what + ": recorded version");
                        File.WriteAllBytes(g.Bundle, other);
                        g.Log.Clear();
                        if (op == "apply")
                        {
                            Check(Apply(g, data), Expect(other, data), what);
                            T.Contains(g.LogText, "新しいデータをバックアップし直します", what + ": log");
                            T.Eq(Util.Sha256(other), Util.Sha256(File.ReadAllBytes(g.Backup)), what + ": the other file is the new backup");
                        }
                        else
                        {
                            Restore(g, data);
                            T.Contains(g.LogText, "そのままにします", what + ": log");
                            T.Eq(Util.Sha256(other), Util.Sha256(File.ReadAllBytes(g.Bundle)), what + ": the other file is kept");
                        }
                        T.True(!g.LogText.Contains("ゲームのバージョンは変わっていないため"), what + ": not the same-version branch: " + T.Show(g.LogText));
                    }
                }
            });

            r.Test("Patcher: an installed bundle in a format this patcher does not read (UnityFS 9) is damage only when the state proves the game version unchanged; otherwise apply asks to verify, then to wait for a patch update, restore keeps it, status 'Unsupported' (R10/R22, S2/S7/S8/S15)", () =>
            {
                byte[] v9 = (byte[])official.Clone();
                // "UnityFS\0", then the format version as a big-endian uint32
                T.Eq("UnityFS", Encoding.ASCII.GetString(v9, 0, 7), "precondition (UnityFS signature)");
                T.True(v9[8] == 0 && v9[9] == 0 && v9[10] == 0 && v9[11] >= 6 && v9[11] <= 8, "precondition (format version 6..8)");
                v9[11] = 9;
                var load = T.Throws<InvalidDataException>(() => LocalisationBundle.Load(v9, "v9.bundle").Dispose(), "UnityFS 9 is refused by Load");
                T.True(UnsupportedFormatException.In(load), "Load reports UnityFS 9 as an unsupported format: " + load.Message);
                string v9Sha = Util.Sha256(v9);
                const string UnsupportedText = "このパッチが対応していない形式です（パッチの更新をお待ちください）";
                string unsupportedRefusal = "日本語データを読み込めません（対応していないバンドルの形式です（版 9））。Steam のライブラリで Two Point Campus を右クリック →「プロパティ」→「インストール済みファイル」→「ゲームファイルの整合性を確認」を実行してから、もう一度お試しください。それでも直らない場合は、パッチの更新をお待ちください";

                // the same game version with a backup (the state proves it): the format bytes are damage, rebuilt from the backup
                var g2 = ctx.NewGame("unsupported_backup", official);
                Apply(g2, data);
                File.WriteAllBytes(g2.Bundle, v9);
                T.Eq(Patcher.BundleState.Damaged, g2.Patcher().GetStatus(data).State, "same version: status");
                g2.Log.Clear();
                Check(Apply(g2, data), offModel, "with a backup");
                T.Contains(g2.LogText, "日本語データが壊れているため、バックアップした公式データから作り直します", "with a backup: log");
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g2.Bundle)), "with a backup: rebuilt");
                File.WriteAllBytes(g2.Bundle, v9);
                g2.Log.Clear();
                Restore(g2, data);
                T.Contains(g2.LogText, "日本語データが壊れているため、バックアップから戻します", "same version: restore log");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g2.Bundle)), "same version: restored from the backup");

                // the state does not prove the game version: an update to V2 (backup of V1), no state, a 1.0.0 state
                var setups = new Dictionary<string, Action<Game>>
                {
                    ["update (backup of the old version)"] = g => g.Update(v9),
                    ["no state, same version.txt"] = g => { File.Delete(g.State); File.WriteAllBytes(g.Bundle, v9); },
                    ["1.0.0 state, same version.txt"] = g => { RemoveGameVersion(g); File.WriteAllBytes(g.Bundle, v9); },
                    ["update, no state"] = g => { File.Delete(g.State); g.Update(v9); },
                };
                foreach (var kv in setups)
                {
                    foreach (bool withData in new[] { true, false })
                    {
                        string what = kv.Key + (withData ? ", restore" : ", restore(null)");
                        var g = ctx.NewGame("unsupported_" + (withData ? "data" : "nodata"), official);
                        Apply(g, data);
                        kv.Value(g);
                        var st = g.Patcher().GetStatus(data);
                        T.Eq(Patcher.BundleState.Unsupported, st.State, what + ": status");
                        T.Eq(UnsupportedText, MainForm.DescribeState(st), what + ": status text");
                        T.Eq(Patcher.BundleState.Unknown, g.Patcher().GetStatus().State, what + ": status without data (nothing read)");
                        g.Log.Clear();
                        T.Eq(unsupportedRefusal, Refuses(g, () => g.Patcher().Apply(data), what + ": apply").Message, what + ": apply message");
                        T.Eq("", g.LogText, what + ": apply announced nothing");
                        // restore: the file is not this patcher's output, so it is kept and the old backup is not written over it
                        Restore(g, withData ? data : null);
                        T.Eq("日本語データはこのパッチが対応していない形式のため、そのままにします", g.LogText, what + ": log");
                        T.Eq(v9Sha, Util.Sha256(File.ReadAllBytes(g.Bundle)), what + ": the installed file is kept");
                        T.True(!File.Exists(g.Backup) && !File.Exists(g.State), what + ": backup and state removed");
                        T.Eq(Patcher.BundleState.Unsupported, g.Patcher().GetStatus(data).State, what + ": status after the restore");
                        T.Eq(0, g.Leftovers().Length, what + ": temp files");
                    }
                }

                // no backup: a fresh install, or a state left of an older version -> 'not patched'; a state of the same version
                // (the backup is lost) proves the version: damage
                foreach (string stateKind in new[] { "no state", "state of an older version", "state of the same version" })
                {
                    var g = ctx.NewGame("unsupported_nobackup", v9);
                    if (stateKind != "no state")
                    {
                        File.WriteAllText(g.State, "original_sha256=" + officialSha + "\r\npatched_sha256=" + patchedSha + "\r\ngame_version="
                            + (stateKind == "state of the same version" ? Game.DefaultVersion : "10.2.0+2024-01-01.0000") + "\r\n");
                    }
                    var st = g.Patcher().GetStatus(data);
                    if (stateKind == "state of the same version")
                    {
                        T.Eq(Patcher.BundleState.DamagedNoBackup, st.State, stateKind + ": status");
                        T.Contains(Refuses(g, () => g.Patcher().Apply(data), stateKind + ": apply").Message, "日本語データが壊れています（", stateKind + ": apply message");
                        T.Contains(Refuses(g, () => g.Patcher().Restore(data), stateKind + ": restore").Message, "日本語データが壊れていて、バックアップもありません。", stateKind + ": restore message");
                        continue;
                    }
                    T.Eq(Patcher.BundleState.Unsupported, st.State, stateKind + ": status");
                    T.Eq(UnsupportedText, MainForm.DescribeState(st), stateKind + ": status text");
                    var e = Refuses(g, () => g.Patcher().Apply(data), stateKind + ": apply");
                    T.Eq(unsupportedRefusal, e.Message, stateKind + ": apply message");
                    T.True(!e.Message.Contains("壊れています"), stateKind + ": not called damaged: " + T.Show(e.Message));
                    g.Log.Clear();
                    Restore(g, data);
                    T.Eq("パッチは適用されていません（日本語データはこのパッチが対応していない形式です）", g.LogText, stateKind + ": restore log");
                    T.Eq(v9Sha, Util.Sha256(File.ReadAllBytes(g.Bundle)), stateKind + ": the installed file is kept");
                    T.True(!File.Exists(g.Backup) && !File.Exists(g.State), stateKind + ": no backup, the stale state removed");
                    g.Log.Clear();
                    Restore(g, null); // without data nothing is read
                    T.Eq("パッチは適用されていません（バックアップがありません）", g.LogText, stateKind + ": restore(null) log");
                    r.Note(stateKind + ": " + e.Message);
                }
            });

            r.Test("Patcher: the state file gets one line per entry: line breaks in a value (version.txt, a carried-over entry) become spaces (R18)", () =>
            {
                var g = ctx.NewGame("state_one_line", official);
                string ls = ((char)0x2028).ToString(), nel = ((char)0x85).ToString();
                g.SetVersion(Game.DefaultVersion + "\r\ngame_version=FORGED\ninjected=1");
                Apply(g, data);
                // a carried-over entry with breaks that File.ReadAllLines keeps but Python's splitlines would split
                File.AppendAllText(g.State, "note=a" + ls + "game_version=FORGED" + nel + "x=1\r\n", new UTF8Encoding(false));
                Apply(g, data);
                string text = File.ReadAllText(g.State, Encoding.UTF8);
                var lines = text.Split(new[] { "\r\n" }, StringSplitOptions.None);
                T.Eq("", lines.Last(), "ends with CRLF");
                T.True(lines.Take(lines.Length - 1).All(l => l.IndexOfAny(new[] { '\r', '\n', '\v', '\f', (char)0x1C, (char)0x1D, (char)0x1E, (char)0x85, (char)0x2028, (char)0x2029 }) < 0),
                    "no line break inside a line: " + T.Show(text));
                var st = g.ReadState();
                T.Eq(Game.DefaultVersion + "  game_version=FORGED injected=1", st["game_version"], "game_version: one line");
                T.True(!st.ContainsKey("injected") && !st.ContainsKey("x"), "no injected entries: " + string.Join(",", st.Keys));
                T.Eq("a game_version=FORGED x=1", st["note"], "carried-over entry: one line");
                T.Eq(1, lines.Count(l => l.StartsWith("game_version=", StringComparison.Ordinal)), "one game_version line");
                // round 3c (S4): version.txt is read the way the state stores it, so the recorded version still matches
                T.Eq(st["game_version"], GameLocator.GameVersion(g.Dir), "version.txt read as recorded");
                File.Delete(g.Bundle);
                g.Log.Clear();
                Check(Apply(g, data), offModel, "a lost bundle is rebuilt (the same version)");
                T.Contains(g.LogText, "日本語データが見つからないため、バックアップした公式データから作り直します", "rebuild log");
            });

            r.Test("Patcher: a version.txt with a line break (or another OneLine character) inside matches its recorded game_version: same-version rebuilds and restores work (S4/S17)", () =>
            {
                foreach (string inner in new[] { "\r\n", "\n", ((char)0x2028).ToString(), ((char)0x85).ToString(), ((char)0x1C).ToString(), "\v" })
                {
                    string raw = "10.3.169253" + inner + "+2024-12-06.1241";
                    string what = "inner " + T.Show(inner) + " (U+" + ((int)inner[0]).ToString("X4") + ")";
                    string expected = "10.3.169253" + new string(' ', inner.Length) + "+2024-12-06.1241";
                    var g = ctx.NewGame("version_inner_break", official);
                    g.SetVersion(raw + "\r\n");
                    T.Eq(expected, GameLocator.GameVersion(g.Dir), what + ": GameVersion (breaks become spaces after Trim)");
                    Apply(g, data);
                    T.Eq(expected, g.ReadState()["game_version"], what + ": recorded");
                    T.Eq(expected, g.Patcher().GetStatus(data).GameVersion, what + ": status version");
                    // the same version: a changed bundle is rebuilt from the backup (not adopted as a new backup) ...
                    File.WriteAllBytes(g.Bundle, updated);
                    g.Log.Clear();
                    Check(Apply(g, data), offModel, what + ": changed bundle");
                    T.Contains(g.LogText, "ゲームのバージョンは変わっていないため", what + ": same-version log");
                    T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), what + ": backup untouched");
                    // ... and a damaged one is restored from it, not refused as another version
                    File.WriteAllBytes(g.Bundle, official.Take(official.Length / 2).ToArray());
                    T.Eq(Patcher.BundleState.Damaged, g.Patcher().GetStatus(data).State, what + ": damaged status offers restore");
                    Restore(g, data);
                    T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), what + ": restored");
                }
                // a leading / trailing break is trimmed as before; U+001C..U+001F are no white space for Trim, so a trailing
                // U+001C becomes a trailing space, stored and read the same way
                var g2 = ctx.NewGame("version_edge_break", official);
                g2.SetVersion("\r\n" + Game.DefaultVersion + ((char)0x1C).ToString());
                T.Eq(Game.DefaultVersion + " ", GameLocator.GameVersion(g2.Dir), "trailing U+001C");
                Apply(g2, data);
                T.Eq(Game.DefaultVersion + " ", g2.ReadState()["game_version"], "trailing U+001C: recorded");
                File.Delete(g2.Bundle);
                Check(Apply(g2, data), offModel, "trailing U+001C: a lost bundle is rebuilt");
            });

            r.Test("Patcher: a preferences.json that cannot be read or replaced is reported with the reason and without the read-only hint (R12/R21)", () =>
            {
                string root = ctx.Dir("prefs_errors");
                string prefsRoot = Path.Combine(root, "Cloud"), appData = Path.Combine(root, "appdata");
                string oldPrefs = Environment.GetEnvironmentVariable("TPCJP_PREFS_ROOT"), oldApp = Environment.GetEnvironmentVariable("TPCJP_APPDATA");
                try
                {
                    Environment.SetEnvironmentVariable("TPCJP_PREFS_ROOT", prefsRoot);
                    Environment.SetEnvironmentVariable("TPCJP_APPDATA", appData);
                    T.Eq(appData, Patcher.AppDataDir, "TPCJP_APPDATA hook active");
                    string Account(string id)
                    {
                        string dir = Path.Combine(prefsRoot, id);
                        Directory.CreateDirectory(dir);
                        string p = Path.Combine(dir, "preferences.json");
                        File.WriteAllBytes(p, Mp.Prefs(Mp.Int(0)));
                        return p;
                    }
                    string locked = Account("1_locked"), denied = Account("2_denied"), readDenied = Account("3_readdenied");
                    Directory.CreateDirectory(denied + SafeFile.TempSuffix); // the new file cannot be created: access denied
                    // round 3c (S11): reading is denied (e.g. a file created by another user): not called a write failure
                    var acl = File.GetAccessControl(readDenied);
                    var deny = new System.Security.AccessControl.FileSystemAccessRule(System.Security.Principal.WindowsIdentity.GetCurrent().User,
                        System.Security.AccessControl.FileSystemRights.ReadData, System.Security.AccessControl.AccessControlType.Deny);
                    acl.AddAccessRule(deny);
                    File.SetAccessControl(readDenied, acl);
                    var log = new List<string>();
                    Patcher.LanguageResult res;
                    Patcher.Status status;
                    try
                    {
                        using (new FileStream(locked, FileMode.Open, FileAccess.Read, FileShare.None))
                            res = new Patcher(root, log.Add).SetLanguage(Preferences.Japanese);
                        status = new Patcher(root, null).GetStatus();
                    }
                    finally
                    {
                        acl.RemoveAccessRule(deny);
                        File.SetAccessControl(readDenied, acl);
                    }
                    T.Eq(3, res.Failures.Count, "failures: " + string.Join(" / ", res.Failures));
                    T.True(res.Failures[0].StartsWith("表示言語を変更できませんでした（1_locked）: 設定ファイルを読み書きできません（", StringComparison.Ordinal),
                        "in use (IOException): " + res.Failures[0]);
                    T.True(res.Failures[1].StartsWith("表示言語を変更できませんでした（2_denied）: 設定ファイルにアクセスできません（", StringComparison.Ordinal),
                        "access denied (UnauthorizedAccessException): " + res.Failures[1]);
                    T.Contains(res.Failures[1], "preferences.json" + SafeFile.TempSuffix, "access denied: the reason names the refused path");
                    T.True(res.Failures[2].StartsWith("表示言語を変更できませんでした（3_readdenied）: 設定ファイルにアクセスできません（", StringComparison.Ordinal),
                        "read denied (UnauthorizedAccessException): " + res.Failures[2]);
                    T.True(status.Languages[2].Item2.StartsWith("読み取れません（", StringComparison.Ordinal), "read denied: status: " + status.Languages[2].Item2);
                    foreach (var f in res.Failures)
                    {
                        T.True(!f.Contains("読み取り専用") && !f.Contains("開けません") && !f.Contains("書き込めません"), "no read-only hint, no 'cannot open', no 'cannot write': " + f);
                        T.True(f.EndsWith("）", StringComparison.Ordinal) && !f.EndsWith("（）", StringComparison.Ordinal), "the reason is given: " + f);
                    }
                    T.Bytes(Mp.Prefs(Mp.Int(0)), File.ReadAllBytes(readDenied), "read denied: unchanged");
                    Directory.Delete(denied + SafeFile.TempSuffix);
                }
                finally
                {
                    Environment.SetEnvironmentVariable("TPCJP_PREFS_ROOT", oldPrefs);
                    Environment.SetEnvironmentVariable("TPCJP_APPDATA", oldApp);
                }
            });

            // TPCJP_PREFS_ROOT / TPCJP_APPDATA pointed into a new folder for `body` (prefs root, app data); restored afterwards
            void WithPrefsRoot(string name, Action<string, string> body)
            {
                string root = ctx.Dir(name);
                string prefsRoot = Path.Combine(root, "Cloud"), appData = Path.Combine(root, "appdata");
                string oldPrefs = Environment.GetEnvironmentVariable("TPCJP_PREFS_ROOT"), oldApp = Environment.GetEnvironmentVariable("TPCJP_APPDATA");
                try
                {
                    Environment.SetEnvironmentVariable("TPCJP_PREFS_ROOT", prefsRoot);
                    Environment.SetEnvironmentVariable("TPCJP_APPDATA", appData);
                    T.Eq(appData, Patcher.AppDataDir, "TPCJP_APPDATA hook active");
                    body(prefsRoot, appData);
                }
                finally
                {
                    Environment.SetEnvironmentVariable("TPCJP_PREFS_ROOT", oldPrefs);
                    Environment.SetEnvironmentVariable("TPCJP_APPDATA", oldApp);
                }
            }

            string PrefsAccount(string prefsRoot, string id, int language)
            {
                string dir = Path.Combine(prefsRoot, id);
                Directory.CreateDirectory(dir);
                string p = Path.Combine(dir, "preferences.json");
                File.WriteAllBytes(p, Mp.Prefs(Mp.Int(language)));
                return p;
            }

            r.Test("Patcher: RestoreLanguage when the patcher's own previous-language record cannot be read (held open, read denied) -> English with a 注意 line; the account is switched, not failed, and preferences.json is not blamed (S12)", () =>
            {
                WithPrefsRoot("prev_unreadable", (prefsRoot, appData) =>
                {
                    string held = PrefsAccount(prefsRoot, "1_held", Preferences.Japanese), denied = PrefsAccount(prefsRoot, "2_denied", Preferences.Japanese);
                    string good = PrefsAccount(prefsRoot, "3_good", Preferences.Japanese);
                    string records = Path.Combine(appData, "previous-language");
                    Directory.CreateDirectory(records);
                    foreach (var id in new[] { "1_held", "2_denied", "3_good" })
                        File.WriteAllText(Path.Combine(records, id + ".txt"), "1"); // French
                    string deniedRecord = Path.Combine(records, "2_denied.txt");
                    var acl = File.GetAccessControl(deniedRecord);
                    var deny = new System.Security.AccessControl.FileSystemAccessRule(System.Security.Principal.WindowsIdentity.GetCurrent().User,
                        System.Security.AccessControl.FileSystemRights.ReadData, System.Security.AccessControl.AccessControlType.Deny);
                    acl.AddAccessRule(deny);
                    File.SetAccessControl(deniedRecord, acl);
                    var log = new List<string>();
                    Patcher.LanguageResult res;
                    try
                    {
                        using (new FileStream(Path.Combine(records, "1_held.txt"), FileMode.Open, FileAccess.Read, FileShare.None))
                            res = Util.Retry(() => new Patcher(prefsRoot, log.Add).RestoreLanguage());
                    }
                    finally
                    {
                        acl.RemoveAccessRule(deny);
                        File.SetAccessControl(deniedRecord, acl);
                    }
                    T.Eq(0, res.Failures.Count, "no failure: " + string.Join(" / ", res.Failures));
                    T.Eq(3, res.Changed, "all three switched back");
                    T.Eq((long)Preferences.English, Preferences.ReadLanguage(held).Selected, "held record: English");
                    T.Eq((long)Preferences.English, Preferences.ReadLanguage(denied).Selected, "read-denied record: English");
                    T.Eq(1L, Preferences.ReadLanguage(good).Selected, "readable record: French");
                    T.Eq(5, log.Count, "log: " + string.Join(" / ", log));
                    T.Eq("表示言語を英語に戻しました（1_held）", log[0], "held: switched");
                    T.True(log[1].StartsWith("注意: 元の表示言語の記録を読み取れないため、英語に戻しました（", StringComparison.Ordinal) && log[1].EndsWith("）", StringComparison.Ordinal)
                        && log[1].Contains("1_held.txt"), "held: the note gives the reason: " + log[1]);
                    T.Eq("表示言語を英語に戻しました（2_denied）", log[2], "read denied: switched");
                    T.True(log[3].StartsWith("注意: 元の表示言語の記録を読み取れないため、英語に戻しました（", StringComparison.Ordinal) && log[3].Contains("2_denied.txt"),
                        "read denied: the note gives the reason: " + log[3]);
                    T.Eq("表示言語をフランス語に戻しました（3_good）", log[4], "readable record: French");
                    T.True(log.All(l => !l.Contains("設定ファイル")), "preferences.json is not blamed: " + string.Join(" / ", log));

                    // a file where the record folder belongs: no record can exist, so English without a note (and no folder is created)
                    PrefsAccount(prefsRoot, "1_held", Preferences.Japanese);
                    Ctx.ForceDelete(records);
                    File.WriteAllText(records, "a file where the folder belongs");
                    log.Clear();
                    res = Util.Retry(() => new Patcher(prefsRoot, log.Add).RestoreLanguage());
                    T.Eq(0, res.Failures.Count, "record folder is a file: no failure: " + string.Join(" / ", res.Failures));
                    T.Eq((long)Preferences.English, Preferences.ReadLanguage(held).Selected, "record folder is a file: English");
                    T.Eq("表示言語を英語に戻しました（1_held）", string.Join(" / ", log), "record folder is a file: log");
                    T.True(File.Exists(records), "record folder is a file: left as it is");
                });
            });

            r.Test("Patcher: SetLanguage / RestoreLanguage remove the leftovers of an interrupted preferences.json write (also of an account they do not change), e.g. a temp file a scanner held (S13)", () =>
            {
                WithPrefsRoot("prefs_leftovers", (prefsRoot, appData) =>
                {
                    string en = PrefsAccount(prefsRoot, "1_en", Preferences.English), ja = PrefsAccount(prefsRoot, "2_ja", Preferences.Japanese);
                    string[] Leftovers(string p) => Directory.GetFiles(Path.GetDirectoryName(p)).Select(Path.GetFileName).Where(n => n != "preferences.json").OrderBy(n => n).ToArray();
                    // the R13 case: a scanner holds the new preferences.json.tpcjp-tmp without FILE_SHARE_DELETE for longer than SafeFile's retries
                    FileStream holder = null;
                    var log = new List<string>();
                    Patcher.LanguageResult res = null;
                    try
                    {
                        SafeFileTests.WithBeforeRename(t => holder = holder ?? new FileStream(t, FileMode.Open, FileAccess.Read, FileShare.Read),
                            () => res = Util.Retry(() => new Patcher(prefsRoot, log.Add).SetLanguage(Preferences.Japanese)));
                    }
                    finally
                    {
                        holder?.Dispose();
                    }
                    T.True(holder != null, "the temp file was held");
                    T.Eq(1, res.Failures.Count, "the held write fails: " + string.Join(" / ", res.Failures));
                    // SafeFile's message names preferences.json and the reason already: shown as it is (no nested brackets)
                    T.True(res.Failures[0].StartsWith("表示言語を変更できませんでした（1_en）: preferences.json を置き換えられません。別のプログラム", StringComparison.Ordinal),
                        "the held write fails: " + res.Failures[0]);
                    T.Eq((long)Preferences.English, Preferences.ReadLanguage(en).Selected, "the held write failed: still English");
                    T.Eq("preferences.json" + SafeFile.TempSuffix, string.Join(",", Leftovers(en)), "the held temp file stayed next to preferences.json");
                    // RestoreLanguage does not change an English account, but removes its leftover (and a File.Replace copy of 1.0.0)
                    File.WriteAllBytes(Path.Combine(Path.GetDirectoryName(ja), "preferences.json~RF1a2b.TMP"), new byte[10]);
                    File.WriteAllBytes(Path.Combine(Path.GetDirectoryName(ja), "preferences.json.bak"), new byte[10]); // not a leftover
                    log.Clear();
                    res = Util.Retry(() => new Patcher(prefsRoot, log.Add).RestoreLanguage());
                    T.Eq(0, res.Failures.Count, "restore: no failure: " + string.Join(" / ", res.Failures));
                    T.Eq(0, Leftovers(en).Length, "restore: the English account's temp file removed");
                    T.Eq("preferences.json.bak", string.Join(",", Leftovers(ja)), "restore: the ~RF copy removed, other files kept");
                    T.Contains(string.Join(" / ", log), "前回中断したときの一時ファイルを 1 個削除しました（1_en）", "restore: reported for 1_en");
                    T.Contains(string.Join(" / ", log), "前回中断したときの一時ファイルを 1 個削除しました（2_ja）", "restore: reported for 2_ja");
                    // SetLanguage on an account that is already Japanese: no write, the leftover is removed all the same; one
                    // still held open is skipped without a failure
                    PrefsAccount(prefsRoot, "1_en", Preferences.Japanese);
                    PrefsAccount(prefsRoot, "2_ja", Preferences.Japanese); // restore switched it to English
                    string tmp1 = en + SafeFile.TempSuffix, tmp2 = ja + SafeFile.TempSuffix;
                    File.WriteAllBytes(tmp1, new byte[10]);
                    File.WriteAllBytes(tmp2, new byte[10]);
                    File.SetAttributes(tmp1, FileAttributes.ReadOnly);
                    log.Clear();
                    using (new FileStream(tmp2, FileMode.Open, FileAccess.Read, FileShare.None))
                        res = Util.Retry(() => new Patcher(prefsRoot, log.Add).SetLanguage(Preferences.Japanese));
                    T.Eq(0, res.Failures.Count, "ja: no failure: " + string.Join(" / ", res.Failures));
                    T.Eq(2, res.Unchanged, "ja: both already Japanese");
                    T.True(!File.Exists(tmp1), "ja: the read-only leftover removed");
                    T.True(File.Exists(tmp2), "ja: the held leftover skipped");
                    T.Eq("前回中断したときの一時ファイルを 1 個削除しました（1_en） / 表示言語は既に日本語です（1_en） / 表示言語は既に日本語です（2_ja）", string.Join(" / ", log), "ja: log");
                    new Patcher(prefsRoot, null).GetStatus();
                    T.True(File.Exists(tmp2), "status removes nothing");
                    Util.Retry(() => new Patcher(prefsRoot, null).SetLanguage(Preferences.Japanese));
                    T.True(!File.Exists(tmp2), "removed by the next run once it is let go");
                });
            });

            r.Test("Patcher: a run killed after writing the new backup of an update (bundle = new backup, old state) -> status Reverted, apply patches it, restore keeps it", () =>
            {
                foreach (bool restore in new[] { false, true })
                {
                    var g = ctx.NewGame("killed_after_backup_" + restore, official);
                    Apply(g, data);
                    var oldState = File.ReadAllBytes(g.State);
                    g.Update(updated);
                    File.WriteAllBytes(g.Backup, updated);
                    T.Bytes(oldState, File.ReadAllBytes(g.State), "old state");
                    T.Eq(Patcher.BundleState.Reverted, g.Patcher().GetStatus(data).State, "status");
                    g.Log.Clear();
                    if (!restore)
                    {
                        Check(Apply(g, data), Expect(updated, data), "apply");
                        T.Contains(g.LogText, "公式の日本語データにパッチを適用します", "apply log");
                        T.Eq(updatedSha, g.ReadState()["original_sha256"], "state original");
                        T.Eq(Util.UpdatedVersion, g.ReadState()["game_version"], "state game_version");
                        Restore(g, data);
                    }
                    else
                    {
                        Restore(g, data);
                        T.Contains(g.LogText, "日本語データは公式のままです", "restore log");
                    }
                    T.Eq(updatedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "the update's official file at the end");
                    T.True(!File.Exists(g.Backup) && !File.Exists(g.State), "backup and state removed");
                }
            });

            r.Test("Patcher: apply removes leftovers of an interrupted run (<file>.tpcjp-tmp, <file>~RF*.TMP of bundle, backup and state), keeps other files", () =>
            {
                var g = ctx.NewGame("leftovers_apply", official);
                Apply(g, data);
                string dir = GameLocator.BundlesDir(g.Dir);
                var leftovers = new[]
                {
                    g.Bundle + SafeFile.TempSuffix, g.Bundle + "~RF38b3fbc.TMP", g.Backup + SafeFile.TempSuffix,
                    g.Backup + "~RF38a219a.TMP", g.State + SafeFile.TempSuffix, g.State + "~RF38c76d5.TMP",
                };
                var others = new[] { Path.Combine(dir, "other_bundle.bundle"), Path.Combine(dir, "other_bundle.bundle~RF1.TMP"), g.Bundle + ".bak" };
                foreach (var f in leftovers.Concat(others))
                    File.WriteAllBytes(f, patched.Take(5000).ToArray());
                File.SetAttributes(leftovers[1], FileAttributes.ReadOnly);
                T.Eq(Patcher.BundleState.Patched, g.Patcher().GetStatus(data).State, "status (leftovers do not matter)");
                T.Eq(leftovers.Length, g.Leftovers().Count(n => !n.StartsWith("other_", StringComparison.Ordinal)), "status removed nothing");
                g.Log.Clear();
                var res = Apply(g, data);
                Check(res, offModel, "apply");
                T.Contains(g.LogText, "前回中断したときの一時ファイルを 6 個削除しました", "log");
                foreach (var f in leftovers)
                    T.True(!File.Exists(f), "removed: " + Path.GetFileName(f));
                foreach (var f in others)
                    T.True(File.Exists(f), "kept: " + Path.GetFileName(f));
                T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "bundle");
                T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), "backup");
                g.Log.Clear();
                Apply(g, data);
                T.True(!g.LogText.Contains("一時ファイル"), "no clean-up message when there is nothing to remove");
            });

            r.Test("Patcher: restore removes the leftovers as well; a leftover held open is skipped and the run still completes", () =>
            {
                var g = ctx.NewGame("leftovers_restore", official);
                Apply(g, data);
                File.WriteAllBytes(g.Bundle + "~RF1.TMP", official);
                File.WriteAllBytes(g.Backup + SafeFile.TempSuffix, official);
                File.WriteAllBytes(g.State + "~RF2.TMP", new byte[10]);
                using (new FileStream(g.Bundle + "~RF1.TMP", FileMode.Open, FileAccess.Read, FileShare.None))
                {
                    g.Log.Clear();
                    Restore(g, data);
                    T.Contains(g.LogText, "前回中断したときの一時ファイルを 2 個削除しました", "log (the open one is skipped)");
                    T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), "restored");
                    T.True(!File.Exists(g.Backup) && !File.Exists(g.State), "backup and state removed");
                }
                T.Eq(1, g.Leftovers().Length, "the open leftover is still there");
                Apply(g, data);
                T.Eq(0, g.Leftovers().Length, "removed by the next run");
                T.Contains(g.LogText, "前回中断したときの一時ファイルを 1 個削除しました", "log of the next run");
            });

            r.Test("Patcher: the files a 1.0.0 apply killed inside File.Replace leaves (F-SBP-7): apply and restore clean up and, without a state file, refuse until a Steam verification (S1); then both recover", () =>
            {
                foreach (string op in new[] { "Apply", "Restore" })
                {
                    // bundle missing, original parked as <bundle>~RF*.TMP, patched output in <bundle>.tpcjp-tmp, backup written, no state
                    var g = ctx.NewGame("interrupted_100_" + op, official);
                    File.WriteAllBytes(g.Backup, official);
                    File.Move(g.Bundle, g.Bundle + "~RF38b3fbc.TMP");
                    File.WriteAllBytes(g.Bundle + SafeFile.TempSuffix, patched);
                    T.Eq(Patcher.BundleState.Missing, g.Patcher().GetStatus(data).State, op + ": status before");
                    g.Log.Clear();
                    // round 3c (S1): without a state file nothing says which game version the backup is for: the leftovers are
                    // removed (first), the backup is not used and nothing else changes
                    var e = T.Throws<PatchException>(() => Util.Retry(() => { if (op == "Apply") g.Patcher().Apply(data); else g.Patcher().Restore(data); }), op + ": refused");
                    T.Contains(e.Message, $"バックアップがどのバージョンのゲームのものか確認できません（記録: 不明、現在のゲームのバージョン: {Game.DefaultVersion}）。バックアップは削除せずに残しました。", op + ": message");
                    T.Eq("前回中断したときの一時ファイルを 2 個削除しました", g.LogText, op + ": log");
                    T.Eq(0, g.Leftovers().Length, op + ": no leftovers");
                    T.True(!File.Exists(g.Bundle) && !File.Exists(g.State), op + ": no bundle or state made up");
                    T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), op + ": backup kept");
                    File.WriteAllBytes(g.Bundle, official); // "verify integrity of game files"
                    g.Log.Clear();
                    if (op == "Apply")
                    {
                        Check(Apply(g, data), offModel, op);
                        T.Eq(patchedSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), op + ": bundle patched");
                        T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Backup)), op + ": backup");
                        T.Eq(Game.DefaultVersion, g.ReadState()["game_version"], op + ": the identified version is recorded");
                        T.Eq(Patcher.BundleState.Patched, g.Patcher().GetStatus(data).State, op + ": status after");
                    }
                    else
                    {
                        Restore(g, data);
                        T.Contains(g.LogText, "日本語データは公式のままです", op + ": log after the verification");
                        T.Eq(officialSha, Util.Sha256(File.ReadAllBytes(g.Bundle)), op + ": bundle official");
                        T.True(!File.Exists(g.Backup) && !File.Exists(g.State), op + ": backup and state removed");
                        T.Eq(Patcher.BundleState.Original, g.Patcher().GetStatus(data).State, op + ": status after");
                    }
                    T.Eq(0, g.Leftovers().Length, op + ": no leftovers");
                    T.Eq(op == "Apply" ? 3 : 1, Directory.GetFiles(GameLocator.BundlesDir(g.Dir)).Length, op + ": files in the folder");
                }
            });
        }
    }
}
