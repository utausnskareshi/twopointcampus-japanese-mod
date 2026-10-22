"""Language-switching tests for TpcJpPatch.exe (Debug build only) with fake preferences.json files.

The Debug build honours three test hooks, and every command of this script runs with all of them:
  TPCJP_PREFS_ROOT  fake Steam Cloud folder (<account>/preferences.json) instead of the real one
  TPCJP_APPDATA     preference backups, previous-language memory and log instead of %LOCALAPPDATA%\\TpcJpPatch
  TPCJP_SIMULATE_NOT_INSTALLED=1  the real game is never located (apply/restore always get --game <sandbox>)
The real preferences.json is only read (copied); every variant (other languages, a wide int encoding,
_hasExpectedPlatformLanguage false, read-only, truncated, locked, ...) is made from that copy.
Before anything can write preferences the script checks that the exe really reads the fake folder
(a Release build ignores the hooks and would change the real settings) and stops if it does not.

usage: python tools/test_language.py [--exe PATH] [--only NAME[,NAME]] [--skip NAME[,NAME]]
                                     [--sandbox DIR] [--prefs FILE] [--concurrent-procs N]
                                     [--concurrent-rounds N] [--keep]
  --exe      Debug build (default patcher/TpcJpPatch/bin/Debug/net48/TpcJpPatch.exe)
  --sandbox  temp root (default %TEMP%/tpcjp_language; deleted at the end unless --keep)
  --prefs    preferences.json to copy (default: the first one in the game's Cloud folder, or a built-in
             minimal file when there is none)
  --concurrent-procs / --concurrent-rounds  simultaneous 'language ja' processes per round / rounds (2 / 6)
Only one TpcJpPatch runs at a time (named mutex Local\\TpcJpPatch): a command refused because another program on
this PC (e.g. another test suite) holds it is retried, except in instance_lock and concurrent, where the refusal is
under test.
Checks tied to a known, still open product problem are reported as XFAIL <finding id> and do not fail the run.
The scenario round2 covers the second round of fixes as far as they meet language switching (cut data is not applied,
a missing bundle is rebuilt, status by content, leftovers removed by restore); malformed also checks that the
Python port (tpcloc.prefs) refuses repeated keys like the exe.
"""
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_patcher as tp  # noqa: E402
from tpcloc import paths  # noqa: E402
from tpcloc import prefs as pyprefs  # noqa: E402

DEFAULT_EXE = paths.PROJECT_ROOT / "patcher" / "TpcJpPatch" / "bin" / "Debug" / "net48" / "TpcJpPatch.exe"
REAL_CLOUD = Path(os.environ["USERPROFILE"]) / "AppData" / "LocalLow" / "Two Point Studios" / "Two Point Campus" / "Cloud"
REAL_APPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "TpcJpPatch"
SANDBOX = Path(tempfile.gettempdir()) / "tpcjp_language"
SEL_KEY = b"\xb1_selectedLanguage"
HAS_KEY = b"\xbc_hasExpectedPlatformLanguage"
EN, FR, IT, DE, JA = 0, 1, 2, 3, 12
NAME = {0: "英語", 1: "フランス語", 2: "イタリア語", 3: "ドイツ語", 12: "日本語"}
RUNNING = tp.RUNNING
BUSY = tp.BUSY
BUSY_LINE = f"エラー: {BUSY}。終わってから、もう一度実行してください"
NOTHING_TO_RESTORE = "表示言語が日本語のアカウントはないため、表示言語は変更しませんでした"
# Account ids used only by this script (searched for in the real log to prove nothing went there).
ACC = "73000000"


def minimal_prefs() -> bytes:
    """[2, 0, {"_languagePreferences": [2, 6, {selected 0, audio 0, hasExpected true, expected 0}]}]"""
    def s(t: str) -> bytes:
        return bytes([0xA0 | len(t)]) + t.encode()
    return (b"\x93\x02\x00\x81" + s("_languagePreferences") + b"\x93\x02\x06\x84"
            + s("_selectedLanguage") + b"\x00" + s("_selectedAudioLanguage") + b"\x00"
            + s("_hasExpectedPlatformLanguage") + b"\xc3" + s("_expectedPlatformLanguage") + b"\x00")


def fixstr(t: str) -> bytes:
    b = t.encode()
    assert len(b) < 32
    return bytes([0xA0 | len(b)]) + b


LANG_ENTRIES = [("_selectedLanguage", b"\x00"), ("_selectedAudioLanguage", b"\x00"),
                ("_hasExpectedPlatformLanguage", b"\xc3"), ("_expectedPlatformLanguage", b"\x00")]


def build_prefs(lang_entries=None, top_extra=()) -> bytes:
    """minimal_prefs() with other language-map entries and/or extra entries (key, encoded value) in the top map."""
    lang = LANG_ENTRIES if lang_entries is None else lang_entries
    langmap = bytes([0x80 | len(lang)]) + b"".join(fixstr(k) + v for k, v in lang)
    top = [(fixstr("_languagePreferences"), b"\x93\x02\x06" + langmap)] + [(fixstr(k), v) for k, v in top_extra]
    return b"\x93\x02\x00" + bytes([0x80 | len(top)]) + b"".join(k + v for k, v in top)


def lang_pos(buf: bytes) -> int:
    i = buf.find(SEL_KEY)
    if i < 0 or buf.find(SEL_KEY, i + 1) >= 0:
        raise ValueError("_selectedLanguage not found exactly once")
    return i + len(SEL_KEY)


def read_int(buf: bytes, pos: int) -> tuple[int, int]:
    """(value, encoded width) of the MessagePack integer at pos."""
    t = buf[pos]
    if t <= 0x7F:
        return t, 1
    if t >= 0xE0:
        return t - 0x100, 1
    size = {0xCC: 1, 0xCD: 2, 0xCE: 4, 0xD0: 1, 0xD1: 2, 0xD2: 4}[t]
    return int.from_bytes(buf[pos + 1:pos + 1 + size], "big", signed=t >= 0xD0), 1 + size


def get_lang(buf: bytes):
    try:
        return read_int(buf, lang_pos(buf))[0]
    except (ValueError, KeyError, IndexError):
        return None


def with_lang(buf: bytes, lang: int, wide: bool | None = None) -> bytes:
    """buf with _selectedLanguage = lang; wide=None keeps the encoding, True uses 0xCC, False a fixint."""
    pos = lang_pos(buf)
    _, width = read_int(buf, pos)
    t = buf[pos]
    if wide is None and t > 0x7F:
        enc = bytes([t]) + lang.to_bytes(width - 1, "big", signed=t >= 0xD0)
    elif wide:
        enc = b"\xcc" + bytes([lang])
    else:
        enc = bytes([lang])
    return buf[:pos] + enc + buf[pos + width:]


def with_has_expected(buf: bytes, value: bool) -> bytes:
    i = buf.find(HAS_KEY) + len(HAS_KEY)
    if i < len(HAS_KEY) or buf[i] not in (0xC2, 0xC3):
        raise ValueError("_hasExpectedPlatformLanguage not found")
    return buf[:i] + (b"\xc3" if value else b"\xc2") + buf[i + 1:]


def is_readonly(p: Path) -> bool:
    return not os.access(p, os.W_OK)


def set_readonly(p: Path, ro: bool):
    os.chmod(p, 0o444 if ro else 0o666)


def run_exe(cmd: list, env: dict, retry=True, timeout=300) -> tuple[int, str]:
    """tp.run_exe with this scenario's environment (retries refusals caused by another test's TPC.exe or TpcJpPatch)."""
    return tp.run_exe(cmd, env=env, retry=retry, timeout=timeout)


