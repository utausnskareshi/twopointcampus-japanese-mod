"""Build the list of terms whose official Japanese is missing, plus an official EN->JA glossary.

Outputs (work/, not distributed):
  worklist.json          every entry needing translation, with references and category
  glossary_official.tsv  EN term -> official JA (short names/labels), with frequency

usage: python tools/worklist.py
"""
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tpcloc import paths, text  # noqa: E402

SKIP_SOURCES = {"I2LS_Fonts"}  # font asset mapping, not text
REF_LANGS = {"Chinese (Simplified)": "zh", "Korean": "ko", "French": "fr", "German": "de"}
LEGAL_KEYS = {"UI/Frontend/Copyright_Text", "Meta/Legal&Credits/Legal"}

CATEGORY_RULES = [
    ("DLC_Hospital", r"DLC_Hospital|Hospital|Medical|Illness|Patient|Ward|Clinic"),
    ("DLC_Ghost", r"DLC_Ghost|Ghost|Haunt|Spook|Plot(Cleansing)?"),
    ("ChallengeMode", r"ChallengeMode|Challenge_Mode|DLC_ChallengeMode|CM\d"),
    ("Mods", r"^UI/Mods"),
    ("Seasonal", r"Valentine|Christmas|Easter|Halloween|Summer|Fair2023|Seasonal|Winter|Spring"),
    ("Promo", r"Promo|Prime|Influencer|Nintendo|Kudosh2023"),
    ("Cafeteria", r"Cafeteria|Crumley"),
]


def category(source: str, key: str) -> str:
    for name, rx in CATEGORY_RULES:
        if re.search(rx, key):
            return name
    return "Other"


def kind(source: str, key: str, en: str) -> str:
    k = key.lower()
    if source in ("I2LS_ArchetypeSpecificNames", "I2LS_CharacterNames"):
        return "person_name"
    if source == "I2LS_VO_Tannoy":
        return "tannoy"
    if source in ("I2LS_VO_Radio", "I2LS_Music_Radio"):
        return "radio"
    if re.search(r"(_name|/name|name)$", k):
        return "name"
    if re.search(r"(title|header|heading)$", k):
        return "title"
    if re.search(r"(desc|description|definition|text|body|tooltip|flavour|flavor)$", k) or len(en) > 60:
        return "text"
    return "label"


def main():
    data = json.loads((paths.WORK_DIR / "extract" / "strings.json").read_text("utf-8"))
    work = []
    keep = []  # left in English by every reference language: format strings, brands, dev-only labels
    gloss = collections.defaultdict(collections.Counter)
    for source, src in data["sources"].items():
        if source in SKIP_SOURCES:
            continue
        en_map = src["langs"]["English"]
        ja_map = src["langs"]["Japanese"]
        seen = set()
        for key in src["keys"]:
            if key in seen:
                continue
            seen.add(key)
            en = en_map.get(key, "")
            ja = ja_map.get(key, "")
            if text.needs_translation(en, ja):
                entry = {"source": source, "key": key, "en": en, "ja_official": ja,
                         "category": category(source, key), "kind": kind(source, key, en)}
                for lang, short in REF_LANGS.items():
                    entry[short] = src["langs"].get(lang, {}).get(key, "")
                if all(entry[short] == en for short in REF_LANGS.values()):
                    keep.append(entry)
                    continue
                if key in LEGAL_KEYS:
                    entry["reason"] = "legal notice: an unofficial translation must not reword it"
                    keep.append(entry)
                    continue
                if key.startswith("Misc/KeyCode/") or en.strip() in ("OK", "XP"):
                    entry["reason"] = "key names / abbreviations stay in English (as in the official JA)"
                    keep.append(entry)
                    continue
                if re.search(r"_(Title|Text)$", key) and re.search(r" (Title|Text)\s*$", en):
                    entry["reason"] = "placeholder text"
                    keep.append(entry)
                    continue
                if not entry["zh"].strip() and not entry["ko"].strip():
                    # Left blank by the other Asian localisations too: dev placeholders / unused text.
                    entry["reason"] = "blank in zh/ko"
                    keep.append(entry)
                    continue
                work.append(entry)
            elif ja and text.has_japanese(ja):
                words = len(text.WORD_RE.findall(en))
                if 1 <= words <= 5 and len(en) <= 40 and "\n" not in en and not en.rstrip().endswith((".", "!", "?")):
                    if kind(source, key, en) in ("name", "title", "label", "person_name"):
                        gloss[en.strip()][ja.strip()] += 1
    out = paths.WORK_DIR / "worklist.json"
    out.write_text(json.dumps(work, ensure_ascii=False, indent=1), "utf-8")
    (paths.WORK_DIR / "keep_english.json").write_text(json.dumps(keep, ensure_ascii=False, indent=1), "utf-8")
    print(f"kept in English (untranslated in all reference languages): {len(keep)}")
    with open(paths.WORK_DIR / "glossary_official.tsv", "w", encoding="utf-8") as fp:
        fp.write("en\tja\tcount\talternatives\n")
        for en, c in sorted(gloss.items(), key=lambda x: x[0].lower()):
            (ja, n), *rest = c.most_common()
            fp.write(f"{en}\t{ja}\t{n}\t{' | '.join(f'{j}({m})' for j, m in rest)}\n")
    cats = collections.Counter((w["category"]) for w in work)
    kinds = collections.Counter((w["kind"]) for w in work)
    words = sum(len(text.WORD_RE.findall(w["en"])) for w in work)
    print(f"worklist: {len(work)} entries, {words} English words -> {out}")
    print("by category:", dict(cats.most_common()))
    print("by kind:", dict(kinds.most_common()))
    print(f"official glossary terms: {len(gloss)}")


if __name__ == "__main__":
    main()
