"""Locations of the game install, its localisation bundles and the per-user data folder."""
import os
import re
from pathlib import Path

APP_ID = "1649080"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORK_DIR = PROJECT_ROOT / "work"
TRANSLATIONS_DIR = PROJECT_ROOT / "translations" / "ja"

# Bundle file suffix for each I2 language name (as used in the Addressables catalog).
BUNDLE_LANGS = {
    "English": "english",
    "French": "french",
    "Italian": "italian",
    "German": "german",
    "Spanish": "spanish",
    "Polish": "polish",
    "Russian": "russian",
    "Chinese (Simplified)": "chinese(simplified)",
    "Chinese (Traditional)": "chinese(traditional)",
    "Korean": "korean",
    "Brazilian Portuguese": "brazilianportuguese",
    "Turkish": "turkish",
    "Japanese": "japanese",
}


def _steam_root() -> Path:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            return Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except OSError:
        return Path(r"C:\Program Files (x86)\Steam")


def find_game_dir() -> Path:
    """Find the Two Point Campus install folder from Steam's library folders."""
    env = os.environ.get("TPC_GAME_DIR")
    if env:
        return Path(env)
    steam = _steam_root()
    libraries = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        libraries += [Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text("utf-8"))]
    for lib in libraries:
        manifest = lib / "steamapps" / f"appmanifest_{APP_ID}.acf"
        if manifest.exists():
            m = re.search(r'"installdir"\s+"([^"]+)"', manifest.read_text("utf-8"))
            if m:
                return lib / "steamapps" / "common" / m.group(1)
    raise FileNotFoundError("Two Point Campus install folder not found (set TPC_GAME_DIR)")


def bundles_dir(game_dir: Path) -> Path:
    return game_dir / "TPC_Data" / "StreamingAssets" / "aa" / "StandaloneWindows64"


def loc_bundle(game_dir: Path, language: str) -> Path:
    return bundles_dir(game_dir) / f"localisation_assets_localisation{BUNDLE_LANGS[language]}.bundle"


UNKNOWN_VERSION = "不明"  # GameLocator.UnknownVersion: what the patcher records when version.txt cannot be read

# File.ReadAllText picks the encoding by a byte order mark (removed) and reads UTF-8 otherwise
_BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
         (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
# String.Trim removes Char.IsWhiteSpace (str.strip would also remove U+001C..U+001F)
_DOTNET_WHITESPACE = ("\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
                      "\u2028\u2029\u202f\u205f\u3000")
# Patcher.OneLine: the characters WriteState turns into a space (line breaks for File.ReadAllLines or str.splitlines)
_ONE_LINE = re.compile("[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]")


def read_game_version(game_dir: Path) -> str | None:
    """version.txt like GameLocator.GameVersion, or None when it cannot be read (missing, a folder, access denied).

    Read like File.ReadAllText (a byte order mark is removed, undecodable bytes become U+FFFD) and trimmed like
    String.Trim; the line breaks Patcher.OneLine replaces in the state file then become spaces as well, so the value
    is the game_version the patcher records and reads back (findings S4/S17).
    """
    try:
        raw = (Path(game_dir) / "version.txt").read_bytes()
    except OSError:
        return None
    encoding = "utf-8"
    for bom, name in _BOMS:
        if raw.startswith(bom):
            raw, encoding = raw[len(bom):], name
            break
    return _ONE_LINE.sub(" ", raw.decode(encoding, errors="replace").strip(_DOTNET_WHITESPACE))


def game_version(game_dir: Path) -> str:
    """The installed game version as TpcJpPatch.exe records it (read_game_version): UNKNOWN_VERSION when version.txt
    cannot be read; "" for an empty file."""
    v = read_game_version(game_dir)
    return UNKNOWN_VERSION if v is None else v


def user_data_dir() -> Path:
    return Path(os.environ["USERPROFILE"]) / "AppData" / "LocalLow" / "Two Point Studios" / "Two Point Campus"


def preferences_files() -> list[Path]:
    return sorted((user_data_dir() / "Cloud").glob("*/preferences.json"))
