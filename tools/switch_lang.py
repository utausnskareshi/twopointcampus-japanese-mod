"""Development helper: show or change the game's saved UI language (preferences.json).

Every change first copies preferences.json to work/backup/prefs/<steam-id>/<timestamp>.json.

usage:
  python tools/switch_lang.py status
  python tools/switch_lang.py ja        # Japanese (12)
  python tools/switch_lang.py en        # English (0)
  python tools/switch_lang.py <id>
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tpcloc import paths, prefs  # noqa: E402


def game_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq TPC.exe", "/NH"], capture_output=True, text=True).stdout
    return "TPC.exe" in out


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "status"
    files = paths.preferences_files()
    if not files:
        sys.exit("preferences.json not found; launch the game once first")
    if arg == "status":
        for f in files:
            lang = prefs.read_language(f)
            name = prefs.LANGUAGE_NAMES.get(lang["_selectedLanguage"], "?")
            print(f"{f.parent.name}: selected={lang['_selectedLanguage']} ({name}) {lang}")
        return
    target = {"ja": prefs.JAPANESE, "en": 0}.get(arg)
    target = int(arg) if target is None else target
    if game_running():
        sys.exit("Two Point Campus is running; close it first")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for f in files:
        backup = paths.WORK_DIR / "backup" / "prefs" / f.parent.name / f"{stamp}.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, backup)
        old = prefs.set_selected_language(f, target)
        print(f"{f.parent.name}: {old} -> {target} (backup {backup})")


if __name__ == "__main__":
    main()
