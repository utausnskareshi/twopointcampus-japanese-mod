"""Shared helpers for the Python test suite (tests/python).

Run from the project root:
    set PYTHONIOENCODING=utf-8
    .venv\\Scripts\\python.exe -m unittest discover -s tests/python -t . -v

Safety: every test works in its own folder under %TEMP%\\tpcjp_pytests. The tools are run through
tests/python/_runtool.py, which points tpcloc.paths (PROJECT_ROOT, WORK_DIR, TRANSLATIONS_DIR,
user_data_dir) and TPC_GAME_DIR at that folder, so the real project data, the real game and the
user's settings are never written. Real files (the official bundle copy, work/extract/strings.json,
the installed language bundles) are only read or copied.
"""
import contextlib
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[2]
TOOLS = PROJECT / "tools"
RUNTOOL = Path(__file__).resolve().with_name("_runtool.py")
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from tpcloc import bundles, i2cache, paths, text, translations  # noqa: E402

TEMP_ROOT = Path(os.environ.get("TEMP") or tempfile.gettempdir()) / "tpcjp_pytests"
OFFICIAL_BUNDLE = PROJECT / "work" / "backup" / "official" / "localisation_assets_localisationjapanese.bundle"
REAL_WORK = PROJECT / "work"
REAL_STRINGS = REAL_WORK / "extract" / "strings.json"
REAL_DATA = PROJECT / "dist" / "data" / "TpcJpPatch.data.tsv"
DEBUG_EXE = PROJECT / "patcher" / "TpcJpPatch" / "bin" / "Debug" / "net48" / "TpcJpPatch.exe"
BUNDLE_REL = Path("TPC_Data/StreamingAssets/aa/StandaloneWindows64")
JA_BUNDLE_NAME = "localisation_assets_localisationjapanese.bundle"
GAME_VERSION = "10.3.169253+2024-12-06.1241"
KEEP = os.environ.get("TPCJP_PYTESTS_KEEP") == "1"


def real_patch_version() -> str:
    """#patch_version of dist/data (the release version status shows for it), e.g. 1.0.1."""
    return re.search(r"^#patch_version\t(.*)$", REAL_DATA.read_text("utf-8"), re.M).group(1)


def finding(fid: str):
    """Mark a test that exposes a reported product problem (finding id) as an expected failure.

    With TPCJP_PYTESTS_FINDINGS=fail the test runs as a normal test, to see the actual failure.
    """
    def deco(func):
        func.finding = fid
        if os.environ.get("TPCJP_PYTESTS_FINDINGS") == "fail":
            return func
        return unittest.expectedFailure(func)
    return deco


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(Path(p).read_bytes())


def base_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def new_dir(test: unittest.TestCase, name: str = "") -> Path:
    """A fresh empty folder under the temp root, removed after the test (kept with TPCJP_PYTESTS_KEEP=1)."""
    base = TEMP_ROOT / "t"
    base.mkdir(parents=True, exist_ok=True)
    label = (name or test.id().rsplit(".", 1)[-1])[:40]
    d = Path(tempfile.mkdtemp(prefix=label + "_", dir=base))
    if not KEEP:
        test.addCleanup(shutil.rmtree, d, True)
    return d


def assert_under_temp(p: Path) -> Path:
    p = Path(p).resolve()
    root = TEMP_ROOT.resolve()
    if root != p and root not in p.parents:
        raise AssertionError(f"refusing to use {p}: not under {root}")
    return p


@contextlib.contextmanager
def project_paths(root: Path):
    """Point tpcloc.paths/translations at a temp project folder (in-process tests)."""
    root = assert_under_temp(root)
    with mock.patch.object(paths, "PROJECT_ROOT", root), \
            mock.patch.object(paths, "WORK_DIR", root / "work"), \
            mock.patch.object(paths, "TRANSLATIONS_DIR", root / "translations" / "ja"), \
            mock.patch.object(translations, "GLOSSARY_NEW", root / "translations" / "glossary_new.tsv"), \
            mock.patch.object(paths, "user_data_dir", lambda: root / "userdata"):
        yield root


class ToolResult:
    def __init__(self, code: int, out: str, cmd: list):
        self.code, self.out, self.cmd = code, out, cmd

    def __repr__(self):
        return f"<exit {self.code}>\n{self.out}"

    def lines(self, prefix: str) -> list[str]:
        return [l for l in self.out.splitlines() if l.startswith(prefix)]


