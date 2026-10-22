"""Export worklist entries for translation and import translated batches.

In listings a real line break is shown as "↵"; a literal backslash-n in the game text stays "\\n".
Batch JSON values use normal JSON escapes ("\\n" in JSON = line break, "\\\\n" = literal \\n).

export: writes a compact listing (id, key, EN, refs, glossary hits) to work/batches/<name>.txt
  python tools/batch.py export <name> [--source S] [--category C] [--kind K] [--prefix P]
                               [--untranslated] [--limit N] [--offset N] [--refs]
import: merges a JSON batch {"<source>|<key>": "<ja>" or {"ja":..., "note":...}} into
        translations/ja/<source>.json as status "draft" (existing reviewed/edited entries are kept;
        a replaced entry keeps its reviewer memo "review")
  python tools/batch.py import work/batches/<name>.ja.json [--overwrite]
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


def glossary_hits(en: str, terms: list) -> list[str]:
    hits = []
    for term_en, term_ja, rx in terms:
        if rx.search(en):
            hits.append(f"{term_en}={term_ja}")
    return hits[:8]


def export(name: str):
    worklist = json.loads((paths.WORK_DIR / "worklist.json").read_text("utf-8"))
    ours = translations.load_all()
    official = translations.load_glossary_official()
    terms = {en: ja for en, ja in official.items()
             if len(en) >= 4 and (len(en.split()) >= 2 or en[:1].isupper())}
    for row in translations.load_glossary_new():
        terms[row["en"]] = row["ja"]
    term_res = [(en, ja, re.compile(r"(?<![A-Za-z])" + re.escape(en) + r"(?![A-Za-z])"))
                for en, ja in sorted(terms.items(), key=lambda x: -len(x[0])) if ja]
    data = json.loads((paths.WORK_DIR / "extract" / "strings.json").read_text("utf-8"))
    sel = []
    for w in worklist:
        if opt("--source") and w["source"] not in opt("--source").split(","):
            continue
        if opt("--category") and w["category"] not in opt("--category").split(","):
            continue
        if opt("--kind") and w["kind"] not in opt("--kind").split(","):
            continue
        if opt("--prefix") and not w["key"].startswith(opt("--prefix")):
            continue
        if "--untranslated" in sys.argv and w["key"] in ours.get(w["source"], {}):
            continue
        sel.append(w)
    off = int(opt("--offset", 0))
    lim = int(opt("--limit", 100000))
    sel = sel[off:off + lim]
    lines = []
    for w in sel:
        lines.append(f"### {w['source']}|{w['key']}  [{w['category']}/{w['kind']}]")
        lines.append("EN: " + w["en"].replace("\n", "↵"))
        if "--refs" in sys.argv and w.get("zh"):
            lines.append("ZH: " + w["zh"].replace("\n", "↵"))
        if "--context" in sys.argv:
            en_map = data["sources"][w["source"]]["langs"]["English"]
            ja_map = data["sources"][w["source"]]["langs"]["Japanese"]
            for a, b in (("_Name", "_Description"), ("Name", "Description"), ("_Title", "_Text"), ("Title", "Text")):
                if w["key"].endswith(a):
                    sib = w["key"][: -len(a)] + b
                    if sib in en_map:
                        ctx = en_map[sib].replace("\n", "↵")
                        lines.append("CTX: " + (ctx[:160] + "…" if len(ctx) > 160 else ctx))
                        if text.has_japanese(ja_map.get(sib, "")):
                            lines.append("CTX-JA: " + ja_map[sib].replace("\n", "↵")[:120])
                    break
        hits = glossary_hits(w["en"], term_res)
        if hits:
            lines.append("GL: " + "; ".join(hits))
    out = paths.WORK_DIR / "batches" / f"{name}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", "utf-8")
    words = sum(len(text.WORD_RE.findall(w["en"])) for w in sel)
    print(f"exported {len(sel)} entries ({words} words) -> {out}")


def import_batch(path: Path):
    batch = json.loads(path.read_text("utf-8"))
    data = json.loads((paths.WORK_DIR / "extract" / "strings.json").read_text("utf-8"))
    ours = translations.load_all()
    overwrite = "--overwrite" in sys.argv
    changed, skipped, problems = {}, 0, []
    for ref, val in batch.items():
        source, _, key = ref.partition("|")
        src = data["sources"].get(source)
        if not src or key not in src["langs"]["English"]:
            problems.append(f"unknown term {ref}")
            continue
        ja = val["ja"] if isinstance(val, dict) else val
        note = val.get("note", "") if isinstance(val, dict) else ""
        ja = ja.replace("↵", "\n")
        en = src["langs"]["English"][key]
        if text.tokens(en) != text.tokens(ja):
            problems.append(f"tokens differ {ref}: EN={text.tokens(en)} JA={text.tokens(ja)}")
        entries = ours.setdefault(source, {})
        old = entries.get(key)
        if old and old.get("status") in ("reviewed", "edited") and not overwrite:
            skipped += 1
            continue
        # keep the reviewer memo ("review") and any other extra fields of the entry being replaced
        extra = {k: v for k, v in (old or {}).items() if k not in ("ja", "status", "note")}
        entries[key] = {"ja": ja, "status": "draft", "note": note or (old or {}).get("note", ""), **extra}
        changed.setdefault(source, 0)
        changed[source] += 1
    for source, n in changed.items():
        translations.save(source, ours[source], data["sources"][source]["keys"])
    print(f"imported {sum(changed.values())} ({changed}), skipped reviewed {skipped}")
    for p in problems:
        print("PROBLEM", p)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    if sys.argv[1] == "export":
        export(sys.argv[2])
    elif sys.argv[1] == "import":
        import_batch(Path(sys.argv[2]))
    else:
        sys.exit(__doc__)
