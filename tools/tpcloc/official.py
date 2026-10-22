"""Find the official Japanese bundle: the patch's backup, or the installed file itself.

Files next to the installed bundle (shared with patcher/TpcJpPatch/Patcher.cs):
  <bundle>.tpcjp-original  the official bundle as it was before patching
  <bundle>.tpcjp-state     key=value lines (original_sha256 = the backup's SHA-256, patched_sha256, game_version =
                           the game version the backup was taken for, patch_version, applied, time, ...)
The backup is trusted while the installed bundle is our output or identical to the backup; an installed bundle
equal to original_sha256 is the recorded official file itself. When only the state file vouches for the backup
(the installed bundle is missing, cannot be read, or differs from the backup while the game version is
unchanged), the backup must match original_sha256 (when recorded) and the state file must record the installed
game version as the backup's, like Patcher.CheckBackupRecorded since round 3c (finding S1): without a state file
or with a state of TpcJpPatch 1.0.0 (no game_version) the backup's version cannot be confirmed and it is not used.
Any other bundle is a new official file (game update, Steam file verification) and the backup is stale. Whenever
the backup is returned it must also load and hold official content (not our patched output), like
Patcher.CheckBackup.

An installed bundle in a format the patcher does not read (LocalisationBundle's UnsupportedFormatException: UnityFS
format version 1..99 other than 6..8, or a serialized file of format version 1..99 other than 22 or big endian) is
neither repaired from the backup nor taken for the official file unless the state file proves the same game version
(then the format bytes are damage and the backup is used). The serialized file is checked only when UnityPy reads
the bundle; one UnityPy cannot read counts as damaged here, which gives the same result (the backup only for a
proven unchanged game version) with another message.

Damage detection differs from the patcher (TpcJpPatch.exe): here a bundle counts as damaged only when UnityPy
cannot read it or finds no TextAsset. LocalisationBundle.Load is much stricter (exact LZ4 expansion, header,
block and directory flags, the serialized file's tables and type trees, objects read exactly with zero padding,
the AssetBundle container, NUL in a text, duplicate names). A file that only the patcher refuses (e.g. non-zero
alignment padding) is classified by its content here: with a backup and no state file, a 1.0.0 state or another
recorded game version, the installed file is returned with a "stale" WARNING, and without a backup it is taken for
the official file, while the patcher refuses it (and rebuilds from the backup only for a proven unchanged game
version, as here). official_japanese only reads files; verify_bundle.py stops on any WARNING and extract.py prints
the reason. version.txt is read like GameLocator.GameVersion (paths.read_game_version); any error (missing, a
folder, access denied) and an empty file count as an unknown version, which never equals a recorded one.
"""
import re
from pathlib import Path

import UnityPy
from UnityPy.files import SerializedFile

from . import bundles, i2cache, paths, text, translations

BACKUP_SUFFIX = ".tpcjp-original"
STATE_SUFFIX = ".tpcjp-state"
UNKNOWN_VERSION = paths.UNKNOWN_VERSION  # GameLocator.UnknownVersion: recorded when version.txt cannot be read


class OfficialBundleError(RuntimeError):
    """No file can be trusted as the official data (e.g. our output, but no matching official backup)."""


def backup_path(bundle: Path) -> Path:
    return bundle.with_name(bundle.name + BACKUP_SUFFIX)


def state_path(bundle: Path) -> Path:
    return bundle.with_name(bundle.name + STATE_SUFFIX)


def read_state(bundle: Path) -> dict[str, str] | None:
    """The patcher's state file (split at the first '=', like Patcher.ReadState), or None.

    Lines end at CR LF, CR or LF only, like File.ReadAllLines (str.splitlines would also split at U+0085, U+2028,
    U+2029, ...); undecodable bytes become U+FFFD as in .NET. A folder at the path is no state file, like
    File.Exists (finding S19).
    """
    path = state_path(bundle)
    if not path.is_file():
        return None
    state = {}
    for line in re.split(r"\r\n|\r|\n", path.read_text("utf-8-sig", errors="replace")):
        key, eq, value = line.partition("=")
        if eq and key:
            state[key] = value
    return state