def run_tool(tool: str, *args, project: Path, game: Path | None = None, env: dict | None = None,
             timeout: int = 600) -> ToolResult:
    """Run tools/<tool> in a subprocess with tpcloc.paths pointing at `project` (a temp folder)."""
    project = assert_under_temp(project)
    e = dict(os.environ)
    e["PYTHONIOENCODING"] = "utf-8"
    e["TPCJP_TEST_PROJECT"] = str(project)
    e.pop("TPC_GAME_DIR", None)
    if game is not None:
        e["TPC_GAME_DIR"] = str(assert_under_temp(game))
    if env:
        e.update(env)
    cmd = [sys.executable, str(RUNTOOL), tool, *[str(a) for a in args]]
    p = subprocess.run(cmd, capture_output=True, env=e, timeout=timeout, cwd=str(project))
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    return ToolResult(p.returncode, out.replace("\r\n", "\n"), cmd)


# ---------------------------------------------------------------- synthetic project

REF_LANGS = {"Chinese (Simplified)": "zh", "Korean": "ko", "French": "fr", "German": "de"}


def write_json(path: Path, obj, indent=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=indent), "utf-8")


def make_project(root: Path, sources: dict, *, worklist: list | None = None, ours: dict | None = None,
                 glossary_official: dict | None = None, glossary_new: list | None = None,
                 categories: dict | None = None, kinds: dict | None = None, ja_sha: str = "ab" * 32,
                 skip_worklist: set | None = None) -> Path:
    """Write a small project: work/extract/strings.json, work/worklist.json, glossaries, translations/ja.

    sources: {source: [(key, en, ja_official), ...]} in game order (a key may repeat).
    worklist: explicit list, or None = every unique (source, key) for which text.needs_translation() holds
              (minus skip_worklist), category/kind from `categories`/`kinds` ({(source, key): value}).
    ours: {source: {key: entry dict}} written with translations.save() (game key order).
    """
    root = assert_under_temp(root)
    categories, kinds = categories or {}, kinds or {}
    strings = {"game_dir": str(root / "game"), "game_version": GAME_VERSION, "bundles": {}, "sources": {}}
    for lang in paths.BUNDLE_LANGS:
        strings["bundles"][lang] = {"file": f"localisation_assets_localisation{paths.BUNDLE_LANGS[lang]}.bundle",
                                    "sha256": ja_sha if lang == "Japanese" else "00" * 32}
    for source, rows in sources.items():
        langs = {"English": {}, "Japanese": {}, **{l: {} for l in REF_LANGS}}
        for key, en, ja in rows:
            langs["English"][key] = en
            langs["Japanese"][key] = ja
            for l, short in REF_LANGS.items():
                langs[l][key] = f"[{short}] {en}"
        strings["sources"][source] = {"keys": [k for k, _, _ in rows], "langs": langs}
    write_json(root / "work" / "extract" / "strings.json", strings)
    if worklist is None:
        worklist, seen = [], set()
        for source, rows in sources.items():
            for key, en, ja in rows:
                if (source, key) in seen or (source, key) in (skip_worklist or set()):
                    continue
                seen.add((source, key))
                if text.needs_translation(en, ja):
                    w = {"source": source, "key": key, "en": en, "ja_official": ja,
                         "category": categories.get((source, key), "Other"), "kind": kinds.get((source, key), "label")}
                    for l, short in REF_LANGS.items():
                        w[short] = f"[{short}] {en}"
                    worklist.append(w)
    write_json(root / "work" / "worklist.json", worklist)
    gl = root / "work" / "glossary_official.tsv"
    with open(gl, "w", encoding="utf-8", newline="") as fp:
        fp.write("en\tja\tcount\talternatives\n")
        for en, ja in (glossary_official or {}).items():
            fp.write(f"{en}\t{ja}\t1\t\n")
    gn = root / "translations" / "glossary_new.tsv"
    gn.parent.mkdir(parents=True, exist_ok=True)
    with open(gn, "w", encoding="utf-8", newline="") as fp:  # same writer as tools/glossary_new.py
        w = csv.writer(fp, delimiter="\t", lineterminator="\n")
        w.writerow(["en", "ja", "variants"])
        for row in glossary_new or []:
            w.writerow([row["en"], row["ja"], ""])
    (root / "translations" / "ja").mkdir(parents=True, exist_ok=True)
    if ours:
        with project_paths(root):
            for source, entries in ours.items():
                keys = strings["sources"].get(source, {}).get("keys")
                translations.save(source, entries, keys)
    return root


def load_ours(root: Path) -> dict:
    out = {}
    for f in sorted((Path(root) / "translations" / "ja").glob("*.json")):
        out[f.stem] = json.loads(f.read_text("utf-8"))
    return out


def snapshot_files(folder: Path) -> dict[str, bytes]:
    folder = Path(folder)
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def entry(ja: str, status: str = "draft", note: str = "", **extra) -> dict:
    return {"ja": ja, "status": status, "note": note, **extra}


