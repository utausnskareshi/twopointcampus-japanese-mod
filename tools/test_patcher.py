"""Scenario tests for TpcJpPatch.exe in sandbox game folders (the real game is never touched).

Each scenario builds %TEMP%/tpcjp_sandbox/<name>/ with a fake TPC.exe, version.txt and a copy of the
official Japanese bundle, runs the patcher CLI with --game (and --no-language / --keep-language, so the
real preferences.json is not changed) and checks exit codes, files and bundle contents.
Every child process also gets TPCJP_APPDATA / TPCJP_PREFS_ROOT inside the sandbox, so a Debug build
(which honours these test hooks) keeps its log and never sees the real preferences.json.

usage: python tools/test_patcher.py [--exe PATH] [--only NAME[,NAME]] [--skip NAME[,NAME]]
                                    [--sandbox DIR] [--concurrent-rounds N] [--kill-sweep N] [--keep]
  --sandbox DIR  sandbox root (default %TEMP%/tpcjp_sandbox; it is deleted at the end unless --keep)
  --skip         scenarios not to run (e.g. --skip cli_args with a Release exe: it runs "language" usage errors)
  --concurrent-rounds N  rounds of two simultaneous applies in concurrent_apply (default 11)
  --kill-sweep N  kill times near the end of an apply per kind in kill_during_apply (default 19; e.g. 300 for
                  a thorough run)
Language switching (preferences.json) is tested by tools/test_language.py with the Debug build.
Files are replaced in one rename (MoveFileEx), so an interrupted run leaves the old or the new file and at most a
"<name>.tpcjp-tmp"; apply/restore delete such leftovers (and "<name>~RF*.TMP" copies left by version 1.0.0's
File.Replace) first and say "前回中断したときの一時ファイルを N 個削除しました".
A simulated game update always writes a new version.txt: a different bundle with an unchanged game
version is not an update (the patcher rebuilds from / restores the backup, see same_version_modified).
Only one TpcJpPatch runs at a time (named mutex Local\\TpcJpPatch): a command refused with
"別の TpcJpPatch が実行中です" because another program on this PC (e.g. another test suite) holds it is
retried, except where the refusal itself is under test (instance_lock, concurrent_*, kill_during_apply).
Checks tied to a known, still open product problem are reported as XFAIL <finding id> and do not fail the run.
"""
import ctypes
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tpcloc import bundles, i2cache, official, paths  # noqa: E402

DEFAULT_EXE = paths.PROJECT_ROOT / "patcher" / "TpcJpPatch" / "bin" / "Release" / "net48" / "TpcJpPatch.exe"
DATA = paths.PROJECT_ROOT / "dist" / "data" / "TpcJpPatch.data.tsv"
OFFICIAL = paths.WORK_DIR / "backup" / "official" / "localisation_assets_localisationjapanese.bundle"
BUNDLE_REL = Path("TPC_Data/StreamingAssets/aa/StandaloneWindows64/localisation_assets_localisationjapanese.bundle")
SANDBOX = Path(tempfile.gettempdir()) / "tpcjp_sandbox"
RUNNING = "Two Point Campus が起動しています"
BUSY = "別の TpcJpPatch が実行中です"
BOM = b"\xef\xbb\xbf"
VERSION = "10.3.169253+2024-12-06.1241"  # version.txt of a sandbox game (the version the data was made for)
NEW_VERSION = "10.4.170125+2025-03-04.0915"  # version.txt after a simulated game update
BUSY_RETRIES = [0]  # commands retried because another TpcJpPatch (not started by the check itself) held the lock
# status lines ("日本語データ: ...", MainForm.DescribeState)
ST_ORIGINAL = "公式のまま（パッチ未適用）"
ST_REVERTED = "公式のデータに戻っています（「日本語化する」で適用し直せます）"
ST_NO_STATE = "日本語化パッチ適用済み（状態の記録なし）"
ST_NO_BACKUP = "日本語化パッチ適用済み（バックアップなし。Steam の整合性の確認で戻せます）"  # round 3 (F-SBP-9)
ST_DAMAGED = "壊れているか読み込めません（「元に戻す」か Steam の整合性の確認で直せます）"
ST_DAMAGED_NO_BACKUP = "壊れているか読み込めません（Steam の整合性の確認で直せます）"  # round 3: no backup restore could use
ST_UPDATED = "ゲーム側で更新されています（「日本語化する」で適用し直してください）"
ST_MISSING = "見つかりません（ゲームのフォルダーを確認してください）"
ST_UNSUPPORTED = "このパッチが対応していない形式です（パッチの更新をお待ちください）"  # round 3c (S2/S8)
CUT_DATA = "翻訳データが途中で切れています（最後の行に改行がありません）"
REBUILT_MISSING = "日本語データが見つからないため、バックアップした公式データから作り直します"
BACKUP_MISMATCH = "バックアップが記録と一致しません（壊れているか、別のファイルに置き換わっています）。バックアップは削除せずに残しました。"
REBACKUP_RECORDED = "日本語データが記録されている公式データと一致するため、バックアップを作り直します"  # round 3
LIBRARY = "AssetsTools.NET.dll"  # shipped next to the exe since 1.0.1 (1.0.0 embedded it)
NO_LIBRARY = "エラー: AssetsTools.NET.dll が TpcJpPatch.exe と同じフォルダーにありません。"  # Program.MissingLibraryMessage
LEFTOVER_RX = re.compile(r"(\.tpcjp-tmp|~RF.*\.TMP)$", re.IGNORECASE)  # SafeFile.RemoveLeftovers' patterns


def removed_msg(n: int) -> str:
    return f"前回中断したときの一時ファイルを {n} 個削除しました"


def other_version_msg(backup_version: str, installed: str) -> str:
    """Round 3 (F-SBP-8): the refusal when only the state file vouches for a backup taken for another game version."""
    return (f"バックアップは別のバージョンのゲーム（{backup_version}）の公式データです（現在のゲームのバージョン: {installed}）。"
            "バックアップは削除せずに残しました。")


def unconfirmed_version_msg(recorded: str, installed: str) -> str:
    """Round 3b (R4/R14/R26): the same refusal when the recorded or the installed game version is unknown (不明, or an
    empty version.txt, shown as 不明): it says that the backup's version cannot be confirmed, not that it differs."""
    return (f"バックアップがどのバージョンのゲームのものか確認できません（記録: {recorded}、現在のゲームのバージョン: {installed}）。"
            "バックアップは削除せずに残しました。")


def status_line(out: str) -> str:
    """The text after "日本語データ: " in a status output ('' when there is none)."""
    return next((ln[len("日本語データ: "):] for ln in out.splitlines() if ln.startswith("日本語データ: ")), "")


class FileHold:
    """Opens a file (as another program would) with the given sharing until closed: share=1 (FILE_SHARE_READ) lets
    others read it but not replace or delete it; share=0 blocks every other open."""

    def __init__(self, path: Path, share: int = 0):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k32 = k32
        self.h = k32.CreateFileW(str(path), 0x80000000, share, None, 3, 0, None)  # GENERIC_READ, OPEN_EXISTING
        if self.h in (None, wintypes.HANDLE(-1).value):
            raise OSError(ctypes.get_last_error(), "CreateFileW failed", str(path))

    def close(self):
        if self.h:
            self.k32.CloseHandle(self.h)
            self.h = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def child_env() -> dict:
    """Environment for the patcher: Debug builds keep their log, backups and preferences inside the sandbox.

    USERPROFILE / LOCALAPPDATA also point into the sandbox: .NET Framework expands the LocalApplicationData
    folder from USERPROFILE, so even a Release build (which ignores the TPCJP_* hooks) writes its log to
    <sandbox>/_profile/AppData/Local/TpcJpPatch instead of the real %LOCALAPPDATA%\\TpcJpPatch.
    (The user-profile folder itself is not redirected, so a Release "status" still reads the real
    preferences.json; nothing here ever writes it.)"""
    env = dict(os.environ)
    env.pop("TPCJP_SIMULATE_NOT_INSTALLED", None)
    env["TPCJP_APPDATA"] = str(SANDBOX / "_appdata")
    env["TPCJP_PREFS_ROOT"] = str(SANDBOX / "_prefs")  # never created: no preferences.json at all
    local = SANDBOX / "_profile" / "AppData" / "Local"
    local.mkdir(parents=True, exist_ok=True)
    env["USERPROFILE"] = str(SANDBOX / "_profile")
    env["LOCALAPPDATA"] = str(local)
    return env


REAL_LOG = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "TpcJpPatch" / "TpcJpPatch.log"


def log_size(p: Path) -> int:
    return p.stat().st_size if p.exists() else 0


def log_tail(p: Path, start: int) -> str:
    if not p.exists():
        return ""
    with open(p, "rb") as fh:
        fh.seek(start)
        return fh.read().decode("utf-8", "replace")


def run_exe(cmd: list, cwd=None, retry=True, timeout=300, env=None) -> tuple[int, str]:
    """Run the patcher. With retry, a refusal that changes nothing and comes from outside this test is retried:
    another test's fake TPC.exe is running (wait 40 s), or another TpcJpPatch holds the instance lock (wait 0.3-2 s,
    for at most about 5 minutes)."""
    running_tries, busy_deadline = 0, time.time() + 300
    while True:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env or child_env(), cwd=cwd)
        out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
        if retry and RUNNING in out and running_tries < 7:
            running_tries += 1
            print("  (TPC.exe is running elsewhere; retrying in 40 s)")
            time.sleep(40)
            continue
        if retry and p.returncode == 1 and BUSY in out and time.time() < busy_deadline:
            BUSY_RETRIES[0] += 1
            print(f"  (another TpcJpPatch holds the lock; retrying: {' '.join(str(x) for x in cmd[1:2])})")
            time.sleep(random.uniform(0.3, 2.0))
            continue
        return p.returncode, out


class InstanceLock:
    """The patcher's instance lock (named mutex Local\\TpcJpPatch), held by this test like a running TpcJpPatch would.

    acquire() waits while another program holds it. The mutex is owned by the acquiring thread, so acquire and
    release on the same thread. Keep the hold short: every other TpcJpPatch on this PC is refused meanwhile."""
    NAME = "Local\\TpcJpPatch"
    WAIT_OBJECT_0, WAIT_ABANDONED, WAIT_TIMEOUT = 0, 0x80, 0x102

    def __init__(self):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k32 = k32
        self.h = k32.CreateMutexW(None, False, self.NAME)
        if not self.h:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        self.owned = False

    def try_acquire(self) -> int:
        """WaitForSingleObject(0): WAIT_OBJECT_0 / WAIT_ABANDONED (now owned) or WAIT_TIMEOUT (held elsewhere)."""
        r = self.k32.WaitForSingleObject(self.h, 0)
        if r in (self.WAIT_OBJECT_0, self.WAIT_ABANDONED):
            self.owned = True
        return r

    def acquire(self, timeout=300):
        deadline = time.time() + timeout
        while self.try_acquire() == self.WAIT_TIMEOUT:
            if time.time() > deadline:
                raise TimeoutError("another program holds Local\\TpcJpPatch")
            time.sleep(0.1)
        return self

    def release(self):
        if self.owned:
            self.k32.ReleaseMutex(self.h)
            self.owned = False

    def close(self):
        self.release()
        if self.h:
            self.k32.CloseHandle(self.h)
            self.h = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.close()


# A child that takes the instance lock and ends without releasing it (like a TpcJpPatch that was killed).
ABANDON_CHILD = ("import ctypes, sys\n"
                 "k = ctypes.WinDLL('kernel32')\n"
                 "k.CreateMutexW.restype = ctypes.c_void_p\n"
                 "k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]\n"
                 "k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]\n"
                 "k.WaitForSingleObject.restype = ctypes.c_uint32\n"
                 "h = k.CreateMutexW(None, False, 'Local\\\\TpcJpPatch')\n"
                 "print(k.WaitForSingleObject(h, 5000), flush=True)\n"
                 "k.ExitProcess(0)\n")


def opt(name, default=None):
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return default


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def data_patch_version() -> str:
    """#patch_version of dist/data (the release version the exe embeds and status shows), e.g. 1.0.1."""
    return re.search(r"^#patch_version\t(.*)$", DATA.read_text("utf-8"), re.M).group(1)


def load_patch_data():
    """{(source, key): ja or None for reference entries}"""
    out = {}
    for line in DATA.read_text("utf-8").split("\n")[1:]:
        if not line or line.startswith("#"):
            continue
        f = line.split("\t")
        if line.startswith("&"):
            out[(f[0][1:], unesc(f[1]))] = None
        else:
            out[(f[0], unesc(f[1]))] = unesc(f[3])
    return out


def unesc(s: str) -> str:
    res, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 1
            res.append({"\\": "\\", "n": "\n", "r": "\r", "t": "\t"}[s[i]])
        else:
            res.append(c)
        i += 1
    return "".join(res)