class Cloud:
    """A fake Steam Cloud folder plus a fake app-data folder for one scenario."""

    def __init__(self, name: str):
        self.root = SANDBOX / name
        if self.root.exists():
            shutil.rmtree(self.root, onerror=lambda f, p, e: (os.chmod(p, 0o666), f(p)))
        self.cloud = self.root / "cloud"
        self.appdata = self.root / "appdata"
        self.cloud.mkdir(parents=True)
        self.original = {}

    def add(self, account: str, content: bytes, readonly=False) -> Path:
        f = self.cloud / account / "preferences.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        if f.exists():
            set_readonly(f, False)
        f.write_bytes(content)
        if readonly:
            set_readonly(f, True)
        self.original[account] = content
        return f

    def pref(self, account: str) -> Path:
        return self.cloud / account / "preferences.json"

    def read(self, account: str) -> bytes:
        return self.pref(account).read_bytes()

    def lang(self, account: str):
        return get_lang(self.read(account))

    def env(self, prefs_root=None, appdata=None) -> dict:
        env = dict(os.environ)
        env["TPCJP_PREFS_ROOT"] = str(prefs_root or self.cloud)
        env["TPCJP_APPDATA"] = str(appdata or self.appdata)
        env["TPCJP_SIMULATE_NOT_INSTALLED"] = "1"
        # also redirect LocalApplicationData (as in test_patcher.child_env), in case a hook were ever missing
        local = SANDBOX / "_profile" / "AppData" / "Local"
        local.mkdir(parents=True, exist_ok=True)
        env["USERPROFILE"] = str(SANDBOX / "_profile")
        env["LOCALAPPDATA"] = str(local)
        return env

    def cmd(self, *args) -> list:
        return [str(EXE), *[str(a) for a in args]]

    def run(self, *args, retry=True, **env_kw) -> tuple[int, str]:
        return run_exe(self.cmd(*args), self.env(**env_kw), retry=retry)

    def backups(self, account: str) -> list[Path]:
        d = self.appdata / "backup" / "preferences" / account
        return sorted(d.glob("*.json")) if d.exists() else []

    def previous_file(self, account: str) -> Path:
        return self.appdata / "previous-language" / f"{account}.txt"

    def previous(self, account: str):
        f = self.previous_file(account)
        return f.read_text("utf-8") if f.exists() else None

    def leftovers(self) -> list[str]:
        return sorted(str(p.relative_to(self.cloud)) for p in self.cloud.rglob("*") if p.name.endswith(".tpcjp-tmp"))

    def snapshot(self) -> dict:
        return {str(p.relative_to(self.cloud)): p.read_bytes() for p in sorted(self.cloud.rglob("*")) if p.is_file()}


def real_files() -> dict:
    """sha256 of the real preferences.json files and of the real app-data backups and previous-language files."""
    out = {}
    if REAL_CLOUD.exists():
        for p in sorted(REAL_CLOUD.glob("*/preferences.json")):
            out[str(p)] = tp.sha(p)
    for sub in ("backup", "previous-language"):
        d = REAL_APPDATA / sub
        if d.exists():
            for p in sorted(d.rglob("*")):
                if p.is_file():
                    out[str(p)] = tp.sha(p)
    return out


def real_log_size() -> int:
    f = REAL_APPDATA / "TpcJpPatch.log"
    return f.stat().st_size if f.exists() else 0


def real_log_tail(start: int) -> str:
    f = REAL_APPDATA / "TpcJpPatch.log"
    if not f.exists():
        return ""
    with open(f, "rb") as fh:
        fh.seek(start)
        return fh.read().decode("utf-8", "replace")


class Lock:
    """Opens a file with no sharing (as another program holding it would) until closed."""

    def __init__(self, path: Path):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.k32 = k32
        self.h = k32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0, None)  # GENERIC_READ, no share, OPEN_EXISTING
        if self.h in (None, wintypes.HANDLE(-1).value):
            raise OSError(ctypes.get_last_error(), "CreateFileW failed", str(path))

    def close(self):
        self.k32.CloseHandle(self.h)


