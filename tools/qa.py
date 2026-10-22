"""Validate translations/ja/*.json against the English source and the style rules.

Errors (exit code 1): unknown keys, broken parameters/tags/plural tags, empty text.
Warnings: line-break differences, leftover English, glossary mismatches, punctuation style,
missing Tannoy/speaker prefixes, likely UI overflow, markup in the English unknown to the token check.
Info: characters outside the static NotoSans_JP atlas (rendered by the dynamic fallback).

usage: python tools/qa.py [--strict]   (writes work/qa_report.txt)
       python tools/qa.py --batch work/batches/<name>.ja.json   (checks a batch before import; prints everything)
"""
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tpcloc import paths, text, translations  # noqa: E402

ALLOWED_ASCII = {"DJ", "VR", "XP", "PC", "UI", "OK", "ID", "TV", "MOD", "MODs", "mod", "io", "Steam", "PlayStation",
                 "Xbox", "Nintendo", "Switch", "DLC", "CD", "DVD", "VHS", "CEO", "SNS", "GPS", "AI", "UFO", "NPC",
                 "HP", "BGM", "SF", "II", "III", "IV", "Mk", "DNA", "pH", "PA", "LED", "TPU", "BC", "AD", "No",
                 "vs", "Ltd", "Inc", "Bros", "TM", "ZZZ", "Zzz", "www", "com", "twopointcampus", "BodyBold", "Tannoy",
                 "Jingle", "UI_General_All_T_TMP_Objectives", "name", "style", "sprite", "color", "br", "Discord",
                 "SECRET", "ABC", "GAME", "OVER", "WiFi", "Wi", "Fi", "USB", "RPG", "FPS", "SCI",
                 "DMCA", "https", "http", "twopointcampus"}
SPEAKER_RE = re.compile(r"^(?:<[^>]+>)*([A-Z][A-Za-z .'\-]{1,40}): ")
MARKUP_RE = re.compile(r"<[^<>]+>|\{[^{}]+\}")  # tag/parameter-looking text (checked against text.TOKEN_RE)
CURATED_TERMS = ["Kudosh", "Janitor", "Assistant", "Teacher", "Two Point County", "Pastoral Care", "Medical Office"]


def load_batch(path: Path) -> dict:
    """A batch file {"<source>|<key>": "<ja>" or {"ja", "note"}} in the shape of translations.load_all()."""
    ours = {}
    for ref, val in json.loads(path.read_text("utf-8")).items():
        source, _, key = ref.partition("|")
        ja = val["ja"] if isinstance(val, dict) else val
        note = val.get("note", "") if isinstance(val, dict) else ""
        ours.setdefault(source, {})[key] = {"ja": ja.replace("↵", "\n"), "status": "draft", "note": note}
    return ours


def display_width(s: str) -> float:
    """Rough rendered width: a CJK glyph is about 1.7 average Latin glyphs in the game fonts."""
    s = text.TOKEN_RE.sub("", s)
    return sum(1.7 if ord(c) > 0x2E7F else 1 for c in s)


