"""I2 Localization language-cache format used by the game's per-language TextAssets.

The format is ``Term=Translation[i2t]Term=Translation...`` (UTF-8, no BOM, no trailing
separator). Translations may contain LF characters. The game splits each entry at the first
``=``, so we do the same; parse() followed by dumps() reproduces the input byte for byte.
"""
SEP = "[i2t]"


def parse(text: str) -> list[tuple[str, str]]:
    if not text:
        return []
    pairs = []
    for chunk in text.split(SEP):
        key, _, value = chunk.partition("=")
        pairs.append((key, value))
    return pairs


def dumps(pairs: list[tuple[str, str]]) -> str:
    return SEP.join(f"{k}={v}" for k, v in pairs)


def to_dict(text: str) -> dict[str, str]:
    return dict(parse(text))