class Game:
    def __init__(self, name: str):
        self.dir = SANDBOX / name
        if self.dir.exists():
            shutil.rmtree(self.dir, onerror=lambda f, p, e: (os.chmod(p, 0o666), f(p)))
        (self.dir / BUNDLE_REL).parent.mkdir(parents=True)
        (self.dir / "TPC.exe").write_bytes(b"")
        self.set_version(VERSION)
        shutil.copy2(OFFICIAL, self.bundle)

    def set_version(self, version: str | None):
        """version.txt as a game update writes it (None: no version.txt)."""
        f = self.dir / "version.txt"
        if version is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(version, "utf-8")

    def update_to(self, bundle_file: Path, version: str):
        """A simulated game update: the Japanese bundle and version.txt change together."""
        shutil.copy2(bundle_file, self.bundle)
        self.set_version(version)

    @property
    def bundle(self) -> Path:
        return self.dir / BUNDLE_REL

    @property
    def backup(self) -> Path:
        return self.bundle.with_name(self.bundle.name + ".tpcjp-original")

    @property
    def state(self) -> Path:
        return self.bundle.with_name(self.bundle.name + ".tpcjp-state")

    def cmd(self, *args, exe=None, game=None) -> list:
        cmd = [str(exe or EXE), *args, "--game", game or str(self.dir)]
        if args[0] == "apply":
            cmd.append("--no-language")
        if args[0] == "restore":
            cmd.append("--keep-language")
        return cmd

    def run(self, *args, exe=None, cwd=None, retry=True, game=None) -> tuple[int, str]:
        return run_exe(self.cmd(*args, exe=exe, game=game), cwd=cwd, retry=retry)

    def leftovers(self) -> list[str]:
        """Temp files of an interrupted or failed run: "<name>.tpcjp-tmp" and "<name>~RF*.TMP" (File.Replace)."""
        if not self.bundle.parent.exists():
            return []
        return sorted(p.name for p in self.bundle.parent.iterdir() if LEFTOVER_RX.search(p.name))

    def files(self) -> list[str]:
        return sorted(p.name for p in self.bundle.parent.iterdir()) if self.bundle.parent.exists() else []

    def tree(self) -> dict:
        """{relative path: sha256} of every file in the game folder except the state file (it holds a time)."""
        return {str(p.relative_to(self.dir)): sha(p) for p in sorted(self.dir.rglob("*"))
                if p.is_file() and p != self.state}

    def state_lines(self) -> list[str]:
        return [ln for ln in self.state.read_text("utf-8").splitlines() if not ln.startswith("time=")]

    def state_dict(self) -> dict:
        if not self.state.exists():
            return {}
        return dict(ln.split("=", 1) for ln in self.state.read_text("utf-8").splitlines() if "=" in ln)


class Checker:
    def __init__(self):
        self.failed = []
        self.passed = 0
        self.xfailed = []
        self.xpassed = []

    def check(self, scenario: str, cond: bool, what: str, detail: str = ""):
        if cond:
            self.passed += 1
        else:
            self.failed.append(f"[{scenario}] {what} {detail}".rstrip())
            print(f"  FAIL {what} {detail}")
        return cond

    def xfail(self, scenario: str, cond: bool, what: str, finding: str, detail: str = ""):
        """A check that fails because of a reported product problem (finding id); it does not fail the run."""
        if cond:
            self.xpassed.append(f"[{scenario}] {what} ({finding})")
            print(f"  XPASS {what} ({finding} seems fixed)")
        else:
            self.xfailed.append(f"[{scenario}] {what} ({finding}) {detail}".rstrip())
            print(f"  XFAIL {finding} {what} {detail}")
        return cond


def texts_match_patch(bundle: Path, official_texts: dict, patch: dict, expect_skip: dict | None = None) -> list[str]:
    """Problems when `bundle` is not official + every patch entry (except expect_skip: {(s,k): value})."""
    expect_skip = expect_skip or {}
    got = bundles.read_texts(bundle)
    problems = []
    if set(got) != set(official_texts):
        return ["TextAsset names differ"]
    for name, text in official_texts.items():
        po, pn = i2cache.parse(text), i2cache.parse(got[name])
        if [k for k, _ in po] != [k for k, _ in pn]:
            problems.append(f"{name}: term order differs")
            continue
        for (k, vo), (_, vn) in zip(po, pn):
            if (name, k) in expect_skip:
                if vn != expect_skip[(name, k)]:
                    problems.append(f"{name}|{k}: expected kept {expect_skip[(name, k)]!r}, got {vn[:40]!r}")
            elif (name, k) in patch:
                want = patch[(name, k)]
                if want is not None and vn != want:
                    problems.append(f"{name}|{k}: not patched")
            elif vn != vo:
                problems.append(f"{name}|{k}: changed unexpectedly")
    return problems


def make_updated_official(dst: Path, patch: dict) -> dict:
    """Write an 'updated official' bundle: one patched term now has an official translation, one has new English."""
    texts = bundles.read_texts(OFFICIAL)
    keys = [k for k, v in patch.items() if v is not None]
    (s1, k1), (s2, k2) = keys[0], next(k for k in keys if k[0] != keys[0][0])
    changed = {}
    for (s, k), val in (((s1, k1), "公式テスト訳（アップデートで追加）"), ((s2, k2), "Changed English text in an update")):
        pairs = [(kk, val if kk == k else vv) for kk, vv in i2cache.parse(texts[s] if s not in changed else changed[s])]
        changed[s] = i2cache.dumps(pairs)
    bundles.write_texts(OFFICIAL, dst, changed)
    return {(s1, k1): "公式テスト訳（アップデートで追加）", (s2, k2): "Changed English text in an update"}


def make_matching_update(dst: Path, patch: dict, n: int) -> dict:
    """An update that officially translates n of our terms exactly as we did, plus one different official text."""
    texts = bundles.read_texts(OFFICIAL)
    keys = [k for k, v in patch.items() if v is not None]
    changed_texts, kept = {}, {}
    chosen = keys[1:n + 1]
    other = keys[n + 5]
    for (s, k) in chosen + [other]:
        val = patch[(s, k)] if (s, k) != other else "公式の新しい訳（アップデート）"
        base = changed_texts.get(s, texts[s])
        changed_texts[s] = i2cache.dumps([(kk, val if kk == k else vv) for kk, vv in i2cache.parse(base)])
    kept[other] = "公式の新しい訳（アップデート）"
    bundles.write_texts(OFFICIAL, dst, changed_texts)
    return kept


def write_data_with_noops(dst: Path, official_texts: dict, n: int):
    """The shipped data plus n entries whose translation equals the official text (no-ops)."""
    lines = DATA.read_text("utf-8").rstrip("\n").split("\n")
    added = 0
    for source, text in official_texts.items():
        for k, v in i2cache.parse(text):
            if added >= n:
                break
            if v and "\t" not in v and "\n" not in v and "\\" not in v and any(ord(c) > 0x3000 for c in v):
                lines.append(f"{source}\t{k}\t{hashlib.sha256(v.encode('utf-8')).hexdigest()[:16]}\t{v}")
                added += 1
    # the patcher checks the declared entry count (a truncated file is not applied), so keep it right
    lines = [f"#entries\t{int(ln.split(chr(9))[1]) + added}" if ln.startswith("#entries\t") else ln for ln in lines]
    dst.write_text("\n".join(lines) + "\n", "utf-8", newline="\n")


def write_unusable_data(dst: Path):
    """The shipped data with every base hash changed, so nothing applies ("適用できる翻訳がありませんでした")."""
    out = []
    for ln in DATA.read_text("utf-8").split("\n"):
        if ln and not ln.startswith("#"):
            f = ln.split("\t")
            f[2] = "0123456789abcdef"
            ln = "\t".join(f)
        out.append(ln)
    dst.write_text("\n".join(out), "utf-8", newline="\n")


