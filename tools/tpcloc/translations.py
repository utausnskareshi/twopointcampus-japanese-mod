"""Our Japanese translations: translations/ja/<source>.json = {term: {"ja", "status", "note"}}.

Status values: "draft" (machine/Claude draft), "reviewed" (accepted by a reviewer),
"edited" (changed by a reviewer). Files keep the game's key order.
"""
import csv
import json
import os
from pathlib import Path

from . import paths

STATUSES = ("draft", "reviewed", "edited")
GLOSSARY_NEW = paths.PROJECT_ROOT / "translations" / "glossary_new.tsv"


def load_all() -> dict[str, dict[str, dict]]:
    out = {}
    for f in sorted(paths.TRANSLATIONS_DIR.glob("*.json")):
        out[f.stem] = json.loads(f.read_text("utf-8"))
    return out


def save(source: str, entries: dict[str, dict], key_order: list[str] | None = None) -> Path:
    if key_order:
        rank = {k: i for i, k in enumerate(key_order)}
        entries = dict(sorted(entries.items(), key=lambda kv: rank.get(kv[0], len(rank))))
    path = paths.TRANSLATIONS_DIR / f"{source}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(entries, ensure_ascii=False, indent=1) + "\n"
    # write a temporary file next to it and swap it in, so a failed write never truncates the only copy
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fp:  # text mode, same line endings as before
            fp.write(body)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def load_glossary_new() -> list[dict]:
    if not GLOSSARY_NEW.exists():
        return []
    with open(GLOSSARY_NEW, encoding="utf-8", newline="") as fp:
        return [row for row in csv.DictReader(fp, delimiter="\t") if row.get("en")]


def load_glossary_official() -> dict[str, str]:
    path = paths.WORK_DIR / "glossary_official.tsv"
    out = {}
    with open(path, encoding="utf-8", newline="") as fp:
        for row in csv.DictReader(fp, delimiter="\t", quoting=csv.QUOTE_NONE):
            out[row["en"]] = row["ja"]
    return out