def main():
    strict = "--strict" in sys.argv
    batch = Path(sys.argv[sys.argv.index("--batch") + 1]) if "--batch" in sys.argv else None
    if batch:
        sys.stdout.reconfigure(encoding="utf-8")
    data = json.loads((paths.WORK_DIR / "extract" / "strings.json").read_text("utf-8"))
    worklist = json.loads((paths.WORK_DIR / "worklist.json").read_text("utf-8"))
    need = {(w["source"], w["key"]): w for w in worklist}
    ours = load_batch(batch) if batch else translations.load_all()
    static_chars = set((paths.WORK_DIR / "notosans_jp_static_charset.txt").read_text("utf-8")) \
        if (paths.WORK_DIR / "notosans_jp_static_charset.txt").exists() else set()

    official = translations.load_glossary_official()
    terms = {en: ja for en, ja in official.items()
             if (len(en.split()) >= 2 and all(w[:1].isupper() for w in en.split() if w.isalpha()))
             or en in CURATED_TERMS}
    for row in translations.load_glossary_new():
        if len(row["en"].split()) >= 2:  # single words are too generic to enforce
            terms[row["en"]] = row["ja"]
    term_res = [(en, ja, re.compile(r"(?<![A-Za-z])" + re.escape(en) + r"(?![A-Za-z])")) for en, ja in terms.items() if ja]

    errors, warnings, info = [], [], []
    outside_chars = collections.Counter()
    done = set()
    for source, entries in ours.items():
        src = data["sources"].get(source)
        if not src:
            errors.append(f"{source}: unknown source file")
            continue
        en_map = src["langs"]["English"]
        for key, e in entries.items():
            where = f"{source} | {key}"
            if key not in en_map:
                errors.append(f"{where}: key not in game data")
                continue
            ja = e.get("ja", "")
            en = en_map[key]
            if e.get("status") not in translations.STATUSES:
                errors.append(f"{where}: bad status {e.get('status')!r}")
            if not ja.strip():
                errors.append(f"{where}: empty translation")
                continue
            done.add((source, key))
            if (source, key) not in need:
                warnings.append(f"{where}: not in worklist (official translation exists or text is unused)")
            if text.tokens(en) != text.tokens(ja):
                msg = f"{where}: tokens differ EN={text.tokens(en)} JA={text.tokens(ja)}"
                # "[token-fix]" in the note marks a deliberate fix of a broken placeholder in the English.
                (warnings if "[token-fix]" in e.get("note", "") else errors).append(msg)
            unknown = MARKUP_RE.findall(text.TOKEN_RE.sub("", en))
            if unknown:  # new markup after a game update: add it to text.TOKEN_RE so it is checked
                warnings.append(f"{where}: markup in EN not covered by the token check: {unknown[:6]}")
            if en.count("[i2p_One]") and ja.count("[i2p_One]") != en.count("[i2p_One]"):
                errors.append(f"{where}: plural tag count differs")
            if "[i2t]" in ja:  # the I2 cache term separator would split the game's term table
                errors.append(f"{where}: contains the term separator [i2t]")
            if "\0" in ja:  # the patcher rejects it (a TextAsset of the game may not contain U+0000)
                errors.append(f"{where}: contains U+0000 (NUL)")
            if re.search(r"_x[0-9A-Fa-f]{4}_", ja) and not re.search(r"_x[0-9A-Fa-f]{4}_", en):
                errors.append(f"{where}: contains an Excel escape like _x000D_ (re-import the review sheet)")
            if en.count("\\n") != ja.count("\\n"):
                errors.append(f"{where}: literal \\n count differs ({en.count(chr(92) + 'n')}->{ja.count(chr(92) + 'n')})")
            if en.count("\n") != ja.count("\n") or en.count("<br>") != ja.count("<br>"):
                warnings.append(f"{where}: line breaks differ (LF {en.count(chr(10))}->{ja.count(chr(10))}, <br> {en.count('<br>')}->{ja.count('<br>')})")
            plain = text.TOKEN_RE.sub(" ", ja)
            leftovers = [w for w in re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", plain) if w not in ALLOWED_ASCII]
            if leftovers:
                warnings.append(f"{where}: English left in JA: {leftovers[:6]}")
            if re.search(r"\.\.\.|[!?](?![A-Za-z])", plain) or re.search(r"[ぁ-んァ-ヶ一-龠][,.](?!\d)", plain):
                warnings.append(f"{where}: half-width punctuation (use … ！ ？ 、 。)")
            if en.startswith('<style="Tannoy">PA System:') and not ja.startswith('<style="Tannoy">構内放送：'):
                warnings.append(f"{where}: Tannoy line should start with 構内放送：")
            m = SPEAKER_RE.match(en)
            if m and source in ("I2LS_VO_Radio",) and "：" not in ja.split("\n")[0][:20]:
                warnings.append(f"{where}: speaker prefix '{m.group(1)}:' should become 'カタカナ名：'")
            for term_en, term_ja, rx in term_res:
                if rx.search(en) and term_ja not in ja:
                    warnings.append(f"{where}: glossary '{term_en}' -> '{term_ja}' not used")
            kind = need.get((source, key), {}).get("kind")
            if kind in ("label", "name", "title") and display_width(ja) > max(14, display_width(en) * 1.6):
                info.append(f"{where}: check UI fit (EN width {display_width(en):.0f}, JA width {display_width(ja):.0f})")
            for ch in ja:
                if static_chars and ord(ch) > 0x7F and ch not in static_chars:
                    outside_chars[ch] += 1

    if batch:
        print(f"{batch.name}: {len(done)} entries  errors {len(errors)}  warnings {len(warnings)}")
        for title, items in (("ERROR", errors), ("WARNING", warnings), ("INFO", info)):
            for i in items:
                print(title, i)
        sys.exit(1 if errors else 0)
    missing = [k for k in need if k not in done]
    by_cat = collections.Counter(need[k]["category"] for k in missing)
    if outside_chars:
        info.append(f"{len(outside_chars)} characters outside the static NotoSans_JP atlas (dynamic fallback needed): "
                    + "".join(c for c, _ in outside_chars.most_common()))
    report = paths.WORK_DIR / "qa_report.txt"
    with open(report, "w", encoding="utf-8") as fp:
        fp.write(f"translated {len(done)} / {len(need)}; missing by category: {dict(by_cat)}\n\n")
        for title, items in (("ERRORS", errors), ("WARNINGS", warnings), ("INFO", info)):
            fp.write(f"== {title} ({len(items)})\n")
            fp.writelines(i + "\n" for i in items)
            fp.write("\n")
    print(f"translated {len(done)} / {len(need)}  errors {len(errors)}  warnings {len(warnings)}  -> {report}")
    if missing:
        print("missing by category:", dict(by_cat.most_common()))
    for e in errors[:20]:
        print("ERROR", e)
    if errors or (strict and warnings):
        sys.exit(1)


if __name__ == "__main__":
    main()
