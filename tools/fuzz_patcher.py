"""Fuzz tests for TpcJpPatch.exe in sandbox folders (the real game and settings are never touched).

Five suites, each case reproducible from (--seed, suite, index):
  bundle  mutated copies of the official / patched Japanese bundle (byte flips in the header, block info and
          data, truncation, appended bytes, zeroed ranges, swapped / duplicated / dropped blocks, damaged fields
          inside the serialized file and its TextAssets) in a fresh sandbox game folder, with and without an
          existing patch; runs "apply --game <sandbox> --no-language" / "restore --game <sandbox> --keep-language",
          then "status --game <sandbox>". A damaged "update" keeps version.txt (not a real update: the exe must
          rebuild from the backup and keep it) or changes it (a real game update).
  update  valid game-side changes after a patch: Steam file verification (the official file is back: status
          "公式のデータに戻っています"), a different but valid bundle with the same version.txt (rebuilt / restored
          from the backup, backup untouched) or with a new version.txt (new backup), lost or old state files.
          Round 2: a bundle lost while it was replaced (maybe with "<name>~RF*.TMP" / "<name>.tpcjp-tmp" leftovers):
          apply rebuilds it from the backup, restore restores it; leftovers are removed; no backup -> apply exits 1.
  data    mutated copies of dist/data/TpcJpPatch.data.tsv passed with --data (bad header, wrong field counts,
          bad escapes, invalid UTF-8, huge lines, empty files, injected separators, ...).
  datacut the data file without a final newline (last line cut inside the translation, final newline removed, CR
          only, BOM, ...: refused as "途中で切れています" even though #entries matches) and complete files with
          CRLF / extra blank lines (accepted).
The status line after the runs is checked in full (round 2: a bundle without a matching state file is classified by
its content: "日本語化パッチ適用済み（状態の記録なし）" / "壊れているか読み込めません").
Round 3: the damaged / patched status texts say whether restore could use the backup ("…（Steam の整合性の確認で直せます）",
"…（バックアップなし。…）"), a bundle without backup and state file is read too, the official file recorded in the state file
is "公式のデータに戻っています"; where only the state file vouches for the backup (bundle missing, damaged, or changed under
the same game version) the backup must have the recorded hash and game version (else exit 1, nothing changed); the state's
game_version is the version the backup was taken for; U+0000 in the data is refused; container_problem mirrors the
strict LocalisationBundle checks (strict LZ4, flags, serialized-file metadata / type trees / objects / container).
  prefs   mutated copies of a real preferences.json in a fake Cloud folder (TPCJP_PREFS_ROOT) with
          "language ja" / "language previous" / "language en", and "apply" / "restore" without
          --no-language / --keep-language on a sandbox game (Debug exe only).
  concurrency  one TpcJpPatch at a time (named mutex Local\\TpcJpPatch): the mutex held by the harness (the exe
          must exit 1 "別の TpcJpPatch が実行中です" and change nothing; status still works), an abandoned mutex,
          and 2-4 commands started at once on one sandbox / Cloud folder (the result must be reachable by
          running the commands that did not report "busy" one after another).

Invariants checked after every run: exit code 0 or 1 (never 2, never a crash), no "予期しないエラー",
no *.tpcjp-tmp left, nothing changed when the exe exits 1, no backup made from a file the exe reported as
damaged, and every file written on success loads (tools/tpcloc/bundles.read_texts, MessagePack) with the
expected content (an independent Python model of the fill-only patching).

The exe allows one TpcJpPatch at a time, so the workers run the exe one at a time (a harness mutex); the Python
checks still run in parallel. A run that hits another TpcJpPatch ("busy") must change nothing; it is retried.
TpcJpPatch.exe processes that do not belong to this harness (other test suites) pause the campaign for
QUIET_SECONDS, so that this harness does not make their runs fail.

Safety: every apply/restore names a sandbox under %TEMP%/tpcjp_fuzz with --game and adds --no-language /
--keep-language, except in the prefs and concurrency suites, which switch the language of the fake Cloud folder
(TPCJP_PREFS_ROOT) only. The Debug exe is used with TPCJP_APPDATA / TPCJP_PREFS_ROOT inside the temp root,
because the Release exe always appends to %LOCALAPPDATA%/TpcJpPatch/TpcJpPatch.log. The tool refuses to run
with any exe other than a Debug build.

usage: python tools/fuzz_patcher.py [--iterations N] [--seed S] [--only bundle,update,data,datacut,prefs,concurrency]
                                    [--bundle-cases N] [--update-cases N] [--data-cases N] [--datacut-cases N]
                                    [--prefs-cases N] [--concurrency-cases N] [--start I] [--workers N] [--resume]
                                    [--case SUITE:INDEX] [--keep] [--keep-failed] [--compare OLD.jsonl]
  --iterations N   bundle N, update N/6, data N/2, datacut N/30, prefs N, concurrency N/10 cases (default 300)
  --case S:I       run one case (e.g. bundle:17) with verbose output and keep its sandbox
                   (%TEMP%/tpcjp_fuzz/cases/<suite>_<index>, with input.bundle / data.tsv / input/<account>)
  --resume         skip cases already recorded in %TEMP%/tpcjp_fuzz/results-<seed>.jsonl
  --compare F      results file of an earlier run with the same seed: its failing cases must have the same input
                   (mutation) now and pass (the fixed findings FZ-01..FZ-12)
Results: %TEMP%/tpcjp_fuzz/results-<seed>.jsonl (every case with its seed) and summary-<seed>.json (groups).
The findings FZ-01..FZ-12 (work/localtest/results/fuzz.json) are fixed: their problem codes are no longer expected
failures (FIXED_FINDINGS only labels a recurrence as a regression). Exit code: 1 when any problem is found.
"""
import argparse
import contextlib
import ctypes
import ctypes.wintypes
import hashlib
import itertools
import json
import os
import random
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tpcloc import bundles, paths  # noqa: E402

import lz4.block  # noqa: E402  (UnityPy dependency)
import msgpack  # noqa: E402  (requirements.txt)

DEFAULT_SEED = 20261021
ROOT = Path(tempfile.gettempdir()) / "tpcjp_fuzz"
DEBUG_EXE = paths.PROJECT_ROOT / "patcher" / "TpcJpPatch" / "bin" / "Debug" / "net48" / "TpcJpPatch.exe"
DATA = paths.PROJECT_ROOT / "dist" / "data" / "TpcJpPatch.data.tsv"
OFFICIAL = paths.WORK_DIR / "backup" / "official" / "localisation_assets_localisationjapanese.bundle"
PREFS_COPY = paths.WORK_DIR / "localtest" / "state" / "Cloud_backup"
BUNDLE_REL = Path("TPC_Data/StreamingAssets/aa/StandaloneWindows64/localisation_assets_localisationjapanese.bundle")
GAME_VERSION = "10.3.169253+2024-12-06.1241"
NEW_GAME_VERSION = "10.4.171000+2025-03-04.0915"  # a simulated game update changes version.txt as well
UNEXPECTED = "予期しないエラー"
RUNNING = "Two Point Campus が起動しています"
BUSY = "別の TpcJpPatch が実行中です"
STATE_WARNING = "注意: 状態ファイルを書き込めませんでした"
DATA_UNREADABLE = "翻訳データを読み込めません"
RESTORE_DATA_FALLBACK = "注意: 翻訳データを読み込めないため、状態ファイルだけで判断します"
NO_JAPANESE_ACCOUNT = "表示言語が日本語のアカウントはないため、表示言語は変更しませんでした"
STATUS_TEXT = {"missing": "見つかりません", "original": "公式のまま", "reverted": "公式のデータに戻っています",
               "patched": "日本語化パッチ適用済み", "unknown": "ゲーム側で更新されています"}
# the whole "日本語データ: ..." line of each state (MainForm.DescribeState; round-2 fix: CS-08 shorter Reverted text,
# F-SBP-6 / CS-07 / pytests-E2E1: a bundle without a matching state file is classified by its content)
STATUS_LINES = {
    "missing": "見つかりません（ゲームのフォルダーを確認してください）",
    "original": "公式のまま（パッチ未適用）",
    "reverted": "公式のデータに戻っています（「日本語化する」で適用し直せます）",
    "patched_nostate": "日本語化パッチ適用済み（状態の記録なし）",
    "damaged": "壊れているか読み込めません（「元に戻す」か Steam の整合性の確認で直せます）",
    "unknown": "ゲーム側で更新されています（「日本語化する」で適用し直してください）",
    # round 3 (CS-09 / F-SBP-9): no backup restore could use
    "patched_nobackup": "日本語化パッチ適用済み（バックアップなし。Steam の整合性の確認で戻せます）",
    "damaged_nobackup": "壊れているか読み込めません（Steam の整合性の確認で直せます）",
    "unsupported": "このパッチが対応していない形式です（パッチの更新をお待ちください）",  # round 3c (S2/S8)
}
PATCHED_LINE_RX = re.compile(r"日本語化パッチ適用済み（(.*)、翻訳 ([0-9,?]+) 件、(.*)）")
TMP_SUFFIX = ".tpcjp-tmp"
RF_RX = re.compile(r"~RF[0-9A-Za-z]*\.TMP$", re.IGNORECASE)  # File.Replace backups (left by version 1.0.0)


def is_leftover(name: str) -> bool:
    """A leftover of an interrupted run, which apply / restore delete first (SafeFile.RemoveLeftovers)."""
    return name.endswith(TMP_SUFFIX) or bool(RF_RX.search(name))
SEP = "[i2t]"
JAPANESE_RX = re.compile("[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")
EXE_TIMEOUT = 180
RUNNING_RETRIES = 20
BUSY_RETRIES = 40
QUIET_SECONDS = 20
PRODUCT_MUTEX = "Local\\TpcJpPatch"
HARNESS_MUTEX = "Local\\tpcjp_fuzz_exe_slot"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def case_seed(seed: int, suite: str, index: int) -> int:
    return int.from_bytes(hashlib.sha256(f"{seed}:{suite}:{index}".encode()).digest()[:8], "big")


def no_error_dialogs():
    """Crashing children must not show a Windows Error Reporting dialog (the mode is inherited)."""
    try:
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except (AttributeError, OSError):
        pass


def under_root(p: Path) -> bool:
    try:
        Path(p).resolve().relative_to(ROOT.resolve())
        return True
    except ValueError:
        return False


def rmtree(p: Path):
    def onerror(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass
    if p.exists():
        assert under_root(p), p
        shutil.rmtree(p, onerror=onerror)


# =====================================================================================================
# running the exe
# =====================================================================================================

def exe_env(case_dir: Path) -> dict:
    env = os.environ.copy()
    env.pop("TPCJP_SIMULATE_NOT_INSTALLED", None)
    env["TPCJP_APPDATA"] = str(case_dir / "appdata")
    env["TPCJP_PREFS_ROOT"] = str(case_dir / "prefs")
    return env


PAUSE_FILE = ROOT / "pause_until"
WAIT_OBJECT_0, WAIT_ABANDONED, WAIT_TIMEOUT = 0, 0x80, 0x102


def _k32():
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.wintypes.BOOL, ctypes.c_wchar_p]
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.wintypes.DWORD]
    k32.WaitForSingleObject.restype = ctypes.wintypes.DWORD
    k32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    return k32


def pause_campaign(seconds=QUIET_SECONDS):
    """Another TpcJpPatch (not started by this harness) is active: let it finish its runs."""
    try:
        until = max(time.time() + seconds, float(PAUSE_FILE.read_text("ascii") or 0))
    except (OSError, ValueError):
        until = time.time() + seconds
    try:
        PAUSE_FILE.write_text(f"{until:.1f}", "ascii")
    except OSError:
        pass


def wait_for_quiet():
    while True:
        try:
            until = float(PAUSE_FILE.read_text("ascii") or 0)
        except (OSError, ValueError):
            return
        if time.time() >= until:
            return
        time.sleep(min(2.0, until - time.time() + 0.05))


class NamedMutex:
    """A Windows named mutex (abandoned on process death, so a crashed worker cannot block the others)."""

    def __init__(self, name, timeout_ms=0xFFFFFFFF):
        self.name, self.timeout_ms, self.h, self.owned, self.abandoned = name, timeout_ms, None, False, False

    def __enter__(self):
        k32 = _k32()
        self.h = k32.CreateMutexW(None, False, self.name)
        r = k32.WaitForSingleObject(self.h, self.timeout_ms)
        self.owned = r in (WAIT_OBJECT_0, WAIT_ABANDONED)
        self.abandoned = r == WAIT_ABANDONED
        return self

    def __exit__(self, *a):
        k32 = _k32()
        if self.owned:
            k32.ReleaseMutex(self.h)
        k32.CloseHandle(self.h)


@contextlib.contextmanager
def exe_slot():
    """The workers of this harness run the exe one at a time (the exe itself allows one TpcJpPatch at a time)."""
    # no time limit: the holder may wait a long time for other test suites (wait_for_quiet); a crashed worker
    # abandons the mutex, and every exe run is bounded by EXE_TIMEOUT
    with NamedMutex(HARNESS_MUTEX) as m:
        if not m.owned:
            raise RuntimeError("harness exe slot: wait failed")
        wait_for_quiet()
        yield


def snapshot_tree(folders) -> dict:
    """Every file below the folders (the exe's own log file excluded: it records a refused run as well)."""
    out = {}
    for d in folders:
        d = Path(d)
        if not d.exists():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.name != "TpcJpPatch.log":
                try:
                    out[str(p)] = p.read_bytes()
                except OSError:
                    out[str(p)] = b"<unreadable>"
            elif p.is_dir():
                out[str(p) + os.sep] = b"<dir>"
    return out


def tree_diff(a: dict, b: dict) -> list:
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def check_safe_args(exe: Path, args: list):
    assert "debug" in str(exe).lower(), "the fuzz tests use the Debug exe only (TPCJP_APPDATA / TPCJP_PREFS_ROOT hooks)"
    if args[0] in ("apply", "restore", "status"):
        gi = args.index("--game")
        assert under_root(Path(args[gi + 1])), "apply/restore/status must name a sandbox"


def run_exe(exe: Path, args: list, case_dir: Path, verbose=False, watch=(), slot=True, retry_busy=True) -> dict:
    """Runs the exe (one at a time with the other workers, unless slot=False); retries while a (fake) TPC.exe
    of another test runs or another TpcJpPatch holds the lock. A "busy" run must not change any file below
    `watch`. Returns rc/out/timeout/cmd/busy/busy_problems."""
    assert under_root(case_dir)
    check_safe_args(exe, args)
    env = exe_env(case_dir)
    cmd = [str(exe), *[str(a) for a in args]]
    busy, busy_probs, running = 0, [], 0
    while True:
        before = snapshot_tree(watch) if watch else None
        t0 = time.time()
        with (exe_slot() if slot else contextlib.nullcontext()):
            t0 = time.time()
            try:
                p = subprocess.run(cmd, capture_output=True, timeout=EXE_TIMEOUT, env=env, creationflags=CREATE_NO_WINDOW)
                rc, out, timed_out = p.returncode, p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace"), False
            except subprocess.TimeoutExpired as e:
                rc, timed_out = None, True
                out = (e.stdout or b"").decode("utf-8", "replace")
            elapsed = time.time() - t0
        if rc == 1 and BUSY in out and retry_busy and busy < BUSY_RETRIES:
            # another TpcJpPatch (not this harness: its runs are serialized) held the lock
            busy += 1
            if watch:
                changed = tree_diff(before, snapshot_tree(watch))
                if changed:
                    busy_probs.append(("busy_changed_files", f"'{BUSY}' (exit 1) but files changed: {changed[:6]}"))
            if UNEXPECTED in out or "エラー: " + BUSY not in out:
                busy_probs.append(("busy_message", out.strip()[-300:]))
            pause_campaign()
            if verbose:
                print(f"  (busy: another TpcJpPatch is running; retry {busy})")
            time.sleep(0.3 + random.random())
            continue
        if rc == 1 and RUNNING in out and running < RUNNING_RETRIES:
            running += 1
            time.sleep(40)
            continue
        break
    if verbose:
        print("  $ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        print("    " + out.strip().replace("\n", "\n    "))
        print(f"    -> exit {rc} ({elapsed:.1f}s)")
    return {"cmd": cmd, "rc": rc, "out": out, "timeout": timed_out, "elapsed": round(elapsed, 2),
            "game_running": rc == 1 and RUNNING in out, "busy": busy, "busy_problems": busy_probs,
            "busy_final": rc == 1 and BUSY in out}


def run_status(exe: Path, game: Path, case_dir: Path, verbose=False) -> dict:
    """status takes no lock: it runs outside the exe slot."""
    return run_exe(exe, ["status", "--game", str(game)], case_dir, verbose, slot=False, retry_busy=False)


def expected_status(game: Path) -> str:
    """Mirror of Patcher.GetStatus (by file hashes only; "unknown" is refined by content in check_status: the status
    command always has translation data, so since round 3 also a bundle without backup and state file is read)."""
    b, k, s = bundle_path(game), backup_path(game), state_path(game)
    if not b.exists():
        return "missing"
    cur = sha(b.read_bytes())
    st = read_state(s.read_bytes()) if s.exists() else None
    if st is not None and st.get("patched_sha256") == cur:
        # round 3b (R2): restore would refuse without the recorded backup
        if k.exists() and st.get("original_sha256", sha(k.read_bytes())) == sha(k.read_bytes()):
            return "patched"
        return "patched_nobackup"
    if k.exists() and sha(k.read_bytes()) == cur:
        return "reverted" if st is not None else "original"
    if st is not None and st.get("original_sha256") == cur:
        return "reverted"  # round 3: the official file recorded in the state file (the backup is missing or another file)
    return "unknown"


def backup_usable(game: Path) -> bool:
    """Mirror of Patcher.BackupUsable (round 3): restore could rebuild a damaged bundle from the backup — it loads (the
    exe's strict checks), holds no patch, and, when the state file records them, has the recorded hash and was taken for
    the installed game version."""
    k, s = backup_path(game), state_path(game)
    if not k.exists():
        return False
    kb = k.read_bytes()
    texts, _ = texts_for_status(k)
    if texts is None or container_problem(kb) is not None or any("\x00" in t for t in texts.values()) \
            or model_is_patched(texts, shared()["data"]):
        return False
    if not s.is_file():
        return False  # round 3c (S1): without a state file nothing vouches for the backup's game version
    st_raw = s.read_bytes()
    st = read_state(st_raw)
    if "original_sha256" in st and st["original_sha256"] != sha(kb):
        return False
    return same_version(st_raw, game)  # round 3c (S1): also False when the state has no game_version


def status_of_line(line: str):
    """The state named by a "日本語データ: ..." line (whole line compared), or None."""
    line = line.strip()
    for k, t in STATUS_LINES.items():
        if line == t:
            return k
    if PATCHED_LINE_RX.fullmatch(line):
        return "patched"
    return None


def check_status(r: dict, game: Path) -> list:
    """status runs with the data next to the exe or embedded in it (the same as dist/data: meta embedded_same), so a
    bundle that matches neither the state file nor the backup is classified by its content (round-2 fix):
    our output -> "状態の記録なし", a file that does not load -> "壊れているか読み込めません"."""
    probs = generic_problems(r)
    if r["rc"] != 0:
        return probs + [("status_failed", r["out"].strip()[-300:])]
    m = re.search(r"日本語データ: (.*)", r["out"])
    line = m.group(1).strip() if m else "?"
    got = status_of_line(line) if m else None
    if m and got is None:
        probs.append(("status_text_unknown", f"status line {line!r} is none of the texts of MainForm.DescribeState"))
        return probs
    want = expected_status(game)
    if want == "patched":
        st = read_state(state_path(game).read_bytes())
        pm = PATCHED_LINE_RX.fullmatch(line)
        try:
            applied = f"{int(st.get('applied', '')):,}"
        except ValueError:
            applied = "?"
        if pm and (pm.group(1) != st.get("patch_version", "?") or pm.group(2) != applied or pm.group(3) != st.get("time", "?")):
            probs.append(("status_patched_details", f"status line {line!r} does not show the state file's patch_version / "
                          f"applied / time ({st.get('patch_version')}, {applied}, {st.get('time')})"))
    if want != "unknown":
        if got != want:
            probs.append(("status_mismatch", f"status says {line!r}, the files say {want}"))
        return probs
    # round 3c (S2/S8): a format the exe does not read is not damage unless the state file proves the game version
    # unchanged (Patcher.IsNewFormat): its own status
    st_raw = state_path(game).read_bytes() if state_path(game).is_file() else None
    if unsupported_format(bundle_path(game).read_bytes()) and not same_version(st_raw, game):
        if got != "unsupported":
            probs.append(("status_unsupported", f"status says {line!r} for a bundle in a format the exe does not read "
                          f"(game version not proven unchanged); expected '{STATUS_LINES['unsupported']}'"))
        return probs
    # neither the state file nor the backup describes the bundle: decided by its content (round 3: the damaged and
    # patched texts say whether restore could use a backup; without backup and state file a loadable, unpatched file
    # is the official one)
    has_backup, has_state = backup_path(game).exists(), state_path(game).exists()
    want_damaged = "damaged" if has_backup and backup_usable(game) else "damaged_nobackup"
    texts, err = texts_for_status(bundle_path(game))
    if texts is None:
        if got != want_damaged:
            code = "status_damaged_advice" if got in ("damaged", "damaged_nobackup") else "status_damage_not_detected"
            probs.append((code, f"status says {line!r} for a bundle that does not load in UnityPy ({err}); expected "
                          f"'{STATUS_LINES[want_damaged]}'"))
        return probs
    patched = model_is_patched(texts, shared()["data"])
    if patched:
        want = "patched_nostate" if has_backup else "patched_nobackup"
    else:
        want = "original" if not has_backup and not has_state else "unknown"
    if got in ("damaged", "damaged_nobackup"):
        # the exe's checks are stricter than UnityPy's (LocalisationBundle.CheckStructure: file size, data blocks,
        # directory; since round 3 also strict LZ4, flags, the serialized file's metadata, type trees, object table and
        # AssetBundle container: container_problem / sf_problem; NUL text; the TextAssets of the official file):
        # "damaged" is wrong only for a file that passes all of them
        official = shared()["official_texts"]
        odd = set(texts) != set(official) or any("\x00" in t and "\x00" not in official.get(n, "") for n, t in texts.items())
        problem = container_problem(bundle_path(game).read_bytes())
        if not odd and problem is None:
            probs.append(("status_damaged_false", f"status says {line!r} for a bundle that loads with all TextAssets "
                          f"(content: {want})"))
        elif got != want_damaged:
            probs.append(("status_damaged_advice", f"status says {line!r}; expected '{STATUS_LINES[want_damaged]}'"))
    elif got != want:
        probs.append(("status_mismatch", f"status says {line!r}; by content (no matching state file) the bundle is {want}"))
    return probs


# ---- other TpcJpPatch processes (other test suites): pause this campaign while they run ----

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.wintypes.DWORD), ("cntUsage", ctypes.wintypes.DWORD),
                ("th32ProcessID", ctypes.wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.wintypes.DWORD), ("cntThreads", ctypes.wintypes.DWORD),
                ("th32ParentProcessID", ctypes.wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def process_table() -> list:
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    h = k32.CreateToolhelp32Snapshot(0x2, 0)
    out = []
    if not h or h == ctypes.c_void_p(-1).value:
        return out
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(ctypes.c_void_p(h), ctypes.byref(e))
        while ok:
            out.append((e.th32ProcessID, e.th32ParentProcessID, e.szExeFile))
            ok = k32.Process32NextW(ctypes.c_void_p(h), ctypes.byref(e))
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))
    return out


