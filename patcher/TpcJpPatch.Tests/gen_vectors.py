"""Reference vectors for the C# tests (patcher/TpcJpPatch.Tests), computed with the project's Python code.

Writes only into --out (never into the project data):
  escape_vectors.tsv       esc(s) <TAB> base64(utf8 s) <TAB> base_hash(s)      (esc/base_hash from tools/build_patch.py)
  escape_patch.tsv         a patch data file of random keys/translations written like build_patch.py
                           (keys without '=' / '[i2t]', texts without '[i2t]', no U+0000, with the #entries count)
  escape_patch_expect.tsv  I|R <TAB> b64 key <TAB> base <TAB> b64 ja <TAB> b64 ref source <TAB> b64 ref key <TAB> noop
  official_texts.tsv       T <TAB> name <TAB> sha256(text) <TAB> pairs     (tpcloc.bundles.read_texts + i2cache.parse)
                           K <TAB> name <TAB> b64 key <TAB> sha256(last value) <TAB> occurrences
  patchdata_expect.tsv     the real dist/data/TpcJpPatch.data.tsv decoded by an independent Python reader:
                           H <TAB> name <TAB> b64 value / E <TAB> source <TAB> b64 key <TAB> base <TAB> b64 ja|- <TAB>
                           ref source|- <TAB> b64 ref key|- <TAB> noop  / S <TAB> count <TAB> refs <TAB> noops
  unitypy_<packer>.bundle  the official bundle with three texts changed, written by UnityPy: "lz4" by
                           tpcloc.bundles.write_texts (block info at the end, flag 0x80), the others by UnityPy's
                           packers (LZ4HC at the end, uncompressed, LZMA, the original flags 0x243, LZ4 at the end with
                           padding 0x200); the patcher must load them (genuine layouts it does not write itself).
                           "lzma_repetitive": every text repetitive, LZMA far beyond LZ4's 255:1 (review R9)
  unitypy_texts.tsv        packer <TAB> name <TAB> sha256(text)   (every TextAsset of each unitypy_*.bundle)

usage: python gen_vectors.py --out DIR [--seed N] [--count N]
"""
import base64
import hashlib
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import build_patch  # noqa: E402  (esc, base_hash)
from tpcloc import bundles, i2cache  # noqa: E402

OFFICIAL = ROOT / "work" / "backup" / "official" / "localisation_assets_localisationjapanese.bundle"
DATA = ROOT / "dist" / "data" / "TpcJpPatch.data.tsv"