# ---------------------------------------------------------------- patch data (port of PatchData.cs)

class PatchDataFormatError(ValueError):
    pass


def cs_unescape(s: str) -> str:
    """Python port of TpcJpPatch.PatchData.Unescape (throws on a dangling or unknown escape)."""
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
        if i >= len(s):
            raise PatchDataFormatError("dangling escape")
        m = {"\\": "\\", "n": "\n", "r": "\r", "t": "\t"}.get(s[i])
        if m is None:
            raise PatchDataFormatError(f"unknown escape \\{s[i]}")
        out.append(m)
        i += 1
    return "".join(out)


SEPARATOR = "[i2t]"
_BASE_RE = re.compile(r"[0-9a-f]{16}")
_USED_HEADERS = {"patch_version", "game_version", "bundle_sha256"}  # PatchData.UsedHeaders


def _is_control(c: str) -> bool:
    """PatchData.IsControl: U+0000..U+001F, U+007F and the Unicode line breaks U+0085, U+2028, U+2029."""
    return ord(c) < 0x20 or c in "\x7f\x85  "


def cs_parse_patch_data(text_: str, check_count: bool = True) -> tuple[dict, list[dict]]:
    """Python port of PatchData.Parse: (header, entries) with the same validation.

    Since the C1 fixes: a key must be non-empty without "=" or "[i2t]", an inline text must not contain
    "[i2t]", the base must be 16 lower-case hex digits, and (check_count) "#entries" must equal the count.
    Since round 3: U+0000 is refused in every line (nul_translation_blamed_on_game_file).
    """
    lines = text_.split("\n")
    if not lines or not lines[0].rstrip("\r").startswith("#TPCJP-PATCH\t1"):
        raise PatchDataFormatError("bad magic")
    if "\x00" in lines[0]:
        raise PatchDataFormatError("line 1: NUL")
    header, entries = {}, []
    for i in range(1, len(lines)):
        line = lines[i].rstrip("\r")
        if "\x00" in line:
            raise PatchDataFormatError(f"line {i + 1}: NUL")
        if not line:
            continue
        f = line.split("\t")
        if line[0] == "#":
            if len(f) >= 2:
                name, value = f[0][1:], cs_unescape(f[1])
                # round 3b (R18): the header values the patcher uses must not contain control characters / line breaks
                if name in _USED_HEADERS and any(_is_control(c) for c in value):
                    raise PatchDataFormatError(f"line {i + 1}: control character in a header value")
                header[name] = value
            continue
        is_ref = line[0] == "&"
        if len(f) != (5 if is_ref else 4):
            raise PatchDataFormatError(f"line {i + 1}: {len(f)} fields")
        e = {"source": f[0][1:] if is_ref else f[0], "key": cs_unescape(f[1]), "base": f[2]}
        if is_ref:
            e.update(ref_source=f[3], ref_key=cs_unescape(f[4]), ja=None)
        else:
            e.update(ja=cs_unescape(f[3]))
        if (not e["key"] or "=" in e["key"] or SEPARATOR in e["key"] or (e["ja"] is not None and SEPARATOR in e["ja"])
                or not _BASE_RE.fullmatch(e["base"])):
            raise PatchDataFormatError(f"line {i + 1}: bad content")
        entries.append(e)
    if check_count:
        declared = header.get("entries")
        try:
            n = int(declared.strip()) if declared is not None and re.fullmatch(r"\s*[+-]?\d+\s*", declared) else None
        except ValueError:
            n = None
        if n != len(entries):
            raise PatchDataFormatError(f"truncated: {len(entries)} entries, header says {declared}")
    return header, entries


def cs_load_patch_data(raw: bytes) -> tuple[dict, list[dict]]:
    """Python port of PatchData.Load(bytes): strict UTF-8 (BOM removed), the text must end with a newline
    (round-2 fix CS-06: a file cut inside its last line is refused), then Parse with the #entries check."""
    try:
        t = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise PatchDataFormatError("not UTF-8") from e
    if t.startswith("\ufeff"):
        t = t[1:]
    if not t.endswith("\n"):
        raise PatchDataFormatError("truncated: no newline at the end")
    return cs_parse_patch_data(t)


