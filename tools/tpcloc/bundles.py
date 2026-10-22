"""Read and rewrite the I2 language-cache TextAssets inside a localisation bundle (UnityPy)."""
import hashlib
import warnings
from pathlib import Path

import UnityPy
import UnityPy.config

# The game's bundles have their Unity version stripped from the header.
UnityPy.config.FALLBACK_UNITY_VERSION = "2020.3.47f1"
warnings.filterwarnings("ignore", category=UserWarning, module="UnityPy")
try:
    from UnityPy.exceptions import UnityVersionFallbackWarning

    warnings.filterwarnings("ignore", category=UnityVersionFallbackWarning)
except ImportError:  # older UnityPy
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for block in iter(lambda: fp.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _script_bytes(text_asset) -> bytes:
    raw = text_asset.m_Script
    return raw.encode("utf-8", "surrogateescape") if isinstance(raw, str) else bytes(raw)


def _load(path: Path):
    # Load from memory so no file handle stays open (Windows would block replacing the file).
    return UnityPy.load(Path(path).read_bytes())


def read_texts(bundle_path: Path) -> dict[str, str]:
    """Return {TextAsset name: decoded text} for every TextAsset in the bundle."""
    env = _load(bundle_path)
    texts = {}
    for obj in env.objects:
        if obj.type.name == "TextAsset":
            ta = obj.read()
            texts[ta.m_Name] = _script_bytes(ta).decode("utf-8")
    return texts


def write_texts(src_bundle: Path, dst_bundle: Path, replacements: dict[str, str]) -> list[str]:
    """Copy src_bundle to dst_bundle with the named TextAssets' contents replaced.

    Returns the names that were replaced. Raises if a requested name is missing.
    """
    env = _load(src_bundle)
    done = []
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        ta = obj.read()
        if ta.m_Name not in replacements:
            continue
        new_bytes = replacements[ta.m_Name].encode("utf-8")
        if isinstance(ta.m_Script, str):
            ta.m_Script = new_bytes.decode("utf-8", "surrogateescape")
        else:
            ta.m_Script = new_bytes
        ta.save()
        done.append(ta.m_Name)
    missing = set(replacements) - set(done)
    if missing:
        raise KeyError(f"TextAssets not found in {src_bundle.name}: {sorted(missing)}")
    bundle = next(iter(env.files.values()))
    data = bundle.save(packer="lz4")
    tmp = dst_bundle.with_suffix(dst_bundle.suffix + ".tmp")
    tmp.write_bytes(data)
    # Verify the written bundle parses and carries the new texts before replacing the target.
    check = read_texts(tmp)
    for name, text in replacements.items():
        if check.get(name) != text:
            tmp.unlink()
            raise RuntimeError(f"verification failed for {name}")
    tmp.replace(dst_bundle)
    return done
