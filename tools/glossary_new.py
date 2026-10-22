"""Rebuild translations/glossary_new.tsv from our translated names/titles.

Picks short name-like entries (worklist kind name/title/label/person_name, at most 5 words,
no sentence punctuation) so later batches and QA see the Japanese chosen for new terms.
Conflicting translations of the same English term are reported.

usage: python tools/glossary_new.py
"""
import collections
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tpcloc import paths, text, translations  # noqa: E402

# Generic words that would create noisy glossary hits.
STOP = {"Doctor", "Nurse", "Patient", "Ghost", "Ward", "Surgery", "Psychiatry", "Item", "Room", "Rooms",
        "Unused", "Generic", "Other", "Search", "Back", "Yes", "No", "Next", "Done", "Error", "Cancel", "Play",
        "Quit", "Report", "Submit", "Type", "Rating", "Queue", "Settings", "Email", "Floor", "Wall", "Rug",
        "Mods", "Collection", "Popularity", "Picture", "Installed", "Installing", "Updating", "Updated"}


def main():
    worklist = json.loads((paths.WORK_DIR / "worklist.json").read_text("utf-8"))
    data = json.loads((paths.WORK_DIR / "extract" / "strings.json").read_text("utf-8"))
    kinds = {(w["source"], w["key"]): w["kind"] for w in worklist}
    ours = translations.load_all()
    terms = collections.defaultdict(collections.Counter)
    for source, entries in ours.items():
        en_map = data["sources"][source]["langs"]["English"]
        for key, e in entries.items():
            # Random student names (person_name) are not terminology.
            if kinds.get((source, key)) not in ("name", "title", "label"):
                continue
            en = en_map.get(key, "").strip()
            words = text.WORD_RE.findall(en)
            if not (1 <= len(words) <= 5) or en in STOP or len(en) < 4:
                continue
            if any(ch in en for ch in ".!?:{<\n") or text.TOKEN_RE.search(en):
                continue
            terms[en][e["ja"].strip()] += 1
    out = translations.GLOSSARY_NEW
    conflicts = 0
    with open(out, "w", encoding="utf-8", newline="") as fp:
        w = csv.writer(fp, delimiter="\t", lineterminator="\n")
        w.writerow(["en", "ja", "variants"])
        for en, c in sorted(terms.items(), key=lambda x: x[0].lower()):
            (ja, _), *rest = c.most_common()
            if rest:
                conflicts += 1
            w.writerow([en, ja, " | ".join(j for j, _ in rest)])
    print(f"glossary_new: {len(terms)} terms ({conflicts} with variant translations) -> {out}")


if __name__ == "__main__":
    main()
