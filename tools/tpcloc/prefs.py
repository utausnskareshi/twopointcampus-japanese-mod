"""In-place editing of the game's preferences.json (MessagePack) language setting.

Layout (written by the game): [2, 0, {"_controlPreferences": [...], "_gamePreferences": [...],
"_languagePreferences": [2, 6, {"_selectedLanguage": <int>, "_selectedAudioLanguage": <int>,
"_hasExpectedPlatformLanguage": <bool>, "_expectedPlatformLanguage": <int>}]}]

At startup the game applies _selectedLanguage without validation when
_hasExpectedPlatformLanguage is true and _expectedPlatformLanguage equals the platform's
recommended language (TPC.ActiveLanguageSettings.ApplyCorrectLanguageAtStartup), so only the
_selectedLanguage integer is rewritten, keeping its encoded width and every other byte.
"""
import struct
from dataclasses import dataclass
from pathlib import Path

JAPANESE = 12
LANGUAGE_NAMES = {0: "English", 1: "French", 2: "Italian", 3: "German", 4: "Spanish", 5: "Polish",
                  7: "Chinese (Simplified)", 8: "Chinese (Traditional)", 9: "Korean",
                  10: "Brazilian Portuguese", 11: "Turkish", 12: "Japanese"}


@dataclass
class Node:
    value: object
    start: int  # offset of the type byte
    end: int


def _read(buf: bytes, pos: int) -> Node:
    b = buf[pos]
    start = pos

    def node(v, end):
        return Node(v, start, end)

    if b <= 0x7F:
        return node(b, pos + 1)
    if b >= 0xE0:
        return node(b - 0x100, pos + 1)
    if 0x80 <= b <= 0x8F:
        return _read_map(buf, pos + 1, b & 0x0F, start)
    if 0x90 <= b <= 0x9F:
        return _read_array(buf, pos + 1, b & 0x0F, start)
    if 0xA0 <= b <= 0xBF:
        n = b & 0x1F
        return node(buf[pos + 1:pos + 1 + n].decode("utf-8"), pos + 1 + n)
    if b == 0xC0:
        return node(None, pos + 1)
    if b in (0xC2, 0xC3):
        return node(b == 0xC3, pos + 1)
    fixed = {0xCC: ">B", 0xCD: ">H", 0xCE: ">I", 0xCF: ">Q", 0xD0: ">b", 0xD1: ">h", 0xD2: ">i",
             0xD3: ">q", 0xCA: ">f", 0xCB: ">d"}
    if b in fixed:
        fmt = fixed[b]
        size = struct.calcsize(fmt)
        return node(struct.unpack_from(fmt, buf, pos + 1)[0], pos + 1 + size)
    if b in (0xD9, 0xDA, 0xDB, 0xC4, 0xC5, 0xC6):
        lf = {0xD9: ">B", 0xDA: ">H", 0xDB: ">I", 0xC4: ">B", 0xC5: ">H", 0xC6: ">I"}[b]
        ls = struct.calcsize(lf)
        n = struct.unpack_from(lf, buf, pos + 1)[0]
        data = buf[pos + 1 + ls:pos + 1 + ls + n]
        return node(data.decode("utf-8") if b >= 0xD9 else data, pos + 1 + ls + n)
    if b in (0xDC, 0xDD):
        lf = ">H" if b == 0xDC else ">I"
        n = struct.unpack_from(lf, buf, pos + 1)[0]
        return _read_array(buf, pos + 1 + struct.calcsize(lf), n, start)
    if b in (0xDE, 0xDF):
        lf = ">H" if b == 0xDE else ">I"
        n = struct.unpack_from(lf, buf, pos + 1)[0]
        return _read_map(buf, pos + 1 + struct.calcsize(lf), n, start)
    if 0xD4 <= b <= 0xD8:
        n = {0xD4: 1, 0xD5: 2, 0xD6: 4, 0xD7: 8, 0xD8: 16}[b]
        return node(("ext", buf[pos + 2:pos + 2 + n]), pos + 2 + n)
    if b in (0xC7, 0xC8, 0xC9):
        lf = {0xC7: ">B", 0xC8: ">H", 0xC9: ">I"}[b]
        ls = struct.calcsize(lf)
        n = struct.unpack_from(lf, buf, pos + 1)[0]
        return node(("ext", buf[pos + 2 + ls:pos + 2 + ls + n]), pos + 2 + ls + n)
    raise ValueError(f"unsupported MessagePack byte 0x{b:02X} at {pos}")


def _read_array(buf, pos, n, start) -> Node:
    items = []
    for _ in range(n):
        item = _read(buf, pos)
        items.append(item)
        pos = item.end
    return Node(items, start, pos)


def _read_map(buf, pos, n, start) -> Node:
    items = {}
    for _ in range(n):
        k = _read(buf, pos)
        v = _read(buf, k.end)
        if k.value in items:  # like Preferences.cs: a repeated key would leave a value this tool did not change
            raise ValueError(f"repeated key {k.value!r} in preferences.json")
        items[k.value] = v
        pos = v.end
    return Node(items, start, pos)


def _language_map(buf: bytes) -> dict:
    root = _read(buf, 0)
    if root.end != len(buf):
        raise ValueError("trailing data after MessagePack root")
    try:
        return root.value[2].value["_languagePreferences"].value[2].value
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise ValueError("unexpected preferences.json layout") from e


def read_language(path: Path) -> dict:
    lang = _language_map(path.read_bytes())
    return {k: v.value for k, v in lang.items()}


def _encode_like(old_header: int, value: int) -> bytes:
    if old_header <= 0x7F:
        if not 0 <= value <= 0x7F:
            raise ValueError("value does not fit a positive fixint")
        return bytes([value])
    fmt = {0xCC: ">B", 0xCD: ">H", 0xCE: ">I", 0xCF: ">Q", 0xD0: ">b", 0xD1: ">h", 0xD2: ">i", 0xD3: ">q"}
    if old_header not in fmt:
        raise ValueError(f"unexpected integer encoding 0x{old_header:02X}")
    return bytes([old_header]) + struct.pack(fmt[old_header], value)


def set_selected_language(path: Path, language_id: int) -> int:
    """Rewrite _selectedLanguage in place; returns the previous value."""
    buf = bytearray(path.read_bytes())
    lang = _language_map(bytes(buf))
    node = lang["_selectedLanguage"]
    if not isinstance(node.value, int) or isinstance(node.value, bool):
        raise ValueError("_selectedLanguage is not an integer")
    if lang.get("_hasExpectedPlatformLanguage") is None or lang["_hasExpectedPlatformLanguage"].value is not True:
        raise ValueError("_hasExpectedPlatformLanguage is not set; launch the game once first")
    new = _encode_like(buf[node.start], language_id)
    if len(new) != node.end - node.start:
        raise ValueError("encoding width changed")
    old = node.value
    buf[node.start:node.end] = new
    # Re-parse to make sure the result is still a valid document with the new value.
    if _language_map(bytes(buf))["_selectedLanguage"].value != language_id:
        raise RuntimeError("verification failed")
    path.write_bytes(bytes(buf))
    return old