def main():
    global EXE, SANDBOX
    warnings.filterwarnings("ignore")
    sys.stdout.reconfigure(encoding="utf-8")
    EXE = Path(opt("--exe", DEFAULT_EXE)).resolve()
    SANDBOX = Path(opt("--sandbox", SANDBOX)).resolve()
    only = set(opt("--only", "").split(",")) - {""}
    skip = set(opt("--skip", "").split(",")) - {""}
    real_log_start = log_size(REAL_LOG)
    patch = load_patch_data()
    official_texts = bundles.read_texts(OFFICIAL)
    official_sha = sha(OFFICIAL)
    c = Checker()
    expected_patched = {}

    def scenario(name):
        run = (not only or name in only) and name not in skip
        if run:
            print(f"== {name}")
        return run

    def reference_sha() -> str:
        """sha of a verified fresh apply in a plain sandbox (the output is deterministic)."""
        if "sha" not in expected_patched:
            g = Game("_reference")
            rc, out = g.run("apply")
            probs = texts_match_patch(g.bundle, official_texts, patch)
            c.check("reference", rc == 0 and not probs, "reference apply = official + all translations",
                    out[-300:] + "; ".join(probs[:5]))
            expected_patched["sha"] = sha(g.bundle)
        return expected_patched["sha"]

    if scenario("fresh_apply"):
        g = Game("fresh_apply")
        rc, out = g.run("apply")
        c.check("fresh_apply", rc == 0, "exit 0", out[-300:])
        c.check("fresh_apply", g.backup.exists() and sha(g.backup) == official_sha, "backup is official")
        c.check("fresh_apply", g.state.exists(), "state written")
        probs = texts_match_patch(g.bundle, official_texts, patch)
        c.check("fresh_apply", not probs, "bundle = official + all translations", "; ".join(probs[:5]))
        c.check("fresh_apply", not g.leftovers(), "no temp files left", str(g.leftovers()))
        expected_patched["sha"] = sha(g.bundle)
        rc, out = g.run("status")
        c.check("fresh_apply", rc == 0 and "適用済み" in out, "status shows patched", out[-200:])

    if scenario("reapply"):
        g = Game("reapply")
        g.run("apply")
        first = sha(g.bundle)
        rc, out = g.run("apply")
        c.check("reapply", rc == 0, "exit 0", out[-300:])
        c.check("reapply", sha(g.bundle) == first, "same output on re-apply")
        c.check("reapply", sha(g.backup) == official_sha, "backup still official")

    if scenario("restore"):
        g = Game("restore")
        g.run("apply")
        rc, out = g.run("restore")
        c.check("restore", rc == 0, "exit 0", out[-300:])
        c.check("restore", sha(g.bundle) == official_sha, "bundle back to official")
        c.check("restore", not g.backup.exists() and not g.state.exists(), "backup and state removed")
        rc, out = g.run("restore")
        c.check("restore", rc == 0, "second restore is a no-op", out[-200:])
        c.check("restore", sha(g.bundle) == official_sha, "still official after second restore")
        rc, out = g.run("apply")
        c.check("restore", rc == 0 and not texts_match_patch(g.bundle, official_texts, patch), "apply again after restore")

    if scenario("steam_verify"):
        g = Game("steam_verify")
        g.run("apply")
        shutil.copy2(OFFICIAL, g.bundle)  # Steam "verify integrity" puts the official file back
        rc, out = g.run("status")
        # changed on purpose (I-2, shortened with CS-08): backup and state are still there: the official file is back
        c.check("steam_verify", rc == 0 and status_line(out) == ST_REVERTED,
                "status after verify: the official data is back (Reverted, short text)", out[-200:])
        rc, out = g.run("apply")
        c.check("steam_verify", rc == 0 and not texts_match_patch(g.bundle, official_texts, patch), "re-patched after verify", out[-300:])
        c.check("steam_verify", sha(g.backup) == official_sha, "backup official")
        rc, out = g.run("status")
        c.check("steam_verify", rc == 0 and "適用済み" in out, "status after re-applying: patched", out[-200:])
        shutil.copy2(OFFICIAL, g.bundle)  # verify again, then restore
        rc, out = g.run("restore")
        c.check("steam_verify", rc == 0 and sha(g.bundle) == official_sha and not g.backup.exists() and not g.state.exists(),
                "restore after verify: official, backup and state removed", out[-200:])
        rc, out = g.run("status")
        c.check("steam_verify", rc == 0 and status_line(out) == ST_ORIGINAL,
                "status after restore: official (not Reverted)", out[-200:])

    if scenario("game_update"):
        g = Game("game_update")
        g.run("apply")
        newer = SANDBOX / "updated_official.bundle"
        kept = make_updated_official(newer, patch)
        g.update_to(newer, NEW_VERSION)  # an update replaces the bundle and version.txt
        rc, out = g.run("status")
        c.check("game_update", rc == 0 and status_line(out) == ST_UPDATED,
                "status after the update: updated by the game (its content is not our output)", out[-200:])
        rc, out = g.run("apply")
        c.check("game_update", rc == 0, "exit 0", out[-400:])
        c.check("game_update", "新しいデータをバックアップし直します" in out, "apply reports the update", out[-400:])
        c.check("game_update", sha(g.backup) == sha(newer), "backup is the updated official file")
        st = g.state_dict()
        c.check("game_update", st.get("game_version") == NEW_VERSION and st.get("original_sha256") == sha(newer)
                and st.get("patched_sha256") == sha(g.bundle), "state records the new version, the new backup and the output", str(st))
        new_texts = bundles.read_texts(newer)
        probs = texts_match_patch(g.bundle, new_texts, patch, expect_skip=kept)
        c.check("game_update", not probs, "fill-only keeps the update's text", "; ".join(probs[:5]))
        rc, out = g.run("restore")
        c.check("game_update", rc == 0 and sha(g.bundle) == sha(newer), "restore gives the updated official file", out[-200:])

    if scenario("update_then_restore"):
        g = Game("update_then_restore")
        g.run("apply")
        newer = SANDBOX / "updated_official2.bundle"
        make_updated_official(newer, patch)
        g.update_to(newer, NEW_VERSION)
        rc, out = g.run("restore")
        c.check("update_then_restore", rc == 0, "exit 0", out[-300:])
        c.check("update_then_restore", sha(g.bundle) == sha(newer), "newer official file kept (not replaced by old backup)")
        c.check("update_then_restore", not g.backup.exists() and not g.state.exists(), "backup and state removed")

    if scenario("missing_backup"):
        g = Game("missing_backup")
        g.run("apply")
        patched = sha(g.bundle)
        g.backup.unlink()
        rc, out = g.run("apply")
        c.check("missing_backup", rc == 1, "apply refuses without backup", out[-300:])
        c.check("missing_backup", sha(g.bundle) == patched, "bundle unchanged")
        c.check("missing_backup", not g.backup.exists(), "patched file not taken as backup")
        rc, out = g.run("restore")
        c.check("missing_backup", rc == 1, "restore reports the missing backup", out[-300:])
        c.check("missing_backup", sha(g.bundle) == patched, "bundle unchanged by restore")
        # round 3b (R2): the state still describes the bundle, but restore cannot undo it: not the plain 'patched' text
        rc, out = g.run("status")
        c.check("missing_backup", rc == 0 and status_line(out) == ST_NO_BACKUP, f"status: {ST_NO_BACKUP}", status_line(out))

    if scenario("missing_state"):
        g = Game("missing_state")
        g.run("apply")
        patched = sha(g.bundle)
        g.state.unlink()
        rc, out = g.run("apply")
        c.check("missing_state", rc == 0, "apply works without state", out[-300:])
        c.check("missing_state", sha(g.backup) == official_sha, "backup still official")
        c.check("missing_state", sha(g.bundle) == patched, "same patched output")
        g.state.unlink()
        rc, out = g.run("restore")
        c.check("missing_state", rc == 0 and sha(g.bundle) == official_sha, "restore without state", out[-300:])

    if scenario("corrupt_bundle"):
        g = Game("corrupt_bundle")
        g.bundle.write_bytes(b"UnityFS\x00garbage" * 100)
        before = sha(g.bundle)
        rc, out = g.run("apply")
        c.check("corrupt_bundle", rc == 1, "apply fails", out[-300:])
        c.check("corrupt_bundle", sha(g.bundle) == before, "bundle unchanged")
        c.check("corrupt_bundle", not g.backup.exists(), "no backup made from a corrupt file")
        c.check("corrupt_bundle", not g.leftovers(), "no temp files left", str(g.leftovers()))
        # bytes appended to a valid file (size checks): a damaged official file is neither patched nor backed up ...
        g = Game("padded_bundle")
        g.bundle.write_bytes(OFFICIAL.read_bytes() + b"\0" * 512)
        before = sha(g.bundle)
        rc, out = g.run("apply")
        c.check("corrupt_bundle", rc == 1 and "壊れています" in out and sha(g.bundle) == before and not g.backup.exists()
                and not g.state.exists(), "padded official file: not patched, not backed up", out[-300:])
        # ... and a padded patched file is rebuilt from the backup
        g = Game("padded_patched")
        g.run("apply")
        g.bundle.write_bytes(g.bundle.read_bytes() + b"\0" * 512)
        rc, out = g.run("apply")
        c.check("corrupt_bundle", rc == 0 and "壊れているため" in out and sha(g.bundle) == reference_sha()
                and sha(g.backup) == official_sha, "padded patched file: rebuilt from the backup", out[-300:])

    if scenario("readonly_bundle"):
        g = Game("readonly_bundle")
        os.chmod(g.bundle, 0o444)
        rc, out = g.run("apply")
        c.check("readonly_bundle", rc != 0 or not texts_match_patch(g.bundle, official_texts, patch),
                "either fails cleanly or patches", out[-300:])
        if rc != 0:
            c.check("readonly_bundle", sha(g.bundle) == official_sha, "bundle unchanged on failure")
        c.check("readonly_bundle", not g.leftovers(), "no temp files left", str(g.leftovers()))
        os.chmod(g.bundle, 0o666)
        rc, out = g.run("apply")
        c.check("readonly_bundle", rc == 0 and not texts_match_patch(g.bundle, official_texts, patch), "apply after clearing read-only", out[-300:])

    if scenario("game_running"):
        g = Game("game_running")
        fake = SANDBOX / "fake_game" / "TPC.exe"
        fake.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(os.environ["SystemRoot"]) / "System32" / "ping.exe", fake)
        proc = subprocess.Popen([str(fake), "-n", "30", "127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            time.sleep(1)
            rc, out = g.run("apply", retry=False)
            c.check("game_running", rc == 1 and "起動" in out, "refuses while TPC.exe runs", out[-300:])
            c.check("game_running", sha(g.bundle) == official_sha and not g.backup.exists(), "nothing changed")
        finally:
            proc.kill()
            proc.wait()

    if scenario("noop_entries"):
        # data with many entries whose translation equals the official text must still patch a clean install
        g = Game("noop_entries")
        noop_data = SANDBOX / "noop_data.tsv"
        write_data_with_noops(noop_data, official_texts, 20)
        rc, out = g.run("apply", "--data", str(noop_data))
        c.check("noop_entries", rc == 0, "clean install with no-op entries applies", out[-400:])
        c.check("noop_entries", g.backup.exists() and sha(g.backup) == official_sha, "backup is official")
        rc, out = g.run("restore", "--data", str(noop_data))
        c.check("noop_entries", rc == 0 and sha(g.bundle) == official_sha, "restore with no-op entries", out[-300:])

    if scenario("update_matching"):
        # an update that translates 30 of our terms exactly like us is a new official file, not our output
        g = Game("update_matching")
        g.run("apply")
        newer = SANDBOX / "updated_matching.bundle"
        kept = make_matching_update(newer, patch, 30)
        g.update_to(newer, NEW_VERSION)
        rc, out = g.run("apply")
        c.check("update_matching", rc == 0, "exit 0", out[-400:])
        c.check("update_matching", sha(g.backup) == sha(newer), "backup is the updated official file")
        probs = texts_match_patch(g.bundle, bundles.read_texts(newer), patch, expect_skip=kept)
        c.check("update_matching", not probs, "fill-only on the update", "; ".join(probs[:5]))
        shutil.copy2(newer, g.bundle)  # another update/verify; then restore must keep it
        rc, out = g.run("restore")
        c.check("update_matching", rc == 0 and sha(g.bundle) == sha(newer), "restore keeps the updated file", out[-300:])

    if scenario("fresh_matching_update"):
        # fresh install of such an update (no backup) must be patched normally
        g = Game("fresh_matching_update")
        newer = SANDBOX / "updated_matching_fresh.bundle"
        kept = make_matching_update(newer, patch, 30)
        g.update_to(newer, NEW_VERSION)
        rc, out = g.run("apply")
        c.check("fresh_matching_update", rc == 0 and sha(g.backup) == sha(newer), "fresh install of an update", out[-400:])

    if scenario("corrupt_backup"):
        g = Game("corrupt_backup")
        g.run("apply")
        patched = sha(g.bundle)
        g.backup.write_bytes(b"\x00" * g.backup.stat().st_size)
        damaged = sha(g.backup)
        rc, out = g.run("apply")
        c.check("corrupt_backup", rc == 1, "apply refuses a damaged backup", out[-300:])
        c.check("corrupt_backup", sha(g.bundle) == patched and sha(g.backup) == damaged, "nothing changed")
        rc, out = g.run("restore")
        c.check("corrupt_backup", rc == 1, "restore refuses a damaged backup", out[-300:])
        c.check("corrupt_backup", g.backup.exists() and g.state.exists() and sha(g.bundle) == patched, "backup, state and bundle kept")

    if scenario("swapped_backup"):
        # the backup is no longer the file recorded in the state
        g = Game("swapped_backup")
        g.run("apply")
        patched = sha(g.bundle)
        newer = SANDBOX / "updated_for_swap.bundle"
        make_updated_official(newer, patch)
        shutil.copy2(newer, g.backup)
        rc, out = g.run("restore")
        c.check("swapped_backup", rc == 1, "restore refuses a backup that does not match the state", out[-300:])
        c.check("swapped_backup", sha(g.bundle) == patched and sha(g.backup) == sha(newer), "nothing changed")
        rc, out = g.run("apply")
        c.check("swapped_backup", rc == 1 and BACKUP_MISMATCH in out and sha(g.bundle) == patched and sha(g.backup) == sha(newer),
                "apply refuses it too, nothing changed", out[-300:])
        # round 3b (R2/R19): the status does not offer a restore that refuses
        rc, out = g.run("status")
        c.check("swapped_backup", rc == 0 and status_line(out) == ST_NO_BACKUP, f"status: {ST_NO_BACKUP}", status_line(out))

    if scenario("patched_backup"):
        # an earlier tool left a patched file as the backup and no state
        g = Game("patched_backup")
        g.run("apply")
        shutil.copy2(g.bundle, g.backup)
        g.state.unlink()
        rc, out = g.run("apply")
        c.check("patched_backup", rc == 1, "apply refuses a patched backup", out[-300:])
        rc, out = g.run("restore")
        c.check("patched_backup", rc == 1 and g.backup.exists(), "restore refuses and keeps it", out[-300:])

    if scenario("truncated_bundle"):
        g = Game("truncated_bundle")
        g.run("apply")
        patched = sha(g.bundle)
        data = g.bundle.read_bytes()
        g.bundle.write_bytes(data[: len(data) // 2])
        rc, out = g.run("apply")
        c.check("truncated_bundle", rc == 0 and sha(g.bundle) == patched, "apply rebuilds a truncated bundle from the backup", out[-300:])
        data = g.bundle.read_bytes()
        g.bundle.write_bytes(data[:-1000])
        rc, out = g.run("restore")
        c.check("truncated_bundle", rc == 0 and sha(g.bundle) == official_sha, "restore replaces a truncated bundle", out[-300:])
        c.check("truncated_bundle", not g.backup.exists(), "backup removed after a verified restore")

    if scenario("truncated_official"):
        g = Game("truncated_official")
        data = g.bundle.read_bytes()
        g.bundle.write_bytes(data[:-1000])
        before = sha(g.bundle)
        rc, out = g.run("apply")
        c.check("truncated_official", rc == 1 and sha(g.bundle) == before, "a damaged official file is not patched", out[-300:])
        c.check("truncated_official", not g.backup.exists(), "and not taken as the backup")

    if scenario("restore_without_data"):
        g = Game("restore_without_data")
        g.run("apply")
        g.state.unlink()
        bad = SANDBOX / "broken_data.tsv"
        bad.write_text("not patch data\n", "utf-8")
        rc, out = g.run("restore", "--data", str(bad))
        c.check("restore_without_data", rc == 1 and g.backup.exists(), "without data and state the backup is kept", out[-300:])
        rc, out = g.run("restore")
        c.check("restore_without_data", rc == 0 and sha(g.bundle) == official_sha and not g.backup.exists(), "restore with data", out[-300:])

    if scenario("readonly_backup"):
        # v1.0.0 could leave a read-only backup; restore must still be able to remove it
        g = Game("readonly_backup")
        g.run("apply")
        os.chmod(g.backup, 0o444)
        rc, out = g.run("restore")
        c.check("readonly_backup", rc == 0 and sha(g.bundle) == official_sha and not g.backup.exists(), "restore with a read-only backup", out[-300:])

    if scenario("cli_args"):
        # Every command names the sandbox and keeps the language, so even an exe that ignores unknown
        # options cannot touch the real game or preferences.json.
        g = Game("cli_args")
        sandbox = ["--game", str(g.dir)]
        for args, want in ((["apply", "--bogus", "--no-language", *sandbox], 2),
                           (["status", *sandbox, "--game"], 2),
                           (["status", "extra", *sandbox], 2),
                           (["restore", "--keep-languages", "--keep-language", *sandbox], 2),
                           (["language"], 2), (["language", "xx"], 2)):
            p = subprocess.run([str(EXE), *args], capture_output=True, timeout=60, env=child_env())
            c.check("cli_args", p.returncode == want, f"{' '.join(args[:2])} -> exit {want}", f"got {p.returncode}")
        c.check("cli_args", sha(g.bundle) == official_sha and not g.backup.exists(), "rejected commands changed nothing")
        p = subprocess.run([str(EXE), "status", "--game", str(g.dir) + '\\"'], capture_output=True, timeout=60, env=child_env())
        c.check("cli_args", p.returncode == 0, "--game with a trailing quote (PowerShell) works", p.stdout.decode("utf-8", "replace")[-200:])

    if scenario("bad_game_dir"):
        rc, out = Game("bad_game_dir").run("status")  # valid dir; now point at a missing one
        p = subprocess.run([str(EXE), "status", "--game", str(SANDBOX / "does_not_exist")], capture_output=True, timeout=60,
                           env=child_env())
        c.check("bad_game_dir", p.returncode == 1, "missing --game folder -> exit 1")
        p = subprocess.run([str(EXE), "frobnicate", "--game", str(SANDBOX / "does_not_exist")], capture_output=True, timeout=60,
                           env=child_env())
        c.check("bad_game_dir", p.returncode == 2, "unknown command -> exit 2")

    # ---------------- added scenarios (paths, repetition, concurrency, data encodings, missing files) ----------------

    def no_crash(out: str) -> bool:
        return "予期しないエラー" not in out and "Unhandled Exception" not in out

    def communicate(p) -> tuple[int, str]:
        out, _ = p.communicate(timeout=300)
        return p.returncode, out.decode("utf-8", "replace")

    def error_line(out: str) -> str:
        return next((ln for ln in out.splitlines() if ln.startswith("エラー")), "")

    def data_count(out: str) -> str | None:
        m = re.search(r"（(\d+) 件、対象", out)
        return m.group(1) if m else None

    def run_pair(make_game, pre: str, make_cmds, delay: float):
        """Start two commands at (almost) the same time on a fresh game prepared as `pre`; returns (game, [(rc, out)]).
        A round in which both are refused by the lock (a third TpcJpPatch - another test on this PC - held it) or in
        which another test's TPC.exe was running is repeated."""
        for attempt in range(10):
            g = make_game()
            if pre != "clean":
                g.run("apply")
            if pre == "no_state":
                g.state.unlink()
            procs = []
            for k, cmd in enumerate(make_cmds(g)):
                if k and delay:
                    time.sleep(delay)
                procs.append(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=child_env()))
            res = [communicate(p) for p in procs]
            if any(RUNNING in o for _, o in res):
                print("  (TPC.exe is running elsewhere; retrying in 40 s)")
                time.sleep(40)
                continue
            if all(r == 1 and BUSY in o for r, o in res) and attempt < 9:
                BUSY_RETRIES[0] += 1
                print("  (both refused: a third TpcJpPatch held the lock; repeating the round)")
                time.sleep(random.uniform(0.5, 2.0))
                continue
            return g, res

    def end_state(g) -> str | None:
        """'official' (no backup/state), 'patched' (reference output, official backup, state describing both) or None."""
        cur = sha(g.bundle) if g.bundle.exists() else ""
        if cur == official_sha and not g.backup.exists() and not g.state.exists():
            return "official"
        st = g.state_dict()
        if (cur == reference_sha() and g.backup.exists() and sha(g.backup) == official_sha
                and st.get("patched_sha256") == cur and st.get("original_sha256") == official_sha):
            return "patched"
        return None

    if scenario("unicode_path"):
        # Japanese characters, spaces and (half- and full-width) parentheses in the game folder
        name = "unicode_path/日本語 フォルダー (テスト)/Two Point Campus（体験） (コピー)"
        g = Game(name)
        ref = reference_sha()
        rc, out = g.run("status")
        c.check(name, rc == 0 and "公式のまま" in out, "status of an official install", out[-300:])
        c.check(name, f"ゲームのフォルダー: {g.dir}" in out, "status prints the folder unchanged (UTF-8 output)", out[-300:])
        rc, out = g.run("apply")
        c.check(name, rc == 0 and no_crash(out), "apply exit 0", out[-300:])
        c.check(name, sha(g.bundle) == ref, "same output as in a plain folder")
        c.check(name, g.backup.exists() and sha(g.backup) == official_sha, "backup is official")
        st = g.state_dict()
        c.check(name, st.get("patched_sha256") == ref and st.get("original_sha256") == official_sha
                and st.get("game_version") == "10.3.169253+2024-12-06.1241", "state records both files and the version", str(st))
        c.check(name, not g.leftovers(), "no temp files left", str(g.leftovers()))
        rc, out = g.run("status")
        c.check(name, rc == 0 and "適用済み" in out, "status shows patched", out[-300:])
        rc, out = g.run("apply", game=str(g.dir) + "\\")  # trailing backslash (quoted by subprocess)
        c.check(name, rc == 0 and sha(g.bundle) == ref, "re-apply with a trailing backslash on --game", out[-300:])
        rc, out = g.run("restore")
        c.check(name, rc == 0 and sha(g.bundle) == official_sha, "restore exit 0 and official", out[-300:])
        c.check(name, not g.backup.exists() and not g.state.exists() and not g.leftovers(), "backup, state and temp files gone")

    if scenario("exe_in_unicode_path"):
        # the exe with AssetsTools.NET.dll (as in the ZIP, first without the .config) copied to and run from such a folder
        tool_dir = SANDBOX / "exe_in_unicode_path" / "ツール 置き場 (日本語)（コピー）"
        if tool_dir.exists():
            shutil.rmtree(tool_dir)
        tool_dir.mkdir(parents=True)
        exe_copy = tool_dir / "TpcJpPatch.exe"
        shutil.copy2(EXE, exe_copy)
        shutil.copy2(EXE.with_name(LIBRARY), tool_dir / LIBRARY)
        cfg = EXE.with_name(EXE.name + ".config")
        g = Game("exe_in_unicode_path/ゲーム 本体 (Steam)")
        ref = reference_sha()
        name = "exe_in_unicode_path"
        rc, out = g.run("status", exe=exe_copy, cwd=tool_dir)
        c.check(name, rc == 0 and "公式のまま" in out, "status from the copied exe", out[-300:])
        rc, out = g.run("apply", exe=exe_copy, cwd=tool_dir)
        c.check(name, rc == 0 and "実行ファイルに内蔵" in out, "apply uses the embedded data", out[-300:])
        c.check(name, sha(g.bundle) == ref and sha(g.backup) == official_sha, "embedded data gives the reference output")
        c.check(name, sorted(p.name for p in tool_dir.iterdir()) == [LIBRARY, "TpcJpPatch.exe"], "nothing written next to the exe",
                str(sorted(p.name for p in tool_dir.iterdir())))
        rc, out = g.run("restore", exe=exe_copy, cwd=SANDBOX)  # another working directory
        c.check(name, rc == 0 and sha(g.bundle) == official_sha and not g.backup.exists(), "restore from another cwd", out[-300:])
        if cfg.exists():
            shutil.copy2(cfg, tool_dir / cfg.name)  # exe + config, as in the ZIP
        shutil.copy2(DATA, tool_dir / DATA.name)  # a data file next to the exe takes precedence
        rc, out = g.run("apply", exe=exe_copy, cwd=SANDBOX)
        c.check(name, rc == 0 and str(tool_dir / DATA.name) in out, "data file next to the exe is used", out[-300:])
        c.check(name, sha(g.bundle) == ref, "same output with the data file")
        rc, out = g.run("restore", exe=exe_copy)
        c.check(name, rc == 0 and sha(g.bundle) == official_sha, "restore", out[-300:])
        (tool_dir / DATA.name).write_text("broken\n", "utf-8")
        rc, out = g.run("apply", exe=exe_copy)
        c.check(name, rc == 1 and "形式" in out and no_crash(out), "a broken data file next to the exe is reported", out[-300:])
        c.check(name, sha(g.bundle) == official_sha and not g.backup.exists() and not g.state.exists(), "nothing changed")

    if scenario("exe_without_library"):
        # the exe without AssetsTools.NET.dll (started from inside the ZIP, or copied out of it alone): every command
        # stops with a message that asks for the ZIP to be extracted, before it reads or writes anything
        tool_dir = SANDBOX / "exe_without_library" / "tool"
        if tool_dir.exists():
            shutil.rmtree(tool_dir)
        tool_dir.mkdir(parents=True)
        exe_copy = tool_dir / "TpcJpPatch.exe"
        shutil.copy2(EXE, exe_copy)
        cfg = EXE.with_name(EXE.name + ".config")
        if cfg.exists():
            shutil.copy2(cfg, tool_dir / cfg.name)
        shipped = sorted(p.name for p in tool_dir.iterdir())
        name = "exe_without_library"
        g = Game("exe_without_library/game")
        clean = g.tree()
        for args in (("status",), ("apply",), ("apply", "--data", str(DATA)), ("restore",), ("frobnicate",)):
            rc, out = g.run(*args, exe=exe_copy, cwd=tool_dir)
            c.check(name, rc == 1 and NO_LIBRARY in out and "ZIP ファイルをすべて展開してから" in out and no_crash(out),
                    f"{' '.join(args)}: exit 1 with the message", out[-300:])
        c.check(name, g.tree() == clean and not g.state.exists() and not g.backup.exists(), "the game is unchanged")
        c.check(name, sorted(p.name for p in tool_dir.iterdir()) == shipped, "nothing written next to the exe",
                str(sorted(p.name for p in tool_dir.iterdir())))
        # the same copy works once the library is next to it
        shutil.copy2(EXE.with_name(LIBRARY), tool_dir / LIBRARY)
        rc, out = g.run("status", exe=exe_copy, cwd=tool_dir)
        c.check(name, rc == 0 and "公式のまま" in out and NO_LIBRARY not in out, "status works with AssetsTools.NET.dll next to the exe", out[-300:])

    if scenario("cycles10"):
        name = "cycles10"
        g = Game(name)
        ref = reference_sha()
        clean_tree = g.tree()
        applied_tree = applied_state = None
        bad = []
        for i in range(1, 11):
            rc, out = g.run("apply")
            if rc != 0:
                bad.append(f"apply {i}: exit {rc} {out[-200:]}")
                continue
            if sha(g.bundle) != ref:
                bad.append(f"apply {i}: bundle {sha(g.bundle)[:12]}")
            if not g.backup.exists() or sha(g.backup) != official_sha:
                bad.append(f"apply {i}: backup not official")
            tree, st = g.tree(), g.state_lines()
            applied_tree = applied_tree or tree
            applied_state = applied_state or st
            if tree != applied_tree:
                bad.append(f"apply {i}: folder differs {set(tree.items()) ^ set(applied_tree.items())}")
            if st != applied_state:
                bad.append(f"apply {i}: state differs {st}")
            if g.leftovers():
                bad.append(f"apply {i}: temp files {g.leftovers()}")
            rc, out = g.run("restore")
            if rc != 0:
                bad.append(f"restore {i}: exit {rc} {out[-200:]}")
            if g.tree() != clean_tree or g.state.exists():
                bad.append(f"restore {i}: folder differs from the clean install")
        c.check(name, not bad, "10 apply/restore cycles give identical files each time", "; ".join(bad[:5]))
        c.check(name, sorted(applied_tree or {}) == sorted([*clean_tree, str(g.backup.relative_to(g.dir))]),
                "applied folder = clean folder + backup (+ state)", str(sorted(applied_tree or {})))

    if scenario("concurrent_apply"):
        # two apply processes started at the same time on one sandbox (optionally slightly staggered)
        ref = reference_sha()
        rounds = [("clean", 0.0), ("clean", 0.0), ("clean", 0.0), ("clean", 0.15), ("clean", 0.4), ("clean", 1.0),
                  ("applied", 0.0), ("applied", 0.0), ("applied", 0.3), ("no_state", 0.0), ("no_state", 0.2)]
        rounds += [("clean", 0.0)] * max(0, int(opt("--concurrent-rounds", len(rounds))) - len(rounds))
        lock_seen = 0
        for n, (pre, delay) in enumerate(rounds, 1):
            name = f"concurrent_apply/{n}-{pre}-{delay}"
            g, res = run_pair(lambda: Game(f"concurrent_apply_{n}"), pre, lambda g: [g.cmd("apply"), g.cmd("apply")], delay)
            codes = [r for r, _ in res]
            outs = " | ".join(o.strip().splitlines()[-1] if o.strip() else "" for _, o in res)
            busy = [r == 1 and BUSY in o for r, o in res]
            lock_seen += any(busy)
            print(f"  {name}: exit {codes}{' (one refused: lock)' if any(busy) else ''}")
            c.check(name, all(r in (0, 1) for r in codes) and all(no_crash(o) for _, o in res), "no crash", f"{codes} {outs}")
            # F-SBP-2 fixed: one apply runs; one that finds it running stops at once and changes nothing
            c.check(name, 0 in codes, "at least one of two concurrent applies succeeds", f"{codes} {outs}")
            c.check(name, all(r == 0 or (b and "適用しました" not in o and "翻訳データ:" not in o) for (r, o), b in zip(res, busy)),
                    "a refused apply says another TpcJpPatch is running and does nothing", outs)
            c.check(name, not g.leftovers(), "no temp files left", str(g.leftovers()))
            c.check(name, sha(g.bundle) == ref, "final bundle is the expected output", f"{codes} {outs}")
            c.check(name, g.backup.exists() and sha(g.backup) == official_sha, "backup is official", f"{codes} {outs}")
            st = g.state_dict()
            c.check(name, st.get("patched_sha256") == sha(g.bundle) and st.get("original_sha256") == official_sha,
                    "state describes the final files", f"{codes} {st.get('patched_sha256', '')[:12]}")
            rc, out = g.run("restore")
            c.check(name, rc == 0 and sha(g.bundle) == official_sha and not g.backup.exists(), "restore afterwards", out[-200:])
        c.check("concurrent_apply", lock_seen > 0, "the two processes overlapped (one was refused) in at least one round",
                f"{lock_seen}/{len(rounds)} rounds")

    if scenario("concurrent_restore"):
        # two restores at the same time, and apply + restore at the same time (end state must be consistent)
        ref = reference_sha()
        for n, (kind, delay) in enumerate([("restore", 0.0), ("restore", 0.0), ("restore", 0.2),
                                           ("mixed", 0.0), ("mixed", 0.0), ("mixed", 0.3)], 1):
            name = f"concurrent_restore/{n}-{kind}-{delay}"
            second = "restore" if kind == "restore" else "apply"
            g, res = run_pair(lambda: Game(f"concurrent_restore_{n}"), "applied",
                              lambda g: [g.cmd("restore"), g.cmd(second)], delay)
            codes = [r for r, _ in res]
            outs = " | ".join(o.strip().splitlines()[-1] if o.strip() else "" for _, o in res)
            busy = [r == 1 and BUSY in o for r, o in res]
            print(f"  {name}: exit {codes}{' (one refused: lock)' if any(busy) else ''}")
            c.check(name, all(r in (0, 1) for r in codes) and all(no_crash(o) for _, o in res), "no crash", f"{codes} {outs}")
            # F-SBP-2 fixed: one command runs, the other is refused at once
            c.check(name, 0 in codes, "at least one of two concurrent commands succeeds", f"{codes} {outs}")
            c.check(name, all(r == 0 or b for r, b in zip(codes, busy)), "a refused command says another TpcJpPatch is running", outs)
            c.check(name, not g.leftovers(), "no temp files left", str(g.leftovers()))
            end = end_state(g)
            winners = {cmd for cmd, r in zip(("restore", second), codes) if r == 0}
            want = {"official"} if winners == {"restore"} else {"patched"} if winners == {"apply"} else {"official", "patched"}
            c.check(name, end in want, "end state matches the command(s) that ran (official without backup/state, or patched "
                    "with official backup and matching state)", f"{codes} end={end} {outs}")
            rc, out = g.run("apply")
            c.check(name, rc == 0 and sha(g.bundle) == ref and sha(g.backup) == official_sha, "apply afterwards", out[-200:])
            rc, out = g.run("restore")
            c.check(name, rc == 0 and sha(g.bundle) == official_sha and not g.backup.exists() and not g.state.exists(),
                    "restore afterwards", out[-200:])

    if scenario("data_crlf_bom"):
        ref = reference_sha()
        raw = DATA.read_bytes()
        c.check("data_crlf_bom", b"\r" not in raw and not raw.startswith(BOM), "shipped data is LF without BOM")
        lf_file = SANDBOX / "data_lf.tsv"
        lf_file.parent.mkdir(parents=True, exist_ok=True)
        lf_file.write_bytes(raw)
        g = Game("data_lf")
        rc, out = g.run("apply", "--data", str(lf_file))
        lf_count, lf_state = data_count(out), (g.state_lines() if g.state.exists() else None)
        c.check("data_crlf_bom", rc == 0 and sha(g.bundle) == ref and lf_count, "LF data via --data", out[-300:])
        crlf = raw.replace(b"\n", b"\r\n")
        for variant, content in (("crlf", crlf), ("bom", BOM + raw), ("bom_crlf", BOM + crlf)):
            name = f"data_crlf_bom/{variant}"
            f = SANDBOX / f"data_{variant}.tsv"
            f.write_bytes(content)
            g = Game(f"data_{variant}")
            rc, out = g.run("apply", "--data", str(f))
            c.check(name, rc == 0 and no_crash(out), "apply exit 0", out[-300:])
            c.check(name, data_count(out) == lf_count, "same entry count as LF", f"{data_count(out)} vs {lf_count}")
            c.check(name, sha(g.bundle) == ref, "same output as LF")
            c.check(name, g.state.exists() and g.state_lines() == lf_state, "same state as LF (no CR or BOM in values)",
                    str(g.state_lines() if g.state.exists() else None))
            rc, out = g.run("status")
            c.check(name, rc == 0 and "適用済み" in out, "status shows patched", out[-200:])
            g.state.unlink()  # restore must recognise our output from this data alone
            rc, out = g.run("restore", "--data", str(f))
            c.check(name, rc == 0 and sha(g.bundle) == official_sha and not g.backup.exists(), "restore by content", out[-300:])
        # CS-06 fixed: build_patch.py ends every line with a newline, so data without one was cut (possibly inside the
        # last translation, e.g. '区画15' -> '区', which the entry count cannot see) and is not applied at all
        text = raw.decode("utf-8").rstrip("\n")
        last = text.split("\n")[-1]
        fields = last.split("\t")
        cut_last = (text[: len(text) - len(last)] + "\t".join(fields[:-1] + [fields[-1][:1]])).encode("utf-8")
        for variant, content in (("no_final_newline", raw.rstrip(b"\n")), ("crlf_no_final_newline", crlf.rstrip(b"\r\n")),
                                 ("crlf_cut_before_lf", crlf[:-1]), ("bom_no_final_newline", BOM + raw.rstrip(b"\n")),
                                 ("last_line_cut", cut_last)):
            name = f"data_crlf_bom/{variant}"
            f = SANDBOX / f"data_{variant}.tsv"
            f.write_bytes(content)
            g = Game(f"data_{variant}")
            rc, out = g.run("apply", "--data", str(f))
            c.check(name, rc == 1 and no_crash(out) and error_line(out) == f"エラー: {CUT_DATA}",
                    "apply refuses data without a final newline (cut)", out[-300:])
            c.check(name, sha(g.bundle) == official_sha and not g.backup.exists() and not g.state.exists() and not g.leftovers(),
                    "nothing changed")
            g.run("apply")
            rc, out = g.run("restore", "--data", str(f))
            c.check(name, rc == 0 and "状態ファイルだけで判断します" in out and CUT_DATA in out and sha(g.bundle) == official_sha
                    and not g.backup.exists() and not g.state.exists(), "restore with that data uses the state file", out[-300:])

    if scenario("missing_bundle"):
        name = "missing_bundle/fresh"
        g = Game("missing_bundle_fresh")
        g.bundle.unlink()
        rc, out = g.run("status")
        c.check(name, rc == 0 and "見つかりません" in out, "status reports the missing file", out[-300:])
        rc, out = g.run("apply")
        c.check(name, rc == 1 and "見つかりません" in out and no_crash(out), "apply fails cleanly", out[-300:])
        rc, out = g.run("restore")
        c.check(name, rc == 0 and no_crash(out), "restore: nothing to do", out[-300:])
        c.check(name, list(g.bundle.parent.iterdir()) == [], "no file created", str(list(g.bundle.parent.iterdir())))

        name = "missing_bundle/after_apply"
        g = Game("missing_bundle_applied")
        g.run("apply")
        bak = (sha(g.backup), g.backup.stat().st_mtime_ns)
        g.bundle.unlink()
        rc, out = g.run("status")
        c.check(name, rc == 0 and status_line(out) == ST_MISSING, "status reports the missing file", out[-300:])
        # changed on purpose (F-SBP-7 fix): a missing bundle is rebuilt from a good backup
        rc, out = g.run("apply")
        c.check(name, rc == 0 and REBUILT_MISSING in out and no_crash(out), "apply rebuilds the missing bundle from the backup", out[-300:])
        st = g.state_dict()
        c.check(name, sha(g.bundle) == reference_sha() and (sha(g.backup), g.backup.stat().st_mtime_ns) == bak
                and st.get("patched_sha256") == reference_sha() and st.get("original_sha256") == official_sha
                and st.get("game_version") == VERSION and not g.leftovers(),
                "bundle = the reference output, backup untouched, state describes both", str(st))
        rc, out = g.run("status")
        c.check(name, rc == 0 and status_line(out).startswith(f"日本語化パッチ適用済み（{data_patch_version()}、"), "status: patched", out[-200:])
        g.bundle.unlink()
        rc, out = g.run("restore")
        c.check(name, rc == 0 and g.bundle.exists() and sha(g.bundle) == official_sha, "restore puts the official file back", out[-300:])
        c.check(name, not g.backup.exists() and not g.state.exists() and not g.leftovers(), "backup and state removed")
        rc, out = g.run("apply")
        c.check(name, rc == 0 and sha(g.bundle) == reference_sha(), "apply again", out[-300:])

        # round 3c (S1): without a state file nothing says which game version the backup is for, so it is not used (apply and
        # restore refuse, nothing changed) until a Steam verification brings the official file back; a damaged or patched
        # backup is refused as before (checked first)
        name = "missing_bundle/no_state"
        g = Game("missing_bundle_no_state")
        g.run("apply")
        g.state.unlink()
        g.bundle.unlink()
        before = g.tree()
        for cmd in ("apply", "restore"):
            rc, out = g.run(cmd)
            err = error_line(out)
            c.check(name, rc == 1 and no_crash(out) and err.startswith(f"エラー: {unconfirmed_version_msg('不明', VERSION)}")
                    and "整合性" in err and "作り直します" not in out and "バックアップから戻します" not in out,
                    f"{cmd} refuses: the backup's version cannot be confirmed", out[-300:])
            c.check(name, not g.bundle.exists() and not g.state.exists() and g.tree() == before and not g.leftovers(),
                    f"{cmd}: nothing created or changed", str(g.files()))
        line = status_line(g.run("status")[1])
        c.check(name, line == ST_MISSING, "status: missing", line)
        shutil.copy2(OFFICIAL, g.bundle)  # Steam 'verify integrity'
        rc, out = g.run("apply")
        c.check(name, rc == 0 and "公式の日本語データにパッチを適用します" in out and sha(g.bundle) == reference_sha()
                and sha(g.backup) == official_sha and g.state_dict().get("game_version") == VERSION,
                "after a Steam verify apply patches it and records the game version", out[-300:])
        g.bundle.unlink()
        rc, out = g.run("apply")
        c.check(name, rc == 0 and REBUILT_MISSING in out and sha(g.bundle) == reference_sha(), "a later loss is rebuilt", out[-300:])
        for what in ("damaged backup", "patched backup"):
            name = f"missing_bundle/{what.replace(' ', '_')}"
            g = Game(f"missing_bundle_{what.replace(' ', '_')}")
            g.run("apply")
            if what == "damaged backup":
                g.backup.write_bytes(b"\0" * 4096)
                msg = "バックアップが壊れています"
            else:
                shutil.copy2(g.bundle, g.backup)  # an older tool backed up its own output
                g.state.unlink()
                msg = "バックアップがパッチ適用後のデータになっています"
            g.bundle.unlink()
            before = g.tree()
            for cmd in ("apply", "restore"):
                rc, out = g.run(cmd)
                c.check(name, rc == 1 and no_crash(out) and msg in error_line(out), f"{cmd} refuses ({msg})", out[-300:])
                c.check(name, not g.bundle.exists() and g.tree() == before and not g.leftovers(), f"{cmd}: nothing created or changed",
                        str(g.files()))

        # F-SBP-8 fixed (round 3): the backup of an older game version is not installed (and recorded) for a newer game
        # version: with the installed bundle missing or damaged only the state file vouches for the backup, and it says
        # the backup was taken for another version -> apply and restore refuse with the Steam verification advice
        newer = SANDBOX / "updated_for_missing.bundle"
        make_updated_official(newer, patch)
        for what in ("missing", "damaged"):
            name = f"missing_bundle/{what}_after_update"
            g = Game(f"missing_bundle_{what}_after_update")
            g.run("apply")
            g.update_to(newer, NEW_VERSION)  # the update replaced the bundle and version.txt ...
            if what == "missing":
                g.bundle.unlink()  # ... and the new file was then lost
            else:
                g.bundle.write_bytes(newer.read_bytes()[:200000])  # ... or damaged
            before = (g.tree(), g.state.read_bytes())
            for cmd in ("apply", "restore"):
                rc, out = g.run(cmd)
                err = error_line(out)
                c.check(name, rc == 1 and no_crash(out) and err.startswith(f"エラー: {other_version_msg(VERSION, NEW_VERSION)}")
                        and "整合性" in err, f"{cmd} on the {what} updated bundle refuses the old version's backup", out[-300:])
                c.check(name, (g.tree(), g.state.read_bytes()) == before and not g.leftovers(), f"{cmd}: nothing changed",
                        str(g.files()))
                # round 3b (R20): a refused restore (or apply) is not announced as a restore / rebuild first
                c.check(name, "バックアップから戻します" not in out and "作り直します" not in out,
                        f"{cmd}: nothing announced before the refusal", out[-300:])
            line = status_line(g.run("status")[1])
            c.check(name, line == (ST_MISSING if what == "missing" else ST_DAMAGED_NO_BACKUP),
                    "status: missing / damaged without a usable backup (no '元に戻す')", line)
            shutil.copy2(newer, g.bundle)  # Steam 'verify integrity' brings the updated official file back
            rc, out = g.run("apply")
            c.check(name, rc == 0 and g.backup.exists() and sha(g.backup) == sha(newer) and "新しいデータをバックアップし直します" in out
                    and g.state_dict().get("game_version") == NEW_VERSION,
                    "after a Steam verify, apply takes the updated official file as the backup (and records the new version)",
                    f"exit {rc}, backup = {'old official' if g.backup.exists() and sha(g.backup) == official_sha else 'other'}; "
                    + out[-200:])
            shutil.copy2(newer, g.bundle)
            rc, out = g.run("restore")
            c.check(name, rc == 0 and sha(g.bundle) == sha(newer), "restore after a Steam verify keeps the updated official file",
                    f"exit {rc}, bundle = {'old official' if sha(g.bundle) == official_sha else 'other'}")
            c.check(name, rc == 0 and not g.backup.exists() and not g.state.exists() and not g.leftovers(),
                    "restore ends with no backup or state", out[-200:])

        # R1/R25 (round 3b), S1 (round 3c): with no state file (or one of version 1.0.0) nothing records the backup's version,
        # so a bundle lost after an update is not rebuilt / restored from the old backup (apply and restore refuse, nothing
        # changed; before round 3c it was used, and a later new backup recorded the new version for it, so the Steam
        # verification could never bring the new official file back); after the verification the new file is adopted
        for what in ("no state", "1.0.0 state"):
            name = f"missing_bundle/{what.replace(' ', '_').replace('.', '')}_lost_after_update_refused"
            g = Game(f"missing_bundle_r1_{what.replace(' ', '_').replace('.', '')}")
            g.run("apply")
            if what == "no state":
                g.state.unlink()
            else:
                g.state.write_text("".join(ln + "\n" for ln in g.state.read_text("utf-8").splitlines()
                                           if not ln.startswith("game_version=")), "utf-8")
            g.update_to(newer, NEW_VERSION)
            g.bundle.unlink()
            before = (g.tree(), g.state.read_bytes() if g.state.exists() else None)
            for cmd in ("apply", "restore"):
                rc, out = g.run(cmd)
                err = error_line(out)
                c.check(name, rc == 1 and no_crash(out) and err.startswith(f"エラー: {unconfirmed_version_msg('不明', NEW_VERSION)}")
                        and "整合性" in err and "作り直します" not in out and "バックアップから戻します" not in out,
                        f"{cmd} refuses the old backup: its version cannot be confirmed", out[-300:])
                c.check(name, (g.tree(), g.state.read_bytes() if g.state.exists() else None) == before and not g.bundle.exists()
                        and not g.leftovers(), f"{cmd}: nothing changed", str(g.files()))
            line = status_line(g.run("status")[1])
            c.check(name, line == ST_MISSING, "status: missing", line)
            shutil.copy2(newer, g.bundle)  # Steam 'verify integrity'
            rc, out = g.run("apply")
            c.check(name, rc == 0 and "新しいデータをバックアップし直します" in out and "ゲームのバージョンは変わっていないため" not in out
                    and sha(g.backup) == sha(newer) and g.state_dict().get("game_version") == NEW_VERSION,
                    "after a Steam verify, apply takes the updated official file as the backup", out[-300:])
            rc, out = g.run("restore")
            c.check(name, rc == 0 and sha(g.bundle) == sha(newer) and not g.backup.exists() and not g.state.exists(),
                    "restore then gives the updated official file", out[-300:])

        # R4/R14/R26 and R15/R27 fixed (round 3b): when the installed version is unknown (no version.txt) or empty, the
        # refusal says that the backup's version cannot be confirmed instead of calling it another version
        for what, installed in (("no version.txt", None), ("empty version.txt", "")):
            name = f"missing_bundle/unconfirmed_version_{'none' if installed is None else 'empty'}"
            g = Game(f"missing_bundle_unconfirmed_{'none' if installed is None else 'empty'}")
            g.run("apply")
            g.set_version(installed)
            g.bundle.unlink()
            before = (g.tree(), g.state.read_bytes())
            for cmd in ("apply", "restore"):
                rc, out = g.run(cmd)
                err = error_line(out)
                c.check(name, rc == 1 and no_crash(out) and err.startswith(f"エラー: {unconfirmed_version_msg(VERSION, '不明')}")
                        and "整合性" in err and "別のバージョン" not in err, f"{cmd} ({what}): the backup's version cannot be confirmed",
                        out[-300:])
                c.check(name, (g.tree(), g.state.read_bytes()) == before and not g.leftovers(), f"{cmd}: nothing changed",
                        str(g.files()))

        # fuzz finding backup_hash_not_checked fixed (round 3): the backup replaced by another valid bundle is not the file
        # recorded in the state; with the installed bundle missing or damaged nothing else vouches for it -> refused.
        # After a Steam verify the installed file is the recorded official file: apply backs it up again, restore keeps it.
        other = SANDBOX / "other_for_swapped_backup.bundle"
        make_updated_official(other, patch)
        for what in ("missing", "damaged"):
            for after in ("apply", "restore"):
                name = f"missing_bundle/swapped_backup_{what}_then_{after}"
                g = Game(f"missing_bundle_swapped_{what}_{after}")
                g.run("apply")
                shutil.copy2(other, g.backup)  # version.txt unchanged
                if what == "missing":
                    g.bundle.unlink()
                else:
                    g.bundle.write_bytes(g.bundle.read_bytes()[:300000])
                before = g.tree()
                for cmd in ("apply", "restore"):
                    rc, out = g.run(cmd)
                    c.check(name, rc == 1 and no_crash(out) and error_line(out).startswith(f"エラー: {BACKUP_MISMATCH}")
                            and g.tree() == before and not g.leftovers(), f"{cmd} refuses the swapped backup, nothing changed",
                            out[-300:])
                line = status_line(g.run("status")[1])
                c.check(name, line == (ST_MISSING if what == "missing" else ST_DAMAGED_NO_BACKUP), "status before the verify", line)
                shutil.copy2(OFFICIAL, g.bundle)  # Steam 'verify integrity'
                line = status_line(g.run("status")[1])
                c.check(name, line == ST_REVERTED, "status after the verify: the recorded official file is back", line)
                rc, out = g.run(after)
                if after == "apply":
                    c.check(name, rc == 0 and REBACKUP_RECORDED in out and "バックアップを作成しました" in out
                            and sha(g.backup) == official_sha and sha(g.bundle) == reference_sha()
                            and g.state_dict().get("original_sha256") == official_sha, "apply backs the official file up again",
                            out[-300:])
                    rc, out = g.run("restore")
                else:
                    c.check(name, rc == 0 and "日本語データは公式のままです" in out, "restore leaves the official file", out[-300:])
                c.check(name, rc == 0 and sha(g.bundle) == official_sha and not g.backup.exists() and not g.state.exists()
                        and not g.leftovers(), "ends with the official file, no backup or state", out[-200:])

        name = "missing_bundle/no_folder"
        g = Game("missing_bundle_folder")
        shutil.rmtree(g.bundle.parent)
        for cmd, want in (("status", 0), ("apply", 1), ("restore", 0)):
            rc, out = g.run(cmd)
            c.check(name, rc == want and no_crash(out), f"{cmd} -> exit {want}", out[-300:])
        c.check(name, not g.bundle.parent.exists(), "bundle folder not created")

    if scenario("missing_data"):
        name = "missing_data"
        g = Game("missing_data")
        (SANDBOX / "data folder").mkdir(parents=True, exist_ok=True)
        missing = SANDBOX / "data folder" / "翻訳データ なし.tsv"
        folder = SANDBOX / "data_is_a_folder"
        folder.mkdir(parents=True, exist_ok=True)
        # F-SBP-1 fixed: any --data read failure is reported as unreadable translation data, with its path
        for what, path in (("missing --data file", missing), ("--data in a missing folder", SANDBOX / "no such folder" / "x.tsv"),
                           ("--data naming a folder", folder)):
            rc, out = g.run("apply", "--data", str(path))
            err = error_line(out)
            c.check(name, rc == 1 and no_crash(out) and err, f"apply with a {what} -> exit 1 with an error", out[-300:])
            c.check(name, sha(g.bundle) == official_sha and not g.backup.exists() and not g.state.exists() and not g.leftovers(),
                    f"{what}: nothing changed")
            c.check(name, err.startswith(f"エラー: 翻訳データを読み込めません: {path}（") and "ゲームや Steam" not in err
                    and "書き込めません" not in err, f"{what}: reported as unreadable translation data with its path", err[:200])
        g.run("apply")
        rc, out = g.run("restore", "--data", str(folder))
        c.check(name, rc == 0 and "状態ファイルだけで判断します" in out and f"翻訳データを読み込めません: {folder}" in out
                and sha(g.bundle) == official_sha and not g.backup.exists() and not g.state.exists(),
                "restore with --data naming a folder falls back to the state file (as for a missing file)", out[-300:])
        empty = SANDBOX / "empty_data.tsv"
        empty.write_bytes(b"")
        rc, out = g.run("apply", "--data", str(empty))
        # changed on purpose (CS-06): an empty file has no final newline, so it is reported as cut ('途中で切れています')
        c.check(name, rc == 1 and no_crash(out) and error_line(out) in (f"エラー: {CUT_DATA}", "エラー: 翻訳データの形式が正しくありません")
                and sha(g.bundle) == official_sha and not g.backup.exists(), "apply with an empty data file -> data error", out[-300:])
        g.run("apply")
        rc, out = g.run("restore", "--data", str(missing))
        c.check(name, rc == 0 and "読み込めない" in out and sha(g.bundle) == official_sha and not g.backup.exists(),
                "restore with a missing --data file uses the state file", out[-300:])
        g.run("apply")
        g.state.unlink()
        patched = sha(g.bundle)
        rc, out = g.run("restore", "--data", str(missing))
        c.check(name, rc == 1 and g.backup.exists() and sha(g.bundle) == patched,
                "without state and data the backup is kept", out[-300:])
        # a data file cut in the middle (entry count checked) or saved in another encoding is not applied
        lines = DATA.read_bytes().split(b"\n")
        cut = SANDBOX / "data_cut.tsv"
        cut.write_bytes(b"\n".join(lines[: len(lines) // 2]) + b"\n")
        sjis = SANDBOX / "data_cp932.tsv"
        sjis.write_bytes(DATA.read_text("utf-8").encode("cp932", "replace"))
        # round 3b (R18): an escaped line break in a header value the patcher uses would add a line to the state file
        forged = SANDBOX / "data_forged_header.tsv"
        first, second, rest = DATA.read_bytes().split(b"\n", 2)
        assert second.startswith(b"#patch_version\t"), second
        forged.write_bytes(first + b"\n" + second + b"\\ngame_version=FORGED\n" + rest)
        for what, path, msg in (("data cut in the middle", cut, "途中で切れているか"), ("data in Shift_JIS", sjis, "UTF-8 として読めません"),
                                ("an escaped line break in #patch_version", forged, "翻訳データ 2 行目の内容が正しくありません")):
            g = Game("damaged_data")
            rc, out = g.run("apply", "--data", str(path))
            c.check(name, rc == 1 and msg in error_line(out) and no_crash(out), f"apply with {what} -> exit 1 ({msg})", out[-300:])
            c.check(name, sha(g.bundle) == official_sha and not g.backup.exists() and not g.state.exists() and not g.leftovers(),
                    f"{what}: nothing changed")
            g.run("apply")
            rc, out = g.run("restore", "--data", str(path))
            c.check(name, rc == 0 and "状態ファイルだけで判断します" in out and sha(g.bundle) == official_sha and not g.backup.exists(),
                    f"restore with {what} uses the state file", out[-300:])

    # ---------------- added with the C1 fixes (version check, backup order, state warning, instance lock) ----------------

    if scenario("same_version_modified"):
        # FZ-05/06: the bundle was replaced by a different valid bundle but version.txt did not change: that is not a
        # game update. apply rebuilds from the backup (which stays untouched), restore puts the backup back.
        name = "same_version_modified"
        ref = reference_sha()
        other = SANDBOX / "same_version_other.bundle"
        make_updated_official(other, patch)  # a different, valid bundle
        g = Game(name)
        g.run("apply")
        bak = (sha(g.backup), g.backup.stat().st_mtime_ns)
        shutil.copy2(other, g.bundle)
        # pytests-OF1 fixed: the Python tools (tpcloc.official) follow the same rule and read the backup
        src, why = official.official_japanese(g.bundle)
        c.check(name, src == g.backup and "game version is unchanged" in why,
                "tpcloc.official: same version -> the backup is the official data (as the exe)", f"{src.name}: {why}")
        rc, out = g.run("status")
        c.check(name, rc == 0 and status_line(out) == ST_UPDATED, "status: the file was changed", out[-200:])
        rc, out = g.run("apply")
        c.check(name, rc == 0 and no_crash(out) and "ゲームのバージョンは変わっていないため、バックアップした公式データから作り直します" in out,
                "apply rebuilds from the backup and says why", out[-300:])
        c.check(name, sha(g.bundle) == ref, "bundle = the output made from the backup (not from the other file)")
        c.check(name, (sha(g.backup), g.backup.stat().st_mtime_ns) == bak, "backup untouched (same content, not rewritten)")
        st = g.state_dict()
        c.check(name, st.get("original_sha256") == official_sha and st.get("patched_sha256") == ref and st.get("game_version") == VERSION,
                "state still describes the official backup", str(st))
        c.check(name, not g.leftovers(), "no temp files left", str(g.leftovers()))
        shutil.copy2(other, g.bundle)
        rc, out = g.run("restore")
        c.check(name, rc == 0 and "ゲームのバージョンは変わっていないため、バックアップから戻します" in out and sha(g.bundle) == official_sha,
                "restore puts the backup back", out[-300:])
        c.check(name, not g.backup.exists() and not g.state.exists() and not g.leftovers(), "backup and state removed after the restore")
        # restore decides by the version even without readable translation data
        g.run("apply")
        shutil.copy2(other, g.bundle)
        rc, out = g.run("restore", "--data", str(SANDBOX / "no such data.tsv"))
        c.check(name, rc == 0 and sha(g.bundle) == official_sha and not g.backup.exists() and not g.state.exists(),
                "restore without translation data also puts the backup back", out[-300:])
        # a damaged backup is never used
        g.run("apply")
        shutil.copy2(other, g.bundle)
        g.backup.write_bytes(b"\0" * 4096)
        damaged = sha(g.backup)
        rc, out = g.run("apply")
        c.check(name, rc == 1 and "バックアップが壊れています" in out and sha(g.bundle) == sha(other) and sha(g.backup) == damaged,
                "damaged backup: apply refuses, nothing changed", out[-300:])
        rc, out = g.run("restore")
        c.check(name, rc == 1 and "バックアップが壊れています" in out and sha(g.bundle) == sha(other) and sha(g.backup) == damaged,
                "damaged backup: restore refuses, nothing changed", out[-300:])
        # the version cannot be compared: the different file is taken as a new official file (as before the fix)
        for what in ("no state file", "no version.txt"):
            g = Game(name + "_nocompare")
            g.run("apply")
            if what == "no state file":
                g.state.unlink()
            else:
                g.set_version(None)
            shutil.copy2(other, g.bundle)
            src, why = official.official_japanese(g.bundle)
            c.check(name, src == g.bundle and why.startswith("WARNING"),
                    f"{what}: tpcloc.official also takes the installed file (stale backup)", f"{src.name}: {why}")
            rc, out = g.run("apply")
            c.check(name, rc == 0 and sha(g.backup) == sha(other), f"{what}: the different file becomes the new backup", out[-300:])

    if scenario("failed_update_no_backup"):
        # FZ-09: a new official file is backed up only after the output was made and verified
        name = "failed_update_no_backup"
        bad = SANDBOX / "unusable_data.tsv"
        write_unusable_data(bad)
        g = Game(name + "_fresh")
        rc, out = g.run("apply", "--data", str(bad))
        c.check(name, rc == 1 and "適用できる翻訳がありませんでした" in out and no_crash(out), "fresh: apply with nothing applicable fails",
                out[-300:])
        c.check(name, sha(g.bundle) == official_sha and not g.backup.exists() and not g.state.exists() and not g.leftovers(),
                "fresh: the failed run wrote no backup or state")
        g = Game(name)
        g.run("apply")
        bak, state_bytes = (sha(g.backup), g.backup.stat().st_mtime_ns), g.state.read_bytes()
        newer = SANDBOX / "updated_for_failed.bundle"
        make_updated_official(newer, patch)
        g.update_to(newer, NEW_VERSION)
        rc, out = g.run("apply", "--data", str(bad))
        c.check(name, rc == 1 and "適用できる翻訳がありませんでした" in out and no_crash(out), "update: apply with nothing applicable fails",
                out[-300:])
        c.check(name, (sha(g.backup), g.backup.stat().st_mtime_ns) == bak and g.state.read_bytes() == state_bytes,
                "update: old backup and state untouched (no new backup from a failed run)")
        c.check(name, sha(g.bundle) == sha(newer) and not g.leftovers(), "update: the updated official file is unchanged")
        rc, out = g.run("apply")
        c.check(name, rc == 0 and sha(g.backup) == sha(newer) and g.state_dict().get("game_version") == NEW_VERSION,
                "update: a working apply then backs up the new file", out[-300:])

    if scenario("state_unwritable"):
        # FZ-10: the bundle is patched and verified but the state file cannot be written -> warning, exit 0
        name = "state_unwritable"
        ref = reference_sha()
        g = Game(name)
        g.state.mkdir()  # a folder in the state file's place: writing it fails
        rc, out = g.run("apply")
        c.check(name, rc == 0 and no_crash(out) and "注意: 状態ファイルを書き込めませんでした（日本語データの書き換えは完了しています）" in out
                and not error_line(out), "apply -> exit 0 with a warning", out[-300:])
        c.check(name, sha(g.bundle) == ref and g.backup.exists() and sha(g.backup) == official_sha, "bundle patched, backup official")
        c.check(name, not g.leftovers(), "no temp files left", str(g.leftovers()))
        rc, out = g.run("status")
        # F-SBP-6 / CS-07 / pytests-E2E1 fixed: status classifies the bundle by its content
        c.check(name, rc == 0 and status_line(out) == ST_NO_STATE, "status after that warning shows the patch as applied (no record)",
                status_line(out))
        rc, out = g.run("apply")
        c.check(name, rc == 0 and sha(g.bundle) == ref and "状態ファイルを書き込めませんでした" in out,
                "re-apply recognises the output by its content", out[-300:])
        rc, out = g.run("status")
        c.check(name, rc == 0 and status_line(out) == ST_NO_STATE, "status after the re-apply: still applied (no record)",
                status_line(out))
        rc, out = g.run("restore")
        c.check(name, rc == 0 and sha(g.bundle) == official_sha and not g.backup.exists() and not g.leftovers(),
                "restore by content", out[-300:])
        g.state.rmdir()

    if scenario("instance_lock"):
        # F-SBP-2: while another TpcJpPatch runs (this test holds its lock) apply/restore stop at once and change nothing
        name = "instance_lock"
        ref = reference_sha()
        g = Game(name)
        g.run("apply")
        # leftovers of an interrupted run belong to the running TpcJpPatch: a refused run must not delete them
        junk = [g.bundle.with_name(g.bundle.name + ".tpcjp-tmp"), g.bundle.with_name(g.bundle.name + "~RF5e1f.TMP")]
        for j in junk:
            j.write_bytes(b"partial")
        before, state_bytes = g.tree(), g.state.read_bytes()
        data_file = SANDBOX / "lock_data.tsv"
        data_file.write_bytes(DATA.read_bytes())
        cmds = (("apply",), ("restore",), ("apply", "--data", str(data_file)), ("restore", "--data", str(data_file)))
        with InstanceLock():
            res = [(args, *g.run(*args, retry=False)) for args in cmds]
            st_rc, st_out = g.run("status", retry=False)
        for args, rc, out in res:
            what = " ".join(args[:2])
            c.check(name, rc == 1 and no_crash(out) and error_line(out) == f"エラー: {BUSY}。終わってから、もう一度実行してください",
                    f"{what}: refused while another TpcJpPatch runs", out[-200:])
            lines = [ln for ln in out.splitlines() if ln.strip()]
            c.check(name, len(lines) == 2 and lines[0].startswith("Two Point Campus 日本語化パッチ"),
                    f"{what}: refused before reading or writing anything (only the title and the error)", str(lines))
        c.check(name, g.tree() == before and g.state.read_bytes() == state_bytes and all(j.exists() for j in junk),
                "nothing changed while locked (the other run's temp files kept)", str(g.files()))
        c.check(name, st_rc == 0 and "適用済み" in st_out, "status still works while another TpcJpPatch runs", st_out[-200:])
        rc, out = g.run("restore")
        c.check(name, rc == 0 and sha(g.bundle) == official_sha and not g.backup.exists(), "restore after the other one ended", out[-200:])
        c.check(name, removed_msg(2) in out and not g.leftovers(), "that restore removes the leftovers", f"{g.files()} {out[-200:]}")
        # a TpcJpPatch that ended without releasing the lock (killed) does not block the next run
        verdict = None
        for attempt in range(10):
            keeper = InstanceLock()  # keeps the mutex alive, so the child's ownership is abandoned (not destroyed)
            try:
                child = subprocess.run([sys.executable, "-I", "-c", ABANDON_CHILD], capture_output=True, timeout=60)
                if child.stdout.decode().strip() not in ("0", "128"):
                    time.sleep(1)  # the child could not take it: someone else holds it
                    continue
                rc, out = g.run("apply", retry=False)
                if rc == 1 and BUSY in out:
                    if keeper.try_acquire() == InstanceLock.WAIT_ABANDONED:
                        verdict = (False, "refused although the only owner had ended (abandoned lock): " + error_line(out))
                        break
                    keeper.release()  # another program took the lock in between: try again
                    time.sleep(1)
                    continue
                verdict = (rc == 0 and sha(g.bundle) == ref and sha(g.backup) == official_sha, out[-300:])
                break
            finally:
                keeper.close()
        c.check(name, verdict is not None and verdict[0], "apply after a TpcJpPatch ended without releasing the lock",
                verdict[1] if verdict else "could not be tested: the lock was always held elsewhere")
        g.run("restore")

    if scenario("kill_during_apply"):
        # an apply killed at any point (F-SBP-7 fixed): every file is replaced in one rename, so the bundle and the backup
        # are never missing and no "<name>~RF*.TMP" copy appears; a backup exists before the bundle is patched; at most a
        # "<name>.tpcjp-tmp" is left, and the next apply or restore (alternately) deletes it, says so and does its work.
        ref = reference_sha()
        newer = SANDBOX / "updated_for_kill.bundle"
        make_updated_official(newer, patch)
        gref = Game("_reference_update")
        gref.update_to(newer, NEW_VERSION)
        rc, out = gref.run("apply")
        ref_new = sha(gref.bundle)
        c.check("kill_during_apply", rc == 0 and sha(gref.backup) == sha(newer), "reference apply on the updated file", out[-200:])
        outcomes, junk_seen = {}, {"apply": 0, "restore": 0}

        def kill_game(kind):
            g = Game(f"kill_{kind}")
            if kind == "update":
                g.run("apply")
                g.update_to(newer, NEW_VERSION)
            return g

        def check_cleanup(g, name, cmd, out, junk):
            if junk:
                junk_seen[cmd] += 1
                c.check(name, removed_msg(len(junk)) in out, f"the next {cmd} says it deleted the {len(junk)} temp file(s)",
                        f"{junk} {out[-300:]}")
            else:
                c.check(name, "前回中断したときの一時ファイル" not in out, f"the next {cmd} reports no temp files when there were none",
                        out[-300:])
            c.check(name, not g.leftovers(), f"no temp files after the next {cmd}", str(g.leftovers()))

        for kind in ("clean", "update"):
            # the files are written in the last few milliseconds: measure a whole run and kill densely near its end
            times = []
            for _ in range(3):
                g = kill_game(kind)
                t0 = time.perf_counter()
                proc = subprocess.Popen(g.cmd("apply"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=child_env())
                proc.communicate(timeout=120)
                times.append(time.perf_counter() - t0)
            full = sorted(times)[1]
            steps = int(opt("--kill-sweep", 19))
            delays = [0.0, 0.05, 0.1] + [round(full * (0.6 + 0.475 * i / max(1, steps - 1)), 4) for i in range(steps)]
            print(f"  {kind}: a whole apply takes {full:.3f} s; kill after {delays[:3]} and {steps} steps "
                  f"from {delays[3]} to {delays[-1]} s")
            for i, delay in enumerate(delays):
                name = f"kill_during_apply/{kind}-{i}-{delay}"
                g = kill_game(kind)
                start, want = (official_sha, ref) if kind == "clean" else (sha(newer), ref_new)
                proc = subprocess.Popen(g.cmd("apply"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=child_env())
                time.sleep(delay)
                proc.kill()
                proc.communicate(timeout=60)
                files = g.files()
                cur = sha(g.bundle) if g.bundle.exists() else None
                bak = sha(g.backup) if g.backup.exists() else None
                junk = g.leftovers()
                key = ("done" if proc.returncode == 0 else "killed",
                       "MISSING" if cur is None else "patched" if cur == want else "old" if cur == start else "?",
                       "no backup" if bak is None else "old backup" if bak == official_sha and kind == "update" else "backup",
                       "temp file" if junk else "")
                outcomes[(kind,) + key] = outcomes.get((kind,) + key, 0) + 1
                c.check(name, cur is not None, "the bundle is never missing after an interrupted apply", str(files))
                c.check(name, not any("~RF" in x.upper() for x in files), "no <name>~RF*.TMP copy is left (one-step rename)",
                        str(files))
                c.check(name, all(x.endswith(".tpcjp-tmp") for x in junk), "only <name>.tpcjp-tmp files can be left", str(junk))
                if kind == "clean":
                    c.check(name, bak in (None, official_sha), "a backup, if any, is the official file", str(bak)[:12])
                else:
                    c.check(name, bak in (official_sha, sha(newer)), "the backup is never missing: the old or the new official file",
                            f"{str(bak)[:12]} {files}")
                if cur is not None:
                    c.check(name, cur in (start, want), "bundle is the old or the new file", f"{cur[:12]}")
                    if kind == "clean":
                        c.check(name, cur == start or bak == official_sha, "a patched bundle always has its backup", str(key))
                    else:
                        c.check(name, cur == start or bak == sha(newer), "a patched update always has the new backup", str(key))
                if i % 2 == 0:  # the next run is an apply
                    rc, out = g.run("apply")
                    c.check(name, rc == 0 and sha(g.bundle) == want and g.backup.exists() and sha(g.backup) == start
                            and g.state_dict().get("patched_sha256") == want, "the next apply completes the work", out[-300:])
                    check_cleanup(g, name, "apply", out, junk)
                else:  # the next run is a restore
                    rc, out = g.run("restore")
                    c.check(name, rc == 0 and sha(g.bundle) == start and not g.backup.exists() and not g.state.exists(),
                            "the next restore gives the official file back", out[-300:])
                    check_cleanup(g, name, "restore", out, junk)
                    rc, out = g.run("apply")
                    c.check(name, rc == 0 and sha(g.bundle) == want and sha(g.backup) == start, "apply after that restore",
                            out[-300:])
                rc, out = g.run("restore")
                c.check(name, rc == 0 and sha(g.bundle) == start and not g.backup.exists() and not g.state.exists()
                        and not g.leftovers(), "restore gives the official file back", out[-300:])
                c.check(name, g.files() == [g.bundle.name], "only the game's file is left in its folder", str(g.files()))
        print("  outcomes after kill:", ", ".join(f"{'/'.join(x for x in k if x)}: {v}" for k, v in sorted(outcomes.items())))
        print(f"  kills that left a temp file, cleaned by the next apply: {junk_seen['apply']}, by the next restore: "
              f"{junk_seen['restore']}")

    if scenario("status_by_content"):
        # F-SBP-6 / CS-07 / pytests-E2E1 fixed: a bundle that matches neither the state file nor the backup is classified
        # by its content (with the translation data built into the exe): our output -> applied (no record), unreadable
        # -> damaged, anything else -> updated by the game. status never changes a file.
        name = "status_by_content"
        newer = SANDBOX / "updated_for_status.bundle"
        make_updated_official(newer, patch)
        matching = SANDBOX / "updated_matching_status.bundle"
        make_matching_update(matching, patch, 30)

        def status_of(g) -> str:
            before = (g.tree(), g.state.read_bytes() if g.state.is_file() else None)
            rc, out = g.run("status")
            after = (g.tree(), g.state.read_bytes() if g.state.is_file() else None)
            ok = rc == 0 and no_crash(out) and before == after
            return status_line(out) if ok else f"(exit {rc}, files changed: {before != after}) {out[-200:]}"

        def case(what, prepare):
            g = Game("status_" + what.replace(" ", "_"))
            g.run("apply")
            prepare(g)
            return g, status_of(g)

        def stale_state(g):
            g.state.write_text(g.state.read_text("utf-8").replace(g.state_dict()["patched_sha256"], "0" * 64), "utf-8")

        for what, prepare, want in (
                ("state removed", lambda g: g.state.unlink(), ST_NO_STATE),
                ("stale state", stale_state, ST_NO_STATE),
                ("official file back", lambda g: shutil.copy2(OFFICIAL, g.bundle), ST_REVERTED),
                ("official update", lambda g: g.update_to(newer, NEW_VERSION), ST_UPDATED),
                ("update translating 30 terms like us", lambda g: g.update_to(matching, NEW_VERSION), ST_UPDATED),
                ("garbage bundle", lambda g: g.bundle.write_bytes(b"UnityFS\x00garbage" * 100), ST_DAMAGED)):
            g, line = case(what, prepare)
            c.check(name, line == want, f"{what}: {want}", line)
        # damaged: restore ("元に戻す") really fixes it, as the status says
        g, line = case("truncated", lambda g: g.bundle.write_bytes(g.bundle.read_bytes()[:300000]))
        c.check(name, line == ST_DAMAGED, f"truncated patched bundle: {ST_DAMAGED}", line)
        rc, out = g.run("restore")
        c.check(name, rc == 0 and sha(g.bundle) == official_sha and status_of(g) == ST_ORIGINAL, "restore fixes the damaged bundle",
                out[-200:])
        # the old state is kept after an update because the state file could not be replaced (FZ-10 warning)
        g = Game("status_old_state_kept")
        g.run("apply")
        old_state = g.state.read_bytes()
        g.update_to(newer, NEW_VERSION)
        with FileHold(g.state, share=1):  # readable, but it cannot be replaced
            rc, out = g.run("apply")
        c.check(name, rc == 0 and "注意: 状態ファイルを書き込めませんでした" in out and g.state.read_bytes() == old_state
                and sha(g.backup) == sha(newer), "update applied, the state of the old version kept (warning)", out[-300:])
        line = status_of(g)
        c.check(name, line == ST_NO_STATE, f"old state kept after an update: {ST_NO_STATE}", line)
        rc, out = g.run("restore")
        c.check(name, rc == 0 and sha(g.bundle) == sha(newer) and not g.backup.exists() and not g.state.exists(),
                "restore then gives the updated official file", out[-300:])
        # F-SBP-9 fixed (round 3): without a backup and a state file status reads the content too; without a backup that
        # restore could use, the damaged text offers only the Steam verification
        g, line = case("patched without backup or state", lambda g: (g.backup.unlink(), g.state.unlink()))
        rc, out = g.run("apply")
        c.check(name, rc == 1 and "以前のパッチで書き換えられていますが、バックアップが見つかりません" in out,
                "apply: patched, but no backup (as before)", out[-200:])
        c.check(name, line == ST_NO_BACKUP, f"status of a patched bundle without backup and state: {ST_NO_BACKUP}", line)
        g = Game("status_garbage_fresh")
        g.bundle.write_bytes(b"UnityFS\x00garbage" * 100)
        line = status_of(g)
        rc, out = g.run("apply")
        c.check(name, rc == 1 and "日本語データが壊れています" in out, "apply: the fresh install's file is damaged (as before)", out[-200:])
        c.check(name, line == ST_DAMAGED_NO_BACKUP, f"status of a damaged file on a fresh install: {ST_DAMAGED_NO_BACKUP}", line)
        g = Game("status_official_fresh")
        line = status_of(g)
        c.check(name, line == ST_ORIGINAL, f"status of a fresh official install (read by content): {ST_ORIGINAL}", line)
        # a damaged bundle whose backup restore cannot use: no '元に戻す' in the status, and restore indeed refuses
        for what, prepare in (("state without backup", lambda g: g.backup.unlink()),
                              ("backup of the old game version", lambda g: g.set_version(NEW_VERSION))):
            g, line = case("damaged, " + what, lambda g, p=prepare: (g.bundle.write_bytes(g.bundle.read_bytes()[:300000]), p(g)))
            c.check(name, line == ST_DAMAGED_NO_BACKUP, f"damaged, {what}: {ST_DAMAGED_NO_BACKUP}", line)
            rc, out = g.run("restore")
            c.check(name, rc == 1 and "整合性" in error_line(out) and no_crash(out), f"damaged, {what}: restore refuses", out[-200:])
        # the recorded official file is back while the backup is missing: Reverted, and apply backs it up again
        g, line = case("official back without backup", lambda g: (shutil.copy2(OFFICIAL, g.bundle), g.backup.unlink()))
        c.check(name, line == ST_REVERTED, f"official file back, backup missing: {ST_REVERTED}", line)
        rc, out = g.run("apply")
        c.check(name, rc == 0 and REBACKUP_RECORDED in out and sha(g.backup) == official_sha, "apply backs it up again", out[-200:])

    if scenario("unsupported_format"):
        # round 3c (S2/S7/S8/S15): a bundle in a format this patcher does not read (simulated: UnityFS format version 9) after
        # a game update, or on a fresh install, is not damage: status says so, apply refuses (verify, then wait for a patch
        # update) and changes nothing, restore keeps the file and removes the old backup and state. With an unchanged game
        # version (the state file proves it) the format bytes are damage: rebuilt from the backup.
        name = "unsupported_format"
        SANDBOX.mkdir(parents=True, exist_ok=True)
        v9 = SANDBOX / "unityfs9.bundle"
        raw = bytearray(OFFICIAL.read_bytes())
        assert raw[:8] == b"UnityFS\0" and raw[8:11] == b"\0\0\0" and 6 <= raw[11] <= 8, "official bundle: UnityFS 6..8"
        raw[11] = 9  # the format version, a big-endian uint32 after the signature
        v9.write_bytes(bytes(raw))
        refusal = ("エラー: 日本語データを読み込めません（対応していないバンドルの形式です（版 9））。Steam のライブラリで Two Point Campus"
                   " を右クリック →「プロパティ」→「インストール済みファイル」→「ゲームファイルの整合性を確認」を実行してから、もう一度お試しください。"
                   "それでも直らない場合は、パッチの更新をお待ちください")
        for what in ("update", "fresh install"):
            g = Game(f"unsupported_{what.replace(' ', '_')}")
            if what == "update":
                g.run("apply")
                g.update_to(v9, NEW_VERSION)
            else:
                shutil.copy2(v9, g.bundle)
            before = (g.tree(), g.state.read_bytes() if g.state.exists() else None)
            line = status_line(g.run("status")[1])
            c.check(name, line == ST_UNSUPPORTED, f"{what}: status: {ST_UNSUPPORTED}", line)
            rc, out = g.run("apply")
            c.check(name, rc == 1 and no_crash(out) and error_line(out) == refusal, f"{what}: apply refuses with the patch-update advice",
                    out[-300:])
            c.check(name, (g.tree(), g.state.read_bytes() if g.state.exists() else None) == before and not g.leftovers(),
                    f"{what}: apply changed nothing", str(g.files()))
            rc, out = g.run("restore")
            want = ("日本語データはこのパッチが対応していない形式のため、そのままにします" if what == "update"
                    else "パッチは適用されていません（日本語データはこのパッチが対応していない形式です）")
            c.check(name, rc == 0 and no_crash(out) and want in out and not error_line(out) and sha(g.bundle) == sha(v9)
                    and not g.backup.exists() and not g.state.exists() and not g.leftovers(),
                    f"{what}: restore keeps the file ({want})", out[-300:])
        g = Game("unsupported_same_version")
        g.run("apply")
        shutil.copy2(v9, g.bundle)  # version.txt unchanged
        line = status_line(g.run("status")[1])
        c.check(name, line == ST_DAMAGED, f"same game version: status {ST_DAMAGED}", line)
        rc, out = g.run("apply")
        c.check(name, rc == 0 and "日本語データが壊れているため、バックアップした公式データから作り直します" in out
                and sha(g.bundle) == reference_sha() and sha(g.backup) == official_sha, "same game version: rebuilt from the backup",
                out[-300:])

    if scenario("leftover_cleanup"):
        # F-SBP-7 fix: apply and restore first delete what an interrupted run left next to the bundle, the backup and the
        # state file ("<name>.tpcjp-tmp", and "<name>~RF*.TMP" from version 1.0.0's File.Replace) - and nothing else
        name = "leftover_cleanup"
        ref = reference_sha()

        def plant(g) -> tuple[list, dict]:
            b, d = g.bundle.name, g.bundle.parent
            junk = [d / x for x in (b + ".tpcjp-tmp", b + "~RF1a2b3c.TMP", b + "~rf9.tmp", b + ".tpcjp-original~RF38a219a.TMP",
                                    b + ".tpcjp-state~RF38c76d5.TMP", b + ".tpcjp-original.tpcjp-tmp",
                                    b + ".tpcjp-state.tpcjp-tmp", b + "~RFro.TMP")]
            for j in junk:
                j.write_bytes(b"partial " + j.name.encode())
            os.chmod(junk[-1], 0o444)  # read-only, as version 1.0.0 could leave it
            keep = {d / x: x.encode() for x in (b + "~RF1.TMP.txt", b + ".tpcjp-tmp.old", b + ".bak", "~RF2.TMP",
                                                "other.bundle~RF3.TMP", "other.bundle.tpcjp-tmp", b + "x~RF4.TMP",
                                                b + ".tpcjp-originalx.tpcjp-tmp")}
            for k, v in keep.items():
                k.write_bytes(v)
            (d / (b + "~RFdir.TMP")).mkdir(exist_ok=True)
            return junk, keep

        def kept(g, keep) -> bool:
            return all(k.exists() and k.read_bytes() == v for k, v in keep.items()) and (
                g.bundle.parent / (g.bundle.name + "~RFdir.TMP")).is_dir()

        g = Game("leftover_cleanup")
        g.run("apply")
        junk, keep = plant(g)
        before = g.tree()
        rc, out = g.run("status")
        c.check(name, rc == 0 and g.tree() == before, "status leaves them alone", out[-200:])
        rc, out = g.run("apply")
        c.check(name, rc == 0 and removed_msg(len(junk)) in out and no_crash(out), f"apply deletes the {len(junk)} temp files and says so",
                out[-300:])
        c.check(name, not any(j.exists() for j in junk) and kept(g, keep), "only the temp files of its three files are deleted",
                str(g.files()))
        c.check(name, sha(g.bundle) == ref and sha(g.backup) == official_sha, "apply then works as usual")
        junk, keep = plant(g)
        rc, out = g.run("restore")
        c.check(name, rc == 0 and removed_msg(len(junk)) in out and not any(j.exists() for j in junk) and kept(g, keep),
                "restore deletes them too (and nothing else)", f"{g.files()} {out[-300:]}")
        c.check(name, sha(g.bundle) == official_sha and not g.backup.exists() and not g.state.exists(), "restore then works as usual")
        rc, out = g.run("apply")
        c.check(name, rc == 0 and "前回中断したときの一時ファイル" not in out, "no message when there is nothing to delete", out[-200:])
        g = Game("leftover_cleanup_fresh")
        junk, keep = plant(g)
        rc, out = g.run("apply")
        c.check(name, rc == 0 and removed_msg(len(junk)) in out and not any(j.exists() for j in junk) and kept(g, keep)
                and sha(g.bundle) == ref and sha(g.backup) == official_sha, "fresh install: deleted, then patched", out[-300:])
        # a temp file another program holds open is left for the next run
        g = Game("leftover_cleanup_locked")
        g.run("apply")
        junk, keep = plant(g)
        with FileHold(junk[1]):
            rc, out = g.run("apply")
        c.check(name, rc == 0 and removed_msg(len(junk) - 1) in out and junk[1].exists()
                and not any(j.exists() for j in junk if j != junk[1]) and sha(g.bundle) == ref,
                "a temp file in use is skipped (not counted), the rest deleted, apply works", f"{g.files()} {out[-300:]}")
        rc, out = g.run("restore")
        c.check(name, rc == 0 and removed_msg(1) in out and not junk[1].exists() and sha(g.bundle) == official_sha,
                "the next restore deletes it", out[-300:])
        # the bundle's own temp file is in use: apply cannot write and stops cleanly; the next apply completes
        g = Game("leftover_cleanup_tmp_in_use")
        tmp = g.bundle.with_name(g.bundle.name + ".tpcjp-tmp")
        tmp.write_bytes(b"partial")
        with FileHold(tmp):
            rc, out = g.run("apply")
        c.check(name, rc == 1 and no_crash(out) and error_line(out) and sha(g.bundle) == official_sha
                and (not g.backup.exists() or sha(g.backup) == official_sha) and not g.state.exists(),
                "the bundle's temp file in use: apply fails cleanly, the official file untouched", out[-300:])
        rc, out = g.run("apply")
        c.check(name, rc == 0 and removed_msg(1) in out and not tmp.exists() and sha(g.bundle) == ref and sha(g.backup) == official_sha,
                "the next apply deletes it and patches", out[-300:])

    # the patcher's log stayed inside the sandbox (Debug: TPCJP_APPDATA, Release: the redirected profile)
    sandbox_logs = [SANDBOX / "_appdata" / "TpcJpPatch.log", SANDBOX / "_profile" / "AppData" / "Local" / "TpcJpPatch" / "TpcJpPatch.log"]
    c.check("log_isolation", any(log_size(p) for p in sandbox_logs), "the patcher log was written inside the sandbox",
            str([str(p) for p in sandbox_logs]))
    tail = log_tail(REAL_LOG, real_log_start)
    c.check("log_isolation", str(SANDBOX) not in tail, "nothing of this run reached the real %LOCALAPPDATA%\\TpcJpPatch log",
            tail[-300:])

    if "sha" in expected_patched:
        print(f"patched sha in sandbox: {expected_patched['sha']}")
    if BUSY_RETRIES[0]:
        print(f"(retried {BUSY_RETRIES[0]} times because another TpcJpPatch on this PC held the lock)")
    print(f"\npassed {c.passed}, failed {len(c.failed)}, xfail {len(c.xfailed)}, xpass {len(c.xpassed)}")
    for f in c.failed:
        print("FAILED", f)
    for f in c.xfailed:
        print("XFAIL", f)
    for f in c.xpassed:
        print("XPASS", f)
    if "--keep" not in sys.argv:
        shutil.rmtree(SANDBOX, ignore_errors=True)
    sys.exit(1 if c.failed else 0)


if __name__ == "__main__":
    main()