class ForeignWatcher:
    """Polls for TpcJpPatch.exe processes that this harness did not start and pauses the campaign."""

    def __init__(self):
        import threading
        self.stop = threading.Event()
        self.seen = set()
        self.examples = []
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def loop(self):
        me = os.getpid()
        while not self.stop.wait(0.05):
            try:
                table = process_table()
            except OSError:
                continue
            parent = {pid: ppid for pid, ppid, _ in table}
            names = {pid: name for pid, _, name in table}
            for pid, ppid, name in table:
                if name.lower() != "tpcjppatch.exe":
                    continue
                anc, ours, chain = ppid, False, []
                for _ in range(4):  # started by this harness (the main process, a worker, a venv launcher)
                    if anc == me:
                        ours = True
                        break
                    chain.append(f"{anc}:{names.get(anc, '?')}")
                    anc = parent.get(anc)
                    if anc is None:
                        break
                if ours:
                    continue
                if pid not in self.seen and len(self.examples) < 10:
                    self.examples.append(" <- ".join(chain))
                self.seen.add(pid)
                pause_campaign()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.thread.join(2)


def generic_problems(r: dict) -> list:
    probs = list(r.get("busy_problems", []))
    if r.get("busy_final"):
        probs.append(("skipped_busy", f"another TpcJpPatch kept the lock ({r.get('busy')} retries)"))
    if r["timeout"]:
        probs.append(("hang", f"no exit within {EXE_TIMEOUT}s"))
    elif r["rc"] not in (0, 1):
        code = r["rc"] & 0xFFFFFFFF if isinstance(r["rc"], int) else r["rc"]
        probs.append((f"exit_code_{code:#x}" if isinstance(code, int) else "exit_code", f"exit code {r['rc']}"))
    if UNEXPECTED in r["out"]:
        m = re.search(UNEXPECTED + r": ([\w.]+)", r["out"])
        exc = m.group(1) if m else "?"
        frame = re.search(r"場所 TpcJpPatch\.([\w.]+)|at TpcJpPatch\.([\w.]+)", r["out"])
        where = (frame.group(1) or frame.group(2)) if frame else "?"
        probs.append((f"unexpected_error:{exc}@{where}", r["out"].strip()[-600:]))
    if r["rc"] == 1 and not r["timeout"] and not any(w in r["out"] for w in ("エラー", "ませんでした", "できません")):
        probs.append(("exit1_without_message", r["out"].strip()[-300:]))
    if r.get("game_running"):
        probs.append(("skipped_game_running", "TPC.exe kept running"))
    return probs


# =====================================================================================================
# UnityFS bundle model (parse / rebuild) used to build consistent mutations
# =====================================================================================================

def align16(n):
    return (n + 15) & ~15


class UFS:
    def __init__(self, b: bytes):
        self.b = b
        p = 0

        def cstr():
            nonlocal p
            e = b.index(b"\0", p)
            s = b[p:e]
            p = e + 1
            return s
        self.sig = cstr()
        self.ver, = struct.unpack_from(">I", b, p)
        p += 4
        self.uver, self.urev = cstr(), cstr()
        self.size_pos = p
        self.size, self.csz, self.usz, self.flags = struct.unpack_from(">qIII", b, p)
        p += 20
        self.hdr_end = p
        if self.ver >= 7:
            p = align16(p)
        self.bi_off = p
        bi = lz4.block.decompress(b[p:p + self.csz], uncompressed_size=self.usz)
        self.bi_len = len(bi)  # lz4 takes usz as an upper bound only
        p += self.csz
        if self.flags & 0x200:
            p = align16(p)
        self.data_off = p
        self.hash16 = bi[:16]
        n, = struct.unpack_from(">i", bi, 16)
        q = 20
        self.blocks = []
        for _ in range(n):
            self.blocks.append(struct.unpack_from(">IIH", bi, q))
            q += 10
        nd, = struct.unpack_from(">i", bi, q)
        q += 4
        self.dirs = []
        for _ in range(nd):
            off, size, fl = struct.unpack_from(">qqI", bi, q)
            q += 20
            e = bi.index(b"\0", q)
            self.dirs.append((off, size, fl, bi[q:e]))
            q = e + 1
        self.bi_extra = len(bi) - q  # round 3: the patcher refuses bytes after the directory list
        self.cblocks = []
        pos = self.data_off
        for u, c, f in self.blocks:
            self.cblocks.append(b[pos:pos + c])
            pos += c
        self._sf = None

    def sf(self) -> bytes:
        if self._sf is None:
            out = []
            for (u, c, f), cb in zip(self.blocks, self.cblocks):
                out.append(cb if f & 0x3F == 0 else lz4.block.decompress(cb, uncompressed_size=u))
            self._sf = b"".join(out)
        return self._sf

    @staticmethod
    def make_bi(hash16, entries, dirs, nblocks=None, ndirs=None) -> bytes:
        out = bytearray(hash16)
        out += struct.pack(">i", len(entries) if nblocks is None else nblocks)
        for u, c, f in entries:
            out += struct.pack(">IIH", u & 0xFFFFFFFF, c & 0xFFFFFFFF, f & 0xFFFF)
        out += struct.pack(">i", len(dirs) if ndirs is None else ndirs)
        for off, size, fl, name in dirs:
            out += struct.pack(">qq", s64(off), s64(size)) + struct.pack(">I", fl & 0xFFFFFFFF) + name + b"\0"
        return bytes(out)

    def build(self, cblocks, bi: bytes, *, size=None, csz=None, usz=None, flags=None, ver=None) -> bytes:
        bic = lz4.block.compress(bi, mode="high_compression", store_size=False)
        head = bytearray(self.sig + b"\0" + struct.pack(">I", self.ver if ver is None else ver & 0xFFFFFFFF)
                         + self.uver + b"\0" + self.urev + b"\0")
        size_pos = len(head)
        head += struct.pack(">qIII", 0, (len(bic) if csz is None else csz) & 0xFFFFFFFF,
                            (len(bi) if usz is None else usz) & 0xFFFFFFFF,
                            (self.flags if flags is None else flags) & 0xFFFFFFFF)
        out = head
        if self.ver >= 7:
            out += b"\0" * (align16(len(out)) - len(out))
        out += bic
        if self.flags & 0x200:
            out += b"\0" * (align16(len(out)) - len(out))
        for cb in cblocks:
            out += cb
        struct.pack_into(">q", out, size_pos, s64(len(out) if size is None else size))
        return bytes(out)

    def rebuild_sf(self, sf: bytes, block=0x20000, dir_size=None) -> bytes:
        cblocks, entries = [], []
        for i in range(0, max(len(sf), 1), block):
            chunk = sf[i:i + block]
            cb = lz4.block.compress(chunk, mode="high_compression", store_size=False)
            cblocks.append(cb)
            entries.append((len(chunk), len(cb), 3))
        if not sf:
            cblocks, entries = [], []
        off, size, fl, name = self.dirs[0]
        dirs = [(off, len(sf) if dir_size is None else dir_size, fl, name)] + self.dirs[1:]
        return self.build(cblocks, self.make_bi(self.hash16, entries, dirs))


VERSION_RX = re.compile(rb"\d+\.[0-9A-Za-z]+\.[0-9A-Za-z]+")


def container_problem(b: bytes):
    """Mirror of LocalisationBundle.CheckStructure plus the container facts the exe relies on (one directory entry,
    named and flagged like the official one; blocks that decompress to their sizes; the serialized file's own size
    field equal to its directory size) and, since round 3, of the strict checks of LocalisationBundle.Load (no bytes
    after the block info's directory list, block flags none / LZ4 / LZ4HC, and the serialized file: sf_problem).
    Returns a reason, or None when the container is like an official file."""
    reason, u, sf = _container(b)
    if reason is not None:
        return reason
    size = u.dirs[0][1]
    if len(sf) < 48:
        return "serialized file too short"
    # SerializedFile header (version >= 22): metadata size u32, file size u32, version u32, data offset u32, ...
    # with 64-bit fields at 0x18 (metadata size u32, file size i64, data offset i64)
    ver_sf, = struct.unpack_from(">I", sf, 8)
    fsize = struct.unpack_from(">q", sf, 0x18)[0] if ver_sf >= 22 else struct.unpack_from(">I", sf, 4)[0]
    if fsize != size:
        return f"serialized file size field {fsize} != directory size {size}"
    return sf_problem(sf)  # round 3: LocalisationBundle.SerializedFile.Check


def unsupported_format(b: bytes) -> bool:
    """Mirror of LocalisationBundle.Load's UnsupportedFormatException (round 3b R10), in the exe's order: the UnityFS
    format version first (before any other check), then, once the container passed CheckStructure / CheckBlocks, the
    serialized-file format of each directory entry flagged 4 with >= 20 bytes (the first offending one decides). Only
    plausible values count (version 1..99, endianness byte 1); anything else is damage."""
    if len(b) >= 12 and b[:8] == b"UnityFS\0":
        v = struct.unpack_from(">I", b, 8)[0]
        if not 6 <= v <= 8:
            return 0 < v < 100
    reason, u, sf = _container(b)
    if reason is not None:
        return False
    for off, size, flags, _ in u.dirs:
        if flags & 4 and size >= 20:
            v, e = struct.unpack_from(">I", sf, off + 8)[0], sf[off + 16]
            if v != 22:
                return 0 < v < 100
            if e != 0:
                return e == 1
    return False


def _container(b: bytes):
    """The container checks of container_problem (up to the blocks decompressing to their sizes): (reason, None, None)
    or (None, UFS, the decompressed directory data)."""
    try:
        u = UFS(b)
    except Exception as e:  # noqa: BLE001
        return f"container does not parse: {type(e).__name__}", None, None
    o = shared()["ufs"]["official"]
    if u.sig != b"UnityFS" or not VERSION_RX.fullmatch(u.uver) or not VERSION_RX.fullmatch(u.urev):
        return "signature / version strings", None, None
    if u.size != len(b):
        return f"declared size {u.size} != {len(b)}", None, None
    if u.flags & 0x80 or (u.flags & ~0x200) != (o.flags & ~0x200) or u.ver != o.ver:
        return "header flags / format version not modelled", None, None  # the official file has 0x243, the exe writes 0x43
    if u.data_off + sum(c for _, c, _ in u.blocks) != len(b):
        return "data blocks do not add up to the file", None, None
    unpacked = sum(x for x, _, _ in u.blocks)
    if any(off < 0 or size < 0 or off + size > unpacked for off, size, _, _ in u.dirs):
        return "directory outside the data", None, None
    if [(d[2], d[3]) for d in u.dirs] != [(d[2], d[3]) for d in o.dirs] or u.dirs[0][0] != 0:
        return "directory entries differ from the official file", None, None
    if u.bi_len != u.usz:
        return f"block info decompresses to {u.bi_len} bytes, header says {u.usz}", None, None
    if u.bi_extra:
        return f"block info has {u.bi_extra} bytes after the directory list", None, None
    if any(f & ~0x7F or f & 0x3F not in (0, 2, 3) for _, _, f in u.blocks):
        return "block flags not modelled", None, None  # round 3: compression none / LZ4 / LZ4HC and 0x40 only
    try:
        parts = [cb if f & 0x3F == 0 else lz4.block.decompress(cb, uncompressed_size=x)
                 for (x, c, f), cb in zip(u.blocks, u.cblocks)]
    except Exception as e:  # noqa: BLE001
        return f"blocks do not decompress: {type(e).__name__}", None, None
    if any(len(part) != x or len(cb) != c for part, cb, (x, c, f) in zip(parts, u.cblocks, u.blocks)):
        return "a block does not decompress to its size", None, None
    return None, u, b"".join(parts)


# ---- mirror of LocalisationBundle.SerializedFile.Check (round 3) ----
# sf_problem(sf) -> None, or a reason why the patcher refuses the serialized file.

_SF_PRIM = {"SInt8": 1, "UInt8": 1, "char": 1, "bool": 1, "short": 2, "SInt16": 2, "unsigned short": 2, "UInt16": 2,
            "int": 4, "SInt32": 4, "unsigned int": 4, "UInt32": 4, "Type*": 4, "float": 4, "long long": 8,
            "SInt64": 8, "unsigned long long": 8, "UInt64": 8, "FileSize": 8, "double": 8}
_SF_TEXTASSET = "1 string m_Name;2 Array Array;3 int size;3 char data;1 string m_Script;2 Array Array;3 int size;3 char data;"
_SF_AB_SHAPES = {
    "m_PreloadTable": "0 vector m_PreloadTable;1 Array Array;2 int size;2 PPtr<Object> data;3 int m_FileID;3 SInt64 m_PathID;",
    "m_Container": "0 map m_Container;1 Array Array;2 int size;2 pair data;3 string first;4 Array Array;5 int size;5 char data;"
                   "3 AssetInfo second;4 int preloadIndex;4 int preloadSize;4 PPtr<Object> asset;5 int m_FileID;5 SInt64 m_PathID;",
}


class _SFBad(Exception):
    pass


class _SFCur:
    """Bounded little-endian reader (positions in the serialized file); alignment padding must be zero."""

    def __init__(self, b, pos, end, what):
        self.b, self.p, self.end, self.what = b, pos, end, what

    def skip(self, n):
        if n < 0 or n > self.end - self.p:
            raise _SFBad(self.what)
        self.p += n
        return self.p - n

    def u8(self):
        return self.b[self.skip(1)]

    def i16(self):
        return struct.unpack_from("<h", self.b, self.skip(2))[0]

    def i32(self):
        return struct.unpack_from("<i", self.b, self.skip(4))[0]

    def u32(self):
        return struct.unpack_from("<I", self.b, self.skip(4))[0]

    def i64(self):
        return struct.unpack_from("<q", self.b, self.skip(8))[0]

    def count(self, min_size):
        n = self.i32()
        if n < 0 or n * min_size > self.end - self.p:
            raise _SFBad(self.what)
        return n

    def cstr(self):
        e = self.b.find(b"\0", self.p, self.end)
        if e < 0:
            raise _SFBad(self.what)
        self.p = e + 1

    def align4(self):
        pad = -self.p & 3
        at = self.skip(pad)
        if any(self.b[at:at + pad]):
            raise _SFBad(self.what)


class _SFNode:
    __slots__ = ("level", "flags", "type", "name", "size", "meta", "children", "checked")


def _sf_name(b, at, size, v):
    if v & 0x80000000:
        from UnityPy.helpers.Tpk import get_common_strings  # the same table as AssetsTools.NET's
        return get_common_strings().get(v & 0x7FFFFFFF, str(v & 0x7FFFFFFF))
    e = b.find(b"\0", at + v, at + size) if v < size else -1
    if e < 0:
        raise _SFBad("type tree")
    try:
        s = b[at + v:e].decode("utf-8")
    except UnicodeDecodeError:
        raise _SFBad("type tree") from None
    if not s or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in s):
        raise _SFBad("type tree")
    return s


def _sf_type(c, typetree, ref):
    cid = c.i32()
    c.skip(1)
    sidx = c.i16()
    if (ref and sidx >= 0) or cid == 114:
        c.skip(16)
    c.skip(16)
    if not typetree:
        return cid, None
    n, size = c.count(32), c.i32()
    if n == 0 or size < 0:
        raise _SFBad("type tree")
    nodes_at = c.skip(32 * n)
    strings_at = c.skip(size)
    nodes, stack = [], []
    for i in range(n):
        ver, lvl, fl, ts, ns, bs, idx, mf, rh = struct.unpack_from("<HBBIIiiiQ", c.b, nodes_at + 32 * i)
        x = _SFNode()
        x.level, x.flags, x.size, x.meta, x.children, x.checked = lvl, fl, bs, mf, [], False
        x.type, x.name = _sf_name(c.b, strings_at, size, ts), _sf_name(c.b, strings_at, size, ns)
        if (i == 0) != (lvl == 0) or (i > 0 and lvl > nodes[-1].level + 1) or bool(fl & 1) != (x.type == "Array") or fl & 0xE:
            raise _SFBad("type tree")
        if i > 0:
            del stack[lvl:]
            stack[-1].children.append(x)
        stack.append(x)
        nodes.append(x)
    if ref:
        c.cstr(), c.cstr(), c.cstr()
    else:
        c.skip(4 * c.count(4))
    return cid, nodes[0]


def _sf_shape(n, base, children_only):
    out = []

    def add(x):
        out.append(f"{x.level - base} {x.type} {x.name};")
        for ch in x.children:
            add(ch)
    for x in (n.children if children_only else [n]):
        add(x)
    return "".join(out)


def _sf_leaf(n, t, size):
    return n.type == t and n.size == size and not n.children


def _sf_walk(c, node, sink, sinks):
    """Reads one value as UnityPy does (by type names) and checks the type tree against it."""
    align = node.meta & 0x4000
    t = node.type
    if t in _SF_PRIM:
        size = _SF_PRIM[t]
        if node.size != size or node.children:
            raise _SFBad("type tree")
        at = c.skip(size)
        if sink is not None and t in ("int", "SInt64"):
            sink.append(struct.unpack_from("<i" if size == 4 else "<q", c.b, at)[0])
    elif t == "string":
        a = node.children[0] if len(node.children) == 1 else None
        if not node.checked and (a is None or a.type != "Array" or not a.meta & 0x4000 or len(a.children) != 2
                                 or not _sf_leaf(a.children[0], "int", 4) or not _sf_leaf(a.children[1], "char", 1)):
            raise _SFBad("type tree")
        node.checked = True
        c.skip(c.count(1))
        c.align4()
    elif t == "TypelessData":
        c.skip(c.count(1))
    elif node.children and node.children[0].type == "Array":
        a = node.children[0]
        if len(node.children) != 1 or len(a.children) != 2 or not _sf_leaf(a.children[0], "int", 4):
            raise _SFBad("type tree")
        if a.meta & 0x4000:
            align = True
        n = c.count(0)
        e = a.children[1]
        if e.type in _SF_PRIM and not e.children:
            if e.size != _SF_PRIM[e.type]:
                raise _SFBad("type tree")
            c.skip(n * _SF_PRIM[e.type])
            if e.meta & 0x4000:
                c.align4()
        else:
            for _ in range(n):
                before = c.p
                _sf_walk(c, e, sink, None)
                if c.p == before:
                    break
    elif t == "pair" and len(node.children) != 2:
        raise _SFBad("type tree")
    elif t in ("ReferencedObject", "ManagedReferencesRegistry"):
        raise _SFBad("type tree")
    else:
        for ch in node.children:
            _sf_walk(c, ch, sink if sink is not None else (sinks or {}).get(ch.name), None)
    if align:
        c.align4()


def sf_problem(sf: bytes):
    """Mirror of LocalisationBundle.SerializedFile.Check: None, or why the patcher refuses the serialized file."""
    try:
        if len(sf) < 48:
            return "serialized file header"
        version, endian = struct.unpack_from(">I", sf, 8)[0], sf[16]
        msize, fsize, doff = struct.unpack_from(">Iqq", sf, 20)
        if version != 22 or endian != 0:
            return f"serialized file format {version} / endianness {endian}"
        if fsize != len(sf) or not 48 + msize <= doff <= fsize:
            return "serialized file header sizes"
        c = _SFCur(sf, 48, 48 + msize, "metadata")
        c.cstr()
        c.skip(4)
        typetree = c.u8() != 0
        types = [_sf_type(c, typetree, False) for _ in range(c.count(23))]
        objs = []
        for _ in range(c.count(24)):
            c.align4()
            pid, start, size, ti = c.i64(), c.i64(), c.u32(), c.i32()
            if not 0 <= ti < len(types) or start < 0 or start > fsize - doff - size:
                return "object table: type index / range"
            objs.append((pid, start, size, ti))
        for _ in range(c.count(12)):
            c.skip(4)
            c.align4()
            c.skip(8)
        externals = c.count(22)
        for _ in range(externals):
            c.cstr()
            c.skip(20)
            c.cstr()
        for _ in range(c.count(23)):
            _sf_type(c, typetree, True)
        c.cstr()
        if c.p != 48 + msize:
            return f"metadata ends at {c.p}, header says {48 + msize}"
        ids = {o[0] for o in objs}
        if len(ids) != len(objs):
            return "duplicate path id"
        so = sorted(objs, key=lambda o: o[1])
        if any(so[i][1] < so[i - 1][1] + so[i - 1][2] for i in range(1, len(so))):
            return "objects overlap"
        preload, container, texts, bundles = [], [], [], 0
        for pid, start, size, ti in objs:
            cid, root = types[ti]
            texts += [pid] if cid == 49 else []
            bundles += cid == 142
            if root is None:
                continue
            sinks = None
            if cid == 49 and _sf_shape(root, 0, True) != _SF_TEXTASSET:
                return "TextAsset type tree"
            if cid == 142:
                sinks = {"m_PreloadTable": preload, "m_Container": container}
                for field, shape in _SF_AB_SHAPES.items():
                    f = [ch for ch in root.children if ch.name == field]
                    if len(f) != 1 or _sf_shape(f[0], 1, False) != shape:
                        return f"AssetBundle type tree: {field}"
            d = _SFCur(sf, doff + start, doff + start + size, "object data")
            _sf_walk(d, root, None, sinks)
            if d.p != doff + start + size:
                return f"object {pid} uses {d.p - doff - start} of {size} bytes"
        if bundles != 1:
            return f"{bundles} AssetBundle objects"
        if typetree:
            def missing(fid, pid):
                return not 0 <= fid <= externals or (fid == 0 and pid != 0 and pid not in ids)
            if any(missing(preload[i], preload[i + 1]) for i in range(0, len(preload) - 1, 2)):
                return "AssetBundle preload table: missing object"
            listed = set()
            for i in range(0, len(container) - 3, 4):
                index, size, fid, pid = container[i:i + 4]
                if missing(fid, pid) or index < 0 or size < 0 or index + size > len(preload) // 2:
                    return "AssetBundle container: missing object / preload range"
                if fid == 0:
                    listed.add(pid)
            if any(t not in listed for t in texts):
                return "TextAsset not in the AssetBundle container"
        return None
    except _SFBad as e:
        return f"serialized file: {e}"


def s64(v):
    v &= 0xFFFFFFFFFFFFFFFF
    return v - (1 << 64) if v >= 1 << 63 else v


class SFInfo:
    """Offsets inside the serialized file (version 22) of a base bundle, found with UnityPy."""

    def __init__(self, bundle_bytes: bytes, sf: bytes):
        import UnityPy
        env = UnityPy.load(bundle_bytes)
        f = next(iter(env.files.values()))
        s = next(iter(f.files.values()))
        self.data_offset = s.header.data_offset
        self.objects = []  # (path_id, abs_start, size, type, table_entry_offset)
        for o in s.objects.values():
            entry = struct.pack("<qqI", o.path_id, o.byte_start - self.data_offset, o.byte_size)
            pos = sf.find(entry, 0, self.data_offset)
            self.objects.append((o.path_id, o.byte_start, o.byte_size, o.type.name, pos))
        self.texts = []  # (abs_start, size, name_len, script_len_offset, script_len)
        for pid, start, size, tname, _ in self.objects:
            if tname != "TextAsset":
                continue
            nlen, = struct.unpack_from("<i", sf, start)
            spos = start + ((4 + nlen + 3) & ~3)
            slen, = struct.unpack_from("<i", sf, spos)
            self.texts.append((start, size, nlen, spos, slen))


# =====================================================================================================
# independent Python model of the patcher (data parsing and fill-only apply)
# =====================================================================================================