def write_state(bundle: Path, updates: dict[str, str]) -> None:
    """Update the state file in the patcher's format (UTF-8 without BOM, CRLF), keeping other keys.

    Raises ValueError (nothing written) for a key or value that would break the line format: CR or LF, or a key
    that is empty or contains '='.
    """
    state = read_state(bundle) or {}
    for k, v in updates.items():
        if not k or "=" in k or re.search(r"[\r\n]", k + v):
            raise ValueError(f"cannot write {k!r}={v!r} to {state_path(bundle).name}: line break, '=' or empty key")
    state.update(updates)
    path = state_path(bundle)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes("".join(f"{k}={v}\r\n" for k, v in state.items()).encode("utf-8"))
    tmp.replace(path)


def classify(bundle: Path) -> tuple[int, int, int]:
    """(ours, untranslated, other) over our translated terms, like Patcher.Classify.

    ours: the value is our Japanese translation; untranslated: the value has no Japanese (the official
    text we translate over); other: other Japanese (an official translation added by an update).
    Translations without Japanese are skipped: they may equal the official text.
    """
    return _classify_texts(bundles.read_texts(bundle))


def _classify_texts(texts: dict[str, str]) -> tuple[int, int, int]:
    ours = untranslated = other = 0
    for source, entries in translations.load_all().items():
        if source not in texts:
            continue
        values = i2cache.to_dict(texts[source])
        for key, e in entries.items():
            ja = e.get("ja", "")
            if key not in values or not text.has_japanese(ja):
                continue
            v = values[key]
            if v == ja:
                ours += 1
            elif not text.has_japanese(v):
                untranslated += 1
            else:
                other += 1
    return ours, untranslated, other


def is_patched(bundle: Path) -> tuple[bool, int]:
    """Our output: our texts clearly outnumber the official ones (an update may translate a few like we did)."""
    return _patched(classify(bundle))


def _patched(counts: tuple[int, int, int]) -> tuple[bool, int]:
    ours, untranslated, other = counts
    return ours >= 3 and ours > untranslated + other, ours


def _read(bundle: Path) -> tuple[dict[str, str] | None, str | None]:
    """(texts, None), or (None, why the bundle cannot be read: UnityPy fails or finds no TextAsset). Less strict than
    LocalisationBundle.Load (see the module docstring)."""
    try:
        texts = bundles.read_texts(bundle)
    except Exception as e:  # UnityPy fails in many ways on damaged data (LZ4BlockError, UnicodeDecodeError, ...)
        return None, f"{type(e).__name__}: {e}"
    return (texts, None) if texts else (None, "no TextAsset")


def _backup_problem(bundle: Path) -> str | None:
    """Why the backup is not official data (None: it is), like Patcher.CheckBackup: it cannot be read or is in a format
    the patcher does not read (「バックアップが壊れています」), or it is our patched output
    (「バックアップがパッチ適用後のデータになっています」)."""
    backup = backup_path(bundle)
    texts, err = _read(backup)
    if err:
        return f"{backup.name} is damaged ({err})"
    unsupported = _unsupported(backup, readable=True)
    if unsupported:  # LocalisationBundle.Load refuses it, so CheckBackup does as well
        return f"{backup.name} is damaged (a format TpcJpPatch.exe does not read: {unsupported})"
    patched, n = _patched(_classify_texts(texts))
    if patched:
        return f"{backup.name} is patched data ({n} of our translations), not the official file"
    return None


def _installed_version(bundle: Path) -> str | None:
    """version.txt of the game folder holding the bundle (<game>/TPC_Data/StreamingAssets/aa/StandaloneWindows64/),
    read like GameLocator.GameVersion (paths.read_game_version); None when unreadable."""
    try:
        game = bundle.parents[4]
    except IndexError:
        return None
    return paths.read_game_version(game)


def installed_version(bundle: Path) -> str:
    """The installed game version as TpcJpPatch.exe records it (GameLocator.GameVersion): UNKNOWN_VERSION when
    version.txt cannot be read."""
    v = _installed_version(bundle)
    return UNKNOWN_VERSION if v is None else v


def _known(v: str | None) -> bool:
    """Like Patcher.KnownVersion: a game version that can be compared (not missing, empty or UNKNOWN_VERSION)."""
    return bool(v) and v != UNKNOWN_VERSION


def _same_game_version(state: dict[str, str] | None, bundle: Path) -> bool:
    """Like Patcher.SameGameVersion: the backup was taken for the installed game version (both must be known)."""
    v = state.get("game_version") if state else None
    cur = _installed_version(bundle)
    return _known(cur) and v == cur