def fill_texts(official_texts: dict[str, str], data_text: str, skip: set | None = None) -> dict[str, str]:
    """What the patcher writes (fill-only): {source: new cache text} for the sources it changes."""
    _, entries = cs_parse_patch_data(data_text)
    last = {n: i2cache.to_dict(t) for n, t in official_texts.items()}
    repl = {}
    for e in entries:
        if skip and (e["source"], e["key"]) in skip:
            continue
        cur = last.get(e["source"], {})
        if e["key"] not in cur or base_hash(cur[e["key"]]) != e["base"]:
            continue
        ja = e["ja"]
        if ja is None:
            ja = last.get(e["ref_source"], {}).get(e["ref_key"])
            if ja is None or not text.has_japanese(ja) or SEPARATOR in ja:
                continue
        repl.setdefault(e["source"], {})[e["key"]] = ja
    out = {}
    for source, r in repl.items():
        out[source] = i2cache.dumps([(k, r.get(k, v)) for k, v in i2cache.parse(official_texts[source])])
    return out


# ---------------------------------------------------------------- sandbox game folders

def make_game(root: Path, japanese: Path | None = OFFICIAL_BUNDLE) -> Path:
    """A fake game folder: TPC.exe (empty), version.txt and optionally a Japanese bundle copy."""
    root = assert_under_temp(root)
    (root / BUNDLE_REL).mkdir(parents=True, exist_ok=True)
    (root / "TPC.exe").write_bytes(b"")
    (root / "version.txt").write_text(GAME_VERSION, "utf-8")
    if japanese is not None:
        shutil.copyfile(japanese, root / BUNDLE_REL / JA_BUNDLE_NAME)
    return root


def ja_bundle(game: Path) -> Path:
    return Path(game) / BUNDLE_REL / JA_BUNDLE_NAME


def write_state(bundle: Path, **values):
    """A state file in the patcher's format (UTF-8 without BOM, CRLF)."""
    p = bundle.with_name(bundle.name + ".tpcjp-state")
    p.write_bytes("".join(f"{k}={v}\r\n" for k, v in values.items()).encode("utf-8"))
    return p


LOCKED_MESSAGE = "別の TpcJpPatch が実行中です"


def run_debug_exe(*args, game: Path | None, appdata: Path, timeout: int = 300, language: bool = False,
                  retry_locked: bool = True) -> tuple[int, str]:
    """Run the Debug patcher on a sandbox game only (--game is forced, language never changed).

    TPCJP_APPDATA / TPCJP_PREFS_ROOT keep its log, backups and preference lookups inside the temp root
    (TPCJP_PREFS_ROOT = <appdata>/prefs, a fake Cloud folder). language=True lets restore run without
    --keep-language and allows "language ...": only the fake Cloud folder is ever read or written then.
    Retries while another test run's fake TPC.exe is running, and (retry_locked) while another TpcJpPatch holds
    the single-instance mutex (other test suites run the patcher at the same time).
    """
    import time

    appdata = assert_under_temp(appdata)
    cmd = [str(DEBUG_EXE), *[str(a) for a in args]]
    if args[0] != "language":
        cmd += ["--game", str(assert_under_temp(game))]
    if args[0] == "apply":
        cmd.append("--no-language")
    elif args[0] == "restore":
        if not language:
            cmd.append("--keep-language")
    elif args[0] == "language":
        if not language:
            raise AssertionError("language commands need language=True")
    elif args[0] not in ("status",):
        raise AssertionError(f"command {args[0]} not allowed in tests")
    prefs_root = assert_under_temp(appdata / "prefs")
    env = dict(os.environ, TPCJP_APPDATA=str(appdata), TPCJP_PREFS_ROOT=str(prefs_root))
    prefs_root.mkdir(parents=True, exist_ok=True)
    attempt = 0
    while True:
        attempt += 1
        p = subprocess.run(cmd, capture_output=True, env=env, timeout=timeout)
        out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
        if p.returncode != 0 and "起動しています" in out and attempt < 6:
            time.sleep(40)
            continue
        if p.returncode != 0 and retry_locked and LOCKED_MESSAGE in out and attempt < 60:
            time.sleep(2)
            continue
        return p.returncode, out.replace("\r\n", "\n")


@contextlib.contextmanager
def hold_patcher_mutex(wait_ms: int = 120000):
    """Own the patcher's single-instance mutex (Local\\TpcJpPatch) like a running TpcJpPatch would.

    Waits for any other holder first; keep the block short (other suites may run the patcher meanwhile).
    """
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.CreateMutexW(None, False, "Local\\TpcJpPatch")
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
    try:
        r = k32.WaitForSingleObject(handle, wait_ms)
        if r not in (0, 0x80):  # WAIT_OBJECT_0, WAIT_ABANDONED
            raise TimeoutError("could not take the TpcJpPatch mutex")
        try:
            yield
        finally:
            k32.ReleaseMutex(handle)
    finally:
        k32.CloseHandle(handle)


# ---------------------------------------------------------------- loading tool modules in-process

def load_tool(name: str):
    """Import tools/<name>.py as a fresh module (its __main__ block does not run)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"tool_{name}", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