class DataError(Exception):
    """Invalid data; `kind` names the rule (utf8: FZ-01, entries: FZ-08, content: FZ-07, header/fields/escape)."""

    def __init__(self, kind, message):
        super().__init__(f"{kind}: {message}")
        self.kind = kind


def unescape(s: str) -> str:
    if "\\" not in s:
        return s
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= len(s) or s[i] not in "\\nrt":
            raise DataError("escape", "bad escape")
        out.append({"\\": "\\", "n": "\n", "r": "\r", "t": "\t"}[s[i]])
        i += 1
    return "".join(out)


def decode_like_dotnet(raw: bytes) -> str:
    """new UTF8Encoding(false, true).GetString(bytes) (every invalid or cut sequence is an error), then a
    leading U+FEFF is removed (PatchData.Parse)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise DataError("utf8", f"invalid UTF-8: {e}") from e
    return text[1:] if text[:1] == "﻿" else text


def dotnet_int_tryparse(s: str):
    """int.TryParse(s) (NumberStyles.Integer: white space 0x09-0x0D / 0x20 around, a sign, trailing NULs)."""
    m = re.fullmatch(r"[\t\n\v\f\r ]*([+-]?[0-9]+)[\t\n\v\f\r ]*\x00*", s)
    if m and -2 ** 31 <= int(m.group(1)) < 2 ** 31:
        return int(m.group(1))
    return None


BASE_RX = re.compile(r"[0-9a-f]{16}")


def parse_data(raw: bytes) -> dict:
    """Mirror of PatchData.Parse: {"header": {}, "sources": {source: [entry]}} or DataError."""
    text = decode_like_dotnet(raw)
    if not text.endswith("\n"):
        # round-2 fix (CS-06): build_patch.py ends every line with a newline; a file cut inside its last line has none
        raise DataError("newline", "the last line has no newline (cut)")
    lines = text.split("\n")
    if not lines[0].rstrip("\r").startswith("#TPCJP-PATCH\t1"):
        raise DataError("header", "bad header")
    if "\x00" in lines[0]:
        # round-3 fix (nul_translation_blamed_on_game_file): U+0000 is refused in every line
        raise DataError("nul", "line 1: U+0000")
    header, sources, count = {}, {}, 0
    for i in range(1, len(lines)):
        line = lines[i].rstrip("\r")
        if "\x00" in line:
            raise DataError("nul", f"line {i + 1}: U+0000")
        if not line:
            continue
        f = line.split("\t")
        if line[0] == "#":
            if len(f) >= 2:
                name, value = f[0][1:], unescape(f[1])
                # round 3b (R18): the header values the patcher uses (state file, log) hold no control character
                if name in ("patch_version", "game_version", "bundle_sha256") and \
                        any(ord(c) < 0x20 or c in "\x7f\x85  " for c in value):
                    raise DataError("content", f"line {i + 1}: control character in #{name}")
                header[name] = value
            continue
        is_ref = line[0] == "&"
        if len(f) != (5 if is_ref else 4):
            raise DataError("fields", f"line {i + 1}: field count")
        source = f[0][1:] if is_ref else f[0]
        e = {"key": unescape(f[1]), "base": f[2]}
        if is_ref:
            e.update(ref_source=f[3], ref_key=unescape(f[4]), ja=None)
        else:
            e.update(ja=unescape(f[3]), ref_source=None)
        if (not e["key"] or "=" in e["key"] or SEP in e["key"] or (e["ja"] is not None and SEP in e["ja"])
                or not BASE_RX.fullmatch(e["base"])):
            raise DataError("content", f"line {i + 1}: key / value / base hash not allowed")
        sources.setdefault(source, []).append(e)
        count += 1
    declared = header.get("entries")
    if declared is None or dotnet_int_tryparse(declared) != count:
        raise DataError("entries", f"{count} entries, header says {declared!r}")
    return {"header": header, "sources": sources, "count": count}


def base_hash(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()[:16]


class Cache:
    """Mirror of I2Cache."""

    def __init__(self, text: str):
        self.entries, self.by_key = [], {}
        if not text:
            return
        for part in text.split(SEP):
            eq = part.find("=")
            e = [part, part if eq < 0 else part[:eq], "" if eq < 0 else part[eq + 1:], False]
            self.entries.append(e)
            self.by_key.setdefault(e[1], []).append(e)

    def get(self, key):
        lst = self.by_key.get(key)
        return lst[-1][2] if lst else None

    def set(self, key, value):
        for e in self.by_key[key]:
            e[2] = value
            e[3] = True

    def dump(self) -> str:
        return SEP.join(f"{e[1]}={e[2]}" if e[3] else e[0] for e in self.entries)


def model_apply(backup_texts: dict, data: dict):
    """Mirror of Patcher.Apply on the backup's texts: (expected texts, applied count)."""
    caches = {n: Cache(t) for n, t in backup_texts.items()}
    applied, expected = 0, {}
    for source, entries in data["sources"].items():
        cache = caches.get(source)
        if cache is None:
            continue
        changed = False
        for e in entries:
            official = cache.get(e["key"])
            if official is None:
                continue
            ja = e["ja"]
            if e["ref_source"] is not None:
                ref = caches.get(e["ref_source"])
                ja = ref.get(e["ref_key"]) if ref else None
                if ja is None or not JAPANESE_RX.search(ja) or SEP in ja:
                    continue
            if base_hash(official) == e["base"]:
                cache.set(e["key"], ja)
                changed = True
                applied += 1
        if changed:
            expected[source] = cache.dump()
    out = dict(backup_texts)
    out.update(expected)
    return out, applied


def model_is_patched(texts: dict, data: dict) -> bool:
    """Mirror of Patcher.Classify(...).IsPatched."""
    ours = official = other = 0
    for source, entries in data["sources"].items():
        if source not in texts:
            continue
        cache = Cache(texts[source])
        for e in entries:
            if e["ja"] is None or base_hash(e["ja"]) == e["base"]:
                continue
            v = cache.get(e["key"])
            if v is None:
                continue
            if v == e["ja"]:
                ours += 1
            elif base_hash(v) == e["base"]:
                official += 1
            elif JAPANESE_RX.search(v):
                other += 1
    return ours >= 3 and ours > official + other


INFLIGHT = ROOT / "inflight"
_CUR = {}


def mark(stage: str):
    """Records what the worker is doing, so a native crash (UnityPy) can be attributed to a case and a file."""
    if "case" not in _CUR:
        return
    try:
        (INFLIGHT / f"{_CUR['case']}.json").write_text(
            json.dumps({"stage": stage, "result": _CUR.get("result")}, ensure_ascii=False), "utf-8")
    except (OSError, TypeError, ValueError):
        pass


def try_read_texts(path: Path):
    mark(f"UnityPy (tools/tpcloc/bundles.read_texts) crashed with an access violation reading {Path(path).name}")
    try:
        return bundles.read_texts(path), None
    except Exception as e:  # noqa: BLE001 - any failure means "does not load"
        return None, f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        mark("running")


ISOLATED_READER = (
    "import json, sys, warnings\n"
    "warnings.filterwarnings('ignore')\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from tpcloc import bundles\n"
    "try:\n"
    "    r = {'texts': bundles.read_texts(sys.argv[2])}\n"
    "except Exception as e:\n"
    "    r = {'err': type(e).__name__ + ': ' + str(e)[:200]}\n"
    "open(sys.argv[3], 'w', encoding='utf-8').write(json.dumps(r, ensure_ascii=False))\n")
_STATUS_TEXTS = {}


def texts_for_status(path: Path):
    """(texts, error) of an installed bundle for the status oracle. The bundle may be a damaged input the exe never
    wrote, on which UnityPy can crash natively: unknown files are read in a child process (a crash = does not load)."""
    s = shared()
    b = Path(path).read_bytes()
    h = sha(b)
    if h == s["official_sha"]:
        return s["official_texts"], None
    if h == s["golden_sha"]:
        if "patched_texts" not in s:
            s["patched_texts"] = model_apply(s["official_texts"], s["data"])[0]  # = the exe's output (prepare_shared)
        return s["patched_texts"], None
    if h not in _STATUS_TEXTS:
        if len(_STATUS_TEXTS) > 16:
            _STATUS_TEXTS.clear()
        (ROOT / "status_tmp").mkdir(parents=True, exist_ok=True)
        out = ROOT / "status_tmp" / f"{os.getpid()}_{h[:16]}.json"
        try:
            p = subprocess.run([sys.executable, "-I", "-c", ISOLATED_READER, str(Path(__file__).resolve().parent),
                                str(path), str(out)], capture_output=True, timeout=300, creationflags=CREATE_NO_WINDOW)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
        try:
            r = json.loads(out.read_text("utf-8"))
            out.unlink()
        except (OSError, json.JSONDecodeError):
            r = {"err": f"UnityPy crashed (exit {rc if not isinstance(rc, int) else hex(rc & 0xFFFFFFFF)})"}
        _STATUS_TEXTS[h] = (r.get("texts"), r.get("err"))
    return _STATUS_TEXTS[h]


def key_lists(texts: dict) -> dict:
    return {n: [p.partition("=")[0] for p in t.split(SEP)] if t else [] for n, t in texts.items()}


# =====================================================================================================
# shared material (built once in the main process, loaded once per worker)
# =====================================================================================================

SHARED = ROOT / "shared"
_S = {}


def shared():
    if not _S:
        warnings.filterwarnings("ignore")
        _S["official"] = (SHARED / "official.bundle").read_bytes()
        _S["patched"] = (SHARED / "patched.bundle").read_bytes()
        _S["state"] = (SHARED / "patched.state").read_bytes()
        _S["data_path"] = SHARED / "data.tsv"
        _S["data_raw"] = _S["data_path"].read_bytes()
        _S["data"] = parse_data(_S["data_raw"])
        _S["official_texts"] = json.loads((SHARED / "official_texts.json").read_text("utf-8"))
        _S["prefs"] = (SHARED / "preferences.json").read_bytes()
        meta = json.loads((SHARED / "meta.json").read_text("utf-8"))
        _S.update(meta)
        _S["ufs"] = {"official": UFS(_S["official"]), "patched": UFS(_S["patched"])}
        _S["sfinfo"] = {}
    return _S


def sfinfo(base: str) -> SFInfo:
    s = shared()
    if base not in s["sfinfo"]:
        s["sfinfo"][base] = SFInfo(s[base], s["ufs"][base].sf())
    return s["sfinfo"][base]


def prepare_shared(exe: Path):
    """Copies the inputs into the temp root and makes the reference patched bundle with a sandbox apply."""
    warnings.filterwarnings("ignore")
    SHARED.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(OFFICIAL, SHARED / "official.bundle")
    shutil.copyfile(DATA, SHARED / "data.tsv")
    src = sorted(PREFS_COPY.glob("*/preferences.json")) or paths.preferences_files()
    if not src:
        raise SystemExit("no preferences.json copy found (work/localtest/state/Cloud_backup)")
    (SHARED / "preferences.json").write_bytes(Path(src[0]).read_bytes())  # read only; the source is never written
    texts = bundles.read_texts(SHARED / "official.bundle")
    (SHARED / "official_texts.json").write_text(json.dumps(texts, ensure_ascii=False), "utf-8")
    golden = {}
    for name, extra in (("data", ["--data", str(SHARED / "data.tsv")]), ("embedded", [])):
        g = ROOT / "golden" / name
        rmtree(g)
        make_game(g, (SHARED / "official.bundle").read_bytes())
        r = run_exe(exe, ["apply", "--game", str(g), "--no-language", *extra], g)
        if r["rc"] != 0:
            raise SystemExit(f"reference apply failed: {r['out']}")
        golden[name] = sha((g / BUNDLE_REL).read_bytes())
        if name == "data":
            shutil.copyfile(g / BUNDLE_REL, SHARED / "patched.bundle")
            shutil.copyfile(state_path(g), SHARED / "patched.state")
        if not (g / "appdata" / "TpcJpPatch.log").exists():
            raise SystemExit("the exe did not log into TPCJP_APPDATA: it is not a Debug build with the test hooks")
    model, applied = model_apply(texts, parse_data((SHARED / "data.tsv").read_bytes()))
    got = bundles.read_texts(SHARED / "patched.bundle")
    if got != model:
        raise SystemExit("the Python model of apply does not match the exe's output on the official bundle")
    meta = {"golden_sha": golden["data"], "embedded_same": golden["embedded"] == golden["data"],
            "official_sha": sha((SHARED / "official.bundle").read_bytes()), "applied": applied}
    (SHARED / "meta.json").write_text(json.dumps(meta), "utf-8")
    return meta


# =====================================================================================================
# sandboxes
# =====================================================================================================

def make_game(d: Path, bundle: bytes | None, version=GAME_VERSION):
    rmtree(d)
    (d / BUNDLE_REL).parent.mkdir(parents=True)
    (d / "TPC.exe").write_bytes(b"")
    (d / "version.txt").write_text(version, "utf-8")
    if bundle is not None:
        (d / BUNDLE_REL).write_bytes(bundle)


def game_version(d: Path) -> str:
    """Mirror of GameLocator.GameVersion (round 3c, S4: BOM, .NET Trim, Patcher.OneLine; 不明 when unreadable)."""
    return paths.game_version(d)


def same_version(pre_state: bytes | None, d: Path) -> bool:
    """Mirror of Patcher.SameGameVersion: the state file was written for the installed game version."""
    if pre_state is None:
        return False
    v = read_state(pre_state).get("game_version")
    cur = game_version(d)
    return v is not None and cur not in ("", "不明") and v == cur  # round 3b: an empty version.txt is unknown too


def expected_game_version(pre: dict, post: dict, game: Path):
    """game_version in the state file after a successful apply (round 3: the version the backup was taken for). It is
    version.txt when the backup was (re)taken; when the installed file is the backup, version.txt unless an unknown
    version would replace a known one; when the installed file is only this backup's output (the state's
    patched_sha256, or byte-identical to the new output), a recorded value is refreshed but none is added (round 3b,
    R1/R3); otherwise the old state's value is kept (None: absent, e.g. after a rebuild without a state file)."""
    bname, kname, sname = BUNDLE_REL.name, BUNDLE_REL.name + ".tpcjp-original", BUNDLE_REL.name + ".tpcjp-state"
    b, k = pre.get(bname), pre.get(kname)
    st = read_state(pre[sname]) if pre.get(sname) is not None else {}
    cur, old, rec = game_version(game), st.get("game_version"), "game_version" in st

    def known(v):
        return v not in (None, "", "不明")
    # round 3c (S3): the official file recorded in the state, installed again while the backup is missing or another file
    # (Patcher.RecordedOriginal), is backed up again: the rule of b == k
    if b is not None and b != k and st.get("patched_sha256") != sha(b) and st.get("original_sha256") == sha(b):
        return cur if known(cur) or not rec or not known(old) else old
    if post.get(kname) != k:
        return cur
    if b is not None and (st.get("patched_sha256") == sha(b) or (b != k and post.get(bname) == b)):
        return cur if rec and (known(cur) or not known(old)) else old
    if b is not None and b == k:
        return cur if known(cur) or not rec or not known(old) else old
    return old


def bundle_path(d):
    return d / BUNDLE_REL


def backup_path(d):
    return (d / BUNDLE_REL).with_name(BUNDLE_REL.name + ".tpcjp-original")


def state_path(d):
    return (d / BUNDLE_REL).with_name(BUNDLE_REL.name + ".tpcjp-state")


def snapshot(folder: Path) -> dict:
    out = {}
    for p in sorted(folder.iterdir()):
        if p.is_file():
            out[p.name] = p.read_bytes()
    return out


def read_state(b: bytes) -> dict:
    """Mirror of Patcher.ReadState (File.ReadAllLines: a UTF-8 BOM is dropped, lines end at \\r\\n, \\r or \\n only)."""
    d = {}
    for line in re.split(r"\r\n|\r|\n", b.decode("utf-8", "replace").removeprefix("﻿")):
        if "=" in line and line.index("=") > 0:
            k, _, v = line.partition("=")
            d[k] = v
    return d


class Locker:
    """Holds files open without sharing (like another program using them) while the exe runs."""

    def __init__(self, files):
        self.files, self.handles = files, []

    def __enter__(self):
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = ctypes.c_void_p
        for f, create in self.files:
            h = k32.CreateFileW(str(f), 0x80000000 | 0x40000000, 0, None, 1 if create else 3, 0x80, None)
            if h and h != ctypes.c_void_p(-1).value:
                self.handles.append(h)
        return self

    def __exit__(self, *a):
        for h in self.handles:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))


# =====================================================================================================
# bundle mutations
# =====================================================================================================

def special32(rng, cur):
    return rng.choice([0, 1, cur - 1, cur + 1, cur + 4, cur * 2, cur // 2, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF,
                       0x7FFFFFF0, 0x10000000, 0x01000000, rng.getrandbits(32), rng.getrandbits(16)]) & 0xFFFFFFFF


def special64(rng, cur):
    return rng.choice([0, cur - 1, cur + 1, cur * 2, -1, 1 << 40, (1 << 63) - 1, 0x7FFFFFFF, 0xFFFFFFFF,
                       0x100000000, rng.getrandbits(63)])


def flip(rng, b: bytearray, lo, hi, n):
    hi = min(hi, len(b))
    if hi <= lo:
        return []
    done = []
    for _ in range(n):
        i = rng.randrange(lo, hi)
        if rng.random() < 0.5:
            b[i] ^= 1 << rng.randrange(8)
        else:
            b[i] = rng.randrange(256)
        done.append(i)
    return done


def rand_range(rng, lo, hi, maxlen):
    hi = max(hi, lo + 1)
    start = rng.randrange(lo, hi)
    length = min(int(rng.expovariate(1 / max(1, maxlen / 8))) + 1, maxlen)
    return start, length


BUNDLE_MUTATIONS = {
    # raw bytes of the file
    "flip_header": 4, "flip_blockinfo": 4, "flip_data": 5, "flip_any": 3, "truncate": 4, "truncate_fix": 3,
    "append": 3, "append_fix": 2, "zero_range": 4, "fill_range": 2, "insert_bytes": 2, "delete_bytes": 2,
    "swap_blocks_raw": 2,
    # consistent container edits (block info rebuilt)
    "swap_blocks": 3, "dup_block": 2, "drop_block": 2, "block_field": 5, "dir_field": 4, "header_field": 5,
    "bi_counts": 2,
    # inside the serialized file (recompressed consistently)
    "sf_flip_header": 3, "sf_flip_meta": 4, "sf_objtable_flip": 3, "sf_obj_field": 5, "sf_flip_objdata": 4,
    "sf_ta_field": 5, "sf_text_utf8": 3, "sf_text_sep": 2, "sf_dup_name": 1, "sf_truncate": 3, "sf_append": 2,
    "sf_zero": 3,
    "combo": 4,
}