def _backup_mismatch(bundle: Path, state: dict[str, str] | None, bak: str) -> str | None:
    """Why the backup cannot stand in for the installed bundle when only the state file vouches for it (None: it can).

    Like Patcher.CheckBackup followed by Patcher.CheckBackupRecorded: it must load and hold official content, be
    the file recorded in original_sha256 (when recorded) and have been taken for the installed game version. Since
    round 3c (finding S1) the state file must prove that: without one, or with a state file of TpcJpPatch 1.0.0
    (no game_version), the version cannot be confirmed, like an unknown recorded or installed version.
    """
    backup = backup_path(bundle)
    why = _backup_problem(bundle)
    if why:
        return why
    if state is not None and state.get("original_sha256", bak) != bak:
        return f"{backup.name} does not match original_sha256 in {state_path(bundle).name} (damaged or replaced backup?)"
    if not _same_game_version(state, bundle):
        recorded, cur = (state or {}).get("game_version"), _installed_version(bundle)
        if _known(recorded) and _known(cur):
            return f"{backup.name} was taken for game version {recorded} but the installed version is {cur}"

        def shown(v):
            return v if _known(v) else "unknown"
        # 「バックアップがどのバージョンのゲームのものか確認できません（記録: …、現在のゲームのバージョン: …）」
        return (f"it cannot be confirmed which game version {backup.name} was taken for (recorded: {shown(recorded)}, "
                f"installed: {shown(cur)})")
    return None


def _unsupported(bundle: Path, readable: bool) -> str | None:
    """Why TpcJpPatch.exe refuses the bundle as a format it does not read (LocalisationBundle's
    UnsupportedFormatException), or None: UnityFS format version 1..99 other than 6..8, or an entry flagged as a
    serialized file (4) whose header (20 bytes or more) has format version 1..99 other than 22 or is big endian (0,
    100 and above or another endianness byte are damage there). The serialized files are only checked when UnityPy
    reads the bundle (readable)."""
    try:
        data = bundle.read_bytes()
    except OSError:
        return None
    if data[:8] == b"UnityFS\0" and len(data) >= 12:
        version = int.from_bytes(data[8:12], "big")
        if not 6 <= version <= 8:
            return f"UnityFS version {version}" if 0 < version < 100 else None
    if not readable:
        return None
    try:
        entries = [f for top in UnityPy.load(data).files.values() for f in getattr(top, "files", {}).values()]
    except Exception:  # changed since it was read: damage is decided elsewhere
        return None
    for f in entries:
        reader = f.reader if isinstance(f, SerializedFile) else f
        if not getattr(f, "flags", 0) & 4 or not hasattr(reader, "read_bytes") or reader.Length < 20:
            continue
        pos = reader.Position
        reader.Position = 0
        header = bytes(reader.read_bytes(20))
        reader.Position = pos
        # SerializedFile.CheckFormat: the first entry that fails decides (unsupported or damage)
        version = int.from_bytes(header[8:12], "big")
        if version != 22:
            return f"serialized file version {version}" if 0 < version < 100 else None
        if header[16] != 0:
            return "big-endian serialized file" if header[16] == 1 else None
    return None


