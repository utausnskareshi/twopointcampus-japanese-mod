"""Development helper: apply our translations to the installed Japanese localisation bundle.

The official bundle is kept as <bundle>.tpcjp-original next to it (created on first apply, or
replaced when the game updated the bundle, only if its SHA-256 matches the extracted official data).
The backup is used while the installed bundle is the backup or our output, and also for a missing,
unreadable or (same game version) modified bundle when the state file vouches for it, which needs the
installed game version recorded as the backup's (no state file or a state of TpcJpPatch 1.0.0 cannot);
a damaged or patched backup is never used, nor is a bundle in a format TpcJpPatch.exe does not read
(tpcloc/official.py). apply records its output in <bundle>.tpcjp-state like TpcJpPatch.exe: game_version
(the version the backup was taken for) is written for a new backup or an installed bundle identical to
it, refreshed (never added) when the installed bundle was our output, and kept as it is when only the
state file vouched for the backup; an unreadable or empty version.txt never replaces a known version
except for a new official file. restore removes both files, also when the installed bundle is the
official file recorded in the state file (it is not overwritten).
Only terms whose official Japanese is missing (the worklist) are replaced.

usage:
  python tools/dev_patch.py apply [--extra overrides.json]   # overrides: {source: {key: text}}
  python tools/dev_patch.py restore
  python tools/dev_patch.py status
"""
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tpcloc import bundles, i2cache, official, paths, translations  # noqa: E402


def game_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq TPC.exe", "/NH"], capture_output=True, text=True).stdout
    return "TPC.exe" in out


def merged_texts(original: dict[str, str], extra: dict) -> tuple[dict[str, str], int]:
    worklist = json.loads((paths.WORK_DIR / "worklist.json").read_text("utf-8"))
    need = {(w["source"], w["key"]) for w in worklist}
    ours = translations.load_all()
    out, count = {}, 0
    for source, text in original.items():
        repl = {k: e["ja"] for k, e in ours.get(source, {}).items() if (source, k) in need and e.get("ja")}
        repl.update(extra.get(source, {}))
        if not repl:
            continue
        pairs = [(k, repl.get(k, v)) for k, v in i2cache.parse(text)]
        count += sum(1 for k, _ in i2cache.parse(text) if k in repl)
        out[source] = i2cache.dumps(pairs)
    return out, count


def copy_verified(src: Path, dst: Path) -> None:
    """Copy src over dst through a temporary file and check the result byte for byte (by SHA-256)."""
    tmp = dst.with_name(dst.name + ".tpcjp-tmp")
    shutil.copy2(src, tmp)
    tmp.replace(dst)
    if bundles.sha256(dst) != bundles.sha256(src):
        sys.exit(f"copy check failed: {dst}; verify the game files in Steam")


def official_source(bundle: Path) -> tuple[Path, str]:
    try:
        return official.official_japanese(bundle)
    except official.OfficialBundleError as e:
        sys.exit(f"error: {e}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    game = paths.find_game_dir()
    bundle = paths.loc_bundle(game, "Japanese")
    backup = official.backup_path(bundle)
    extract = json.loads((paths.WORK_DIR / "extract" / "strings.json").read_text("utf-8"))
    official_sha = extract["bundles"]["Japanese"]["sha256"]
    if cmd == "status":
        cur = bundles.sha256(bundle) if bundle.exists() else None
        print("bundle:", bundle)
        print("state:", "missing" if cur is None else "original" if cur == official_sha else "patched/modified",
              "| backup exists:", backup.exists())
        try:
            print("official data:", official.official_japanese(bundle)[1])
        except official.OfficialBundleError as e:
            print("official data: ERROR:", e)
        return
    if game_running():
        sys.exit("Two Point Campus is running; close it first")
    if cmd == "restore":
        source, reason = official_source(bundle)
        if source != backup:
            if bundle.exists() and (official.read_state(bundle) or {}).get("original_sha256") == bundles.sha256(bundle):
                # like TpcJpPatch.exe: the official file recorded in the state file (e.g. after Steam's file
                # verification) is not overwritten by a missing or other backup
                removed = [f.name for f in (backup, official.state_path(bundle)) if f.exists()]
                backup.unlink(missing_ok=True)
                official.state_path(bundle).unlink(missing_ok=True)
                print(f"{reason}\nthe installed bundle is already the official one (not restored); "
                      f"removed: {', '.join(removed)}")
                return
            if not backup.exists():
                sys.exit("no backup found")
            sys.exit(f"{reason}\nnot restoring: the installed bundle is newer than the backup; keeping it")
        if reason.startswith("WARNING"):
            print(reason)
        if bundle.exists() and bundles.sha256(bundle) == bundles.sha256(backup):
            print("bundle is already the official one (identical to the backup)")
            return
        copy_verified(backup, bundle)
        backup.unlink()
        official.state_path(bundle).unlink(missing_ok=True)
        print("restored official bundle (verified against the backup; backup and state removed); "
              "matches extract:", bundles.sha256(bundle) == official_sha)
        return
    if cmd == "apply":
        source, reason = official_source(bundle)
        state = official.read_state(bundle) or {}
        cur = bundles.sha256(bundle) if bundle.exists() else None
        if source != backup:
            # No backup, or a stale one: the installed bundle is official and becomes the backup.
            if bundles.sha256(bundle) != official_sha:
                if backup.exists():
                    sys.exit(f"{reason}\nthe installed bundle differs from work/extract; "
                             "run tools/extract.py and tools/worklist.py first")
                sys.exit("current bundle is not the official one and no backup exists; verify game files in Steam")
            copy_verified(bundle, backup)
            print("backup created:", backup)
        elif reason.startswith("WARNING"):
            print(reason)
        bak = bundles.sha256(backup)
        extra = {}
        if "--extra" in sys.argv:
            extra = json.loads(Path(sys.argv[sys.argv.index("--extra") + 1]).read_text("utf-8"))
        original = bundles.read_texts(backup)
        texts, count = merged_texts(original, extra)
        if not texts:
            sys.exit("nothing to apply")
        bundles.write_texts(backup, bundle, texts)
        out = bundles.sha256(bundle)
        # Record the output so this tool, extract.py and TpcJpPatch.exe recognise it as ours.
        updates = {
            "patch_version": "dev_patch",
            "original_sha256": bak,
            "patched_sha256": out,
            "applied": str(count),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        # game_version is the version the backup was taken for (like Patcher.Apply): written for a new backup or an
        # installed bundle identical to the backup; when the installed bundle was our output of this backup, only a
        # recorded version is refreshed; when only the state file vouched for the backup (WARNING: missing,
        # unreadable or same-version modified bundle; since round 3c only a state recording the installed version
        # can) it is kept, so that the new official file of a later game update is not taken for a modified file of
        # the backup's version.
        # An unreadable or empty version.txt never replaces a known version there (R3), nor when the official file
        # recorded in the state file is backed up again (S3); only a new official file always takes version.txt.
        describes = cur is not None and state.get("patched_sha256") == cur
        version = official.installed_version(bundle)
        fresh = version not in ("", official.UNKNOWN_VERSION) or state.get("game_version") in (None, "", official.UNKNOWN_VERSION)
        if source != backup:
            if fresh or state.get("original_sha256") != cur:
                updates["game_version"] = version
        elif cur == bak and not describes:
            if fresh:
                updates["game_version"] = version
        elif "game_version" in state and (describes or (cur == out and not reason.startswith("WARNING"))):
            if fresh:
                updates["game_version"] = version
        official.write_state(bundle, updates)
        print(f"patched {count} entries in {len(texts)} sources -> {bundle.name}")
        return
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