def mutate_bundle(rng, base: str, data: bytes = None, depth=0, force=None):
    """Returns (bytes, description) for a mutation of the shared base bundle ('official' or 'patched')."""
    s = shared()
    m = s["ufs"][base] if data is None else None
    data = s[base] if data is None else data
    if m is None:
        try:
            m = UFS(data)
        except Exception:  # noqa: BLE001 - an earlier mutation broke the container: raw mutations only
            m = None
    kinds = list(BUNDLE_MUTATIONS)
    weights = [BUNDLE_MUTATIONS[k] for k in kinds]
    kind = force or rng.choices(kinds, weights)[0]
    if depth > 0 and kind == "combo":
        kind = "flip_any"
    if m is None and not kind.startswith(("flip", "truncate", "append", "zero", "fill", "insert", "delete")):
        kind = "flip_any"
    if kind.startswith("sf_") and m is not None and data is not s[base]:
        try:
            m.sf()
        except Exception:  # noqa: BLE001 - an earlier mutation broke the blocks: raw mutation instead
            kind = "flip_any"
    b = bytearray(data)
    n = len(b)
    hdr_end = m.hdr_end if m else 44
    bi_lo, bi_hi = (m.bi_off, m.bi_off + m.csz) if m else (48, 180)
    data_off = m.data_off if m else 192
    desc = kind

    if kind == "flip_header":
        desc += f" at {flip(rng, b, 0, hdr_end, rng.randint(1, 3))}"
    elif kind == "flip_blockinfo":
        desc += f" at {flip(rng, b, bi_lo, bi_hi, rng.randint(1, 3))}"
    elif kind == "flip_data":
        desc += f" at {flip(rng, b, data_off, n, rng.randint(1, 16))}"
    elif kind == "flip_any":
        desc += f" at {flip(rng, b, 0, n, rng.randint(1, 8))}"
    elif kind in ("truncate", "truncate_fix"):
        cut = rng.choice([0, rng.randrange(0, hdr_end), rng.randrange(hdr_end, data_off + 1),
                          rng.randrange(data_off, n), n - rng.randint(1, 64), rng.randrange(0, n)])
        b = b[:max(0, cut)]
        desc += f" to {len(b)}"
        if kind == "truncate_fix" and m and len(b) >= m.size_pos + 8:
            struct.pack_into(">q", b, m.size_pos, len(b))
    elif kind in ("append", "append_fix"):
        k = rng.choice([1, 3, 16, rng.randint(1, 65536)])
        extra = bytes(k) if rng.random() < 0.4 else rng.randbytes(k)
        b += extra
        desc += f" {k} bytes"
        if kind == "append_fix" and m:
            struct.pack_into(">q", b, m.size_pos, len(b))
    elif kind in ("zero_range", "fill_range"):
        region = rng.choice([(0, hdr_end), (bi_lo, bi_hi), (data_off, n), (0, n)])
        start, length = rand_range(rng, region[0], region[1], 65536)
        val = 0 if kind == "zero_range" else rng.choice([0xFF, 0x41, rng.randrange(256)])
        end = min(n, start + length)
        b[start:end] = bytes([val]) * (end - start)
        desc += f" {start}+{end - start}"
    elif kind == "insert_bytes":
        pos = rng.randrange(0, n + 1)
        k = rng.randint(1, 64)
        b[pos:pos] = rng.randbytes(k)
        desc += f" {k} at {pos}"
    elif kind == "delete_bytes":
        start, length = rand_range(rng, 0, n, 4096)
        del b[start:start + length]
        desc += f" {start}+{length}"
    elif kind == "swap_blocks_raw":
        i, j = rng.sample(range(len(m.cblocks)), 2)
        cb = list(m.cblocks)
        cb[i], cb[j] = cb[j], cb[i]
        b = bytearray(data[:m.data_off] + b"".join(cb))
        desc += f" {i}<->{j}"
    elif kind in ("swap_blocks", "dup_block", "drop_block"):
        cb, entries = list(m.cblocks), list(m.blocks)
        if kind == "swap_blocks":
            i, j = rng.sample(range(len(cb)), 2)
            cb[i], cb[j] = cb[j], cb[i]
            entries[i], entries[j] = entries[j], entries[i]
            desc += f" {i}<->{j}"
        elif kind == "dup_block":
            i, j = rng.sample(range(len(cb)), 2)
            cb[j], entries[j] = cb[i], entries[i]
            desc += f" {i}->{j}"
        else:
            i = rng.randrange(len(cb))
            del cb[i], entries[i]
            desc += f" {i}"
        b = bytearray(m.build(cb, m.make_bi(m.hash16, entries, m.dirs)))
    elif kind == "block_field":
        entries = [list(e) for e in m.blocks]
        i = rng.randrange(len(entries))
        f = rng.randrange(3)
        old = entries[i][f]
        entries[i][f] = special32(rng, old) if f < 2 else rng.choice([0, 1, 2, 4, 5, 0x40, 0x3F, rng.getrandbits(16)])
        desc += f" block {i} field {('usize', 'csize', 'flags')[f]} {old}->{entries[i][f]}"
        b = bytearray(m.build(m.cblocks, m.make_bi(m.hash16, [tuple(e) for e in entries], m.dirs)))
    elif kind == "dir_field":
        off, size, fl, name = m.dirs[0]
        f = rng.randrange(4)
        if f == 0:
            off = special64(rng, off)
        elif f == 1:
            size = special64(rng, size)
        elif f == 2:
            fl = rng.choice([0, 1, 2, 4 ^ 0xFFFF, rng.getrandbits(32)])
        else:
            name = rng.choice([b"", b"x", name[:-1], name + b"y", rng.randbytes(8).replace(b"\0", b"a")])
        desc += f" {('offset', 'size', 'flags', 'name')[f]} -> {(off, size, fl, name)[f]!r}"
        b = bytearray(m.build(m.cblocks, m.make_bi(m.hash16, m.blocks, [(off, size, fl, name)])))
    elif kind == "header_field":
        f = rng.choice(["size", "csz", "usz", "flags", "ver"])
        kw = {}
        bi = m.make_bi(m.hash16, m.blocks, m.dirs)
        if f == "size":
            kw["size"] = special64(rng, n)
        elif f == "csz":
            kw["csz"] = special32(rng, len(lz4.block.compress(bi, mode="high_compression", store_size=False)))
        elif f == "usz":
            kw["usz"] = special32(rng, len(bi))
        elif f == "flags":
            kw["flags"] = rng.choice([m.flags & ~0x3F, (m.flags & ~0x3F) | 1, (m.flags & ~0x3F) | 2, m.flags | 0x80,
                                      m.flags ^ 0x200, m.flags & ~0x40, rng.getrandbits(32)])
        else:
            kw["ver"] = rng.choice([0, 5, 6, 7, 9, 0xFFFFFFFF])
        desc += f" {f}={list(kw.values())[0]}"
        b = bytearray(m.build(m.cblocks, bi, **kw))
    elif kind == "bi_counts":
        f = rng.choice(["nblocks", "ndirs"])
        val = rng.choice([0, -1, 1, 2, 1000, 0x7FFFFFFF, len(m.blocks) + 1, len(m.blocks) - 1])
        bi = m.make_bi(m.hash16, m.blocks, m.dirs, **{f: val})
        desc += f" {f}={val}"
        b = bytearray(m.build(m.cblocks, bi))
    elif kind.startswith("sf_"):
        sf = bytearray(m.sf())
        info = sfinfo(base) if data is s[base] else None
        if info is None and kind in ("sf_objtable_flip", "sf_obj_field", "sf_ta_field", "sf_text_utf8", "sf_text_sep", "sf_dup_name"):
            kind = "sf_flip_objdata"
            desc = kind
        doff = info.data_offset if info else 4096
        if kind == "sf_flip_header":
            desc += f" at {flip(rng, sf, 0, 48, rng.randint(1, 2))}"
        elif kind == "sf_flip_meta":
            desc += f" at {flip(rng, sf, 48, doff, rng.randint(1, 4))}"
        elif kind == "sf_objtable_flip":
            o = rng.choice([o for o in info.objects if o[4] >= 0])
            desc += f" obj {o[0]} at {flip(rng, sf, o[4], o[4] + 24, rng.randint(1, 2))}"
        elif kind == "sf_obj_field":
            o = rng.choice([o for o in info.objects if o[4] >= 0])
            f = rng.choice(["path_id", "start", "size", "type"])
            pos = o[4]
            if f == "path_id":
                other = rng.choice(info.objects)[0]
                struct.pack_into("<q", sf, pos, other)
                desc += f" {o[0]} path_id={other}"
            elif f == "start":
                v = special64(rng, o[1] - doff)
                struct.pack_into("<q", sf, pos + 8, s64(v))
                desc += f" {o[0]} start={v}"
            elif f == "size":
                v = special32(rng, o[2])
                struct.pack_into("<I", sf, pos + 16, v)
                desc += f" {o[0]} size={v}"
            else:
                v = rng.choice([-1, 0, 1, 2, 1000, rng.getrandbits(31)])
                struct.pack_into("<i", sf, pos + 20, v)
                desc += f" {o[0]} type_index={v}"
        elif kind == "sf_flip_objdata":
            desc += f" at {flip(rng, sf, doff, len(sf), rng.randint(1, 8))}"
        elif kind == "sf_ta_field":
            start, size, nlen, spos, slen = rng.choice(info.texts)
            f = rng.choice(["name_len", "script_len"])
            cur = nlen if f == "name_len" else slen
            v = rng.choice([-1, 0, cur + 1, cur - 1, cur + 4, cur - 4, size, 0x7FFFFFFF, 0x7FFFFFF0, -0x80000000,
                            rng.getrandbits(31)])
            struct.pack_into("<i", sf, start if f == "name_len" else spos, s32(v))
            desc += f" TextAsset@{start} {f} {cur}->{v}"
        elif kind == "sf_text_utf8":
            start, size, nlen, spos, slen = rng.choice([t for t in info.texts if t[4] > 0])
            if rng.random() < 0.2:
                i = rng.randrange(start + 4, start + 4 + nlen)  # inside m_Name
            else:
                i = rng.randrange(spos + 4, spos + 4 + slen)
            sf[i] = rng.choice([0xFF, 0xFE, 0xC0, 0xC1, 0x80, 0xED, 0xF8])
            desc += f" byte {i} = {sf[i]:#x}"
        elif kind == "sf_text_sep":
            start, size, nlen, spos, slen = rng.choice([t for t in info.texts if t[4] > 0])
            seg = bytes(sf[spos + 4:spos + 4 + slen])
            k = rng.randint(1, 5)
            how = rng.choice(["[i2t]->[i2x]", "=->-", "key->Key"])
            if how == "[i2t]->[i2x]":
                seg = seg.replace(b"[i2t]", b"[i2x]", k)
            elif how == "=->-":
                seg = seg.replace(b"=", b"-", k)
            else:
                seg = seg.replace(b"UI/", b"Ui/", k)
            sf[spos + 4:spos + 4 + slen] = seg
            desc += f" TextAsset@{start} {how} x{k}"
        elif kind == "sf_dup_name":
            groups = {}
            for t in info.texts:
                groups.setdefault(t[2], []).append(t)
            pairs = [g for g in groups.values() if len(g) > 1]
            if pairs:
                a, c = rng.sample(rng.choice(pairs), 2)
                sf[c[0] + 4:c[0] + 4 + c[2]] = sf[a[0] + 4:a[0] + 4 + a[2]]
                desc += f" TextAsset@{c[0]} renamed like @{a[0]}"
            else:
                desc += " (no equal-length names)"
        elif kind == "sf_truncate":
            cut = rng.choice([0, rng.randrange(0, 48), rng.randrange(48, doff), rng.randrange(doff, len(sf))])
            sf = sf[:cut]
            desc += f" to {cut}"
        elif kind == "sf_append":
            k = rng.randint(1, 70000)
            sf += bytes(k) if rng.random() < 0.5 else rng.randbytes(k)
            desc += f" {k}"
        elif kind == "sf_zero":
            region = rng.choice([(0, 48), (48, doff), (doff, len(sf))])
            start, length = rand_range(rng, region[0], region[1], 65536)
            end = min(len(sf), start + length)
            sf[start:end] = bytes(end - start)
            desc += f" {start}+{end - start}"
        b = bytearray(m.rebuild_sf(bytes(sf)))
    elif kind == "combo":
        out, parts = data, []
        for _ in range(rng.randint(2, 3)):
            try:
                out, d = mutate_bundle(rng, base, out if out is not data else None, depth + 1)
            except Exception:  # noqa: BLE001 - not applicable to the already mutated file
                out, d = mutate_bundle(rng, base, out if out is not data else None, depth + 1, force="flip_any")
            parts.append(d)
        return out, "combo[" + "; ".join(parts) + "]"
    return bytes(b), desc


def s32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v >= 1 << 31 else v


# =====================================================================================================
# bundle suite
# =====================================================================================================

BUNDLE_SCENARIOS = {
    "fresh": 30,          # no patch yet: the installed bundle is a damaged official file
    "patched_cur": 22,    # patch applied, then the installed (patched) bundle got damaged
    "update_cur": 10,     # patch applied, then a damaged "update" replaced the installed bundle; half of the cases
                          # also change version.txt (a real update), the others keep it (not a real update)
    "bad_backup": 15,     # patch applied, then the backup got damaged
    "nostate_cur": 8,     # patch applied without a state file (older version), installed bundle damaged (round 3c, S1:
                          # nothing vouches for the backup, so a damaged file is refused, a still readable one of ours rebuilt)
    "stale_state": 5,     # no backup but a state file of an earlier patch, damaged official bundle
    "missing_cur": 3,     # patch applied, installed bundle damaged and backup damaged
    "stale_tmp": 4,       # like fresh, plus leftover *.tpcjp-tmp files from a crashed run
    "locked": 3,          # like patched_cur, plus one file held open by another program during the first run
}
OP_SEQUENCES = [["apply"], ["restore"], ["apply", "restore"], ["restore", "apply"], ["apply", "apply"],
                ["apply", "restore", "apply"], ["apply", "restore"], ["apply", "restore"]]


def bundle_case(idx: int, seed: int, exe: str, verbose=False, keep=False) -> dict:
    s = shared()
    rng = random.Random(case_seed(seed, "bundle", idx))
    scen = rng.choices(list(BUNDLE_SCENARIOS), list(BUNDLE_SCENARIOS.values()))[0]
    case_dir = ROOT / "cases" / f"bundle_{idx:05d}"
    game = case_dir / "game"
    rmtree(case_dir)
    make_game(game, None)
    bdir = bundle_path(game).parent
    base = "patched" if scen in ("patched_cur", "nostate_cur", "locked") else "official"
    mutated, desc = mutate_bundle(rng, base)
    if keep:
        (case_dir / "input.bundle").write_bytes(mutated)  # the mutated file, for a manual repro
    lock = None
    if scen in ("fresh", "stale_tmp"):
        bundle_path(game).write_bytes(mutated)
    elif scen == "stale_state":
        bundle_path(game).write_bytes(mutated)
        state_path(game).write_bytes(s["state"])
    else:
        bundle_path(game).write_bytes(s["patched"])
        backup_path(game).write_bytes(s["official"])
        state_path(game).write_bytes(s["state"])
        if scen in ("patched_cur", "update_cur", "locked"):
            bundle_path(game).write_bytes(mutated)
        elif scen == "nostate_cur":
            bundle_path(game).write_bytes(mutated)
            state_path(game).unlink()
        elif scen == "bad_backup":
            backup_path(game).write_bytes(mutated)
        elif scen == "missing_cur":
            inst, inst_desc = mutate_bundle(rng, "patched")
            bundle_path(game).write_bytes(inst)
            backup_path(game).write_bytes(mutated)
            desc += " (backup; bundle also damaged)"
            installed_note = inst_desc  # recorded below (not in "mutation": that text identifies the input in --compare)
    leftovers = []
    if scen != "missing_cur":
        installed_note = None
    if scen == "stale_tmp":
        for p in (bundle_path(game), backup_path(game), state_path(game)):
            if rng.random() < 0.7:
                Path(str(p) + TMP_SUFFIX).write_bytes(rng.randbytes(rng.randint(0, 5000)))
                leftovers.append(p.name + TMP_SUFFIX)
        # round-2 fix (F-SBP-7): File.Replace of version 1.0.0 could leave "<name>~RF*.TMP" (separate RNG: the
        # inputs of the earlier runs are unchanged)
        rrf = random.Random(case_seed(seed, "bundle-rf", idx))
        for p in (bundle_path(game), backup_path(game), state_path(game)):
            if rrf.random() < 0.5:
                name = f"{p.name}~RF{rrf.getrandbits(24):x}.TMP"
                (p.parent / name).write_bytes(rrf.choice([s["patched"], s["official"], rrf.randbytes(rrf.randint(0, 5000))]))
                leftovers.append(name)
    if scen == "locked":
        lock = rng.choice([bundle_path(game), backup_path(game), state_path(game),
                           Path(str(bundle_path(game)) + TMP_SUFFIX), Path(str(state_path(game)) + TMP_SUFFIX)])
    ops = rng.choice(OP_SEQUENCES)
    use_data = [rng.random() < 0.5 or not s["embedded_same"] for _ in ops]
    if scen == "update_cur" and random.Random(case_seed(seed, "bundle-version", idx)).random() < 0.5:
        # a real game update changes version.txt too (separate RNG: the inputs of the other cases are unchanged)
        (game / "version.txt").write_text(NEW_GAME_VERSION, "utf-8")
        desc += f" (version.txt -> {NEW_GAME_VERSION})"
    result = {"suite": "bundle", "index": idx, "seed": case_seed(seed, "bundle", idx), "scenario": scen,
              "mutation": desc, "ops": [], "problems": []}
    if leftovers:
        result["leftovers"] = leftovers
    if installed_note:
        result["installed_mutation"] = installed_note
    _CUR["result"] = result
    if verbose:
        print(f"bundle:{idx} scenario={scen} mutation={desc}" + (f" installed={installed_note}" if installed_note else "")
              + f"\n  sandbox {game}")
    for k, op in enumerate(ops):
        args = [op, "--game", str(game)] + (["--data", str(s["data_path"])] if use_data[k] else []) + \
               (["--no-language"] if op == "apply" else ["--keep-language"])
        pre = snapshot(bdir)
        if lock is not None and k == 0:
            with Locker([(lock, not lock.exists())]):
                r = run_exe(Path(exe), args, case_dir, verbose, watch=[bdir])
            preexisting_tmp = {lock.name} if lock.name.endswith(TMP_SUFFIX) else set()
            if lock.name.endswith(TMP_SUFFIX) and lock.exists() and lock.name not in pre:
                lock.unlink()  # the lock created it; it was not left by the exe
        else:
            r = run_exe(Path(exe), args, case_dir, verbose, watch=[bdir])
            preexisting_tmp = set()
        post = snapshot(bdir)
        probs = generic_problems(r)
        probs += check_bundle_run(op, r, pre, post, game, locked=lock if k == 0 else None,
                                  preexisting_tmp=preexisting_tmp)
        result["ops"].append({"args": [a if a != str(game) else "<sandbox>" for a in args], "rc": r["rc"],
                              "elapsed": r["elapsed"], "out": r["out"].strip()[-400:]})
        for code, detail in probs:
            result["problems"].append({"op": k, "code": code, "detail": detail})
        if verbose:
            for code, detail in probs:
                print(f"  PROBLEM [{code}] {detail[:500]}")
        if r["timeout"] or (r["rc"] not in (0, 1)):
            break
    else:
        # the status line (no lock) must describe the files left by the runs ("公式のデータに戻っています" etc.)
        r = run_status(Path(exe), game, case_dir, verbose)
        result["ops"].append({"args": ["status", "--game", "<sandbox>"], "rc": r["rc"], "elapsed": r["elapsed"],
                              "out": r["out"].strip()[-300:]})
        for code, detail in check_status(r, game):
            result["problems"].append({"op": len(ops), "code": code, "detail": detail})
            if verbose:
                print(f"  PROBLEM [{code}] {detail[:500]}")
    if not keep and not result["problems"]:
        rmtree(case_dir)
    result["sandbox"] = str(game)
    return result


def check_bundle_run(op, r, pre, post, game, locked=None, preexisting_tmp=frozenset()) -> list:
    s = shared()
    probs = []
    bname, kname, sname = BUNDLE_REL.name, BUNDLE_REL.name + ".tpcjp-original", BUNDLE_REL.name + ".tpcjp-state"
    # stale temp files of an earlier crashed run that this run did not touch are not counted
    left = [n for n in post if n.endswith(TMP_SUFFIX) and n not in preexisting_tmp and pre.get(n) != post[n]]
    if left:
        probs.append(("tmp_left", f"{left}"))
    if r["rc"] not in (0, 1) or r["timeout"] or r.get("busy_final"):
        return probs
    out = r["out"]
    # round-2 fix (F-SBP-7): apply / restore start by deleting the leftovers of an interrupted run
    # ("<name>.tpcjp-tmp", "<name>~RF*.TMP"); only a file held open by another program may stay
    stale = [n for n in pre if is_leftover(n) and n in post and (locked is None or n != locked.name)]
    if stale:
        probs.append(("leftover_not_removed", f"{op} (exit {r['rc']}) left the leftovers of an interrupted run: {stale}"))
    damaged_reported = "日本語データが壊れて" in out or ("壊れ" in out and "バックアップ" not in out.split("壊れ")[0][-20:])
    # the official file of an unchanged game version is the backup itself: it is never replaced (FZ-05/FZ-06 fix)
    same_ver = pre.get(kname) is not None and same_version(pre.get(sname), game)
    # round 3c (S2/S7/S8): a format the exe does not read is damage only when the state file proves the game version
    # unchanged; otherwise apply refuses and restore keeps the file (Patcher.IsNewFormat)
    unsupported = pre.get(bname) is not None and unsupported_format(pre[bname]) and not same_version(pre.get(sname), game)
    if unsupported and locked is None and op == "apply" and (r["rc"] != 1 or "日本語データを読み込めません（" not in out
                                                             or "それでも直らない場合は、パッチの更新をお待ちください" not in out):
        probs.append(("unsupported_apply", f"apply on a bundle in a format the exe does not read must exit 1 with the "
                      f"patch-update advice (exit {r['rc']})"))
    if r["rc"] == 0 and pre.get(kname) is not None and not unsupported:
        # round 3c (S1): a file that is neither identified by hash nor our readable output (missing, damaged, changed under
        # the same version) is rebuilt / restored from the backup only when the state file vouches for the backup (the
        # recorded hash and the installed game version)
        b, k = pre.get(bname), pre.get(kname)
        st_raw = pre.get(sname)
        st = read_state(st_raw) if st_raw is not None else {}
        hashed = b is not None and (b == k or st.get("patched_sha256") == sha(b) or st.get("original_sha256") == sha(b))
        used = (op == "apply" and post.get(kname) == k and post.get(bname) != b) or \
               (op == "restore" and post.get(bname) == k and b != k)
        if used and not hashed:
            ours = False
            if b is not None:
                bp = game / "check_s1.bundle"
                bp.write_bytes(b)
                texts, _ = texts_for_status(bp)
                bp.unlink()
                # UnityPy is the more lenient reader: a file it reads as our output is one the exe may classify as ours
                ours = texts is not None and model_is_patched(texts, s["data"])
            vouched = st.get("original_sha256", sha(k)) == sha(k) and same_version(st_raw, game)
            if not ours and not vouched:
                probs.append(("backup_used_unvouched", f"{op} used the backup for a {'missing' if b is None else 'damaged or changed'}"
                              f" file although the state file does not vouch for it (state: "
                              f"{'none' if st_raw is None else 'game_version ' + repr(st.get('game_version'))})"))
    if unsupported and locked is None and op == "restore":
        want = ("日本語データはこのパッチが対応していない形式のため、そのままにします" if pre.get(kname) is not None
                else "パッチは適用されていません（日本語データはこのパッチが対応していない形式です）")
        if r["rc"] != 0 or want not in out or post.get(bname) != pre.get(bname) or kname in post or sname in post:
            probs.append(("unsupported_restore", f"restore on a bundle in a format the exe does not read must keep it, "
                          f"remove backup and state and say '{want}' (exit {r['rc']})"))
    # a backup is only ever made from the installed bundle, and never from a damaged one
    if post.get(kname) is not None and post.get(kname) != pre.get(kname):
        if post[kname] != pre.get(bname):
            probs.append(("backup_not_from_bundle", "the new backup is not a copy of the installed bundle"))
        if "日本語データが壊れて" in out:
            probs.append(("backup_from_damaged", "a backup was written although the exe reported the bundle as damaged"))
        if same_ver:
            probs.append(("backup_replaced_same_version", "the backup was replaced although version.txt still matches "
                          "the state file (not a real game update)"))
        if r["rc"] == 1:
            probs.append(("backup_on_failure", "a new backup was written by a run that exited 1"))
        bp = game / "check_backup.bundle"
        bp.write_bytes(post[kname])
        texts, err = try_read_texts(bp)
        bp.unlink()
        if texts is None:
            probs.append(("backup_unloadable", f"new backup does not load with read_texts: {err}"))
    if r["rc"] == 1:
        if locked is not None:
            # a run that fails because another program holds a file may already have made the new backup of a
            # loadable official file (checked above); the game's file itself must be unchanged
            if post.get(bname) != pre.get(bname):
                probs.append(("changed_on_failure", "bundle changed although the exe exited 1"))
        else:
            changed = sorted(set(n for n in set(pre) | set(post)
                                 if pre.get(n) != post.get(n) and n not in preexisting_tmp and not is_leftover(n)))
            if changed:
                probs.append(("changed_on_failure", f"files changed although the exe exited 1: {changed}"))
        return probs
    # ---- exit 0 ----
    if op == "apply":
        if bname not in post or kname not in post or sname not in post:
            probs.append(("apply_files_missing", f"after apply: {sorted(post)}"))
            return probs
        if same_ver and post.get(kname) == pre.get(kname) and sha(pre[kname]) == s["official_sha"] \
                and sha(post[bname]) != s["golden_sha"]:
            probs.append(("output_mismatch", "same game version: the output was not rebuilt from the official backup"))
        texts, err = try_read_texts(bundle_path(game))
        if texts is None:
            probs.append(("output_unloadable", f"patched bundle does not load with read_texts: {err}"))
            return probs
        nul = [n for n, t in texts.items() if "\x00" in t and "\x00" not in s["official_texts"].get(n, "")]
        if nul:
            probs.append(("output_nul_text", f"the patched bundle has NUL-filled text in {nul[:5]} (zero-filled data "
                          "from a damaged input was written as valid text)"))
        st = read_state(post[sname])
        if STATE_WARNING in out:
            # FZ-10 fix: the state file could not be written after the verified bundle write -> warning, exit 0
            state_files = {state_path(game).name, state_path(game).name + TMP_SUFFIX}
            if locked is None or locked.name not in state_files:
                probs.append(("state_warning_unexpected", "state file warning although no state file was locked"))
            if post.get(sname) != pre.get(sname):
                probs.append(("state_changed_with_warning", "the state file changed although the exe says it could "
                              "not write it"))
        elif st.get("patched_sha256") != sha(post[bname]) or st.get("original_sha256") != sha(post[kname]):
            probs.append(("state_mismatch", "state file does not describe the bundle / backup"))
        elif st.get("game_version") != expected_game_version(pre, post, game):
            probs.append(("state_version_mismatch", f"state game_version {st.get('game_version')!r}, expected "
                          f"{expected_game_version(pre, post, game)!r} (version.txt {game_version(game)!r})"))
        if locked is not None and STATE_WARNING not in out and locked.name == state_path(game).name + TMP_SUFFIX:
            probs.append(("state_warning_missing", "apply exited 0 while the state temp file was held open by another "
                          "program, but no state-file warning was printed"))
        if sha(post[kname]) == s["official_sha"] and sha(post[bname]) != s["golden_sha"]:
            probs.append(("output_mismatch", "backup is official but the output differs from the reference patch"))
        bp = game / "check_backup.bundle"
        bp.write_bytes(post[kname])
        btexts, err = try_read_texts(bp)
        bp.unlink()
        if btexts is None:
            probs.append(("backup_unloadable", f"backup used for apply does not load with read_texts: {err}"))
            return probs
        if set(btexts) != set(s["official_texts"]):
            # UnityPy (like Unity: objects by path id, strict headers) does not see the TextAssets the exe saw
            probs.append(("backup_incomplete", f"the backup the exe accepted has {len(btexts)} of "
                          f"{len(s['official_texts'])} TextAssets in UnityPy (missing {sorted(set(s['official_texts']) - set(btexts))[:4]})"))
            return probs
        if model_is_patched(btexts, s["data"]):
            probs.append(("patched_backup_used", "the backup holds patched data"))
        want, applied = model_apply(btexts, s["data"])
        if texts != want:
            bad = [n for n in want if texts.get(n) != want[n]] + [n for n in texts if n not in want]
            probs.append(("output_mismatch", f"output differs from model(backup)+data in {bad[:5]}"))
        if pre.get(bname) is not None and pre.get(kname) is None and damaged_reported:
            probs.append(("damaged_patched", "the exe reported damage but patched without a backup"))
    else:  # restore
        if kname in post or sname in post:
            probs.append(("restore_left_files", f"after restore: {sorted(post)}"))
        cur = post.get(bname)
        if cur is not None and cur != pre.get(bname) and cur != pre.get(kname):
            probs.append(("restore_unknown_content", "restored bundle is neither the old bundle nor the backup"))
        if same_ver and cur != pre.get(kname):
            probs.append(("restore_kept_same_version", "version.txt still matches the state file, but restore kept "
                          "the installed file instead of the backup"))
        if pre.get(kname) is not None and cur == pre.get(bname) and cur != pre.get(kname) and not unsupported:
            # the backup was deleted and the installed file kept ("updated by the game"): it must be usable
            texts, err = try_read_texts(bundle_path(game))
            if texts is None:
                probs.append(("restore_kept_damaged", f"backup deleted, kept bundle does not load: {err}"))
            elif model_is_patched(texts, s["data"]):
                probs.append(("restore_kept_patched", "backup deleted, kept bundle still holds the patch"))
        if cur is not None and cur == pre.get(kname) and pre.get(kname) is not None:
            texts, err = try_read_texts(bundle_path(game))
            if texts is None:
                probs.append(("restore_wrote_damaged", f"restored from a backup that does not load: {err}"))
    return probs


