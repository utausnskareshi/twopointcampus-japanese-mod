"""Check the installed Japanese bundle against the official backup and the built patch data.

The backup must be the official data: it has to be the file tpcloc/official.py trusts without a warning,
match original_sha256 (and the bundle patched_sha256) in the state file and hold none of our translations.
Its SHA-256 is also compared with work/extract/strings.json (a warning only, e.g. after a game update).
Every entry of dist/data/TpcJpPatch.data.tsv is checked the way the patcher fills it: our text (the
data's or translations/ja's, as dev_patch.py writes; for a reference line, the referenced official
text) where the official value still has the entry's base hash, otherwise the official value is kept.
Every other term and every untouched source must be identical to the official data, in the same order.

usage: python tools/verify_bundle.py [--data dist/data/TpcJpPatch.data.tsv]
"""
import hashlib
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tpcloc import bundles, i2cache, official, paths, text, translations  # noqa: E402

DATA = paths.PROJECT_ROOT / "dist" / "data" / "TpcJpPatch.data.tsv"


def unesc(s: str) -> str:
    """Undo the TSV escapes (backslash, tab, CR, LF) like PatchData.Unescape."""
    res, i = [], 0
    while i < len(s):
        if s[i] == "\\":
            i += 1
            res.append({"\\": "\\", "n": "\n", "r": "\r", "t": "\t"}[s[i]])
        else:
            res.append(s[i])
        i += 1
    return "".join(res)


def load_data(path: Path) -> dict[str, dict[str, tuple[str, str | None, tuple[str, str] | None]]]:
    """{source: {key: (base, ja, (ref_source, ref_key) or None)}} from the built patch data."""
    lines = path.read_text("utf-8").split("\n")
    if not lines[0].rstrip("\r").startswith("#TPCJP-PATCH\t1"):
        sys.exit(f"{path}: not a patch data file")
    out = {}
    for line in lines[1:]:
        line = line.rstrip("\r")
        if not line or line.startswith("#"):
            continue
        f = line.split("\t")
        if line.startswith("&"):
            out.setdefault(f[0][1:], {})[unesc(f[1])] = (f[2], None, (f[3], unesc(f[4])))
        else:
            out.setdefault(f[0], {})[unesc(f[1])] = (f[2], unesc(f[3]), None)
    return out


def base_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def check_baseline(game: Path, bundle: Path) -> tuple[Path, list[str]]:
    """Pick the official baseline and return it with the reasons it cannot be trusted (empty = ok)."""
    backup = official.backup_path(bundle)
    errors = []
    if backup.exists():
        try:
            baseline, reason = official.official_japanese(bundle)
        except official.OfficialBundleError as e:
            return backup, [str(e)]
        if baseline != backup or reason.startswith("WARNING"):
            # a stale backup, or an installed bundle that is unreadable or changed while the game version is unchanged
            return baseline, [f"{reason.removeprefix('WARNING: ')}; the installed bundle is not our patched output"]
    else:
        baseline = paths.WORK_DIR / "backup" / "official" / bundle.name
        reason = f"no backup in the game folder; comparing with {baseline}"
    print("official data:", reason)
    base_sha, cur_sha = bundles.sha256(baseline), bundles.sha256(bundle)
    extract = json.loads((paths.WORK_DIR / "extract" / "strings.json").read_text("utf-8"))
    if base_sha != extract["bundles"]["Japanese"]["sha256"]:
        version = paths.game_version(game)  # like TpcJpPatch.exe; never raises
        if version in ("", paths.UNKNOWN_VERSION):
            hint = "it is not the extracted official bundle; version.txt is missing, empty or unreadable"
        elif extract["game_version"] != version:
            hint = "the game was updated since the extract: re-run tools/extract.py and tools/worklist.py"
        else:
            hint = "it is not the extracted official bundle"
        print(f"WARNING: {baseline.name} sha256 differs from work/extract/strings.json ({hint})")
    state = official.read_state(bundle)
    if state is not None:
        if state.get("original_sha256") != base_sha:
            errors.append(f"original_sha256 in {official.state_path(bundle).name} does not match {baseline.name}")
        if state.get("patched_sha256") != cur_sha:
            errors.append(f"patched_sha256 in {official.state_path(bundle).name} does not match the installed bundle "
                          "(stale state file)")
    patched, n = official.is_patched(baseline)
    if patched:
        errors.append(f"{baseline.name} already holds {n} of our translations (patched data, not official)")
    return baseline, errors