def b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def sha_full(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


PIECES = [
    "\\", "\\\\", "\t", "\r", "\n", "\r\n", "\\n", "\\t", "\\r", "n", "t", "r", " ", "=", "[i2t]", "#", "&",
    "a", "Z", "0", "{SCORE}", "<b>", "</b>", "日本語", "ひらがな", "カタカナ", "漢字", "ｶﾀｶﾅ", "　", "、", "。",
    "😀", "𠮷", " ", "\u0085", "\x0b", "\x0c", "\x1c", "\x00", "é", "ß", "﻿", "\\\t", "\t\\", "\\\\n",
]


def rand_text(rng: random.Random, max_pieces: int) -> str:
    n = rng.randint(0, max_pieces)
    out = []
    for _ in range(n):
        r = rng.random()
        if r < 0.6:
            out.append(rng.choice(PIECES))
        elif r < 0.8:
            out.append(chr(rng.randint(0x3041, 0x30FF)))
        elif r < 0.9:
            out.append(chr(rng.randint(0x4E00, 0x9FFF)))
        else:
            out.append(chr(rng.randint(0x20, 0x7E)))
    return "".join(out)


def no_sep(s: str) -> str:
    """Removes the I2 term separator "[i2t]", also where a removal forms a new one."""
    while "[i2t]" in s:
        s = s.replace("[i2t]", "")
    return s


def unesc(s: str) -> str:
    """Independent reference decoder of build_patch.esc()."""
    res, i = [], 0
    table = {"\\": "\\", "n": "\n", "r": "\r", "t": "\t"}
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 1
            res.append(table[s[i]])
        else:
            res.append(c)
        i += 1
    return "".join(res)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    out = Path(sys.argv[sys.argv.index("--out") + 1])
    seed = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 20261021
    count = int(sys.argv[sys.argv.index("--count") + 1]) if "--count" in sys.argv else 3000
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    # 1. escape vectors
    vec = []
    fixed = ["", "\\", "\\\\", "\t", "\r", "\n", "\\n", "a\\tb", "\r\n", "日本語\\n改行", "末尾\\", "😀𠮷", "\x00"]
    for s in fixed + [rand_text(rng, 40) for _ in range(count)]:
        e = build_patch.esc(s)
        assert "\t" not in e and "\n" not in e and "\r" not in e
        assert unesc(e) == s
        vec.append(f"{e}\t{b64(s)}\t{build_patch.base_hash(s)}")
    (out / "escape_vectors.tsv").write_text("\n".join(vec) + "\n", "utf-8", newline="\n")

    # 2. a patch data file written the way build_patch.py writes lines
    lines = ["#TPCJP-PATCH\t1", "#patch_version\t9.9.9-test", "#game_version\tfuzz", "#bundle_sha256\t" + "0" * 64,
             "#note\t" + build_patch.esc("header\twith\\escapes\n"), None]  # None: the #entries line, filled in below
    expect = []
    seen = set()
    for i in range(1500):
        # the patcher rejects keys with '=' or the term separator and texts with the separator (FZ-07),
        # and U+0000 anywhere (round 3: nul_translation_blamed_on_game_file)
        key = no_sep(rand_text(rng, 12).replace("=", "").replace("\x00", "")) + f"#{i}"  # unique, never empty
        if key in seen:
            continue
        seen.add(key)
        ja = no_sep(rand_text(rng, 30).replace("\x00", ""))
        kind = rng.random()
        if kind < 0.15:
            ref_key = rand_text(rng, 8).replace("\x00", "") + "/ref"
            base = build_patch.base_hash(rand_text(rng, 10))
            lines.append(f"&I2LS_Fuzz\t{build_patch.esc(key)}\t{base}\tI2LS_Ref\t{build_patch.esc(ref_key)}")
            expect.append(f"R\t{b64(key)}\t{base}\t-\t{b64('I2LS_Ref')}\t{b64(ref_key)}\t0")
        else:
            noop = kind < 0.3
            base = build_patch.base_hash(ja if noop else ja + "x")
            lines.append(f"I2LS_Fuzz\t{build_patch.esc(key)}\t{base}\t{build_patch.esc(ja)}")
            expect.append(f"I\t{b64(key)}\t{base}\t{b64(ja)}\t-\t-\t{1 if noop else 0}")
    lines[lines.index(None)] = f"#entries\t{len(expect)}"  # like build_patch.py; the patcher checks the count (CS-02)
    (out / "escape_patch.tsv").write_text("\n".join(lines) + "\n", "utf-8", newline="\n")
    (out / "escape_patch_expect.tsv").write_text("\n".join(expect) + "\n", "utf-8", newline="\n")

    # 3. official texts as the Python tools read them
    texts = bundles.read_texts(OFFICIAL)
    rows = []
    for name, text in sorted(texts.items()):
        pairs = i2cache.parse(text)
        assert i2cache.dumps(pairs) == text
        rows.append(f"T\t{name}\t{sha_full(text)}\t{len(pairs)}")
        last, occ = {}, {}
        for k, v in pairs:
            last[k] = v
            occ[k] = occ.get(k, 0) + 1
        for k, v in last.items():
            rows.append(f"K\t{name}\t{b64(k)}\t{sha_full(v)}\t{occ[k]}")
    (out / "official_texts.tsv").write_text("\n".join(rows) + "\n", "utf-8", newline="\n")

    # 4. the shipped patch data, decoded independently
    rows = []
    n = refs = noops = 0
    raw = DATA.read_text("utf-8").split("\n")
    assert raw[0].rstrip("\r").startswith("#TPCJP-PATCH\t1")
    for line in raw[1:]:
        line = line.rstrip("\r")
        if not line:
            continue
        f = line.split("\t")
        if line.startswith("#"):
            if len(f) >= 2:
                rows.append(f"H\t{f[0][1:]}\t{b64(unesc(f[1]))}")
            continue
        n += 1
        if line.startswith("&"):
            assert len(f) == 5
            refs += 1
            rows.append(f"E\t{f[0][1:]}\t{b64(unesc(f[1]))}\t{f[2]}\t-\t{f[3]}\t{b64(unesc(f[4]))}\t0")
        else:
            assert len(f) == 4
            ja = unesc(f[3])
            noop = build_patch.base_hash(ja) == f[2]
            noops += noop
            rows.append(f"E\t{f[0]}\t{b64(unesc(f[1]))}\t{f[2]}\t{b64(ja)}\t-\t-\t{int(noop)}")
    rows.append(f"S\t{n}\t{refs}\t{noops}")
    (out / "patchdata_expect.tsv").write_text("\n".join(rows) + "\n", "utf-8", newline="\n")
    # 5. bundles written by UnityPy (own RNG: the vectors above stay the same)
    brng = random.Random(seed + 5)
    pieces = ["日本語", "😀", "𠮷", "a", "\\n", "\r\n", "[i2t]k=v"]
    changed = {name: texts[name] + "".join(brng.choice(pieces) for _ in range(brng.randint(1, 4000)))
               for name in ("I2LS_DLCs", "I2LS_UI", "I2LS_Advisor")}
    repetitive = {name: name + "\n" + "あ" * 20000 for name in texts}  # R9: LZMA compresses this far beyond 255:1
    rows = []
    packers = {"lz4": None, "lz4hc": "lz4hc", "none": "none", "lzma": "lzma", "original": "original", "lz4_padded": (0x2C2, 2),
               "lzma_repetitive": "lzma"}
    for label, packer in packers.items():
        dst = out / f"unitypy_{label}.bundle"
        new_texts = repetitive if label == "lzma_repetitive" else changed
        want = dict(texts, **new_texts)
        if packer is None:
            bundles.write_texts(OFFICIAL, dst, new_texts)  # the project's writer (UnityPy, packer "lz4")
        else:
            env = bundles.UnityPy.load(OFFICIAL.read_bytes())
            for obj in env.objects:
                ta = obj.read() if obj.type.name == "TextAsset" else None
                if ta is not None and ta.m_Name in new_texts:
                    new = new_texts[ta.m_Name].encode("utf-8")
                    ta.m_Script = new.decode("utf-8", "surrogateescape") if isinstance(ta.m_Script, str) else new
                    ta.save()
            dst.write_bytes(next(iter(env.files.values())).save(packer=packer))
        got = bundles.read_texts(dst)
        assert got == want, label
        rows += [f"{label}\t{name}\t{sha_full(text)}" for name, text in sorted(got.items())]
    (out / "unitypy_texts.tsv").write_text("\n".join(rows) + "\n", "utf-8", newline="\n")
    print(f"vectors written to {out}: escape {len(vec)}, fuzz patch {len(expect)}, texts {len(texts)}, "
          f"data entries {n} (refs {refs}, no-ops {noops}), UnityPy bundles {len(packers)}")


if __name__ == "__main__":
    main()
