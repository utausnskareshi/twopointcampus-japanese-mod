"""Look up game text by key, English or Japanese (official data plus our translations).

usage: python tools/lookup.py <regex> [--en | --ja] [--source S] [--limit N]
  default: <regex> is matched against the key (case-insensitive)
  --en:    matched against the English text
  --ja:    matched against the Japanese text (official or ours)
Output: "JA:" is the official translation, "JA*:" is ours (with status).
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tpcloc import paths, text, translations  # noqa: E402


def opt(name, default=None):
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return default


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.stdout.reconfigure(encoding="utf-8")
    rx = re.compile(sys.argv[1], re.I)
    limit = int(opt("--limit", 40))
    data = json.loads((paths.WORK_DIR / "extract" / "strings.json").read_text("utf-8"))
    ours = translations.load_all()
    shown = 0
    for source, src in data["sources"].items():
        if opt("--source") and source not in opt("--source").split(","):
            continue
        en_map, ja_map = src["langs"]["English"], src["langs"]["Japanese"]
        mine = ours.get(source, {})
        for key in dict.fromkeys(src["keys"]):
            en = en_map.get(key, "")
            ja = ja_map.get(key, "") if text.has_japanese(ja_map.get(key, "")) else ""
            our = mine.get(key)
            if "--en" in sys.argv:
                hay = en
            elif "--ja" in sys.argv:
                hay = ja + "\n" + (our or {}).get("ja", "")
            else:
                hay = key
            if not rx.search(hay):
                continue
            print(f"### {source}|{key}")
            print("EN: " + en.replace("\n", "↵"))
            if ja:
                print("JA: " + ja.replace("\n", "↵"))
            if our:
                print(f"JA*({our.get('status')}): " + our["ja"].replace("\n", "↵"))
            shown += 1
            if shown >= limit:
                print(f"... (limit {limit}; use --limit N)")
                return


if __name__ == "__main__":
    main()