def main():
    warnings.filterwarnings("ignore")
    if "--status" in sys.argv:
        print("note: --status is ignored; the expected entries come from the built patch data")
    data_path = Path(sys.argv[sys.argv.index("--data") + 1]) if "--data" in sys.argv else DATA
    game = paths.find_game_dir()
    bundle = paths.loc_bundle(game, "Japanese")
    if not bundle.exists():
        sys.exit(f"{bundle} is missing; nothing compared (verify the game files in Steam)")
    baseline, errors = check_baseline(game, bundle)
    for e in errors:
        print("ERROR", e)
    if errors:
        sys.exit("the official baseline cannot be trusted; nothing compared")
    official_texts = bundles.read_texts(baseline)
    try:
        patched = bundles.read_texts(bundle)
    except Exception as e:  # damaged data without a backup (with one, tpcloc/official.py refuses it above)
        sys.exit(f"{bundle.name} cannot be read ({type(e).__name__}: {e}); nothing compared "
                 "(verify the game files in Steam)")
    data = load_data(data_path)
    ours = translations.load_all()
    if set(official_texts) != set(patched):
        sys.exit("TextAsset names differ")
    last = {name: i2cache.to_dict(t) for name, t in official_texts.items()}
    applied, wrong, untouched, changed = 0, [], 0, []
    kept, source_changed, missing, stale = 0, 0, 0, []
    for name, entries in data.items():
        for key, (_, ja, ref) in entries.items():
            if key not in last.get(name, {}):
                missing += 1
            elif ref is None and ours.get(name, {}).get(key, {}).get("ja") != ja:
                stale.append(f"{name}|{key}")
    for name, t in official_texts.items():
        po, pn = i2cache.parse(t), i2cache.parse(patched[name])
        if [k for k, _ in po] != [k for k, _ in pn]:
            sys.exit(f"{name}: term order differs")
        entries = data.get(name, {})
        for (key, vo), (_, vn) in zip(po, pn):
            entry = entries.get(key)
            if entry is None:
                if vn == vo:
                    untouched += 1
                else:
                    changed.append(f"{name}|{key}")
                continue
            base, ja, ref = entry
            mine = ours.get(name, {}).get(key, {}).get("ja") if ref is None else None
            if ref is not None:
                # Same English as an officially translated term: the patcher copies that term's text.
                ja = last.get(ref[0], {}).get(ref[1])
                if ja is None or not text.has_japanese(ja):
                    missing += 1
                    ja = None
            # Fill-only: the patcher replaces the term only while its (last) official value has the base hash.
            if ja is not None and base_hash(last[name][key]) == base:
                if vn == ja or (mine and vn == mine):  # data text, or translations/ja (dev_patch.py)
                    applied += 1
                else:
                    wrong.append(f"{name}|{key}")
            elif vn != vo:
                changed.append(f"{name}|{key}")
            elif ja is not None and text.has_japanese(vo):
                kept += 1
            elif ja is not None:
                source_changed += 1
    print(f"translated occurrences OK: {applied}, not applied: {len(wrong)}, "
          f"official unchanged: {untouched}, unexpected changes: {len(changed)}")
    print(f"official kept (official Japanese changed since the build): {kept}, "
          f"source changed (rebuild the patch): {source_changed}, missing in game data: {missing}")
    if stale:
        print(f"WARNING: {len(stale)} entries in {data_path.name} differ from translations/ja "
              "(rebuild the patch data; either text is accepted)")
    for w in wrong[:20]:
        print("NOT APPLIED", w)
    for c in changed[:20]:
        print("CHANGED", c)
    for s in stale[:20]:
        print("DATA DIFFERS", s)
    sys.exit(1 if wrong or changed else 0)


if __name__ == "__main__":
    main()