def main():
    global EXE, SANDBOX
    warnings.filterwarnings("ignore")
    sys.stdout.reconfigure(encoding="utf-8")
    EXE = Path(tp.opt("--exe", DEFAULT_EXE)).resolve()
    SANDBOX = Path(tp.opt("--sandbox", SANDBOX)).resolve()
    tp.SANDBOX = SANDBOX / "_games"  # sandbox game folders (tp.Game) and tp.child_env
    tp.EXE = EXE
    only = set(tp.opt("--only", "").split(",")) - {""}
    skip = set(tp.opt("--skip", "").split(",")) - {""}
    if not EXE.exists():
        sys.exit(f"exe not found: {EXE}")

    src = tp.opt("--prefs")
    if src is None:
        found = sorted(REAL_CLOUD.glob("*/preferences.json")) if REAL_CLOUD.exists() else []
        src = found[0] if found else None
    raw = Path(src).read_bytes() if src else minimal_prefs()  # read only: the source is never written
    print(f"preferences copied from: {src or '(built-in minimal file)'} ({len(raw)} bytes, language {get_lang(raw)})")
    base = with_has_expected(with_lang(raw, EN, wide=False), True)  # English, fixint, language set up
    if get_lang(base) != EN:
        sys.exit("could not derive the test preferences from the copy")
    trunc = base[: len(base) // 2]

    real_before = real_files()
    log_start = real_log_size()
    c = tp.Checker()
    official_sha = tp.sha(tp.OFFICIAL)

    def scenario(name):
        run = (not only or name in only) and name not in skip
        if run:
            print(f"== {name}")
        return run

    def no_crash(out: str) -> bool:
        return "予期しないエラー" not in out and "Unhandled Exception" not in out

    def errors(out: str) -> list[str]:
        return [ln for ln in out.splitlines() if ln.startswith("エラー")]

    # ---------------- guard: the exe must use the fake folders (never the real settings) ----------------
    guard = Cloud("_guard")
    guard.add(ACC + "99", base)
    gg = tp.Game("_guard_game")
    rc, out = guard.run("status", "--game", gg.dir)
    real_accounts = [p.name for p in REAL_CLOUD.iterdir() if p.is_dir()] if REAL_CLOUD.exists() else []
    if rc != 0 or f"表示言語（{ACC}99）" not in out or any(f"表示言語（{a}）" in out for a in real_accounts):
        print(out)
        sys.exit("STOP: this exe does not use TPCJP_PREFS_ROOT (a Release build?). Language tests need the Debug build.")
    c.check("guard", guard.read(ACC + "99") == base and not guard.appdata.joinpath("backup").exists(),
            "status changes nothing")

    # ---------------- status lists every account ----------------
    if scenario("status"):
        name = "status"
        cl = Cloud(name)
        cl.add(ACC + "01", base)
        cl.add(ACC + "02", with_lang(base, FR))
        cl.add(ACC + "03", with_lang(base, JA))
        cl.add(ACC + "04", with_lang(base, DE, wide=True))
        cl.add(ACC + "05", with_has_expected(base, False))
        cl.add(ACC + "06", base, readonly=True)
        cl.add(ACC + "07", trunc)
        cl.add(ACC + "08", with_lang(base, 6))
        (cl.cloud / (ACC + "09") / "Saves").mkdir(parents=True)  # an account folder without preferences.json
        (cl.cloud / "steam_autocloud.vdf").write_text("x", "utf-8")
        before = cl.snapshot()
        g = tp.Game("status_game")
        rc, out = cl.run("status", "--game", g.dir)
        c.check(name, rc == 0 and no_crash(out), "exit 0", out[-300:])
        for acc, want in (("01", "英語"), ("02", "フランス語"), ("03", "日本語"), ("04", "ドイツ語"), ("05", "英語"),
                          ("06", "英語"), ("07", "読み取れません（preferences.json が途中で切れています）"), ("08", "不明(6)")):
            c.check(name, f"表示言語（{ACC}{acc}）: {want}" in out, f"account {acc}: {want}", out[-600:])
        c.check(name, f"（{ACC}09）" not in out and "steam_autocloud" not in out, "folders without preferences.json are not listed")
        lines = [ln for ln in out.splitlines() if ln.startswith("表示言語（")]
        c.check(name, lines == sorted(lines), "accounts listed in order", str(lines))
        c.check(name, cl.snapshot() == before and not cl.appdata.joinpath("backup").exists(), "status changes nothing")
        empty = Cloud("status_empty")
        rc, out = empty.run("status", "--game", g.dir)
        c.check(name, rc == 0 and "表示言語: 設定ファイルがありません" in out, "no accounts: status says so", out[-300:])
        rc, out = empty.run("status", "--game", g.dir, prefs_root=empty.root / "no such folder")
        c.check(name, rc == 0 and "表示言語: 設定ファイルがありません" in out, "missing Cloud folder: status says so", out[-300:])

    # ---------------- language ja / en / previous with several accounts, some of them failing ----------------
    if scenario("switch"):
        name = "switch"
        cl = Cloud(name)
        o = {"01": base, "02": with_lang(base, FR), "03": with_lang(base, JA), "04": with_lang(base, DE, wide=True),
             "05": with_has_expected(base, False), "06": base, "07": trunc}
        a = {k: ACC + k for k in o}
        for k, content in o.items():
            cl.add(a[k], content, readonly=k == "06")
        (cl.cloud / (ACC + "08") / "Saves").mkdir(parents=True)

        def same(*keys):
            return [k for k in keys if cl.read(a[k]) != o[k]]

        # 1. language ja
        rc, out = cl.run("language", "ja")
        c.check(name, rc == 1 and no_crash(out), "ja with failing accounts -> exit 1", out[-300:])
        for k, old in (("01", EN), ("02", FR), ("04", DE)):
            c.check(name, f"表示言語を{NAME[old]}から日本語に変更しました（{a[k]}）" in out, f"ja: {k} reported changed", out[-800:])
            c.check(name, cl.read(a[k]) == with_lang(o[k], JA), f"ja: {k} only _selectedLanguage changed (encoding kept)",
                    cl.read(a[k]).hex()[-120:])
            c.check(name, cl.previous(a[k]) == str(old), f"ja: {k} previous language {old} remembered", str(cl.previous(a[k])))
            bk = cl.backups(a[k])
            c.check(name, len(bk) == 1 and bk[0].read_bytes() == o[k], f"ja: {k} one backup = the file before", str(bk))
        c.check(name, f"表示言語は既に日本語です（{a['03']}）" in out, "ja: 03 already Japanese", out[-800:])
        for k, msg in (("05", "ゲームの言語設定がまだ作られていません"), ("06", "読み取り専用"), ("07", "途中で切れています")):
            line = next((ln for ln in out.splitlines() if f"（{a[k]}）" in ln), "")
            c.check(name, line.startswith("表示言語を変更できませんでした") and msg in line, f"ja: {k} failure reported ({msg})", line)
        c.check(name, not same("03", "05", "06", "07"), "ja: unchanged and failed accounts keep their bytes", str(same("03", "05", "06", "07")))
        c.check(name, is_readonly(cl.pref(a["06"])), "ja: read-only file stays read-only")
        c.check(name, all(cl.previous(a[k]) is None for k in ("03", "05", "06", "07")), "ja: no memory for unchanged/failed accounts")
        c.check(name, not cl.backups(a["03"]) and not cl.backups(a["07"]), "ja: no backup for unchanged/unreadable accounts")
        c.check(name, all(b.read_bytes() == o[k] for k in ("05", "06") for b in cl.backups(a[k])),
                "ja: any backup of a failed account is the unchanged file")
        c.check(name, f"（{ACC}08）" not in out, "ja: folder without preferences.json ignored")
        c.check(name, not cl.leftovers(), "ja: no temp files", str(cl.leftovers()))
        log = cl.appdata / "TpcJpPatch.log"
        c.check(name, log.exists() and f"日本語に変更しました（{a['01']}）" in log.read_text("utf-8"), "ja: log written under TPCJP_APPDATA")

        # 2. language ja again: nothing changes, the memory is not overwritten with Japanese
        rc, out = cl.run("language", "ja")
        c.check(name, rc == 1 and all(f"表示言語は既に日本語です（{a[k]}）" in out for k in ("01", "02", "04")),
                "ja again: changed accounts already Japanese", out[-600:])
        c.check(name, [cl.previous(a[k]) for k in ("01", "02", "04")] == ["0", "1", "3"], "ja again: memory kept")
        c.check(name, [len(cl.backups(a[k])) for k in ("01", "02", "04")] == [1, 1, 1], "ja again: no new backups")

        # 3. language previous
        rc, out = cl.run("language", "previous")
        c.check(name, rc == 1 and no_crash(out), "previous with a truncated account -> exit 1", out[-300:])
        c.check(name, not same("01", "02", "04"), "previous: 01/02/04 back to the exact original bytes", str(same("01", "02", "04")))
        c.check(name, f"表示言語をフランス語に戻しました（{a['02']}）" in out and f"表示言語をドイツ語に戻しました（{a['04']}）" in out,
                "previous: reported", out[-600:])
        c.check(name, cl.read(a["03"]) == with_lang(o["03"], EN) and f"表示言語を英語に戻しました（{a['03']}）" in out,
                "previous: Japanese account without memory -> English", out[-600:])
        c.check(name, not same("05", "06", "07"), "previous: accounts not in Japanese untouched")
        line = next((ln for ln in out.splitlines() if f"（{a['07']}）" in ln), "")
        c.check(name, line.startswith("表示言語を戻せませんでした") and "途中で切れています" in line, "previous: truncated reported", line)
        c.check(name, [len(cl.backups(a[k])) for k in ("01", "02", "03", "04")] == [2, 2, 1, 2], "previous: one backup per change")
        c.check(name, cl.backups(a["02"])[-1].read_bytes() == with_lang(o["02"], JA), "previous: backup = the Japanese file")

        # 4. language en
        rc, out = cl.run("language", "en")
        c.check(name, rc == 1 and no_crash(out), "en with a truncated account -> exit 1", out[-300:])
        c.check(name, cl.read(a["02"]) == with_lang(o["02"], EN) and cl.read(a["04"]) == with_lang(o["04"], EN),
                "en: 02/04 English (04 keeps the 0xCC encoding)", cl.read(a["04"]).hex()[-120:])
        c.check(name, all(f"表示言語は既に英語です（{a[k]}）" in out for k in ("01", "03", "05", "06")), "en: others already English",
                out[-800:])
        c.check(name, [cl.previous(a[k]) for k in ("01", "02", "03", "04")] == ["0", "1", None, "3"], "en: memory untouched")

        # 5. previous with no Japanese account: nothing to do
        cl.add(a["07"], with_lang(base, FR))
        o["07"] = with_lang(base, FR)
        before, nb = cl.snapshot(), sum(len(cl.backups(x)) for x in a.values())
        rc, out = cl.run("language", "previous")
        c.check(name, rc == 0 and not errors(out), "previous with no Japanese account -> exit 0", out[-300:])
        c.check(name, NOTHING_TO_RESTORE in out, "previous with no Japanese account says nothing was changed (I-1)", out[-300:])
        c.check(name, cl.snapshot() == before and sum(len(cl.backups(x)) for x in a.values()) == nb, "previous: nothing written")

        # 6. ja: the failing accounts are fixed one by one
        rc, out = cl.run("language", "ja")
        c.check(name, rc == 1 and len([ln for ln in out.splitlines() if "できませんでした" in ln]) == 2,
                "ja: 05 and 06 still fail", out[-500:])
        set_readonly(cl.pref(a["06"]), False)
        cl.add(a["05"], base)
        rc, out = cl.run("language", "ja")
        c.check(name, rc == 0 and not errors(out) and all(cl.lang(x) == JA for x in a.values()), "ja after the fixes -> exit 0, all Japanese",
                out[-500:])
        c.check(name, [cl.previous(a[k]) for k in ("01", "02", "04", "05", "06", "07")] == ["0", "0", "0", "0", "0", "1"],
                "ja: memory = the language each account had just before", str([cl.previous(a[k]) for k in a]))

        # 7. a read-only file while Japanese: previous and en fail cleanly
        set_readonly(cl.pref(a["06"]), True)
        ro = cl.read(a["06"])
        for cmd in ("previous", "en"):
            rc, out = cl.run("language", cmd)
            line = next((ln for ln in out.splitlines() if f"（{a['06']}）" in ln), "")
            c.check(name, rc == 1 and "読み取り専用" in line, f"{cmd}: read-only account reported", line or out[-300:])
            c.check(name, cl.read(a["06"]) == ro and is_readonly(cl.pref(a["06"])), f"{cmd}: read-only file unchanged")
        c.check(name, cl.lang(a["07"]) == EN, "en after previous: 07 English")
        c.check(name, not cl.leftovers(), "no temp files at the end", str(cl.leftovers()))

    # ---------------- previous-language memory ----------------
    if scenario("memory"):
        name = "memory"
        cl = Cloud(name)
        acc = ACC + "21"
        de = with_lang(base, DE)
        cl.add(acc, de)
        steps = [("ja", JA, "3"), ("ja", JA, "3"), ("previous", DE, "3"), ("previous", DE, "3"),
                 ("ja", JA, "3"), ("en", EN, "3"), ("previous", EN, "3"), ("ja", JA, "0"), ("previous", EN, "0")]
        for i, (cmd, want, mem) in enumerate(steps, 1):
            rc, out = cl.run("language", cmd)
            c.check(name, rc == 0 and cl.lang(acc) == want and cl.previous(acc) == mem,
                    f"step {i} {cmd}: language {want}, memory {mem}", f"exit {rc} lang {cl.lang(acc)} mem {cl.previous(acc)} {out[-200:]}")
        c.check(name, cl.read(acc) == with_lang(de, EN), "only the language byte ever changed")
        for content, want in (("2\r\n", IT), (" 1 ", FR), ("abc", EN), ("12", EN), ("", EN), ("3.0", EN)):
            cl.run("language", "ja")
            cl.previous_file(acc).write_text(content, "utf-8")
            rc, out = cl.run("language", "previous")
            c.check(name, rc == 0 and cl.lang(acc) == want, f"memory {content!r} -> {NAME[want]}", f"exit {rc} lang {cl.lang(acc)} {out[-200:]}")
        for content in ("999", "-1", "128", "99999999999"):
            cl.run("language", "ja")
            ja_bytes = cl.read(acc)
            cl.previous_file(acc).write_text(content, "utf-8")
            rc, out = cl.run("language", "previous")
            got = cl.lang(acc)
            c.check(name, rc in (0, 1) and no_crash(out) and got in (JA, EN) and (rc == 0 or cl.read(acc) == ja_bytes)
                    and not cl.leftovers(), f"memory {content!r}: handled cleanly", f"exit {rc} lang {got} {out[-200:]}")
            cl.add(acc, with_lang(de, EN))
        # memory per account: two accounts never mix
        cl2 = Cloud("memory_two")
        cl2.add(ACC + "31", with_lang(base, FR))
        cl2.add(ACC + "32", with_lang(base, IT))
        cl2.run("language", "ja")
        rc, out = cl2.run("language", "previous")
        c.check(name, rc == 0 and cl2.lang(ACC + "31") == FR and cl2.lang(ACC + "32") == IT, "two accounts keep their own language")
        # the memory file cannot be written: a warning, the switch still happens
        cl3 = Cloud("memory_readonly")
        cl3.add(ACC + "41", with_lang(base, FR))
        cl3.previous_file(ACC + "41").parent.mkdir(parents=True)
        cl3.previous_file(ACC + "41").write_text("2", "utf-8")
        set_readonly(cl3.previous_file(ACC + "41"), True)
        rc, out = cl3.run("language", "ja")
        c.check(name, rc == 0 and cl3.lang(ACC + "41") == JA and "元の表示言語を記録できませんでした" in out,
                "unwritable memory: switched with a warning", out[-300:])
        # F-SBP-4 fixed: the old record is removed, so restore really uses English as the warning says
        c.check(name, "（「元に戻す」では英語に戻します）" in out and cl3.previous(ACC + "41") is None,
                "unwritable memory: the stale record is removed and the warning says English", f"{cl3.previous(ACC + '41')!r} {out[-300:]}")
        rc, out = cl3.run("language", "previous")
        got = cl3.lang(ACC + "41")
        c.check(name, rc == 0 and got == EN, "unwritable memory: previous gives English as the warning says",
                f"exit {rc} got {NAME.get(got, got)} {out[-200:]}")
        # the record can neither be written nor removed (held open by another program): the warning says an older
        # remembered language may be used, and that is what happens
        cl4 = Cloud("memory_locked")
        cl4.add(ACC + "42", with_lang(base, FR))
        cl4.previous_file(ACC + "42").parent.mkdir(parents=True)
        cl4.previous_file(ACC + "42").write_text("2", "utf-8")
        lock = Lock(cl4.previous_file(ACC + "42"))
        try:
            rc, out = cl4.run("language", "ja")
        finally:
            lock.close()
        c.check(name, rc == 0 and cl4.lang(ACC + "42") == JA and "（「元に戻す」では以前に記録した言語に戻る場合があります）" in out,
                "locked memory: switched, warning says an older record may be used", out[-300:])
        rc, out = cl4.run("language", "previous")
        c.check(name, rc == 0 and cl4.lang(ACC + "42") == IT, "locked memory: previous uses the older record, as warned",
                f"exit {rc} lang {cl4.lang(ACC + '42')}")

    # ---------------- a preferences.json held open by another program ----------------
    if scenario("locked"):
        name = "locked"
        cl = Cloud(name)
        cl.add(ACC + "51", base)
        cl.add(ACC + "52", with_lang(base, FR))
        lock = Lock(cl.pref(ACC + "52"))
        try:
            rc, out = cl.run("language", "ja")
        finally:
            lock.close()
        line = next((ln for ln in out.splitlines() if f"（{ACC}52）" in ln), "")
        # round 3b (R12/R21): 「設定ファイルを読み書きできません（<reason>）」 (was 「設定ファイルを開けません（…）」)
        c.check(name, rc == 1 and no_crash(out) and line.startswith(f"表示言語を変更できませんでした（{ACC}52）: 設定ファイルを読み書きできません（")
                and line.endswith("）"), "locked account reported with the reason, exit 1", line or out[-300:])
        c.check(name, cl.lang(ACC + "51") == JA and cl.read(ACC + "52") == with_lang(base, FR), "other account switched, locked one unchanged")
        c.check(name, cl.previous(ACC + "52") is None and not cl.leftovers(), "no memory or temp file for the locked account")
        rc, out = cl.run("language", "ja")
        c.check(name, rc == 0 and cl.lang(ACC + "52") == JA and cl.previous(ACC + "52") == "1", "after unlocking it switches")
        rc, out = cl.run("language", "previous")
        c.check(name, rc == 0 and cl.lang(ACC + "51") == EN and cl.lang(ACC + "52") == FR, "previous after the lock", out[-300:])
        lock = Lock(cl.pref(ACC + "51"))
        try:
            rc, out = cl.run("language", "en")
        finally:
            lock.close()
        c.check(name, rc == 1 and cl.lang(ACC + "52") == EN and cl.read(ACC + "51") == base and not cl.leftovers(),
                "en: the locked account is reported, the other one switched", out[-300:])
        # round 3c (S13): what an interrupted write left next to preferences.json (e.g. a temp file a scanner held) is removed
        # even when the account is not changed, so that it does not stay in the Steam Cloud folder
        stale = [cl.pref(ACC + "51").with_name("preferences.json.tpcjp-tmp"), cl.pref(ACC + "52").with_name("preferences.json~RF1a2b.TMP")]
        for f in stale:
            f.write_bytes(b"partial")
        rc, out = cl.run("language", "en")  # both accounts are English already
        c.check(name, rc == 0 and no_crash(out) and not any(f.exists() for f in stale) and cl.read(ACC + "51") == base
                and f"前回中断したときの一時ファイルを 1 個削除しました（{ACC}51）" in out and f"前回中断したときの一時ファイルを 1 個削除しました（{ACC}52）" in out,
                "leftovers next to preferences.json are removed (and reported) although nothing is changed", out[-300:])

    # ---------------- apply / restore with language switching ----------------
    if scenario("apply_restore"):
        name = "apply_restore"
        ref_game = tp.Game("_reference")
        rc, out = ref_game.run("apply")  # --no-language, tp.child_env (no preferences at all)
        ref = tp.sha(ref_game.bundle)
        c.check(name, rc == 0, "reference apply", out[-200:])
        cl = Cloud(name)
        acc = {k: ACC + "6" + k for k in "abcd"}
        cl.add(acc["a"], with_lang(base, FR))
        cl.add(acc["b"], base)
        cl.add(acc["c"], with_has_expected(base, False))
        cl.add(acc["d"], trunc)
        orig = dict(cl.original)
        g = tp.Game("apply_restore_game")
        rc, out = cl.run("apply", "--game", g.dir)
        c.check(name, rc == 1 and no_crash(out) and "日本語データは適用しましたが、表示言語を日本語に切り替えられませんでした" in out,
                "apply with failing accounts -> exit 1 with a summary", out[-400:])
        c.check(name, tp.sha(g.bundle) == ref and tp.sha(g.backup) == official_sha and g.state.exists(), "the bundle is patched anyway")
        c.check(name, cl.lang(acc["a"]) == JA and cl.lang(acc["b"]) == JA, "a/b switched to Japanese")
        c.check(name, cl.read(acc["c"]) == orig[acc["c"]] and cl.read(acc["d"]) == orig[acc["d"]], "c/d unchanged")
        c.check(name, cl.previous(acc["a"]) == "1" and cl.previous(acc["b"]) == "0", "previous languages remembered")
        rc, out = cl.run("status", "--game", g.dir)
        c.check(name, rc == 0 and "適用済み" in out and f"表示言語（{acc['a']}）: 日本語" in out and f"表示言語（{acc['d']}）: 読み取れません" in out,
                "status: patched, per-account languages", out[-500:])
        rc, out = cl.run("restore", "--game", g.dir)
        c.check(name, rc == 1 and no_crash(out) and "日本語データは元に戻しましたが、表示言語を戻せませんでした" in out,
                "restore with a truncated account -> exit 1 with a summary", out[-400:])
        c.check(name, tp.sha(g.bundle) == official_sha and not g.backup.exists() and not g.state.exists(), "the bundle is restored anyway")
        c.check(name, cl.read(acc["a"]) == orig[acc["a"]] and cl.read(acc["b"]) == orig[acc["b"]], "a/b back to the original bytes")
        cl.add(acc["c"], base)
        cl.add(acc["d"], with_lang(base, IT))
        orig = dict(cl.original)
        rc, out = cl.run("apply", "--game", g.dir)
        c.check(name, rc == 0 and not errors(out) and all(cl.lang(x) == JA for x in acc.values()), "apply after the fixes -> exit 0",
                out[-300:])
        rc, out = cl.run("restore", "--game", g.dir)
        c.check(name, rc == 0 and not errors(out) and all(cl.read(x) == orig[x] for x in acc.values())
                and tp.sha(g.bundle) == official_sha, "restore -> exit 0, every account back", out[-300:])
        # --no-language / --keep-language never touch preferences
        before = cl.snapshot()
        nb = sum(len(cl.backups(x)) for x in acc.values())
        rc, out = cl.run("apply", "--game", g.dir, "--no-language")
        c.check(name, rc == 0 and cl.snapshot() == before and sum(len(cl.backups(x)) for x in acc.values()) == nb,
                "apply --no-language leaves preferences alone", out[-200:])
        cl.run("language", "ja")
        before = cl.snapshot()
        nb = sum(len(cl.backups(x)) for x in acc.values())
        rc, out = cl.run("restore", "--game", g.dir, "--keep-language")
        c.check(name, rc == 0 and cl.snapshot() == before and sum(len(cl.backups(x)) for x in acc.values()) == nb
                and tp.sha(g.bundle) == official_sha, "restore --keep-language leaves preferences alone", out[-200:])
        rc, out = cl.run("restore", "--game", g.dir)
        c.check(name, rc == 0 and all(cl.read(x) == orig[x] for x in acc.values()),
                "restore with nothing to restore still switches the language back", out[-300:])
        # a failing apply / restore does not switch the language
        before = cl.snapshot()
        g.bundle.unlink()
        rc, out = cl.run("apply", "--game", g.dir)
        c.check(name, rc == 1 and cl.snapshot() == before and not g.backup.exists(), "failed apply: language not switched", out[-300:])
        g2 = tp.Game("apply_restore_game2")
        cl.run("apply", "--game", g2.dir)
        g2.backup.write_bytes(b"\x00" * 1000)
        before = cl.snapshot()
        rc, out = cl.run("restore", "--game", g2.dir)
        c.check(name, rc == 1 and cl.snapshot() == before and all(cl.lang(x) == JA for x in acc.values()),
                "failed restore: language stays Japanese", out[-300:])
        # no preferences at all
        empty = Cloud("apply_no_accounts")
        g3 = tp.Game("apply_restore_game3")
        rc, out = empty.run("apply", "--game", g3.dir)
        c.check(name, rc == 1 and "設定ファイルが見つかりません" in out and tp.sha(g3.bundle) == ref,
                "apply without any preferences: patched, exit 1 with advice", out[-300:])
        rc, out = empty.run("restore", "--game", g3.dir)
        c.check(name, rc == 0 and tp.sha(g3.bundle) == official_sha, "restore without any preferences -> exit 0", out[-300:])
        c.check(name, NOTHING_TO_RESTORE in out, "restore without any preferences says the language was not changed", out[-300:])
        # restore when no account is Japanese (apply --no-language): the bundle is restored, the language left alone
        cl5 = Cloud("restore_not_japanese")
        cl5.add(ACC + "65", with_lang(base, FR))
        g4 = tp.Game("apply_restore_game4")
        cl5.run("apply", "--game", g4.dir, "--no-language")
        before = cl5.snapshot()
        rc, out = cl5.run("restore", "--game", g4.dir)
        c.check(name, rc == 0 and not errors(out) and tp.sha(g4.bundle) == official_sha and not g4.backup.exists(),
                "restore with no Japanese account -> exit 0, bundle restored", out[-300:])
        c.check(name, NOTHING_TO_RESTORE in out and cl5.snapshot() == before and not cl5.backups(ACC + "65"),
                "restore with no Japanese account: says the language was not changed, writes nothing", out[-300:])
        # an unreadable Japanese-or-not account is a failure, not 'nothing to do'
        cl5.add(ACC + "66", trunc)
        rc, out = cl5.run("language", "previous")
        c.check(name, rc == 1 and NOTHING_TO_RESTORE not in out and "表示言語を戻せませんでした" in out,
                "previous with an unreadable account reports the failure (no 'not changed' message)", out[-300:])

    # ---------------- no accounts ----------------
    if scenario("no_accounts"):
        name = "no_accounts"
        cl = Cloud(name)
        for root in (cl.cloud, cl.root / "no such folder"):
            for cmd, want in (("ja", 1), ("en", 1), ("previous", 0)):
                rc, out = cl.run("language", cmd, prefs_root=root)
                ok = rc == want and no_crash(out) and ("設定ファイルが見つかりません" in out if want else NOTHING_TO_RESTORE in out)
                c.check(name, ok, f"{cmd} with {'an empty' if root == cl.cloud else 'a missing'} Cloud folder -> exit {want}", out[-300:])
        c.check(name, not (cl.root / "no such folder").exists() and not cl.appdata.joinpath("backup").exists(), "nothing created")

    # ---------------- usage ----------------
    if scenario("usage"):
        name = "usage"
        cl = Cloud(name)
        cl.add(ACC + "71", base)
        g = tp.Game("usage_game")
        for args in (["language"], ["language", "xx"], ["language", "ja", "en"], ["language", "ja", "--game", g.dir],
                     ["language", "--no-language", "ja"], ["language", ""]):
            rc, out = cl.run(*args)
            c.check(name, rc == 2 and "使い方" in out, f"{' '.join(str(x) for x in args)!r} -> exit 2", f"exit {rc}")
        c.check(name, cl.read(ACC + "71") == base and not cl.appdata.joinpath("backup").exists(), "usage errors change nothing")
        rc, out = cl.run("LANGUAGE", "JA")
        c.check(name, rc == 0 and cl.lang(ACC + "71") == JA, "command and value are case-insensitive", out[-200:])
        rc, out = cl.run("language", "Previous")
        c.check(name, rc == 0 and cl.lang(ACC + "71") == EN, "'Previous' works", out[-200:])

    # ---------------- the game is running ----------------
    if scenario("game_running"):
        name = "game_running"
        cl = Cloud(name)
        cl.add(ACC + "81", base)
        fake = SANDBOX / "fake_game" / "TPC.exe"
        fake.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(os.environ["SystemRoot"]) / "System32" / "ping.exe", fake)
        proc = subprocess.Popen([str(fake), "-n", "30", "127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            time.sleep(1)
            res = [cl.run("language", cmd, retry=False) for cmd in ("ja", "en", "previous")]
        finally:
            proc.kill()
            proc.wait()
        c.check(name, all(rc == 1 and "起動" in out for rc, out in res), "language ja/en/previous refused while TPC.exe runs",
                str([rc for rc, _ in res]))
        c.check(name, cl.read(ACC + "81") == base and not cl.appdata.joinpath("backup").exists(), "nothing changed")

    # ---------------- the app-data folder cannot be used ----------------
    if scenario("appdata_unusable"):
        name = "appdata_unusable"
        cl = Cloud(name)
        cl.add(ACC + "91", base)
        blocker = cl.root / "appdata_is_a_file"
        blocker.write_text("x", "utf-8")
        rc, out = cl.run("language", "ja", appdata=blocker)
        c.check(name, rc == 1 and no_crash(out) and cl.read(ACC + "91") == base,
                "no backup possible -> the language is not changed, exit 1", out[-300:])
        line = next((ln for ln in out.splitlines() if f"（{ACC}91）" in ln), "")
        c.check(name, line.startswith(f"表示言語を変更できませんでした（{ACC}91）: 設定ファイルのバックアップを作成できません（{blocker}")
                and "設定ファイルを読み書きできません" not in line and "設定ファイルに書き込めません" not in line
                and "設定ファイルにアクセスできません" not in line,
                "the failure names the backup folder, not the preferences file", line)
        # round 3c (S12): the record of the language before Japanese cannot be read (held open by another program):
        # 'language previous' and 'restore' still switch the account back, to English, with a note, exit 0; the game's
        # preferences.json is not blamed
        cl2 = Cloud("appdata_record_unreadable")
        acc = ACC + "92"
        g = tp.Game("appdata_record_unreadable_game")
        rc, out = g.run("apply")  # tp.child_env: no preferences at all
        c.check(name, rc == 0, "record unreadable: apply (no language)", out[-200:])
        for cmd in (("language", "previous"), ("restore", "--game", g.dir)):
            what = f"record unreadable: {cmd[0]}"
            cl2.add(acc, with_lang(base, JA))
            cl2.previous_file(acc).parent.mkdir(parents=True, exist_ok=True)
            cl2.previous_file(acc).write_text("1", "utf-8")  # French
            lock = Lock(cl2.previous_file(acc))
            try:
                rc, out = cl2.run(*cmd)
            finally:
                lock.close()
            c.check(name, rc == 0 and no_crash(out) and not errors(out) and cl2.lang(acc) == EN
                    and f"表示言語を英語に戻しました（{acc}）" in out and "注意: 元の表示言語の記録を読み取れないため、英語に戻しました（" in out
                    and "設定ファイル" not in out, f"{what}: English with a note, exit 0, preferences.json not blamed", out[-300:])
        c.check(name, tp.sha(g.bundle) == official_sha and not g.backup.exists(), "record unreadable: restore restored the bundle")

    # ---------------- two language commands at the same time ----------------
    if scenario("concurrent"):
        # several 'language ja' at the same time on one Cloud folder (e.g. the tool started twice)
        name = "concurrent"
        nproc = int(tp.opt("--concurrent-procs", 2))
        lock_seen = 0
        for rnd in range(int(tp.opt("--concurrent-rounds", 6))):
            rname = f"{name}/{rnd}"
            cl = Cloud(f"concurrent_{rnd % 3}")
            accs = [ACC + f"a{i}" for i in range(4)]
            fr = with_lang(base, FR)
            for x in accs:
                cl.add(x, fr)
            for attempt in range(10):
                procs = [subprocess.Popen(cl.cmd("language", "ja"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=cl.env())
                         for _ in range(nproc)]
                res = [(p.communicate(timeout=120)[0].decode("utf-8", "replace"), p.returncode) for p in procs]
                if any(RUNNING in o for o, _ in res) or (all(r == 1 and BUSY in o for o, r in res) and attempt < 9):
                    # another test's TPC.exe, or a TpcJpPatch not started here held the lock: repeat the round
                    print("  (refused because of another program; repeating the round)")
                    time.sleep(40 if any(RUNNING in o for o, _ in res) else 1)
                    for x in accs:
                        cl.add(x, fr)
                    continue
                break
            codes = [r for _, r in res]
            busy = [r == 1 and BUSY in o for o, r in res]
            lock_seen += any(busy)
            print(f"  {rname}: exit {codes}")
            c.check(rname, all(r in (0, 1) for r in codes) and all(no_crash(o) for o, _ in res), "no crash", str(codes))
            # F-SBP-2 fixed: one process does the work, the others stop at once with a clear message
            c.check(rname, 0 in codes, f"at least one of {nproc} simultaneous 'language ja' succeeds", str(codes))
            c.check(rname, all(r == 0 or (b and errors(o) == [BUSY_LINE] and "変更しました" not in o) for (o, r), b in zip(res, busy)),
                    "every refused process says another TpcJpPatch is running and changes nothing",
                    str([errors(o) for o, r in res if r]))
            c.check(rname, all(cl.read(x) == with_lang(base, JA) for x in accs) and not cl.leftovers(),
                    "every account is Japanese (only the language byte changed), no temp files", str([cl.lang(x) for x in accs]))
            c.check(rname, all(cl.previous(x) == "1" for x in accs), "every account remembers French", str([cl.previous(x) for x in accs]))
            c.check(rname, all(len(cl.backups(x)) == 1 for x in accs), "one backup per account", str([len(cl.backups(x)) for x in accs]))
            rc, out = cl.run("language", "ja")
            c.check(rname, rc == 0 and all(f"表示言語は既に日本語です（{x}）" in out for x in accs) and all(cl.previous(x) == "1" for x in accs),
                    "a later 'language ja' finds everything done", out[-200:])
            rc, out = cl.run("language", "previous")
            c.check(rname, rc == 0 and all(cl.read(x) == fr for x in accs), "previous gives French back", out[-200:])
        c.check(name, lock_seen > 0, "the processes overlapped (one was refused) in at least one round", f"{lock_seen} rounds")

    # ---------------- another TpcJpPatch is running (this test holds its lock) ----------------
    if scenario("instance_lock"):
        name = "instance_lock"
        cl = Cloud(name)
        cl.add(ACC + "b1", with_lang(base, FR))
        cl.add(ACC + "b2", with_lang(base, JA))
        g = tp.Game("instance_lock_game")
        before = cl.snapshot()
        cmds = (("language", "ja"), ("language", "en"), ("language", "previous"), ("apply", "--game", g.dir),
                ("restore", "--game", g.dir))
        with tp.InstanceLock():
            res = [(args, *cl.run(*args, retry=False)) for args in cmds]
            st_rc, st_out = cl.run("status", "--game", g.dir, retry=False)
        for args, rc, out in res:
            what = " ".join(str(a) for a in args[:2])
            c.check(name, rc == 1 and no_crash(out) and errors(out) == [BUSY_LINE], f"{what}: refused while another TpcJpPatch runs",
                    out[-200:])
            c.check(name, len([ln for ln in out.splitlines() if ln.strip()]) == 2, f"{what}: nothing else done or printed", out[-300:])
        c.check(name, cl.snapshot() == before and not cl.appdata.joinpath("backup").exists() and cl.previous(ACC + "b1") is None,
                "preferences, backups and memory unchanged")
        c.check(name, tp.sha(g.bundle) == official_sha and not g.backup.exists() and not g.state.exists(), "game folder unchanged")
        c.check(name, st_rc == 0 and f"表示言語（{ACC}b1）: フランス語" in st_out and f"表示言語（{ACC}b2）: 日本語" in st_out,
                "status still works", st_out[-300:])
        rc, out = cl.run("language", "ja")
        c.check(name, rc == 0 and cl.lang(ACC + "b1") == JA and cl.previous(ACC + "b1") == "1", "after the other one ended: ja works",
                out[-200:])

    # ---------------- damaged or hostile preferences.json (depth, duplicate keys, huge lengths) ----------------
    if scenario("malformed"):
        name = "malformed"
        if build_prefs() != minimal_prefs():
            sys.exit("build_prefs() does not reproduce minimal_prefs()")
        deep_ok = build_prefs(top_extra=[("_deep", b"\x91" * 30 + b"\x00")])  # the innermost value at depth 32: allowed
        variants = [
            ("c1", "depth 33", build_prefs(top_extra=[("_deep", b"\x91" * 31 + b"\x00")]), "入れ子が深すぎます"),
            ("c2", "depth 200000", build_prefs(top_extra=[("_deep", b"\x91" * 200000 + b"\x00")]), "入れ子が深すぎます"),
            ("c3", "map depth 33", build_prefs(top_extra=[("_deep", b"\x81\x00" * 31 + b"\x00")]), "入れ子が深すぎます"),
            ("c4", "duplicate _selectedLanguage", build_prefs(lang_entries=LANG_ENTRIES + [("_selectedLanguage", b"\x0c")]),
             "同じ項目（_selectedLanguage）が重複しています"),
            ("c5", "duplicate _languagePreferences",
             build_prefs(top_extra=[("_languagePreferences", b"\x93\x02\x06\x81" + fixstr("_selectedLanguage") + b"\x0c")]),
             "同じ項目（_languagePreferences）が重複しています"),
            ("c6", "str32 length 0xFFFFFFF0", build_prefs(top_extra=[("_s", b"\xdb\xff\xff\xff\xf0abc")]), "途中で切れています"),
            ("c7", "str32 length 0x7FFFFFFF", build_prefs(top_extra=[("_s", b"\xdb\x7f\xff\xff\xffabc")]), "途中で切れています"),
            ("c8", "array32 count 0x7FFFFFFF", build_prefs(top_extra=[("_a", b"\xdd\x7f\xff\xff\xff\x00")]), "途中で切れています"),
            ("c9", "map32 count 0xFFFFFFFF", build_prefs(top_extra=[("_m", b"\xdf\xff\xff\xff\xff")]), "途中で切れています"),
            ("ca", "bin32 length 0xFFFFFFFF", build_prefs(top_extra=[("_b", b"\xc6\xff\xff\xff\xff\x00")]), "途中で切れています"),
            ("cb", "ext32 length 0x80000000", build_prefs(top_extra=[("_e", b"\xc9\x80\x00\x00\x00\x01")]), "途中で切れています"),
            ("cc", "uint64 cut short", build_prefs(top_extra=[("_u", b"\xcf\x00\x00")]), "途中で切れています"),
            ("cd", "a byte after the end", build_prefs() + b"\x00", "末尾に余分なデータ"),
        ]
        cl = Cloud(name)
        cl.add(ACC + "c0", deep_ok)
        for k, _, content, _ in variants:
            cl.add(ACC + k, content)
        rc, out = cl.run("status", "--game", tp.Game("malformed_game").dir)
        c.check(name, rc == 0 and no_crash(out) and f"表示言語（{ACC}c0）: 英語" in out, "status: a file nested 32 deep is read", out[-300:])
        for k, what, _, msg in variants:
            line = next((ln for ln in out.splitlines() if ln.startswith(f"表示言語（{ACC}{k}）")), "")
            c.check(name, line.startswith(f"表示言語（{ACC}{k}）: 読み取れません（") and msg in line, f"status: {what} -> {msg}", line)
        rc, out = cl.run("language", "ja")
        c.check(name, rc == 1 and no_crash(out), "ja with damaged files -> exit 1 (no crash)", out[-300:])
        c.check(name, cl.read(ACC + "c0") == with_lang(deep_ok, JA) and cl.previous(ACC + "c0") == "0",
                "ja: the 32-deep file is switched (only the language byte)", out[-300:])
        for k, what, content, msg in variants:
            line = next((ln for ln in out.splitlines() if f"（{ACC}{k}）" in ln), "")
            c.check(name, line.startswith(f"表示言語を変更できませんでした（{ACC}{k}）: ") and msg in line, f"ja: {what} reported", line)
            c.check(name, cl.read(ACC + k) == content and not cl.backups(ACC + k) and cl.previous(ACC + k) is None,
                    f"ja: {what}: file unchanged, no backup or memory")
        rc, out = cl.run("language", "previous")
        c.check(name, rc == 1 and no_crash(out) and cl.read(ACC + "c0") == deep_ok, "previous: the 32-deep file is back to English",
                out[-300:])
        c.check(name, all(f"表示言語を戻せませんでした（{ACC}{k}）" in out for k, _, _, _ in variants)
                and all(cl.read(ACC + k) == content for k, _, content, _ in variants), "previous: damaged files reported and unchanged",
                out[-500:])
        c.check(name, not cl.leftovers(), "no temp files", str(cl.leftovers()))
        # pytests-PF1 fixed: the Python port (tpcloc.prefs, used by switch_lang.py) refuses repeated keys like the exe
        for k, what, content, msg in variants:
            if "重複" not in msg:
                continue
            for action in ("read", "write"):
                try:
                    if action == "read":
                        pyprefs.read_language(cl.pref(ACC + k))
                    else:
                        pyprefs.set_selected_language(cl.pref(ACC + k), JA)
                    got = "accepted"
                except ValueError as e:
                    got = str(e)
                c.check(name, "repeated key" in got and cl.read(ACC + k) == content,
                        f"tpcloc.prefs {action}: {what} refused, file unchanged", got)
        got = pyprefs.read_language(cl.pref(ACC + "c0")).get("_selectedLanguage")
        c.check(name, got == EN, "tpcloc.prefs reads the 32-deep file (English after previous)", str(got))

    # ---------------- round-2 fixes seen through apply / restore / status with language switching ----------------
    if scenario("round2"):
        name = "round2"
        ref_game = tp.Game("_reference_r2")
        rc, out = ref_game.run("apply")
        ref = tp.sha(ref_game.bundle)
        c.check(name, rc == 0, "reference apply", out[-200:])
        cl = Cloud(name)
        acc = ACC + "d1"
        fr = with_lang(base, FR)
        cl.add(acc, fr)
        g = tp.Game("round2_game")
        # CS-06: data cut inside its last line (no final newline) is not applied, and the language is not switched
        cut = SANDBOX / "data_cut_last_line.tsv"
        cut.write_bytes(tp.DATA.read_bytes().rstrip(b"\n")[:-2])  # ...'区画15' -> '区画'
        before = cl.snapshot()
        rc, out = cl.run("apply", "--game", g.dir, "--data", cut)
        c.check(name, rc == 1 and no_crash(out) and errors(out) == [f"エラー: {tp.CUT_DATA}"], "apply with cut data: refused", out[-300:])
        c.check(name, cl.snapshot() == before and not cl.backups(acc) and cl.previous(acc) is None and tp.sha(g.bundle) == official_sha
                and not g.backup.exists() and not g.state.exists(), "apply with cut data: language and game unchanged")
        # the bundle is lost after an apply: apply rebuilds it from the backup; the language is already Japanese
        rc, out = cl.run("apply", "--game", g.dir)
        c.check(name, rc == 0 and cl.lang(acc) == JA and cl.previous(acc) == "1" and tp.sha(g.bundle) == ref,
                "apply: patched and switched to Japanese", out[-300:])
        g.bundle.unlink()
        rc, out = cl.run("status", "--game", g.dir)
        c.check(name, rc == 0 and tp.status_line(out) == tp.ST_MISSING and f"表示言語（{acc}）: 日本語" in out,
                "status: the bundle is missing, the account Japanese", out[-300:])
        rc, out = cl.run("apply", "--game", g.dir)
        c.check(name, rc == 0 and tp.REBUILT_MISSING in out and f"表示言語は既に日本語です（{acc}）" in out and tp.sha(g.bundle) == ref,
                "apply rebuilds the missing bundle; the language is already Japanese", out[-300:])
        c.check(name, cl.previous(acc) == "1" and len(cl.backups(acc)) == 1, "the remembered language (French) is kept, no new backup",
                f"{cl.previous(acc)} {len(cl.backups(acc))}")
        # the state file is lost: status still shows the patch (by content) and the account's language
        g.state.unlink()
        rc, out = cl.run("status", "--game", g.dir)
        c.check(name, rc == 0 and tp.status_line(out) == tp.ST_NO_STATE and f"表示言語（{acc}）: 日本語" in out,
                "status without the state file: applied (no record), the account Japanese", out[-300:])
        # restore with cut data and no state cannot tell what the bundle is: refused, the language stays Japanese
        before = cl.snapshot()
        rc, out = cl.run("restore", "--game", g.dir, "--data", cut)
        c.check(name, rc == 1 and no_crash(out) and tp.CUT_DATA in out and "バックアップは削除せずに残しました" in out
                and cl.snapshot() == before and tp.sha(g.bundle) == ref and g.backup.exists(),
                "restore with cut data and no state: refused, nothing changed, still Japanese", out[-300:])
        # leftovers of an interrupted run: restore deletes them, restores the bundle and then French
        junk = [g.bundle.with_name(g.bundle.name + s) for s in (".tpcjp-tmp", "~RF2d4e.TMP", ".tpcjp-state.tpcjp-tmp")]
        for j in junk:
            j.write_bytes(b"partial")
        rc, out = cl.run("restore", "--game", g.dir)
        c.check(name, rc == 0 and not errors(out) and tp.removed_msg(len(junk)) in out and not any(j.exists() for j in junk),
                "restore deletes the leftovers and says so", out[-300:])
        c.check(name, tp.sha(g.bundle) == official_sha and not g.backup.exists() and not g.state.exists() and cl.read(acc) == fr,
                "restore: official bundle, French again", out[-300:])
        rc, out = cl.run("status", "--game", g.dir)
        c.check(name, rc == 0 and tp.status_line(out) == tp.ST_ORIGINAL and f"表示言語（{acc}）: フランス語" in out,
                "status after restore: official, French", out[-300:])

    # ---------------- safety: the real settings and app data were never touched ----------------
    c.check("safety", real_files() == real_before, "real preferences.json and real backups/previous-language unchanged")
    tail = real_log_tail(log_start)
    c.check("safety", ACC not in tail and str(SANDBOX) not in tail, "nothing of this run reached the real log", tail[-300:])

    print(f"\npassed {c.passed}, failed {len(c.failed)}, xfail {len(c.xfailed)}, xpass {len(c.xpassed)}")
    for f in c.failed:
        print("FAILED", f)
    for f in c.xfailed:
        print("XFAIL", f)
    for f in c.xpassed:
        print("XPASS", f)
    if "--keep" not in sys.argv:
        try:
            shutil.rmtree(SANDBOX, onerror=lambda f, p, e: (os.chmod(p, 0o666), f(p)))
        except OSError as e:
            print("could not remove the sandbox:", e)
    sys.exit(1 if c.failed else 0)


if __name__ == "__main__":
    main()
