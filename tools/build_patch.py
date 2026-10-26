"""Build the patcher's translation data (dist/data/TpcJpPatch.data.tsv) from translations/ja/*.json.

Only our own translations are written. Each line carries the hash of the official Japanese value it
replaces, so the patcher fills in missing text only and never overwrites a translation that a game
update has added. A translation identical to an official translation of another term is written as a
reference to that term (the patcher copies it from the game's own data), so no official text is
redistributed; inline translations nearly identical to an official text are reported as warnings.
Entries whose current official value no longer needs translation (stale worklist.json) are skipped.
The header values end up in the patcher's state file: --version may hold letters, digits and . + _ -
only, and a game version with control characters or a bundle hash that is not 64 hex digits (both from
work/extract/strings.json) stops the build.

usage: python tools/build_patch.py [--version 1.0.1] [--status draft,reviewed,edited] [--out FILE]
"""
import bisect
import collections
import datetime
import difflib
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tpcloc import paths, text, translations  # noqa: E402

DEFAULT_OUT = paths.PROJECT_ROOT / "dist" / "data" / "TpcJpPatch.data.tsv"
NEAR_COPY_RATIO = 0.9  # warn when an inline translation is this similar to an official text of another term
NEAR_COPY_MIN_LEN = 30  # ...and at least this long (shorter ones are names and template phrases)
SPEAKER_JA_RE = re.compile(r"^[^：\n]{1,20}：")
VERSION_RE = re.compile(r"[0-9A-Za-z.+_-]+")  # --version: the patcher shows it and records it in its state file
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def opt(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name) + 1
        if i >= len(sys.argv) or sys.argv[i].startswith("--"):
            sys.exit(f"{name} needs a value")
        return sys.argv[i]
    return default


def parse_statuses(arg: str) -> set[str]:
    """Comma-separated status names; spaces around a name are ignored, unknown or empty names are an error."""
    names = {s.strip() for s in arg.split(",")}
    bad = sorted(names - set(translations.STATUSES))
    if bad:
        sys.exit(f"unknown --status value(s) {bad}; valid: {','.join(translations.STATUSES)} "
                 "(comma-separated; quote the value if it contains spaces)")
    return names


def base_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


def has_control(s: str) -> bool:
    """Control characters and line/paragraph separators: they would end or split a line of the data, or of the
    patcher's state file (the patcher unescapes header values and records the version there)."""
    return any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in s)


def plain(s: str) -> str:
    """Text without tags, line breaks, whitespace and a leading 'Speaker：' prefix (for the near-copy check)."""
    s = re.sub(r"<[^<>]*>", "", s).replace("\\n", "\n").strip()
    return re.sub(r"\s+", "", SPEAKER_JA_RE.sub("", s))


def near_copies(inline: list[tuple[str, str, str]], official_index: dict) -> list[str]:
    """Inline translations that are nearly identical to an official Japanese text of another term.

    Such text is redistributed in the exe, although official text should only be referenced.
    """
    cands = {}
    for ja, ref in official_index.items():
        cands.setdefault(plain(ja), ref)
    cands = sorted(cands.items(), key=lambda kv: len(kv[0]))
    lens = [len(c) for c, _ in cands]
    r = NEAR_COPY_RATIO
    found = []
    for source, key, ja in inline:
        p = plain(ja)
        if len(p) < NEAR_COPY_MIN_LEN:
            continue
        sm = difflib.SequenceMatcher(None, autojunk=False)
        sm.set_seq2(p)
        best, best_ref = r, None
        # ratio = 2*matches/(a+b) can exceed r only for lengths inside this window
        window = cands[bisect.bisect_left(lens, len(p) * r / (2 - r)):bisect.bisect_right(lens, len(p) * (2 - r) / r)]
        for c, ref in window:
            sm.set_seq1(c)
            if sm.real_quick_ratio() > best and sm.quick_ratio() > best and sm.ratio() > best:
                best, best_ref = sm.ratio(), ref
        if best_ref:
            found.append(f"{source}|{key}: nearly identical ({best:.2f}) to official {best_ref[0]}|{best_ref[1]}")
    return found