# =====================================================================================================
# data suite
# =====================================================================================================

DATA_MUTATIONS = {
    "header_bad": 6, "fields_more": 4, "fields_less": 4, "ref_flag": 3, "escape_bad": 6, "escape_ok": 2,
    "utf8_bad": 7, "truncate": 4, "huge_line": 3, "empty": 3, "flip": 6, "lines_shuffle": 2, "lines_dup": 2,
    "lines_delete": 2, "crlf": 2, "cr_only": 1, "nul": 2, "sep_inject": 4, "base_bad": 3, "all_bases_bad": 2,
    "key_weird": 2, "ref_bad": 3, "bom": 2, "header_values": 2, "combo": 4,
}


def mutate_data(rng, raw: bytes, depth=0, force=None):
    kinds = list(DATA_MUTATIONS)
    kind = force or rng.choices(kinds, [DATA_MUTATIONS[k] for k in kinds])[0]
    if depth and kind == "combo":
        kind = "flip"
    text = raw.decode("utf-8", "surrogateescape")
    lines = text.split("\n")
    body = [i for i, l in enumerate(lines) if i > 0 and l and not l.startswith("#")]
    normal = [i for i in body if not lines[i].startswith("&")]
    refs = [i for i in body if lines[i].startswith("&")]
    heads = [i for i, l in enumerate(lines) if i > 0 and l.startswith("#")]
    desc = kind

    def pick(lst):
        return rng.choice(lst) if lst else rng.randrange(1, max(2, len(lines)))

    def join():
        return "\n".join(lines).encode("utf-8", "surrogateescape")

    if kind == "header_bad":
        v = rng.choice(["", "TPCJP-PATCH\t1", "#TPCJP-PATCH\t2", "#TPCJP-PATCH 1", "#tpcjp-patch\t1", " #TPCJP-PATCH\t1",
                        "#TPCJP-PATCH", "#TPCJP-PATCH\t", "garbage", "﻿#TPCJP-PATCH\t1"])
        if rng.random() < 0.3:
            del lines[0]
            desc += " (first line removed)"
        else:
            lines[0] = v
            desc += f" {v!r}"
        return join(), desc
    if kind in ("fields_more", "fields_less"):
        i = pick(body)
        f = lines[i].split("\t")
        if kind == "fields_more":
            f.insert(rng.randrange(len(f) + 1), rng.choice(["", "x", "0123456789abcdef"]))
        elif len(f) > 1:
            del f[rng.randrange(len(f))]
        lines[i] = "\t".join(f)
        return join(), f"{desc} line {i + 1}"
    if kind == "ref_flag":
        i = pick(body)
        lines[i] = lines[i][1:] if lines[i].startswith("&") else "&" + lines[i]
        return join(), f"{desc} line {i + 1}"
    if kind in ("escape_bad", "escape_ok"):
        i = pick(body + heads)
        f = lines[i].split("\t")
        j = rng.randrange(1, len(f)) if len(f) > 1 else 0
        if kind == "escape_bad":
            bad = rng.choice(["\\", "\\x", "\\u3042", "\\0", "\\N", "\\ "])
            pos = len(f[j]) if bad == "\\" else rng.randrange(len(f[j]) + 1)
            f[j] = f[j][:pos] + bad + f[j][pos:]
        else:
            pos = rng.randrange(len(f[j]) + 1)
            f[j] = f[j][:pos] + rng.choice(["\\n", "\\t", "\\\\", "\\r"]) + f[j][pos:]
        lines[i] = "\t".join(f)
        return join(), f"{desc} line {i + 1} field {j}"
    if kind == "utf8_bad":
        b = bytearray(raw)
        seq = rng.choice([b"\xff", b"\xfe", b"\xc3\x28", b"\x80", b"\xc0\xaf", b"\xed\xa0\x80", b"\xf8\x88\x80\x80\x80",
                          b"\xe3\x81", b"\xf4\x90\x80\x80"])
        pos = rng.randrange(len(b) + 1)
        if rng.random() < 0.5:
            b[pos:pos] = seq
        else:
            b[pos:pos + len(seq)] = seq
        return bytes(b), f"{desc} {seq!r} at {pos}"
    if kind == "truncate":
        cut = rng.randrange(len(raw))
        return raw[:cut], f"{desc} to {cut}"
    if kind == "huge_line":
        size = rng.randint(1, 6) << 20
        how = rng.choice(["ja", "junk", "key", "header"])
        if how == "ja":
            i = pick(normal)
            f = lines[i].split("\t")
            f[3] = "あ" * (size // 3)
            lines[i] = "\t".join(f)
        elif how == "junk":
            lines.insert(rng.randrange(1, len(lines)), "x" * size)
        elif how == "key":
            lines.insert(rng.randrange(1, len(lines)), f"I2LS_UI\t{'k' * size}\t0123456789abcdef\tja")
        else:
            lines.insert(1, f"#comment\t{'h' * size}")
        return join(), f"{desc} {how} {size >> 20}MB"
    if kind == "empty":
        v = rng.choice([b"", b"\n", b"\r\n", b"\xef\xbb\xbf", b"#TPCJP-PATCH\t1\n", b"#TPCJP-PATCH\t1", b" \n\t\n",
                        b"\x00" * 100, raw[:raw.index(b"\n#built")] + b"\n"])
        return v, f"{desc} {v[:40]!r}"
    if kind == "flip":
        b = bytearray(raw)
        for _ in range(rng.randint(1, 8)):
            b[rng.randrange(len(b))] = rng.randrange(256)
        return bytes(b), desc
    if kind == "lines_shuffle":
        sub = lines[1:]
        rng.shuffle(sub)
        lines[1:] = sub
        return join(), desc
    if kind == "lines_dup":
        for _ in range(rng.randint(1, 20)):
            i = pick(body)
            f = lines[i].split("\t")
            if not lines[i].startswith("&") and rng.random() < 0.5:
                f[3] = "重複テスト"
            lines.append("\t".join(f))
        return join(), desc
    if kind == "lines_delete":
        k = rng.randint(1, max(1, len(body) - 1))
        drop = set(rng.sample(body, min(k, len(body))))
        lines = [l for i, l in enumerate(lines) if i not in drop]
        return join(), f"{desc} {len(drop)}"
    if kind == "crlf":
        return "\r\n".join(lines).encode("utf-8", "surrogateescape"), desc
    if kind == "cr_only":
        return "\r".join(lines).encode("utf-8", "surrogateescape"), desc
    if kind == "nul":
        i = pick(body)
        pos = rng.randrange(len(lines[i]) + 1)
        lines[i] = lines[i][:pos] + "\x00" + lines[i][pos:]
        return join(), f"{desc} line {i + 1}"
    if kind == "sep_inject":
        i = pick(normal)
        f = lines[i].split("\t")
        inj = rng.choice(["[i2t]", "[i2t]Injected/Key=注入", "=", "[i2t]" + f[1] + "=二重", "[i2t]="])
        f[3] = f[3] + inj
        lines[i] = "\t".join(f)
        return join(), f"{desc} line {i + 1} {inj!r}"
    if kind in ("base_bad", "all_bases_bad"):
        targets = body if kind == "all_bases_bad" else [pick(body) for _ in range(rng.randint(1, 30))]
        v = rng.choice(["", "zzzz", "0123456789ABCDEF", "00000000000000000", "x" * 16])
        for i in targets:
            f = lines[i].split("\t")
            if len(f) >= 3:
                f[2] = v if v != "0123456789ABCDEF" else f[2].upper()
                lines[i] = "\t".join(f)
        return join(), f"{desc} {v!r} x{len(targets)}"
    if kind == "key_weird":
        i = pick(body)
        f = lines[i].split("\t")
        f[1] = rng.choice(["", "=", f[1] + "=x", "[i2t]", f[1] + "[i2t]"])
        lines[i] = "\t".join(f)
        return join(), f"{desc} line {i + 1} {f[1][-10:]!r}"
    if kind == "ref_bad":
        i = pick(refs)
        f = lines[i].split("\t")
        if len(f) == 5:
            which = rng.randrange(3)
            if which == 0:
                f[3] = "I2LS_DoesNotExist"
            elif which == 1:
                f[4] = "No/Such/Key"
            else:
                f[3], f[4] = "I2LS_UI", "UI/General/ModItem"  # a term without official Japanese
        lines[i] = "\t".join(f)
        return join(), f"{desc} line {i + 1}"
    if kind == "bom":
        return rng.choice([b"\xef\xbb\xbf" + raw, b"\xff\xfe" + raw, b"\xfe\xff" + raw]), desc
    if kind == "header_values":
        lines.insert(1, rng.choice(["#", "#\t", "#bundle_sha256\t\\q", "#patch_version", "#x\ty\tz", "#game_version\t\t"]))
        return join(), f"{desc} {lines[1]!r}"
    # combo
    out, parts = raw, []
    for _ in range(rng.randint(2, 3)):
        try:
            out, d = mutate_data(rng, out, depth + 1)
        except (IndexError, ValueError):  # not applicable to the already mutated file
            try:
                out, d = mutate_data(rng, out, depth + 1, force="flip")
            except (IndexError, ValueError):  # empty file
                d = "(nothing left to mutate)"
        parts.append(d)
    return out, "combo[" + "; ".join(parts) + "]"


CUT_VARIANTS = ["final_newline_removed", "last_line_cut", "cut_after_cr", "bom_no_newline", "crlf_complete",
                 "header_only_no_newline", "trailing_blank_lines"]


def cut_variant(rng, raw: bytes):
    """CS-06 (round-2 fix): data whose last line has no newline is cut, even when #entries still matches.
    Returns (bytes, description); crlf_complete / trailing_blank_lines are complete files that must be accepted."""
    kind = rng.choice(CUT_VARIANTS)
    body = raw.rstrip(b"\n")
    last_start = body.rindex(b"\n") + 1
    last = body[last_start:].decode("utf-8")
    if kind == "final_newline_removed":
        return body, kind
    if kind == "last_line_cut":
        # cut inside the translation of the last line (on a character boundary): 4 fields, #entries unchanged
        f = last.split("\t")
        cut = rng.randrange(1, max(2, len(f[-1])))
        f[-1] = f[-1][:cut]
        return body[:last_start] + "\t".join(f).encode("utf-8"), f"{kind} (translation {len(last.split(chr(9))[-1])} -> {cut} chars)"
    if kind == "cut_after_cr":
        return body.replace(b"\n", b"\r\n") + b"\r", kind
    if kind == "bom_no_newline":
        return b"\xef\xbb\xbf" + body, kind
    if kind == "crlf_complete":
        return body.replace(b"\n", b"\r\n") + b"\r\n", kind
    if kind == "header_only_no_newline":
        return b"#TPCJP-PATCH\t1\n#entries\t0", kind
    return raw + rng.choice([b"\n", b"\n\n", b"\r\n", b"\n\r\n"]), kind


def datacut_case(idx: int, seed: int, exe: str, verbose=False, keep=False) -> dict:
    return data_case(idx, seed, exe, verbose, keep, suite="datacut")


def data_case(idx: int, seed: int, exe: str, verbose=False, keep=False, suite="data") -> dict:
    s = shared()
    rng = random.Random(case_seed(seed, suite, idx))
    case_dir = ROOT / "cases" / f"{suite}_{idx:05d}"
    game = case_dir / "game"
    scen = rng.choice(["fresh", "fresh", "patched"])
    rmtree(case_dir)
    make_game(game, s["official"])
    if scen == "patched":
        bundle_path(game).write_bytes(s["patched"])
        backup_path(game).write_bytes(s["official"])
        state_path(game).write_bytes(s["state"])
    special = rng.random() < 0.04 if suite == "data" else False
    data_file = case_dir / "data.tsv"
    if suite == "datacut":
        how = None
        raw, desc = cut_variant(rng, s["data_raw"])
        data_file.write_bytes(raw)
    elif special:
        how = rng.choice(["missing", "directory", "locked"])
        desc = f"path_{how}"
        if how == "directory":
            data_file.mkdir()
        elif how == "locked":
            data_file.write_bytes(s["data_raw"])
        raw = None
    else:
        how = None
        raw, desc = mutate_data(rng, s["data_raw"])
        data_file.write_bytes(raw)
    model_kind = None
    try:
        model = parse_data(raw) if raw is not None else None
        model_err = None if raw is not None else "path: not a readable file"
        model_kind = None if raw is not None else "path"
    except DataError as e:
        model, model_err, model_kind = None, str(e), e.kind
    ops = rng.choice([["apply"], ["apply"], ["apply", "restore"], ["restore"]])
    result = {"suite": suite, "index": idx, "seed": case_seed(seed, suite, idx), "scenario": scen,
              "mutation": desc, "model": "invalid: " + model_err if model_err else f"valid ({model['count']} entries)",
              "ops": [], "problems": []}
    _CUR["result"] = result
    if verbose:
        print(f"{suite}:{idx} scenario={scen} mutation={desc} model={result['model']}\n  sandbox {game}")
    bdir = bundle_path(game).parent
    for k, op in enumerate(ops):
        args = [op, "--game", str(game), "--data", str(data_file), "--no-language" if op == "apply" else "--keep-language"]
        pre = snapshot(bdir)
        if how == "locked":
            with Locker([(data_file, False)]):
                r = run_exe(Path(exe), args, case_dir, verbose, watch=[bdir])
        else:
            r = run_exe(Path(exe), args, case_dir, verbose, watch=[bdir])
        post = snapshot(bdir)
        probs = generic_problems(r) + check_data_run(op, r, pre, post, game, model, model_err, how, model_kind)
        result["ops"].append({"args": [a if a != str(game) else "<sandbox>" for a in args], "rc": r["rc"],
                              "elapsed": r["elapsed"], "out": r["out"].strip()[-400:]})
        for code, detail in probs:
            result["problems"].append({"op": k, "code": code, "detail": detail})
        if verbose:
            for code, detail in probs:
                print(f"  PROBLEM [{code}] {detail[:500]}")
        if r["timeout"] or r["rc"] not in (0, 1):
            break
    if not keep and not result["problems"]:
        rmtree(case_dir)
    result["sandbox"] = str(game)
    return result


ACCEPTED_CODES = {"utf8": "invalid_utf8_accepted", "entries": "truncated_data_accepted",
                  "newline": "cut_last_line_accepted", "content": "term_separator_accepted", "nul": "nul_data_accepted"}
DATA_CUT = "翻訳データが途中で切れています（最後の行に改行がありません）"


def check_data_run(op, r, pre, post, game, model, model_err, special, model_kind=None) -> list:
    s = shared()
    probs = []
    if r["rc"] not in (0, 1) or r["timeout"] or r.get("busy_final"):
        return probs
    bname, kname, sname = BUNDLE_REL.name, BUNDLE_REL.name + ".tpcjp-original", BUNDLE_REL.name + ".tpcjp-state"
    left = [n for n in post if n.endswith(TMP_SUFFIX)]
    if left:
        probs.append(("tmp_left", f"{left}"))
    out = r["out"]
    last = out.strip().splitlines()[-1][:200] if out.strip() else ""
    if model_kind == "newline" and DATA_CUT not in out:
        # CS-06 fix: apply refuses it, restore falls back to the state file, both naming the cut data
        probs.append(("cut_data_message", f"{op}: data without a final newline, expected '{DATA_CUT}' in the output: {last}"))
    if special is not None and op == "apply" and (r["rc"] != 1 or DATA_UNREADABLE not in out):
        # FZ-11 fix: a missing / locked file or a folder is reported as unreadable translation data
        probs.append(("data_path_message", f"--data {special}: expected exit 1 with '{DATA_UNREADABLE}: <path>', "
                      f"got exit {r['rc']}: {last}"))
    if op == "apply":
        if r["rc"] == 1:
            changed = [n for n in set(pre) | set(post) if pre.get(n) != post.get(n)]
            if changed:
                probs.append(("changed_on_failure", f"files changed although the exe exited 1: {sorted(changed)}"))
            if model is not None:
                base_texts = json_texts_for(pre, game)
                want, applied = model_apply(base_texts, model)
                nul = [n for n, t in want.items() if "\x00" in t and "\x00" not in base_texts.get(n, "")]
                if applied > 0 and nul:
                    # a translation with U+0000 is refused by the exe's output check (a TextAsset may not contain
                    # NUL since the FZ-05 fix); the message must name the translation data, not the game's file
                    if "翻訳データ" not in last:
                        probs.append(("nul_translation_blamed_on_game_file", f"a translation with U+0000 (in {nul[:3]}) "
                                      f"is refused with a message about the game's file: {last}"))
                elif applied > 0:
                    probs.append(("valid_data_rejected", f"the model applies {applied} entries but the exe failed: {last}"))
            elif "翻訳データ" not in out and UNEXPECTED not in out:
                probs.append(("unclear_message", "invalid data rejected without naming the translation data: " + last))
            return probs
        # exit 0
        if model is None:
            probs.append((ACCEPTED_CODES.get(model_kind, "invalid_data_accepted"),
                          f"data the model rejects ({model_err}) was applied"))
            return probs
        texts, err = try_read_texts(bundle_path(game))
        if texts is None:
            probs.append(("output_unloadable", f"output does not load: {err}"))
            return probs
        backup_texts = s["official_texts"]
        want, applied = model_apply(backup_texts, model)
        if texts != want:
            bad = [n for n in want if texts.get(n) != want[n]]
            probs.append(("output_mismatch", f"output differs from model in {bad[:5]}"))
        if key_lists(texts) != key_lists(backup_texts):
            probs.append(("term_structure_changed", "the patched bundle has different I2 terms than the official one "
                          "(a translation containing the [i2t] separator was written unchecked)"))
        if post.get(kname) is None or sha(post[kname]) != s["official_sha"]:
            probs.append(("backup_not_official", "after apply the backup is not the official file"))
        st = read_state(post[sname]) if sname in post else {}
        if st.get("patched_sha256") != sha(post[bname]) or st.get("original_sha256") != s["official_sha"]:
            probs.append(("state_mismatch", "state file does not describe the bundle / backup"))
    else:
        # restore never depends on the data: unreadable data falls back to the state file (FZ-01 / FZ-11 fix)
        if model is None and RESTORE_DATA_FALLBACK not in out:
            probs.append(("restore_no_fallback", f"restore with unreadable data ({model_err}) did not say "
                          f"'{RESTORE_DATA_FALLBACK}': {last}"))
        if r["rc"] == 1:
            probs.append(("restore_failed", f"restore failed (the sandbox holds the official file or a patch with a "
                          f"good backup and state file): {last}"))
            changed = [n for n in set(pre) | set(post) if pre.get(n) != post.get(n)]
            if changed:
                probs.append(("changed_on_failure", f"files changed although the exe exited 1: {sorted(changed)}"))
        else:
            cur = post.get(bname)
            if cur is None or sha(cur) != s["official_sha"]:
                probs.append(("restore_not_official", "restore exited 0 but the bundle is not the official file"))
            if kname in post or sname in post:
                probs.append(("restore_left_files", f"after restore: {sorted(post)}"))
    return probs


def json_texts_for(pre, game):
    s = shared()
    k = BUNDLE_REL.name + ".tpcjp-original"
    return s["official_texts"] if pre.get(k) is None or sha(pre[k]) == s["official_sha"] else bundles.read_texts(backup_path(game))


# =====================================================================================================
# preferences suite
# =====================================================================================================

class MP:
    """Offset-keeping MessagePack reader, like Preferences.cs: nesting deeper than 32 levels and a key repeated
    inside a map are errors (FZ-04 / FZ-12 fix); every length is checked against the remaining bytes (FZ-03)."""

    MAX_DEPTH = 32

    class Err(Exception):
        pass

    def __init__(self, b: bytes, max_depth=MAX_DEPTH, dup_check=True):
        self.b, self.max_depth, self.dup_check = b, max_depth, dup_check

    @classmethod
    def legacy(cls, b: bytes):
        """The reader of the first campaign (no depth limit in practice, first key wins): used only to place the
        mutations, so that every case index still produces the same input as before the fixes."""
        return cls(b, max_depth=3000, dup_check=False)

    def need(self, pos, n):
        if n < 0 or pos + n > len(self.b):
            raise MP.Err("truncated")

    def read(self, pos, depth=0):
        b = self.b
        self.need(pos, 1)
        if depth > self.max_depth:
            raise MP.Err("too deep")
        t = b[pos]
        start = pos

        def node(v, end, kind="scalar"):
            return {"v": v, "s": start, "e": end, "k": kind}
        if t <= 0x7F:
            return node(t, pos + 1, "int")
        if t >= 0xE0:
            return node(t - 256, pos + 1, "int")
        if 0x80 <= t <= 0x8F:
            return self.map(pos + 1, t & 15, start, depth)
        if 0x90 <= t <= 0x9F:
            return self.arr(pos + 1, t & 15, start, depth)
        if 0xA0 <= t <= 0xBF:
            return self.raw(pos + 1, t & 31, start, "str")
        if t == 0xC0:
            return node(None, pos + 1, "nil")
        if t in (0xC2, 0xC3):
            return node(t == 0xC3, pos + 1, "bool")
        ints = {0xCC: ">B", 0xCD: ">H", 0xCE: ">I", 0xCF: ">Q", 0xD0: ">b", 0xD1: ">h", 0xD2: ">i", 0xD3: ">q"}
        if t in ints:
            n = struct.calcsize(ints[t])
            self.need(pos + 1, n)
            v = struct.unpack_from(ints[t], b, pos + 1)[0]
            return node(s64(v) if t == 0xCF else v, pos + 1 + n, "int")  # C#: (long)ulong
        if t in (0xCA, 0xCB):
            n = 4 if t == 0xCA else 8
            self.need(pos + 1, n)
            return node(struct.unpack_from(">f" if n == 4 else ">d", b, pos + 1)[0], pos + 1 + n, "float")
        lens = {0xD9: (1, "str"), 0xDA: (2, "str"), 0xDB: (4, "str"), 0xC4: (1, "bin"), 0xC5: (2, "bin"), 0xC6: (4, "bin")}
        if t in lens:
            n, kind = lens[t]
            self.need(pos + 1, n)
            return self.raw(pos + 1 + n, int.from_bytes(b[pos + 1:pos + 1 + n], "big"), start, kind)
        if t in (0xDC, 0xDD, 0xDE, 0xDF):
            n = 2 if t in (0xDC, 0xDE) else 4
            self.need(pos + 1, n)
            cnt = int.from_bytes(b[pos + 1:pos + 1 + n], "big")
            return (self.arr if t <= 0xDD else self.map)(pos + 1 + n, cnt, start, depth)
        if 0xD4 <= t <= 0xD8:
            n = 1 << (t - 0xD4)
            self.need(pos + 2, n)
            return node(("ext", b[pos + 2:pos + 2 + n]), pos + 2 + n, "ext")
        if t in (0xC7, 0xC8, 0xC9):
            n = {0xC7: 1, 0xC8: 2, 0xC9: 4}[t]
            self.need(pos + 1, n + 1)
            ln = int.from_bytes(b[pos + 1:pos + 1 + n], "big")
            self.need(pos + 2 + n, ln)
            return node(("ext", b[pos + 2 + n:pos + 2 + n + ln]), pos + 2 + n + ln, "ext")
        raise MP.Err(f"bad type byte {t:#x}")

    def raw(self, pos, n, start, kind):
        self.need(pos, n)
        return {"v": self.b[pos:pos + n], "s": start, "e": pos + n, "k": kind}

    def arr(self, pos, n, start, depth):
        if n > len(self.b) - pos:
            raise MP.Err("truncated")
        items = []
        for _ in range(n):
            it = self.read(pos, depth + 1)
            items.append(it)
            pos = it["e"]
        return {"v": items, "s": start, "e": pos, "k": "arr"}

    def map(self, pos, n, start, depth):
        if n > len(self.b) - pos:
            raise MP.Err("truncated")
        pairs, seen = [], set()
        for _ in range(n):
            k = self.read(pos, depth + 1)
            v = self.read(k["e"], depth + 1)
            ident = MP.key_identity(k) if self.dup_check else None
            if ident is not None:
                if ident in seen:
                    raise MP.Err(f"duplicate key {k['v']!r}")
                seen.add(ident)
            pairs.append((k, v))
            pos = v["e"]
        return {"v": pairs, "s": start, "e": pos, "k": "map"}

    @staticmethod
    def key_identity(k):
        """Equality of the boxed C# key (Dictionary<object, Node>): long / string / bool / float / double by value,
        byte[] (bin, ext), lists and maps by reference (never equal), nil keys are not stored."""
        kind, v = k["k"], k["v"]
        if kind == "int":
            return ("long", v)
        if kind == "str":
            return ("string", v.decode("utf-8", "replace"))
        if kind == "bool":
            return ("bool", v)
        if kind == "float":
            width = "single" if k["e"] - k["s"] == 5 else "double"
            return (width, "nan" if v != v else (0.0 if v == 0 else v))
        if kind == "nil":
            return None
        return ("ref", id(k))


def mp_get(m, key: bytes, last=False):
    hits = [v for k, v in m["v"] if k["k"] == "str" and k["v"] == key]
    return (hits[-1] if last else hits[0]) if hits else None


def prefs_view(b: bytes, legacy=False) -> dict:
    """What Preferences.cs needs: the language map nodes (first and last occurrences), or an error."""
    try:
        root = (MP.legacy(b) if legacy else MP(b)).read(0)
        if root["e"] != len(b):
            return {"ok": False, "err": "trailing data"}
        if root["k"] != "arr" or len(root["v"]) < 3 or root["v"][2]["k"] != "map":
            return {"ok": False, "err": "layout"}
        lp = mp_get(root["v"][2], b"_languagePreferences")
        if lp is None or lp["k"] != "arr" or len(lp["v"]) < 3 or lp["v"][2]["k"] != "map":
            return {"ok": False, "err": "layout"}
        lm = lp["v"][2]
        sel = mp_get(lm, b"_selectedLanguage")
        sel_last = mp_get(lm, b"_selectedLanguage", last=True)
        lp_last = mp_get(root["v"][2], b"_languagePreferences", last=True)
        has = mp_get(lm, b"_hasExpectedPlatformLanguage")
        v = {"ok": True, "sel": sel["v"] if sel and sel["k"] == "int" else None, "sel_node": sel,
             "sel_last": sel_last["v"] if sel_last and sel_last["k"] == "int" else None,
             "has": bool(has and has["k"] == "bool" and has["v"] is True),
             "dup": (sel is not sel_last) or (lp is not lp_last)}
        return v
    except (MP.Err, RecursionError) as e:
        return {"ok": False, "err": str(e) or type(e).__name__}


def strict_msgpack_ok(b: bytes) -> bool:
    try:
        msgpack.unpackb(b, raw=True, strict_map_key=False, object_pairs_hook=lambda p: p)
        return True
    except Exception:  # noqa: BLE001
        return False


def encodable(header: int, value: int) -> bool:
    if header <= 0x7F:
        return 0 <= value <= 0x7F
    return header in (0xCC, 0xCD, 0xCE, 0xD2, 0xD0, 0xD1)


def enc_int(kind, v):
    return {"fix": lambda: bytes([v & 0x7F]), "cc": lambda: b"\xcc" + struct.pack(">B", v & 0xFF),
            "cd": lambda: b"\xcd" + struct.pack(">H", v & 0xFFFF), "ce": lambda: b"\xce" + struct.pack(">I", v & 0xFFFFFFFF),
            "cf": lambda: b"\xcf" + struct.pack(">Q", v & (2 ** 64 - 1)), "d0": lambda: b"\xd0" + struct.pack(">b", max(-128, min(127, v))),
            "d1": lambda: b"\xd1" + struct.pack(">h", max(-32768, min(32767, v))), "d2": lambda: b"\xd2" + struct.pack(">i", s32(v)),
            "d3": lambda: b"\xd3" + struct.pack(">q", s64(v)), "neg": lambda: bytes([0xE0 | (v & 0x1F)]),
            "f32": lambda: b"\xca" + struct.pack(">f", float(v)), "f64": lambda: b"\xcb" + struct.pack(">d", float(v)),
            "str": lambda: bytes([0xA0 | len(str(v))]) + str(v).encode(), "nil": lambda: b"\xc0",
            "true": lambda: b"\xc3", "bin": lambda: b"\xc4\x01" + bytes([v & 0xFF]), "arr": lambda: b"\x91" + bytes([v & 0x7F])}[kind]()


PREFS_MUTATIONS = {
    "none": 4, "flip": 6, "flip_lang": 8, "truncate": 5, "append": 3, "insert": 3, "delete": 3, "zero": 3,
    "sel_encoding": 10, "has_expected": 5, "rename_key": 4, "delete_pair": 3, "dup_key": 3, "container": 4,
    "big_len": 6, "nest_bomb": 3, "ext": 2, "readonly": 2, "locked": 2, "combo": 5,
}


def prefs_base(rng):
    """The shared real preferences.json with _selectedLanguage set to a random language (same width)."""
    s = shared()
    b = bytearray(s["prefs"])
    v = prefs_view(bytes(b))
    node = v["sel_node"]
    val = rng.choice([0, 0, 12, 12, 3, 9, 1])
    if b[node["s"]] <= 0x7F:
        b[node["s"]] = val
    return bytes(b), val


def mutate_prefs(rng, b: bytes, depth=0, force=None):
    kinds = list(PREFS_MUTATIONS)
    kind = force or rng.choices(kinds, [PREFS_MUTATIONS[k] for k in kinds])[0]
    if depth and kind in ("combo", "readonly", "locked", "none"):
        kind = "flip"
    v = prefs_view(b, legacy=True)
    b = bytearray(b)
    desc = kind
    if not v["ok"] and kind not in ("flip", "truncate", "append", "insert", "delete", "zero", "readonly", "locked", "none"):
        kind = desc = "flip"
    if kind in ("none", "readonly", "locked"):
        return bytes(b), desc
    root = MP.legacy(bytes(b)).read(0) if v["ok"] else None
    lp = mp_get(root["v"][2], b"_languagePreferences") if root else None
    lm = lp["v"][2] if lp else None
    if kind == "flip":
        for _ in range(rng.randint(1, 4)):
            b[rng.randrange(len(b))] = rng.randrange(256) if rng.random() < 0.5 else b[rng.randrange(len(b))] ^ (1 << rng.randrange(8))
    elif kind == "flip_lang":
        for _ in range(rng.randint(1, 3)):
            i = rng.randrange(lp["s"], lp["e"])
            b[i] ^= 1 << rng.randrange(8)
            desc += f" {i}"
    elif kind == "truncate":
        cut = rng.randrange(len(b))
        b = b[:cut]
        desc += f" {cut}"
    elif kind == "append":
        b += rng.randbytes(rng.randint(1, 64))
    elif kind == "insert":
        pos = rng.randrange(len(b) + 1)
        b[pos:pos] = rng.randbytes(rng.randint(1, 16))
        desc += f" at {pos}"
    elif kind == "delete":
        start = rng.randrange(len(b))
        del b[start:start + rng.randint(1, 32)]
        desc += f" at {start}"
    elif kind == "zero":
        start = rng.randrange(len(b))
        n = rng.randint(1, 64)
        b[start:start + n] = bytes(len(b[start:start + n]))
        desc += f" {start}+{n}"
    elif kind == "sel_encoding":
        node = mp_get(lm, b"_selectedLanguage")
        enc = rng.choice(["fix", "cc", "cd", "ce", "cf", "d0", "d1", "d2", "d3", "neg", "f32", "f64", "str", "nil", "true", "bin", "arr"])
        val = rng.choice([0, 3, 12, 12, 127, 200, 255, 300, 65535, 70000, -1, -100])
        new = enc_int(enc, val)
        b[node["s"]:node["e"]] = new
        desc += f" {enc}({val})"
    elif kind == "has_expected":
        node = mp_get(lm, b"_hasExpectedPlatformLanguage")
        new = rng.choice([b"\xc2", b"\xc0", b"\x01", b"\xa4true", b"\x91\xc3"])
        b[node["s"]:node["e"]] = new
        desc += f" -> {new!r}"
    elif kind == "rename_key":
        key = rng.choice([b"_selectedLanguage", b"_languagePreferences", b"_hasExpectedPlatformLanguage"])
        i = bytes(b).find(key)
        j = i + rng.randrange(len(key))
        b[j] = ord("X") if b[j] != ord("X") else ord("Y")
        desc += f" {key.decode()}"
    elif kind == "delete_pair":
        k, val = rng.choice(lm["v"])
        del b[k["s"]:val["e"]]
        if b[lm["s"]] & 0xF0 == 0x80:
            b[lm["s"]] -= 1
        desc += f" {k['v']!r}"
    elif kind == "dup_key":
        val = rng.choice([0, 3, 12])
        pair = b"\xb1_selectedLanguage" + bytes([val])
        at = rng.choice(["first", "last"])
        pos = lm["s"] + 1 if at == "first" else lm["e"]
        b[pos:pos] = pair
        b[lm["s"]] += 1
        desc += f" {at} value {val}"
    elif kind == "container":
        how = rng.choice(["lp_int", "lp_short", "lp_map", "lp_third_str", "root_map", "root_short", "root_wrap"])
        if how == "lp_int":
            b[lp["s"]:lp["e"]] = b"\x05"
        elif how == "lp_short":
            b[lp["s"]:lp["e"]] = b"\x92\x02\x06"
        elif how == "lp_map":
            b[lp["s"]:lp["e"]] = b"\x80"
        elif how == "lp_third_str":
            b[lp["s"]:lp["e"]] = b"\x93\x02\x06\xa1x"
        elif how == "root_map":
            b[0] = 0x83
        elif how == "root_short":
            b[0] = 0x92
        else:
            b[0:0] = b"\x91"
        desc += f" {how}"
    elif kind == "big_len":
        strs = []

        def walk(n):
            if n["k"] in ("str", "bin"):
                strs.append(n)
            elif n["k"] == "arr":
                for it in n["v"]:
                    walk(it)
            elif n["k"] == "map":
                for k, val in n["v"]:
                    walk(k)
                    walk(val)
        walk(root)
        node = rng.choice(strs)
        hdr_len = (node["e"] - node["s"]) - len(node["v"])
        n = rng.choice([0x7FFFFFF0, 0xFFFFFFFF, 0x80000000, 0x7FFFFFFF, len(node["v"]) + 1000, 0xFFFF])
        fam = rng.choice(["str32", "bin32", "arr32", "map32", "str16"])
        new = {"str32": b"\xdb", "bin32": b"\xc6", "arr32": b"\xdd", "map32": b"\xdf", "str16": b"\xda"}[fam]
        new += struct.pack(">I", n) if fam != "str16" else struct.pack(">H", n & 0xFFFF)
        b[node["s"]:node["s"] + hdr_len] = new
        desc += f" {fam}={n:#x} at {node['s']}"
    elif kind == "nest_bomb":
        node = mp_get(lm, b"_selectedLanguage")
        d = rng.choice([100, 1000, 5000, 20000, 100000, 300000])
        b[node["s"]:node["s"]] = b"\x91" * d
        desc += f" depth {d}"
    elif kind == "ext":
        node = mp_get(lm, rng.choice([b"_selectedAudioLanguage", b"_expectedPlatformLanguage"]))
        new = rng.choice([b"\xd4\x01\x02", b"\xd8\x05" + bytes(16), b"\xc7\x03\x01abc", b"\xc9\x00\x00\x00\x02\x01ab"])
        if node:
            b[node["s"]:node["e"]] = new
    elif kind == "combo":
        out, parts = bytes(b), []
        for _ in range(rng.randint(2, 3)):
            try:
                out, d = mutate_prefs(rng, out, depth + 1)
            except (TypeError, IndexError, ValueError, KeyError):  # key removed by an earlier mutation
                out, d = mutate_prefs(rng, out, depth + 1, force="flip")
            parts.append(d)
        return out, "combo[" + "; ".join(parts) + "]"
    return bytes(b), desc


def dotnet_read_text(b: bytes) -> str:
    """File.ReadAllText(path): UTF-8 by default, with BOM detection (the BOM is not part of the text)."""
    if b[:3] == b"\xef\xbb\xbf":
        return b[3:].decode("utf-8", "replace")
    if b[:2] == b"\xff\xfe":
        return b[2:].decode("utf-16-le", "replace")
    if b[:2] == b"\xfe\xff":
        return b[2:].decode("utf-16-be", "replace")
    return b.decode("utf-8", "replace")


def dotnet_int(text):
    """string.Trim() then int.TryParse (NumberStyles.Integer)."""
    t = text.strip(" \t\r\n\v\f \u0085  　")
    t = t.rstrip("\0").rstrip(" \t\r\n\v\f")  # .NET number parsing ignores trailing NULs
    if re.fullmatch(r"[+-]?[0-9]+", t) and -2 ** 31 <= int(t) < 2 ** 31:
        return int(t)
    return None


PREV_CONTENTS = [None, None, "0", "3", "12", " 7 \r\n", "-1", "99999999999", "abc", "", "+5", "1e3", "200",
                 "﻿9", "3\x00", "DIR", b"\xff\xfe\x00garbage"]


def prefs_case(idx: int, seed: int, exe: str, verbose=False, keep=False) -> dict:
    rng = random.Random(case_seed(seed, "prefs", idx))
    case_dir = ROOT / "cases" / f"prefs_{idx:05d}"
    rmtree(case_dir)
    cloud = case_dir / "prefs"
    appdata = case_dir / "appdata"
    cloud.mkdir(parents=True)
    accounts = []
    n_acc = rng.choices([1, 2, 3], [70, 20, 10])[0]
    descs = []
    lock_files, readonly = [], []
    for a in range(n_acc):
        acct = str(rng.randint(10 ** 8, 10 ** 10))
        d = cloud / acct
        d.mkdir()
        base, val = prefs_base(rng)
        if a == 0 or rng.random() < 0.3:
            data, desc = mutate_prefs(rng, base)
        else:
            data, desc = base, "none"
        descs.append(f"{acct}: base {val}, {desc}")
        f = d / "preferences.json"
        f.write_bytes(data)
        if desc.startswith("readonly") or "readonly" in desc.split("[")[0]:
            os.chmod(f, stat.S_IREAD)
            readonly.append(f)
        if desc == "locked":
            lock_files.append((f, False))
        accounts.append((acct, f))
    command = rng.choices(["ja", "previous", "en"], [50, 40, 10])[0]
    # apply / restore without --no-language / --keep-language switch the language like "language ja" /
    # "language previous" (separate RNG: the inputs of the other cases are unchanged)
    crng = random.Random(case_seed(seed, "prefs-command", idx))
    if command == "ja" and crng.random() < 0.12:
        command = "apply"
    elif command == "previous" and crng.random() < 0.15:
        command = "restore"
    prev = {}
    for acct, _ in accounts:
        c = rng.choice(PREV_CONTENTS)
        if c is None:
            continue
        pf = appdata / "previous-language" / f"{acct}.txt"
        pf.parent.mkdir(parents=True, exist_ok=True)
        if c == "DIR":
            pf.mkdir()
        else:
            pf.write_bytes(c if isinstance(c, bytes) else c.encode("utf-8"))
        prev[acct] = c
    if prev:
        descs.append(f"previous-language files: {prev!r}")
    result = {"suite": "prefs", "index": idx, "seed": case_seed(seed, "prefs", idx), "scenario": command,
              "mutation": " | ".join(descs), "ops": [], "problems": []}
    before = {acct: f.read_bytes() for acct, f in accounts}
    if keep:
        for acct, f in accounts:  # the mutated files, for a manual repro
            (case_dir / "input" / acct).mkdir(parents=True, exist_ok=True)
            (case_dir / "input" / acct / "preferences.json").write_bytes(before[acct])
    game = case_dir / "game"
    if command in ("apply", "restore"):
        make_game(game, shared()["official"])
        args = [command, "--game", str(game), "--data", str(shared()["data_path"])]
    else:
        args = ["language", command]
    if verbose:
        print(f"prefs:{idx} {' '.join(args)}\n  " + "\n  ".join(descs) + f"\n  sandbox {case_dir}")
    with Locker(lock_files):
        r = run_exe(Path(exe), args, case_dir, verbose, watch=[cloud, appdata] + ([game] if game.exists() else []))
    probs = generic_problems(r)
    if not r["timeout"] and r["rc"] in (0, 1) and not r["busy_final"]:
        lang = {"apply": "ja", "restore": "previous"}.get(command, command)
        for acct, f in accounts:
            probs += check_prefs_file(acct, f, before[acct], f.read_bytes(), lang, r, prev.get(acct), appdata,
                                      f in readonly, any(f == lf for lf, _ in lock_files))
        probs += check_prefs_run(command, lang, r, accounts, before, lock_files, game)
    result["ops"].append({"args": [a if a != str(game) else "<sandbox>" for a in args], "rc": r["rc"],
                          "elapsed": r["elapsed"], "out": r["out"].strip()[-600:]})
    for code, detail in probs:
        result["problems"].append({"op": 0, "code": code, "detail": detail})
    if verbose:
        for code, detail in probs:
            print(f"  PROBLEM [{code}] {detail[:500]}")
    for f in readonly:
        os.chmod(f, stat.S_IWRITE | stat.S_IREAD)
    if not keep and not result["problems"]:
        rmtree(case_dir)
    result["sandbox"] = str(case_dir)
    return result


def check_prefs_file(acct, f, b0, b1, command, r, prev_content, appdata, readonly, locked) -> list:
    probs = []
    tmp = [p.name for p in f.parent.iterdir() if p.name.endswith(TMP_SUFFIX)]
    if tmp:
        probs.append(("tmp_left", f"{acct}: {tmp}"))
    v0 = prefs_view(b0)
    if command == "ja":
        target = 12
    elif command == "en":
        target = 0
    else:
        pc = None if prev_content in (None, "DIR") else dotnet_read_text(
            prev_content if isinstance(prev_content, bytes) else prev_content.encode("utf-8"))
        p = dotnet_int(pc) if pc is not None else None
        # round 3c review: only one of the game's languages 0..12 other than Japanese is restored (Preferences.IsGameLanguage)
        target = p if p is not None and p != 12 and 0 <= p <= 12 else 0
    applicable = command != "previous" or (v0["ok"] and v0["sel"] == 12)
    if b1 != b0:
        if not v0["ok"]:
            # too deep (FZ-04), a repeated key (FZ-12), a length beyond the file (FZ-03), ...: must be refused
            probs.append(("prefs_invalid_file_changed", f"{acct}: a file the reader must reject ({v0['err']}) was rewritten"))
        if command == "ja" and prev_content == "DIR" and "注意: 元の表示言語を記録できませんでした" not in r["out"]:
            probs.append(("prefs_previous_warning_missing", f"{acct}: the previous language could not be recorded "
                          "(a folder is in the way) but no warning was printed"))
        v1 = prefs_view(b1)
        if not strict_msgpack_ok(b1) or not v1["ok"]:
            probs.append(("prefs_invalid_after", f"{acct}: the rewritten file is not a valid MessagePack document"))
            return probs
        if len(b1) != len(b0):
            probs.append(("prefs_length_changed", f"{acct}: {len(b0)} -> {len(b1)} bytes"))
        diff = [i for i in range(min(len(b0), len(b1))) if b0[i] != b1[i]]
        if diff and diff[-1] - diff[0] >= 9:
            probs.append(("prefs_other_bytes_changed", f"{acct}: bytes {diff[0]}..{diff[-1]} changed"))
        if v1["sel"] != target:
            probs.append(("prefs_wrong_language", f"{acct}: _selectedLanguage {v1['sel']} after 'language {command}', expected {target}"))
        if v1["dup"] and v1["sel_last"] != v1["sel"]:
            probs.append(("prefs_duplicate_key_ignored", f"{acct}: the first _selectedLanguage was changed to {v1['sel']}, "
                          f"a later duplicate still says {v1['sel_last']} (reported as changed)"))
        backups = list((appdata / "backup" / "preferences" / acct).glob("*.json")) if (appdata / "backup").exists() else []
        if not any(p.read_bytes() == b0 for p in backups):
            probs.append(("prefs_no_backup", f"{acct}: changed without a backup of the previous file"))
        if command == "ja" and v0["ok"] and v0["sel"] is not None:
            pf = appdata / "previous-language" / f"{acct}.txt"
            if not pf.is_file() or pf.read_text("utf-8", "replace") != str(v0["sel"]):
                if not (prev_content == "DIR"):
                    probs.append(("prefs_previous_not_recorded", f"{acct}: previous language {v0['sel']} not recorded"))
        if not applicable:
            probs.append(("prefs_changed_not_applicable", f"{acct}: changed by 'language previous' although it was not Japanese"))
        return probs
    # unchanged
    if r["rc"] == 0 and applicable:
        if not v0["ok"]:
            probs.append(("prefs_unparseable_accepted", f"{acct}: exit 0, file unchanged, but it is not readable ({v0['err']})"))
        elif v0["sel"] != target:
            probs.append(("prefs_success_without_change", f"{acct}: exit 0 but _selectedLanguage is still {v0['sel']} (wanted {target})"))
    if r["rc"] == 0 and command == "previous" and not applicable and not v0["ok"]:
        probs.append(("prefs_unreadable_ignored", f"{acct}: 'language previous' exited 0 although the file is unreadable ({v0['err']})"))
    # a file Preferences.cs should be able to change but the exe refused
    if (applicable and v0["ok"] and v0["sel"] is not None and v0["sel"] != target and v0["has"] and not readonly
            and not locked and not v0["dup"] and encodable(b0[v0["sel_node"]["s"]], target)
            and target_fits(b0[v0["sel_node"]["s"]], target)):
        probs.append(("prefs_valid_file_refused", f"{acct}: a valid, changeable file was not changed (exit {r['rc']}): "
                      + r["out"].strip().splitlines()[-1][:200]))
    return probs


def check_prefs_run(command, lang, r, accounts, before, lock_files, game) -> list:
    """Messages and the game files of one prefs-suite run (all accounts together)."""
    probs = []
    out = r["out"]
    locked = {lf for lf, _ in lock_files}
    views = {acct: prefs_view(before[acct]) for acct, _ in accounts}
    if lang == "previous":
        # I-1 fix: no account in Japanese and nothing failed -> "表示言語は変更しませんでした", exit 0
        none_ja = all(f not in locked and views[a]["ok"] and views[a]["sel"] is not None and views[a]["sel"] != 12
                      for a, f in accounts)
        if none_ja and (NO_JAPANESE_ACCOUNT not in out or r["rc"] != 0):
            probs.append(("no_japanese_account_message", f"no account was Japanese: expected exit 0 and "
                          f"'{NO_JAPANESE_ACCOUNT}', got exit {r['rc']}: {out.strip().splitlines()[-1][:200]}"))
        if not none_ja and NO_JAPANESE_ACCOUNT in out:
            probs.append(("no_japanese_account_message_wrong", "'表示言語は変更しませんでした' although an account "
                          "was Japanese or could not be read"))
    if command == "apply":
        cur = bundle_path(game).read_bytes() if bundle_path(game).exists() else b""
        if sha(cur) != shared()["golden_sha"]:
            probs.append(("apply_not_patched", f"apply (with the language switch) left the bundle unpatched (exit {r['rc']})"))
        if r["rc"] == 1 and "日本語データは適用しましたが、表示言語を日本語に切り替えられませんでした" not in out:
            probs.append(("apply_language_summary", "apply failed in the language step without the summary line"))
    if command == "restore":
        cur = bundle_path(game).read_bytes() if bundle_path(game).exists() else b""
        if sha(cur) != shared()["official_sha"] or backup_path(game).exists() or state_path(game).exists():
            probs.append(("restore_changed_game", "restore on an unpatched sandbox changed the game files"))
        if r["rc"] == 1 and "日本語データは元に戻しましたが、表示言語を戻せませんでした" not in out:
            probs.append(("restore_language_summary", "restore failed in the language step without the summary line"))
    return probs


def target_fits(header, value):
    if header <= 0x7F:
        return 0 <= value <= 0x7F
    return {0xCC: 0 <= value <= 255, 0xCD: 0 <= value <= 65535, 0xCE: value >= 0, 0xD2: True,
            0xD0: -128 <= value <= 127, 0xD1: -32768 <= value <= 32767}.get(header, False)


# =====================================================================================================
# update suite: valid game-side changes after a patch (Steam file verification, game updates)
# =====================================================================================================

UPDATE_EVENTS = {
    "steam_verify": 4,     # the official file is back (= the backup): status "公式のデータに戻っています"
    "valid_same": 4,       # a different valid bundle, version.txt unchanged: not a real update -> backup kept
    "valid_new": 4,        # a different valid bundle and a new version.txt: a real update -> new backup
    "valid_nostate": 2,    # like valid_same, but the state file is gone (version unknown) -> new backup
    "valid_oldstate": 2,   # like valid_same, state file of version 1.0.0 (no game_version) -> new backup
    "patched_new": 2,      # our output still installed, new version.txt (other game files updated)
    "official_new": 2,     # the official file is back and version.txt is new
    "backup_swapped": 2,   # the backup was replaced by another (valid) file, so it no longer has the hash recorded
                           # in the state file, and the installed bundle is damaged (cut): the backup must not be used
    # round-2 fixes (F-SBP-7): a run killed while replacing the bundle
    "bundle_missing": 3,   # the installed bundle is gone (File.Replace of 1.0.0 renamed it to "<name>~RF*.TMP"), backup and
                           # state are there, maybe with leftovers: apply rebuilds from the backup, restore restores it
    "leftovers": 2,        # patch installed, leftovers "<name>.tpcjp-tmp" / "<name>~RF*.TMP" of a killed run: removed
    "missing_nobackup": 1, # the bundle is gone and there is no backup (only a state file): apply exits 1 and changes
                           # nothing, restore removes the stale state file
}
BACKUP_MISMATCH = "バックアップが記録と一致しません"
BACKUP_OTHER_VERSION = "バックアップは別のバージョンのゲーム（"  # round 3 (F-SBP-8)
BACKUP_UNCONFIRMED_VERSION = "バックアップがどのバージョンのゲームのものか確認できません（"  # round 3b (R4): a version is unknown
UPDATE_OPS = [["apply"], ["restore"], ["apply", "restore"], ["restore", "apply"], ["apply", "apply"],
              ["apply", "restore", "apply"]]
JA_CHAR_RX = re.compile("[぀-ヿ一-鿿]")


def valid_update_bundle(rng):
    """The official bundle with one Japanese character of one I2 value replaced (same UTF-8 length): a valid,
    different file, as a game update would bring. Returns (bytes, texts, description)."""
    s = shared()
    info = sfinfo("official")
    sf = bytearray(s["ufs"]["official"].sf())

    def candidates(t):
        text = bytes(sf[t[3] + 4:t[3] + 4 + t[4]]).decode("utf-8")
        parts = text.split(SEP)
        return parts, [i for i, p in enumerate(parts) if "=" in p and JA_CHAR_RX.search(p.partition("=")[2])]
    usable = [t for t in info.texts if t[4] > 0 and candidates(t)[1]]  # TextAssets with a Japanese value
    start, size, nlen, spos, slen = rng.choice(usable)
    name = bytes(sf[start + 4:start + 4 + nlen]).decode("utf-8")
    parts, cands = candidates((start, size, nlen, spos, slen))
    i = rng.choice(cands)
    key, _, val = parts[i].partition("=")
    m = next(m for m in JA_CHAR_RX.finditer(val))
    new_char = "更" if m.group(0) != "更" else "新"
    parts[i] = f"{key}={val[:m.start()]}{new_char}{val[m.end():]}"
    new_text = SEP.join(parts)
    raw = new_text.encode("utf-8")
    assert len(raw) == slen, "same length replacement"
    sf[spos + 4:spos + 4 + slen] = raw
    texts = dict(s["official_texts"])
    texts[name] = new_text
    return s["ufs"]["official"].rebuild_sf(bytes(sf)), texts, f"{name}: {key} value changed"


def update_expect(op, pre, game, texts_of):
    """Oracle for the update suite (every file is valid): what Patcher.Apply / Restore must do.
    texts_of(bytes) -> texts. Returns (message, expected files {"bundle_texts"|"bundle", "backup", "state"})."""
    s = shared()
    bname, kname, sname = BUNDLE_REL.name, BUNDLE_REL.name + ".tpcjp-original", BUNDLE_REL.name + ".tpcjp-state"
    b, k, st_raw = pre.get(bname), pre.get(kname), pre.get(sname)
    st = read_state(st_raw) if st_raw is not None else None
    describes = b is not None and st is not None and st.get("patched_sha256") == sha(b)
    same = k is not None and same_version(st_raw, game)
    # round 3: the official file recorded in the state file (the backup is missing or another file)
    recorded = b is not None and b != k and not describes and st is not None and st.get("original_sha256") == sha(b)

    def refusal():
        # round 3 (Patcher.CheckBackupRecorded): where only the state file vouches for the backup, it must be the recorded
        # file, taken for the installed game version; round 3c (S1): a missing state file or one without game_version
        # cannot vouch (refused like an unknown recorded version); None when the backup may be used
        if st is not None and "original_sha256" in st and st["original_sha256"] != sha(k):
            return BACKUP_MISMATCH
        if same_version(st_raw, game):
            return None
        unknown = (None, "", "不明")
        v = st.get("game_version") if st is not None else None
        return BACKUP_UNCONFIRMED_VERSION if v in unknown or game_version(game) in unknown else BACKUP_OTHER_VERSION
    if b is None:
        # round-2 fix: a missing bundle is rebuilt / restored from the backup
        if refusal():
            return "REFUSE:" + refusal(), None
        if op == "apply":
            return "日本語データが見つからないため、バックアップした公式データから作り直します", {"source": k}
        return "日本語データを公式のものに戻しました", {"bundle": k}
    if op == "apply":
        if describes:
            return "適用済みのパッチを、バックアップした公式データから作り直します", {"source": k}
        if k is not None and k == b:
            return "公式の日本語データにパッチを適用します", {"source": k}
        if recorded:
            return "日本語データが記録されている公式データと一致するため、バックアップを作り直します", {"source": b}
        if model_is_patched(texts_of(b), s["data"]):
            return "以前に適用したパッチを検出しました", {"source": k}
        if same:
            if refusal():
                return "REFUSE:" + refusal(), None
            return "ゲームのバージョンは変わっていないため、バックアップした公式データから作り直します", {"source": k}
        return "バックアップを作成しました", {"source": b}
    if recorded:
        return "日本語データは公式のままです", {"bundle": b}
    if k is None:
        return "パッチは適用されていません", {"bundle": b}
    if b == k:
        return "日本語データは公式のままです", {"bundle": b}
    if describes or model_is_patched(texts_of(b), s["data"]):
        return "日本語データを公式のものに戻しました", {"bundle": k}
    if same:
        if refusal():
            return "REFUSE:" + refusal(), None
        return "ゲームのバージョンは変わっていないため、バックアップから戻します", {"bundle": k}
    return "日本語データはゲームのアップデートで新しくなっているため、そのままにします", {"bundle": b}


def update_case(idx: int, seed: int, exe: str, verbose=False, keep=False) -> dict:
    s = shared()
    rng = random.Random(case_seed(seed, "update", idx))
    event = rng.choices(list(UPDATE_EVENTS), list(UPDATE_EVENTS.values()))[0]
    case_dir = ROOT / "cases" / f"update_{idx:05d}"
    game = case_dir / "game"
    bdir = bundle_path(game).parent
    rmtree(case_dir)
    make_game(game, s["official"])
    bundle_path(game).write_bytes(s["patched"])
    backup_path(game).write_bytes(s["official"])
    state_path(game).write_bytes(s["state"])
    known = {sha(s["official"]): s["official_texts"]}
    desc = event
    if event in ("valid_same", "valid_new", "valid_nostate", "valid_oldstate"):
        upd, texts, d = valid_update_bundle(rng)
        known[sha(upd)] = texts
        bundle_path(game).write_bytes(upd)
        desc += f" ({d})"
    elif event in ("steam_verify", "official_new"):
        bundle_path(game).write_bytes(s["official"])
    elif event == "backup_swapped":
        upd, texts, d = valid_update_bundle(rng)
        backup_path(game).write_bytes(upd)
        cut = rng.randrange(1000, len(s["patched"]) - 1000)
        bundle_path(game).write_bytes(s["patched"][:cut])
        desc += f" (backup: {d}; bundle cut to {cut} bytes)"
    elif event in ("bundle_missing", "missing_nobackup"):
        bundle_path(game).unlink()
        if event == "missing_nobackup":
            backup_path(game).unlink()
    if event in ("bundle_missing", "leftovers"):
        # what a killed run leaves: File.Replace (1.0.0) renames the old file to "<name>~RF<hex>.TMP"; SafeFile writes
        # "<name>.tpcjp-tmp" first (maybe only partly written)
        left = []
        for p, content in ((bundle_path(game), s["patched"]), (backup_path(game), s["official"]),
                           (state_path(game), s["state"])):
            first = p == bundle_path(game)
            if rng.random() < (0.6 if first else 0.0 if event == "bundle_missing" else 0.4):
                rf = p.parent / f"{p.name}~RF{rng.getrandbits(24):x}.TMP"
                rf.write_bytes(content)
                left.append(rf.name)
            if rng.random() < (0.5 if first else 0.0 if event == "bundle_missing" else 0.4):
                tmp = Path(str(p) + TMP_SUFFIX)
                tmp.write_bytes(content[:rng.randrange(len(content) + 1)])
                left.append(tmp.name)
        desc += f" (leftovers: {', '.join(n.replace(BUNDLE_REL.name, '<bundle>') for n in left) or 'none'})"
    if event in ("valid_new", "patched_new", "official_new"):
        (game / "version.txt").write_text(NEW_GAME_VERSION, "utf-8")
    if event == "bundle_missing":
        # round 3c (S1): a lost bundle with a state file that cannot vouch for the backup (none, or one without
        # game_version): apply and restore refuse (separate RNG: the inputs of the other cases are unchanged)
        vr = random.Random(case_seed(seed, "update-s1", idx)).random()
        if vr < 0.15:
            state_path(game).unlink()
            desc += " (state removed)"
        elif vr < 0.3:
            state_path(game).write_bytes(b"".join(l + b"\r\n" for l in s["state"].splitlines() if not l.startswith(b"game_version=")))
            desc += " (state without game_version)"
    if event == "valid_nostate":
        state_path(game).unlink()
    if event == "valid_oldstate":
        state_path(game).write_bytes(b"".join(l + b"\r\n" for l in s["state"].splitlines() if not l.startswith(b"game_version=")))
    ops = rng.choice(UPDATE_OPS)
    use_data = [rng.random() < 0.5 for _ in ops]
    result = {"suite": "update", "index": idx, "seed": case_seed(seed, "update", idx), "scenario": event,
              "mutation": desc, "ops": [], "problems": []}
    _CUR["result"] = result
    if keep and bundle_path(game).exists():  # bundle_missing / missing_nobackup have none
        (case_dir / "input.bundle").write_bytes(bundle_path(game).read_bytes())
    if verbose:
        print(f"update:{idx} event={desc}\n  sandbox {game}")

    def texts_of(b: bytes):
        h = sha(b)
        if h not in known:
            p = case_dir / "check.bundle"
            p.write_bytes(b)
            known[h], err = try_read_texts(p)
            p.unlink()
            if known[h] is None:
                raise RuntimeError(f"update suite: a file does not load ({err})")
        return known[h]

    def add(k, probs):
        for code, detail in probs:
            result["problems"].append({"op": k, "code": code, "detail": detail})
            if verbose:
                print(f"  PROBLEM [{code}] {detail[:500]}")

    r = run_status(Path(exe), game, case_dir, verbose)
    add(-1, check_status(r, game))
    if event in ("steam_verify", "official_new") and STATUS_TEXT["reverted"] not in r["out"]:
        add(-1, [("status_not_reverted", "the official file is back after a patch, but status does not say "
                  f"'{STATUS_TEXT['reverted']}'")])
    for k, op in enumerate(ops):
        args = [op, "--game", str(game)] + (["--data", str(s["data_path"])] if use_data[k] else []) + \
               (["--no-language"] if op == "apply" else ["--keep-language"])
        pre = snapshot(bdir)
        if event == "backup_swapped":
            r = run_exe(Path(exe), args, case_dir, verbose, watch=[bdir])
            post = snapshot(bdir)
            probs = generic_problems(r)
            result["ops"].append({"args": [a if a != str(game) else "<sandbox>" for a in args], "rc": r["rc"],
                                  "elapsed": r["elapsed"], "out": r["out"].strip()[-400:]})
            # the state file records the official file's hash; a backup with another hash is not the official file
            if r["rc"] != 1 or BACKUP_MISMATCH not in r["out"] or pre != post:
                probs.append(("backup_hash_not_checked", f"{op}: the backup's hash differs from original_sha256 in the "
                              f"state file, but the exe used it (exit {r['rc']}, files changed: {pre != post}): "
                              + (r["out"].strip().splitlines() or [""])[-1][:200]))
            add(k, probs)
            continue
        if event == "missing_nobackup":
            r = run_exe(Path(exe), args, case_dir, verbose, watch=[bdir])
            post = snapshot(bdir)
            probs = generic_problems(r)
            result["ops"].append({"args": [a if a != str(game) else "<sandbox>" for a in args], "rc": r["rc"],
                                  "elapsed": r["elapsed"], "out": r["out"].strip()[-400:]})
            last = (r["out"].strip().splitlines() or [""])[-1][:200]
            sname = state_path(game).name
            if op == "apply" and (r["rc"] != 1 or "が見つかりません" not in r["out"] or pre != post):
                probs.append(("missing_nobackup_apply", f"bundle and backup missing: apply must exit 1 naming the missing "
                              f"file and change nothing (exit {r['rc']}, files changed: {pre != post}): {last}"))
            if op == "restore" and (r["rc"] != 0 or "パッチは適用されていません" not in r["out"]
                                    or set(post) != set(pre) - {sname}):
                probs.append(("missing_nobackup_restore", f"bundle and backup missing: restore must exit 0 and remove "
                              f"only the stale state file (exit {r['rc']}, files after: {sorted(post)}): {last}"))
            rs = run_status(Path(exe), game, case_dir, verbose)
            probs += check_status(rs, game)
            add(k, probs)
            continue
        message, want = update_expect(op, pre, game, texts_of)
        r = run_exe(Path(exe), args, case_dir, verbose, watch=[bdir])
        post = snapshot(bdir)
        probs = generic_problems(r)
        result["ops"].append({"args": [a if a != str(game) else "<sandbox>" for a in args], "rc": r["rc"],
                              "elapsed": r["elapsed"], "out": r["out"].strip()[-400:]})
        if message.startswith("REFUSE:"):
            # round 3: only the state file vouches for the backup and it does not fit -> exit 1, nothing changed (apart from
            # the leftovers of an interrupted run, which every apply / restore removes first)
            kept = {n: v for n, v in pre.items() if not is_leftover(n)}
            if r["rc"] != 1 or message[7:] not in r["out"] or post != kept:
                probs.append(("update_refusal_expected", f"{op}: expected exit 1 with '{message[7:]}' and no change "
                              f"(exit {r['rc']}, files changed: {pre != post}): "
                              + (r["out"].strip().splitlines() or [""])[-1][:200]))
            add(k, probs)
            continue
        if r["rc"] != 0 or r["busy_final"]:
            probs.append(("update_op_failed", f"{op} must succeed here (expected '{message}'): "
                          + (r["out"].strip().splitlines() or [""])[-1][:200]))
            add(k, probs)
            break
        if message not in r["out"]:
            probs.append(("update_wrong_path", f"{op}: expected '{message}'"))
        bname, kname, sname = BUNDLE_REL.name, BUNDLE_REL.name + ".tpcjp-original", BUNDLE_REL.name + ".tpcjp-state"
        if [n for n in post if n.endswith(TMP_SUFFIX) and n not in pre]:
            probs.append(("tmp_left", f"{sorted(post)}"))
        stale = [n for n in pre if is_leftover(n) and n in post]
        if stale:
            # round-2 fix (F-SBP-7): apply / restore delete the leftovers of an interrupted run first
            probs.append(("leftover_not_removed", f"{op} left the leftovers of an interrupted run: {stale}"))
        extra = sorted(set(post) - {bname, kname, sname} - set(pre))
        if extra:
            probs.append(("unexpected_files", f"{op} created {extra}"))
        if op == "apply":
            src = want["source"]
            if post.get(kname) != src:
                probs.append(("update_backup", "the backup after apply is not the expected official file "
                              f"({'the installed file' if src == pre.get(bname) else 'the old backup'})"))
            st = read_state(post.get(sname, b""))
            if st.get("patched_sha256") != sha(post.get(bname, b"")) or st.get("original_sha256") != sha(src) \
                    or st.get("game_version") != expected_game_version(pre, post, game):
                probs.append(("state_mismatch", f"state file does not describe the bundle / backup / version: {st}"))
            got, err = try_read_texts(bundle_path(game))
            if got is None:
                probs.append(("output_unloadable", err))
            elif got != model_apply(texts_of(src), s["data"])[0]:
                probs.append(("output_mismatch", "the output is not the patch of the expected official file"))
        else:
            if post.get(bname) != want["bundle"]:
                probs.append(("update_restore_content", "restore left another file than expected "
                              f"({'the backup' if want['bundle'] == pre.get(kname) else 'the installed file'})"))
            if kname in post or sname in post:
                probs.append(("restore_left_files", f"after restore: {sorted(post)}"))
        r = run_status(Path(exe), game, case_dir, verbose)
        probs += check_status(r, game)
        add(k, probs)
    if not keep and not result["problems"]:
        rmtree(case_dir)
    result["sandbox"] = str(game)
    return result


# =====================================================================================================
# concurrency suite: one TpcJpPatch at a time (Patcher.Lock, named mutex Local\TpcJpPatch)
# =====================================================================================================

CONCURRENCY_KINDS = {"held": 5, "abandoned": 2, "race": 7}
ABANDON_HELPER = ("import ctypes, sys\n"
                  "k = ctypes.windll.kernel32\n"
                  "k.CreateMutexW.restype = ctypes.c_void_p\n"
                  "h = k.CreateMutexW(None, False, sys.argv[1])\n"
                  "r = k.WaitForSingleObject(ctypes.c_void_p(h), 10000)\n"
                  "sys.exit(0 if r in (0, 0x80) else 3)  # exits while owning the mutex: abandoned\n")


def race_commands(rng, game: Path, data: Path):
    pool = {
        "apply": ["apply", "--game", str(game), "--no-language"],
        "apply+lang": ["apply", "--game", str(game)],
        "restore": ["restore", "--game", str(game), "--keep-language"],
        "restore+lang": ["restore", "--game", str(game)],
        "ja": ["language", "ja"], "previous": ["language", "previous"], "en": ["language", "en"],
        "status": ["status", "--game", str(game)],
    }
    names = rng.choices(list(pool), [4, 2, 4, 2, 3, 3, 1, 2], k=rng.randint(2, 4))
    cmds = []
    for n in names:
        c = list(pool[n])
        if c[0] in ("apply", "restore") and rng.random() < 0.5:
            c += ["--data", str(data)]
        cmds.append((n, c))
    return cmds


def race_simulate(order, start):
    """Effect of commands run one after another: (bundle 'official'/'patched', selected language)."""
    bundle, sel, prev = start
    for n in order:
        if n.startswith("apply"):
            bundle = "patched"
        if n.startswith("restore"):
            bundle = "official"
        if n in ("ja", "apply+lang") and sel != 12:
            prev, sel = sel, 12
        if n in ("previous", "restore+lang") and sel == 12:
            sel = prev if prev is not None and prev != 12 else 0
        if n == "en":
            sel = 0
    return bundle, sel


def bundle_state(game: Path) -> str:
    s = shared()
    b, k, st = bundle_path(game), backup_path(game), state_path(game)
    cur = sha(b.read_bytes()) if b.exists() else None
    if cur == s["official_sha"] and not k.exists() and not st.exists():
        return "official"
    if cur == s["golden_sha"] and k.exists() and sha(k.read_bytes()) == s["official_sha"] and st.exists() \
            and read_state(st.read_bytes()).get("patched_sha256") == cur:
        return "patched"
    return "inconsistent"


def concurrency_case(idx: int, seed: int, exe: str, verbose=False, keep=False) -> dict:
    s = shared()
    rng = random.Random(case_seed(seed, "concurrency", idx))
    kind = rng.choices(list(CONCURRENCY_KINDS), list(CONCURRENCY_KINDS.values()))[0]
    case_dir = ROOT / "cases" / f"concurrency_{idx:05d}"
    game, cloud, appdata = case_dir / "game", case_dir / "prefs", case_dir / "appdata"
    rmtree(case_dir)
    start_bundle = rng.choice(["official", "patched"])
    make_game(game, s["official"])
    if start_bundle == "patched":
        bundle_path(game).write_bytes(s["patched"])
        backup_path(game).write_bytes(s["official"])
        state_path(game).write_bytes(s["state"])
    acct = str(rng.randint(10 ** 8, 10 ** 10))
    prefs, sel0 = prefs_base(rng)
    (cloud / acct).mkdir(parents=True)
    pf = cloud / acct / "preferences.json"
    pf.write_bytes(prefs)
    watch = [game, cloud, appdata]
    result = {"suite": "concurrency", "index": idx, "seed": case_seed(seed, "concurrency", idx), "scenario": kind,
              "mutation": f"bundle {start_bundle}, language {sel0}", "ops": [], "problems": []}
    _CUR["result"] = result

    def add(k, probs):
        for code, detail in probs:
            result["problems"].append({"op": k, "code": code, "detail": detail})
            if verbose:
                print(f"  PROBLEM [{code}] {detail[:500]}")

    def rec(args, r):
        result["ops"].append({"args": [a if a != str(game) else "<sandbox>" for a in args], "rc": r["rc"],
                              "elapsed": r["elapsed"], "out": r["out"].strip()[-300:]})

    if kind in ("held", "abandoned"):
        name, cmd = race_commands(rng, game, s["data_path"])[0]
        if name == "status":
            name, cmd = "apply", ["apply", "--game", str(game), "--no-language"]
        result["mutation"] += f", {name}"
        if verbose:
            print(f"concurrency:{idx} {kind} {' '.join(cmd)}\n  sandbox {case_dir}")
        with exe_slot():
            if kind == "held":
                with NamedMutex(PRODUCT_MUTEX, 120_000) as m:
                    if not m.owned:
                        add(0, [("skipped_busy", "another TpcJpPatch kept the lock for 120 s")])
                    else:
                        before = snapshot_tree(watch)
                        r = run_exe(Path(exe), cmd, case_dir, verbose, slot=False, retry_busy=False)
                        rec(cmd, r)
                        probs = [p for p in generic_problems(r) if p[0] != "skipped_busy"]
                        if r["rc"] != 1 or "エラー: " + BUSY not in r["out"]:
                            probs.append(("lock_not_enforced", f"the lock was held, but '{' '.join(cmd[:1])}' exited "
                                          f"{r['rc']}: {(r['out'].strip().splitlines() or [''])[-1][:200]}"))
                        changed = tree_diff(before, snapshot_tree(watch))
                        if changed:
                            probs.append(("busy_changed_files", f"files changed while the lock was held: {changed[:6]}"))
                        rs = run_status(Path(exe), game, case_dir, verbose)
                        rec(["status"], rs)
                        probs += check_status(rs, game)
                        add(0, probs)
            else:
                k32 = _k32()
                guard = k32.CreateMutexW(None, False, PRODUCT_MUTEX)  # keeps the mutex alive (not owned) after the helper
                try:
                    hp = subprocess.run([sys.executable, "-I", "-c", ABANDON_HELPER, PRODUCT_MUTEX], capture_output=True,
                                        timeout=60, creationflags=CREATE_NO_WINDOW)
                    result["mutation"] += f", helper exit {hp.returncode}"
                    r = run_exe(Path(exe), cmd, case_dir, verbose, slot=False, watch=watch)
                    rec(cmd, r)
                    result["mutation"] += f", busy retries {r['busy']}"  # >0: another process took the lock first
                    probs = generic_problems(r)
                    if r["rc"] != 0:
                        probs.append(("abandoned_lock_blocks", f"after a TpcJpPatch ended without releasing the lock, "
                                      f"'{cmd[0]}' exited {r['rc']}: {(r['out'].strip().splitlines() or [''])[-1][:200]}"))
                    add(0, probs)
                finally:
                    k32.CloseHandle(guard)
        # the lock is free again: the same command works
        r = run_exe(Path(exe), cmd, case_dir, verbose, watch=watch)
        rec(cmd, r)
        probs = generic_problems(r)
        if r["rc"] != 0:
            probs.append(("lock_left_behind", f"'{cmd[0]}' failed after the lock was released: "
                          f"{(r['out'].strip().splitlines() or [''])[-1][:200]}"))
        add(1, probs)
    else:
        cmds = race_commands(rng, game, s["data_path"])
        delays = [rng.choice([0, 0, 0.01, 0.03, 0.08]) for _ in cmds]
        result["mutation"] += ", race " + " | ".join(n for n, _ in cmds)
        if verbose:
            print(f"concurrency:{idx} race {[n for n, _ in cmds]}\n  sandbox {case_dir}")
        for _, c in cmds:
            check_safe_args(Path(exe), c)
        env = exe_env(case_dir)
        outs = []
        with exe_slot():
            procs = []
            for (n, c), d in zip(cmds, delays):
                if d:
                    time.sleep(d)
                procs.append((n, c, subprocess.Popen([exe, *c], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                     env=env, creationflags=CREATE_NO_WINDOW)))
            for n, c, p in procs:
                try:
                    o, _ = p.communicate(timeout=EXE_TIMEOUT)
                    outs.append((n, c, p.returncode, o.decode("utf-8", "replace"), False))
                except subprocess.TimeoutExpired:
                    p.kill()
                    o, _ = p.communicate()
                    outs.append((n, c, None, o.decode("utf-8", "replace"), True))
        probs, done, busy = [], [], 0
        for n, c, rc, o, to in outs:
            r = {"rc": rc, "out": o, "timeout": to, "elapsed": 0}
            rec(c, r)
            if verbose:
                print(f"  {n}: exit {rc}: {(o.strip().splitlines() or [''])[-1]}")
            probs += [p for p in generic_problems(r) if p[0] != "exit1_without_message"]
            if to or rc not in (0, 1):
                continue
            if rc == 1 and BUSY in o:
                busy += 1
                if "エラー: " + BUSY not in o:
                    probs.append(("busy_message", o.strip()[-300:]))
            elif rc == 1 and n == "status":
                pass  # status takes no lock: reading while another run replaces a file may fail with a message
            elif rc == 1:
                probs.append(("race_unexpected_failure", f"{n}: {(o.strip().splitlines() or [''])[-1][:200]}"))
            elif n != "status":
                done.append(n)
        result["mutation"] += f" (busy {busy})"
        result["busy_runs"] = busy
        left = [str(p) for p in case_dir.rglob("*" + TMP_SUFFIX)]
        if left:
            probs.append(("tmp_left", f"{left}"))
        final_bundle = bundle_state(game)
        v = prefs_view(pf.read_bytes())
        if final_bundle == "inconsistent":
            probs.append(("race_inconsistent_files", f"bundle / backup / state do not fit together: {expected_status(game)}"))
        if not v["ok"] or not strict_msgpack_ok(pf.read_bytes()) or len(pf.read_bytes()) != len(prefs):
            probs.append(("race_prefs_damaged", f"preferences.json after the race: {v.get('err')}"))
        else:
            finals = {race_simulate(order, (start_bundle, sel0, None)) for order in itertools.permutations(done)}
            if (final_bundle, v["sel"]) not in finals and final_bundle != "inconsistent":
                probs.append(("race_unreachable_state", f"final (bundle {final_bundle}, language {v['sel']}) is not the "
                              f"result of running {done} one after another (possible: {sorted(finals)})"))
        rs = run_status(Path(exe), game, case_dir, verbose)
        rec(["status"], rs)
        probs += check_status(rs, game)
        add(0, probs)
    if not keep and not result["problems"]:
        rmtree(case_dir)
    result["sandbox"] = str(case_dir)
    return result


# =====================================================================================================
# driver
# =====================================================================================================

SUITES = {"bundle": bundle_case, "update": update_case, "data": data_case, "datacut": datacut_case,
          "prefs": prefs_case, "concurrency": concurrency_case}


def run_one(suite, idx, seed, exe, verbose=False, keep=False):
    no_error_dialogs()
    warnings.filterwarnings("ignore")
    INFLIGHT.mkdir(parents=True, exist_ok=True)
    _CUR.clear()
    _CUR["case"] = f"{suite}_{idx}"
    mark("start")
    try:
        return SUITES[suite](idx, seed, exe, verbose, keep)
    except Exception:  # noqa: BLE001 - a broken test case must not stop the run; it is reported
        return {"suite": suite, "index": idx, "seed": case_seed(seed, suite, idx), "scenario": "?", "mutation": "?",
                "ops": [], "problems": [{"op": -1, "code": "harness_error", "detail": traceback.format_exc()[-1500:]}]}
    finally:
        try:
            (INFLIGHT / f"{suite}_{idx}.json").unlink()
        except OSError:
            pass
        _CUR.clear()


def crashed_result(suite, idx, seed) -> dict:
    """Result of a case whose worker died (native crash in the checker), from its in-flight marker."""
    m = INFLIGHT / f"{suite}_{idx}.json"
    try:
        info = json.loads(m.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        info = {"stage": "worker died", "result": None}
    r = info.get("result") or {"suite": suite, "index": idx, "seed": case_seed(seed, suite, idx), "scenario": "?",
                               "mutation": "?", "ops": [], "problems": []}
    r["problems"] = list(r.get("problems", [])) + [{"op": max(0, len(r.get("ops", [])) - 1), "code": "checker_crash",
                                                    "detail": info.get("stage", "?")}]
    try:
        m.unlink()
    except OSError:
        pass
    return r


def run_cases(todo, seed, exe, workers, keep, on_result):
    """Runs the cases in a process pool; a worker killed by a native crash is isolated and recorded."""
    remaining = list(todo)
    while remaining:
        for f in (INFLIGHT.glob("*.json") if INFLIGHT.exists() else []):
            f.unlink()
        finished = set()
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(run_one, su, i, seed, exe, False, keep): (su, i) for su, i in remaining}
                for fut in as_completed(futs):
                    r = fut.result()
                    finished.add(futs[fut])
                    on_result(r)
            remaining = []
        except BrokenProcessPool:
            remaining = [k for k in remaining if k not in finished]
            suspects = [k for k in remaining if (INFLIGHT / f"{k[0]}_{k[1]}.json").exists()] or remaining[:workers]
            print(f"  worker crashed; re-running {len(suspects)} in-flight cases one at a time")
            for k in suspects:
                try:
                    with ProcessPoolExecutor(max_workers=1) as one:
                        r = one.submit(run_one, k[0], k[1], seed, exe, False, keep).result()
                except BrokenProcessPool:
                    r = crashed_result(k[0], k[1], seed)
                on_result(r)
                remaining.remove(k)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--iterations", type=int, default=300)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--only", default="bundle,update,data,datacut,prefs,concurrency")
    ap.add_argument("--bundle-cases", type=int)
    ap.add_argument("--update-cases", type=int)
    ap.add_argument("--data-cases", type=int)
    ap.add_argument("--datacut-cases", type=int)
    ap.add_argument("--prefs-cases", type=int)
    ap.add_argument("--concurrency-cases", type=int)
    ap.add_argument("--start", type=int, default=0, help="first case index")
    ap.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 2))
    ap.add_argument("--exe", default=str(DEBUG_EXE))
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--case")
    ap.add_argument("--keep", action="store_true", help="keep every sandbox")
    ap.add_argument("--keep-failed", action="store_true", help="keep the sandboxes of failing cases")
    ap.add_argument("--compare", help="results-<seed>.jsonl of an earlier run: its failing cases must pass now")
    ap.add_argument("--strict", action="store_true", help="(kept for compatibility: every problem fails the run)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore")
    no_error_dialogs()
    exe = Path(a.exe).resolve()
    if "debug" not in str(exe).lower():
        raise SystemExit("use the Debug exe: the Release exe writes %LOCALAPPDATA%\\TpcJpPatch\\TpcJpPatch.log "
                         "and has no TPCJP_PREFS_ROOT / TPCJP_APPDATA hooks")
    ROOT.mkdir(parents=True, exist_ok=True)
    with ForeignWatcher() as watcher:
        meta = prepare_shared(exe)
        print(f"exe {exe}\nreference patch {meta['golden_sha'][:16]} (embedded data same: {meta['embedded_same']}), "
              f"{meta['applied']} entries")
        if a.case:
            suite, idx = a.case.split(":")
            res = run_one(suite, int(idx), a.seed, str(exe), verbose=True, keep=True)
            print(json.dumps({k: v for k, v in res.items() if k != "ops"}, ensure_ascii=False, indent=1))
            sys.exit(1 if res["problems"] else 0)
        counts = {"bundle": a.bundle_cases if a.bundle_cases is not None else a.iterations,
                  "update": a.update_cases if a.update_cases is not None else max(1, a.iterations // 6),
                  "data": a.data_cases if a.data_cases is not None else max(1, a.iterations // 2),
                  "datacut": a.datacut_cases if a.datacut_cases is not None else max(1, a.iterations // 30),
                  "prefs": a.prefs_cases if a.prefs_cases is not None else a.iterations,
                  "concurrency": a.concurrency_cases if a.concurrency_cases is not None else max(1, a.iterations // 10)}
        suites = [x for x in a.only.split(",") if x]
        log = ROOT / f"results-{a.seed}.jsonl"
        done = {}
        if a.resume and log.exists():
            for line in log.read_text("utf-8").splitlines():
                try:
                    r = json.loads(line)
                    done[(r["suite"], r["index"])] = r
                except (json.JSONDecodeError, KeyError):
                    pass
        elif log.exists():
            log.unlink()
        todo = [(su, i) for su in suites for i in range(a.start, a.start + counts[su]) if (su, i) not in done]
        print(f"cases: {', '.join(f'{su} {counts[su]}' for su in suites)}; {len(done)} already done, {len(todo)} to run, "
              f"{a.workers} workers")
        t0 = time.time()
        results = [r for k, r in done.items() if k[0] in suites and a.start <= k[1] < a.start + counts[k[0]]]
        with open(log, "a", encoding="utf-8") as lf:
            def on_result(r):
                results.append(r)
                lf.write(json.dumps(r, ensure_ascii=False) + "\n")
                lf.flush()
                if r["problems"] and not (a.keep or a.keep_failed):
                    rmtree(ROOT / "cases" / f"{r['suite']}_{r['index']:05d}")  # reproducible with --case
                if r["problems"]:
                    print(f"  FAIL {r['suite']}:{r['index']} [{', '.join(sorted(set(p['code'] for p in r['problems'])))}] "
                          f"{r['scenario']} / {r['mutation'][:120]}", flush=True)
                n = len(results) - len(done)
                if n % 500 == 0:
                    print(f"  ... {n}/{len(todo)} ({time.time() - t0:.0f}s; TpcJpPatch processes of other test "
                          f"suites seen so far: {len(watcher.seen)})", flush=True)
            run_cases(todo, a.seed, str(exe), a.workers, a.keep, on_result)
        print(f"elapsed {time.time() - t0:.0f}s; TpcJpPatch processes of other test suites seen: {len(watcher.seen)} "
              f"(parents, e.g.: {watcher.examples[:4]})")
    compared = compare_with(a.compare, results) if a.compare else None
    summarize(results, a.seed, log, compared)


# The findings of the first fuzz campaign (work/localtest/results/fuzz.json) are fixed (work/localtest/results/
# fixes.md). Their problem codes are no longer expected failures: a recurrence fails the run and is labelled as
# a regression of the finding.
FIXED_FINDINGS = {
    "unexpected_error:System.Text.DecoderFallbackException": "FZ-01",
    "invalid_utf8_accepted": "FZ-01",
    "restore_no_fallback": "FZ-01/FZ-11",
    "restore_failed": "FZ-01/FZ-11",
    "unexpected_error:System.OutOfMemoryException@LocalisationBundle": "FZ-02",
    "unexpected_error:System.ArgumentOutOfRangeException@Preferences": "FZ-03",
    "unexpected_error:System.OutOfMemoryException@Preferences": "FZ-03",
    "prefs_valid_file_refused": "FZ-03",
    "exit_code_0xc00000fd": "FZ-04",
    "prefs_invalid_file_changed": "FZ-03/FZ-04/FZ-12",
    "output_nul_text": "FZ-05",
    "backup_unloadable": "FZ-05/FZ-06",
    "restore_wrote_damaged": "FZ-05/FZ-06",
    "backup_replaced_same_version": "FZ-05/FZ-06",
    "restore_kept_same_version": "FZ-05/FZ-06",
    "output_unloadable": "FZ-06",
    "backup_incomplete": "FZ-06",
    "checker_crash": "FZ-06",
    "term_structure_changed": "FZ-07",
    "term_separator_accepted": "FZ-07",
    "truncated_data_accepted": "FZ-08",
    "invalid_data_accepted": "FZ-08",
    "changed_on_failure": "FZ-09/FZ-10",
    "backup_on_failure": "FZ-09",
    "state_warning": "FZ-10",
    "data_path_message": "FZ-11",
    "misleading_message_data_path": "FZ-11",
    "prefs_duplicate_key_ignored": "FZ-12",
    # round 2 (work/localtest/results/fixes.md "修正 2回目")
    "cut_last_line_accepted": "CS-06",
    "cut_data_message": "CS-06",
    "leftover_not_removed": "F-SBP-7",
    "missing_nobackup_": "F-SBP-7",
    "status_mismatch": "F-SBP-6/CS-07/pytests-E2E1",
    "status_damaged_false": "F-SBP-6/CS-07/pytests-E2E1",
    "status_text_unknown": "CS-08",
    # round 3 (fixes.md "修正 3回目"; the bundle damage codes above also cover the strict LocalisationBundle checks)
    "backup_hash_not_checked": "R3 backup_hash_not_checked",
    "nul_translation_blamed_on_game_file": "R3 nul_translation",
    "nul_data_accepted": "R3 nul_translation",
    "status_damage_not_detected": "R3 strict bundle checks",
    "status_damaged_advice": "CS-09/F-SBP-9",
    "update_refusal_expected": "F-SBP-8/CS-10",
    "state_version_mismatch": "F-SBP-8/CS-10",
    # round 3c (fixes.md "修正 3回目の追加（3c）")
    "status_unsupported": "3c S2/S8",
    "backup_used_unvouched": "3c S1",
    "unsupported_apply": "3c S2/S7/S15",
    "unsupported_restore": "3c S2/S7/S15",
}


def fixed_finding(code: str):
    return next((f for k, f in FIXED_FINDINGS.items() if code.startswith(k)), None)


def compare_with(path, results) -> dict:
    """The failing cases of an earlier run (same seed): same input now (mutation text), and do they pass?"""
    old = {}
    for line in Path(path).read_text("utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("problems"):
            old[(r["suite"], r["index"])] = r
    new = {(r["suite"], r["index"]): r for r in results}
    out = {"file": str(path), "old_failed": len(old), "same_input_pass": 0, "same_input_fail": [],
           "variant": 0, "variant_fail": [], "not_run": 0}
    for key, o in sorted(old.items()):
        n = new.get(key)
        if n is None:
            out["not_run"] += 1
            continue
        same = n["mutation"] == o["mutation"] and n["scenario"] == o["scenario"]
        # same mutated input with a new variant (version.txt of an update, apply/restore instead of language)
        variant = not same and n["mutation"].split(" (version.txt")[0] == o["mutation"]
        cid = f"{key[0]}:{key[1]}"
        codes = sorted(set(p["code"] for p in n["problems"]))
        if same:
            if n["problems"]:
                out["same_input_fail"].append({"case": cid, "codes": codes})
            else:
                out["same_input_pass"] += 1
        elif variant:
            out["variant"] += 1
            if n["problems"]:
                out["variant_fail"].append({"case": cid, "codes": codes})
        else:
            out.setdefault("input_changed", []).append({"case": cid, "codes": codes, "old": o["mutation"][:120],
                                                        "new": n["mutation"][:120]})
    print(f"\n== compared with {path}: {out['old_failed']} cases failed there; same input now passing "
          f"{out['same_input_pass']}, same input still failing {len(out['same_input_fail'])}, variants {out['variant']} "
          f"(failing {len(out['variant_fail'])}), input changed {len(out.get('input_changed', []))}, "
          f"not run {out['not_run']}")
    for f in (out["same_input_fail"] + out["variant_fail"] + out.get("input_changed", []))[:40]:
        print(f"  {f}")
    return out


def summarize(results, seed, log, compared=None):
    by_suite = {}
    groups = {}
    for r in results:
        st = by_suite.setdefault(r["suite"], {"cases": 0, "failed": 0, "runs": 0, "exit0": 0, "exit1": 0})
        st["cases"] += 1
        st["runs"] += len(r["ops"])
        st["exit0"] += sum(1 for o in r["ops"] if o["rc"] == 0)
        st["exit1"] += sum(1 for o in r["ops"] if o["rc"] == 1)
        if r.get("busy_runs"):
            st["race_busy_runs"] = st.get("race_busy_runs", 0) + r["busy_runs"]
            st["races_with_busy"] = st.get("races_with_busy", 0) + 1
        if r["problems"]:
            st["failed"] += 1
        for p in r["problems"]:
            g = groups.setdefault(p["code"], {"count": 0, "cases": [], "example": None})
            g["count"] += 1
            cid = f"{r['suite']}:{r['index']}"
            if cid not in g["cases"]:
                g["cases"].append(cid)
            if g["example"] is None:
                g["example"] = {"case": cid, "seed": r["seed"], "scenario": r["scenario"], "mutation": r["mutation"],
                                "detail": p["detail"][:800], "ops": r["ops"]}
    summary = {"seed": seed, "suites": by_suite, "problems": groups, "log": str(log), "compared": compared}
    (ROOT / f"summary-{seed}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), "utf-8")
    print("\n== summary")
    for su, st in by_suite.items():
        extra = f", races with a busy run: {st['races_with_busy']}" if st.get("races_with_busy") else ""
        print(f"{su}: {st['cases']} cases, {st['runs']} runs (exit 0: {st['exit0']}, exit 1: {st['exit1']}), "
              f"{st['failed']} failed{extra}")
    for code, g in sorted(groups.items(), key=lambda kv: -kv[1]["count"]):
        label = f"REGRESSION of {fixed_finding(code)}" if fixed_finding(code) else "NEW"
        print(f"  [{code}] ({label}) {len(g['cases'])} cases, e.g. {', '.join(g['cases'][:6])}")
        print(f"      {g['example']['mutation'][:150]}")
        print(f"      {g['example']['detail'][:300]}".replace("\n", " "))
    print(f"\nrepro: python tools/fuzz_patcher.py --seed {seed} --case <suite:index>   (details: {ROOT / f'summary-{seed}.json'})")
    total_failed = sum(st["failed"] for st in by_suite.values())
    print(f"passed {sum(st['cases'] for st in by_suite.values()) - total_failed}, failed {total_failed}, xfail 0 "
          f"(problem codes: {sorted(groups) or 'none'})")
    bad_compare = compared and (compared["same_input_fail"] or compared["variant_fail"] or compared.get("input_changed"))
    sys.exit(1 if total_failed or bad_compare else 0)


if __name__ == "__main__":
    main()