def official_japanese(bundle: Path) -> tuple[Path, str]:
    """Return (file holding the official Japanese data, short reason): the file the patcher would start from.

    Raises OfficialBundleError when no file can be trusted: the installed bundle is our output, missing or
    unreadable and the backup is missing or does not fit the state file (which must prove the game version), or
    the installed bundle was changed while the game version is unchanged and the backup does not fit the state file
    either, or the installed bundle is in a format the patcher does not read and the state file does not prove the
    same game version, or the backup that would be returned is damaged or our patched output
    (Patcher.CheckBackup). A reason starting with "WARNING" means the backup is stale or the installed bundle is
    not usable as it is.
    """
    backup = backup_path(bundle)
    state = read_state(bundle)
    bak = bundles.sha256(backup) if backup.exists() else None
    if not bundle.exists():
        # e.g. a patcher run killed while replacing it: like Patcher.Apply, only a backup that fits the state is used
        if bak is None:
            raise OfficialBundleError(f"{bundle.name} and {backup.name} are missing; verify the game files in Steam")
        why = _backup_mismatch(bundle, state, bak)
        if why:
            raise OfficialBundleError(f"{bundle.name} is missing and {why}; verify the game files in Steam")
        return backup, f"WARNING: the installed bundle is missing; official data from {backup.name}"
    cur = bundles.sha256(bundle)
    if state is not None and state.get("patched_sha256") == cur:
        if bak is None:
            raise OfficialBundleError(f"{bundle.name} is our patched output ({state_path(bundle).name}) but "
                                      f"{backup.name} is missing; verify the game files in Steam")
        if state.get("original_sha256", bak) != bak:
            raise OfficialBundleError(f"{backup.name} does not match original_sha256 in {state_path(bundle).name} "
                                      "(damaged backup?); verify the game files in Steam")
        why = _backup_problem(bundle)
        if why:
            raise OfficialBundleError(f"{bundle.name} is our patched output ({state_path(bundle).name}) but {why}; "
                                      "verify the game files in Steam")
        return backup, f"installed bundle is our patched output; official data from {backup.name}"
    if bak == cur:
        why = _backup_problem(bundle)
        if why:
            raise OfficialBundleError(f"{bundle.name} is identical to {backup.name} and {why}; "
                                      "verify the game files in Steam")
        return backup, f"installed bundle is identical to {backup.name} (not patched)"
    if state is not None and state.get("original_sha256") == cur:
        # the official file the backup was taken from: the backup was damaged, replaced or removed since
        if bak is None:
            return bundle, (f"installed bundle is the official file recorded in {state_path(bundle).name} "
                            f"(original_sha256); {backup.name} is missing")
        return bundle, (f"WARNING: {backup.name} differs from the installed bundle, which is the official file recorded "
                        f"in {state_path(bundle).name} (original_sha256; damaged or replaced backup?); "
                        "reading the installed bundle")
    texts, damaged = _read(bundle)
    unsupported = _unsupported(bundle, readable=not damaged)
    if unsupported and not (bak is not None and _same_game_version(state, bundle)):
        # like Patcher (round 3c): a format the patcher does not read (e.g. after a game update) is neither damage it
        # repairs from a backup of another or an unconfirmed game version nor a file it patches
        unused = (f"; {backup.name} is not used (the state file does not prove the same game version)"
                  if bak is not None else "")
        raise OfficialBundleError(f"{bundle.name} is in a format TpcJpPatch.exe does not read ({unsupported}){unused}; "
                                  "verify the game files in Steam, and if that does not help, the patch needs an update")
    if damaged:
        # never official data; like Patcher.Apply, only a backup that fits the state is used
        if bak is None:
            raise OfficialBundleError(f"{bundle.name} cannot be read ({damaged}) and {backup.name} is missing; "
                                      "verify the game files in Steam")
        why = _backup_mismatch(bundle, state, bak)
        if why:
            raise OfficialBundleError(f"{bundle.name} cannot be read ({damaged}) and {why}; "
                                      "verify the game files in Steam")
        return backup, f"WARNING: the installed bundle cannot be read ({damaged}); official data from {backup.name}"
    patched, n = _patched(_classify_texts(texts))
    if patched:
        if bak is None:
            raise OfficialBundleError(f"{bundle.name} holds {n} of our translations but {backup.name} is missing; "
                                      "verify the game files in Steam")
        why = _backup_problem(bundle)
        if why:
            raise OfficialBundleError(f"{bundle.name} holds {n} of our translations but {why}; "
                                      "verify the game files in Steam")
        return backup, (f"installed bundle holds {n} of our translations (not recorded in the state file); "
                        f"official data from {backup.name}")
    if bak is not None and _same_game_version(state, bundle):
        # like Patcher.SameGameVersion: the official file of an unchanged game version is the backup itself
        why = _backup_mismatch(bundle, state, bak)
        if why:
            raise OfficialBundleError(f"the installed bundle differs from {backup.name} while the game version is "
                                      f"unchanged, and {why}; verify the game files in Steam")
        return backup, (f"WARNING: the installed bundle differs from {backup.name} but the game version is unchanged "
                        f"(modified or damaged file); official data from {backup.name}")
    if bak is not None:
        return bundle, (f"WARNING: {backup.name} is stale: the installed bundle is neither the backup nor our output "
                        "(game update or Steam file verification?); reading the installed bundle "
                        "(the backup is replaced on the next apply)")
    return bundle, "no backup; the installed bundle is the official one"