def main():
    version = opt("--version", "1.0.1")  # the current release (<Version> in patcher/TpcJpPatch/TpcJpPatch.csproj)
    if not VERSION_RE.fullmatch(version):
        sys.exit(f"--version {version!r} is not valid: use letters, digits and . + _ - only")
    statuses = parse_statuses(opt("--status", ",".join(translations.STATUSES)))
    out = Path(opt("--out", DEFAULT_OUT))
    data = json.loads((paths.WORK_DIR / "extract" / "strings.json").read_text("utf-8"))
    worklist = json.loads((paths.WORK_DIR / "worklist.json").read_text("utf-8"))
    need = {(w["source"], w["key"]): w for w in worklist}
    ours = translations.load_all()

    # official Japanese text -> first term that has it (for references)
    official_index = {}
    for source, src in data["sources"].items():
        for key, ja in src["langs"].get("Japanese", {}).items():
            if text.has_japanese(ja) and (source, key) not in need:
                official_index.setdefault(ja, (source, key))

    lines, refs, problems, stale, inline = [], 0, [], [], []
    available, written = collections.Counter(), collections.Counter()
    for source, src in data["sources"].items():
        entries = ours.get(source, {})
        ja_map = src["langs"].get("Japanese", {})
        en_map = src["langs"]["English"]
        for key in dict.fromkeys(src["keys"]):
            e = entries.get(key)
            if not e or not e.get("ja", "").strip():
                continue
            available[e.get("status")] += 1
            if e.get("status") not in statuses:
                continue
            if (source, key) not in need:
                problems.append(f"{source}|{key}: not in worklist (official translation exists); skipped")
                continue
            if key not in ja_map:
                sys.exit(f"{source}|{key} is missing from the Japanese data in work/extract/strings.json and the "
                         "patcher cannot add terms. The extract probably mixes game versions: re-run tools/extract.py")
            if not text.needs_translation(en_map[key], ja_map[key]):
                # worklist.json is older than strings.json; hashing this official value as the base
                # would let the patcher overwrite the official translation
                stale.append(f"{source}|{key}: official value no longer needs translation; skipped")
                continue
            ja = e["ja"]
            if text.tokens(en_map[key]) != text.tokens(ja) and "[token-fix]" not in e.get("note", ""):
                sys.exit(f"tokens differ in {source}|{key}; run tools/qa.py")
            if "[i2t]" in ja:  # the patcher rejects data whose text contains the I2 term separator
                sys.exit(f"{source}|{key} contains the term separator [i2t]; run tools/qa.py")
            if "\0" in ja:  # the patcher rejects data containing U+0000 (a TextAsset of the game may not contain it)
                sys.exit(f"{source}|{key} contains U+0000 (NUL); run tools/qa.py")
            base = base_hash(ja_map[key])
            ref = official_index.get(ja)
            if ref:
                lines.append(f"&{source}\t{esc(key)}\t{base}\t{ref[0]}\t{esc(ref[1])}")
                refs += 1
            else:
                lines.append(f"{source}\t{esc(key)}\t{base}\t{esc(ja)}")
                inline.append((source, key, ja))
            written[e.get("status")] += 1

    def current(source, key, lang):
        return data["sources"].get(source, {}).get("langs", {}).get(lang, {}).get(key, "")
    outdated = sum(1 for (source, key), w in need.items()
                   if (current(source, key, "English"), current(source, key, "Japanese")) != (w["en"], w["ja_official"]))
    print("by status (written/translated):",
          ", ".join(f"{s} {written[s]}/{available[s]}" for s in dict.fromkeys([*translations.STATUSES, *available])))
    if not lines:
        sys.exit(f"0 entries selected with --status {','.join(sorted(statuses))}; nothing written")

    header = [
        "#TPCJP-PATCH\t1",
        f"#patch_version\t{version}",
        f"#game_version\t{esc(data['game_version'])}",
        f"#bundle_sha256\t{data['bundles']['Japanese']['sha256']}",
        f"#built\t{datetime.datetime.now().isoformat(timespec='seconds')}",
        f"#entries\t{len(lines)}",
    ]
    nul = next((l for l in header + lines if "\0" in l), None)
    if nul is not None:  # the patcher rejects U+0000 anywhere in the data (keys and versions come from the extract)
        sys.exit(f"U+0000 (NUL) in the data to write: {nul[:80]!r}; work/extract/strings.json looks damaged: "
                 "re-run tools/extract.py")
    if has_control(data["game_version"]):
        sys.exit(f"control character in the game version {data['game_version']!r} of work/extract/strings.json; "
                 "it looks damaged: re-run tools/extract.py")
    if not SHA256_RE.fullmatch(data["bundles"]["Japanese"]["sha256"]):
        sys.exit(f"Japanese bundle sha256 {data['bundles']['Japanese']['sha256']!r} in work/extract/strings.json is "
                 "not 64 lower-case hex digits; it looks damaged: re-run tools/extract.py")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(header + lines) + "\n", "utf-8", newline="\n")
    for p in problems:
        print("NOTE", p)
    for p in stale:
        print("WARNING", p)
    if outdated or stale:
        print(f"WARNING work/worklist.json does not match work/extract/strings.json ({outdated} terms differ); "
              "run tools/worklist.py")
    for p in near_copies(inline, official_index):
        print("WARNING", p)
    print(f"{len(lines)} entries ({refs} references to official text), patch version {version} -> {out}")


if __name__ == "__main__":
    main()
