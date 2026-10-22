"""Run a tools/*.py script with tpcloc.paths pointing at a temp project (used by helpers.run_tool).

usage (environment): TPCJP_TEST_PROJECT=<temp project>  [TPC_GAME_DIR=<sandbox game>]
    python tests/python/_runtool.py <tool.py> [args...]

The project folder and the game folder must be under %TEMP%\\tpcjp_pytests; without TPC_GAME_DIR a
non-existent folder is used, so no tool can find (or touch) the real installed game.
"""
import os
import runpy
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"
TEMP_ROOT = (Path(os.environ.get("TEMP") or tempfile.gettempdir()) / "tpcjp_pytests").resolve()


def under_temp(p: Path) -> Path:
    p = Path(p).resolve()
    if TEMP_ROOT != p and TEMP_ROOT not in p.parents:
        sys.exit(f"_runtool: refusing {p} (not under {TEMP_ROOT})")
    return p


def main():
    root = under_temp(os.environ["TPCJP_TEST_PROJECT"])
    game = os.environ.get("TPC_GAME_DIR")
    os.environ["TPC_GAME_DIR"] = str(under_temp(game) if game else root / "no-game-here")
    sys.path.insert(0, str(TOOLS))
    from tpcloc import paths

    paths.PROJECT_ROOT = root
    paths.WORK_DIR = root / "work"
    paths.TRANSLATIONS_DIR = root / "translations" / "ja"
    paths.user_data_dir = lambda: root / "userdata"
    from tpcloc import translations

    translations.GLOSSARY_NEW = root / "translations" / "glossary_new.tsv"
    tool = TOOLS / sys.argv[1]
    if tool.name in ("dev_patch.py", "switch_lang.py", "package.py", "test_patcher.py"):
        sys.exit(f"_runtool: {tool.name} is not run by the tests")
    sys.argv = [str(tool), *sys.argv[2:]]
    runpy.run_path(str(tool), run_name="__main__")


if __name__ == "__main__":
    main()
