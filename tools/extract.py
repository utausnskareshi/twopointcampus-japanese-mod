"""Extract every language's I2 cache text from the installed game into work/extract/.

Reads the Japanese data from the patch's backup (<bundle>.tpcjp-original) while the installed bundle
is our patched output or identical to the backup, and also when it is missing, cannot be read or was
modified under the same game version and the state file vouches for the backup (it records the installed
game version as the backup's); after a game update the installed bundle is used and the stale backup is
ignored (see tpcloc/official.py), so the extract always reflects the current official data. It stops when
no file can be trusted (e.g. a damaged or patched backup, or a bundle format TpcJpPatch.exe does not read).
The game version is read from version.txt like TpcJpPatch.exe reads it (tpcloc/paths.game_version); it stops
before writing anything when version.txt cannot be read or is empty.
The official Japanese bundle it read is also kept in work/backup/official/ (the tests and the sandbox test tools
start from that copy).

usage: python tools/extract.py
"""
import json
import os
import shutil
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from tpcloc import bundles, i2cache, official, paths  # noqa: E402


def main():
    game = paths.find_game_dir()
    version = paths.game_version(game)
    if version in ("", paths.UNKNOWN_VERSION):
        # the patch data's #game_version would be meaningless (TpcJpPatch.exe never takes it for the installed version)
        sys.exit(f"error: the game version cannot be read from {game / 'version.txt'} (missing, empty or unreadable); "
                 "verify the game files in Steam")
    out_dir = paths.WORK_DIR / "extract"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"game_dir": str(game), "game_version": version, "bundles": {}, "sources": {}}
    official_ja = None
    for lang in paths.BUNDLE_LANGS:
        bundle = paths.loc_bundle(game, lang)
        if lang == "Japanese":
            try:
                bundle, reason = official.official_japanese(bundle)
            except official.OfficialBundleError as e:
                sys.exit(f"error: {e}")
            print(reason)
            official_ja = bundle
        result["bundles"][lang] = {"file": bundle.name, "sha256": bundles.sha256(bundle)}
        for source, text in bundles.read_texts(bundle).items():
            src = result["sources"].setdefault(source, {"keys": [], "langs": {}})
            pairs = i2cache.parse(text)
            if lang == "English":
                src["keys"] = [k for k, _ in pairs]
            src["langs"][lang] = dict(pairs)
        print(f"{lang}: {bundle.name}")
    check_japanese_keys(result["sources"])
    copy = stage_official_copy(official_ja, result["bundles"]["Japanese"]["sha256"],
                               paths.loc_bundle(game, "Japanese").name)
    path = out_dir / "strings.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    total = sum(len(s["keys"]) for s in result["sources"].values())
    print(f"wrote {path} ({len(result['sources'])} sources, {total} keys, game {result['game_version']})")
    if copy:
        os.replace(*copy)
        print(f"official Japanese bundle -> {copy[1]}")
    write_static_charset(game)


def stage_official_copy(bundle, sha256: str, name: str):
    """Copy the official Japanese bundle to work/backup/official/<name>.tmp and check that it is the file that was
    extracted; returns (tmp, final path), or None when the copy there is already that file."""
    dst = paths.WORK_DIR / "backup" / "official" / name
    if dst.is_file() and bundles.sha256(dst) == sha256:
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(name + ".tmp")
    shutil.copyfile(bundle, tmp)
    if bundles.sha256(tmp) != sha256:
        tmp.unlink()
        sys.exit(f"error: {bundle} changed while it was read; run extract.py again")
    return tmp, dst


def check_japanese_keys(sources: dict) -> None:
    """Warn when the Japanese terms differ from the English ones (mixed game versions?)."""
    for name, src in sources.items():
        ja = src["langs"].get("Japanese")
        if ja is None:
            print(f"WARNING: {name}: no Japanese data")
            continue
        missing, extra = set(src["keys"]) - set(ja), set(ja) - set(src["keys"])
        if missing or extra:
            print(f"WARNING: {name}: {len(missing)} English terms missing from Japanese, {len(extra)} extra "
                  f"(e.g. {sorted(missing or extra)[:3]}); the Japanese data does not match this game version")


def write_static_charset(game):
    """Characters baked into the static NotoSans_JP atlases (anything else needs the dynamic fallback)."""
    import UnityPy

    env = UnityPy.load(str(paths.bundles_dir(game) / "fonts_assets_all.bundle"))
    chars = set()
    for obj in env.objects:
        if obj.type.name == "MonoBehaviour" and obj.peek_name() in ("NotoSans_JP", "NotoSans_JP_LanguageNames"):
            chars.update(chr(c["m_Unicode"]) for c in obj.read_typetree().get("m_CharacterTable", []))
    out = paths.WORK_DIR / "notosans_jp_static_charset.txt"
    out.write_text("".join(sorted(chars)), "utf-8")
    print(f"static NotoSans_JP characters: {len(chars)} -> {out}")


if __name__ == "__main__":
    main()
