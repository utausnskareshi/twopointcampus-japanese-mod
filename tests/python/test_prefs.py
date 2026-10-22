"""tpcloc.prefs: reading and rewriting _selectedLanguage in preferences.json (MessagePack).

Works on a copy of the user's real preferences.json (copied once, read-only, into
%TEMP%\\tpcjp_pytests\\fixtures); the original is never opened for writing. Synthetic documents cover
every integer width and the layouts that must be refused.
"""
import os
import shutil
import struct
import unittest
from pathlib import Path

from . import helpers as h
from tpcloc import prefs

REAL_CLOUD = Path(os.environ.get("USERPROFILE", "")) / "AppData" / "LocalLow" / "Two Point Studios" / "Two Point Campus" / "Cloud"
FIXTURE = h.TEMP_ROOT / "fixtures" / "preferences.json"


def real_copy() -> Path | None:
    """The fixture copy of the real preferences.json (made with copyfile = read-only access to the original)."""
    if not FIXTURE.exists():
        found = sorted(REAL_CLOUD.glob("*/preferences.json")) if REAL_CLOUD.exists() else []
        if not found:
            return None
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(found[0], FIXTURE)
    return FIXTURE


# ---------------------------------------------------------------- a tiny MessagePack writer for synthetic docs

def mp_str(s: str) -> bytes:
    b = s.encode("utf-8")
    if len(b) < 32:
        return bytes([0xA0 | len(b)]) + b
    if len(b) < 256:
        return b"\xd9" + bytes([len(b)]) + b
    return b"\xda" + struct.pack(">H", len(b)) + b


def mp_int(v: int, header: int | None = None) -> bytes:
    if header is None:
        if 0 <= v <= 0x7F:
            return bytes([v])
        if -32 <= v < 0:
            return struct.pack(">b", v)
        header = 0xD2
    fmt = {0xCC: ">B", 0xCD: ">H", 0xCE: ">I", 0xCF: ">Q", 0xD0: ">b", 0xD1: ">h", 0xD2: ">i", 0xD3: ">q"}[header]
    return bytes([header]) + struct.pack(fmt, v)


def mp_map(items: list[tuple[bytes, bytes]], wide: bool = False) -> bytes:
    head = (b"\xde" + struct.pack(">H", len(items))) if wide or len(items) > 15 else bytes([0x80 | len(items)])
    return head + b"".join(k + v for k, v in items)


def mp_arr(items: list[bytes], wide: bool = False) -> bytes:
    head = (b"\xdc" + struct.pack(">H", len(items))) if wide or len(items) > 15 else bytes([0x90 | len(items)])
    return head + b"".join(items)


TRUE, FALSE, NIL = b"\xc3", b"\xc2", b"\xc0"


def lang_map(selected: bytes = b"\x0c", has_expected: bytes | None = TRUE, extra: list | None = None,
             drop: str | None = None) -> bytes:
    items = [(mp_str("_selectedLanguage"), selected), (mp_str("_selectedAudioLanguage"), b"\x00")]
    if has_expected is not None:
        items.append((mp_str("_hasExpectedPlatformLanguage"), has_expected))
    items.append((mp_str("_expectedPlatformLanguage"), b"\x00"))
    items += extra or []
    items = [(k, v) for k, v in items if drop is None or k != mp_str(drop)]
    return mp_map(items)


def document(lang: bytes | None = None, before: bytes = b"", wide: bool = False) -> bytes:
    """[2, 0, {"_controlPreferences": [...], "_gamePreferences": [...], "_languagePreferences": [2, 6, {...}]}]"""
    control = mp_arr([b"\x02", b"\x01", mp_map([(mp_str("_panSensitivity"), b"\xca" + struct.pack(">f", 0.5)),
                                                 (mp_str("_bindingsJson"), mp_str("x" * 40))])])
    game = mp_arr([b"\x02", b"\x02", mp_map([(mp_str("_logLevel"), b"\x04"),
                                              (mp_str("_favorites"), mp_arr([b"\x02", b"\x03", mp_arr([])]))],
                                             wide=wide)])
    root_map = [(mp_str("_controlPreferences"), control), (mp_str("_gamePreferences"), game),
                (mp_str("_languagePreferences"), mp_arr([b"\x02", b"\x06", lang if lang is not None else lang_map()]))]
    return before + mp_arr([b"\x02", b"\x00", mp_map(root_map, wide=wide)], wide=wide)


class RealPreferencesCopy(unittest.TestCase):
    def setUp(self):
        src = real_copy()
        if src is None:
            self.skipTest("no preferences.json of the game on this PC")
        self.dir = h.new_dir(self)
        self.path = self.dir / "preferences.json"
        shutil.copyfile(src, self.path)
        self.orig = self.path.read_bytes()

    def test_read_language(self):
        lang = prefs.read_language(self.path)
        self.assertEqual(set(lang), {"_selectedLanguage", "_selectedAudioLanguage", "_hasExpectedPlatformLanguage",
                                     "_expectedPlatformLanguage"})
        self.assertIsInstance(lang["_selectedLanguage"], int)
        self.assertIsInstance(lang["_hasExpectedPlatformLanguage"], bool)
        self.assertEqual(self.path.read_bytes(), self.orig)  # reading never writes

    def test_set_changes_only_the_language_bytes(self):
        lang = prefs.read_language(self.path)
        if lang["_hasExpectedPlatformLanguage"] is not True:
            self.skipTest("this preferences.json has _hasExpectedPlatformLanguage unset")
        node = prefs._language_map(self.orig)["_selectedLanguage"]
        old = lang["_selectedLanguage"]
        for target in (0, 12, 0, old):
            with self.subTest(target=target):
                before = self.path.read_bytes()
                prev = prefs.set_selected_language(self.path, target)
                after = self.path.read_bytes()
                self.assertEqual(prev, prefs._language_map(before)["_selectedLanguage"].value)
                self.assertEqual(len(after), len(before))
                self.assertEqual(after[:node.start], before[:node.start])
                self.assertEqual(after[node.end:], before[node.end:])
                got = prefs.read_language(self.path)
                self.assertEqual(got["_selectedLanguage"], target)
                for k in ("_selectedAudioLanguage", "_hasExpectedPlatformLanguage", "_expectedPlatformLanguage"):
                    self.assertEqual(got[k], lang[k])
        self.assertEqual(self.path.read_bytes(), self.orig)  # back to the original value: byte identical

    def test_every_value_of_the_document_parses(self):
        root = prefs._read(self.orig, 0)
        self.assertEqual(root.end, len(self.orig))
        self.assertEqual([n.value for n in root.value[:2]], [2, 0])
        self.assertIn("_languagePreferences", root.value[2].value)

    def test_trailing_data_refused_and_file_untouched(self):
        self.path.write_bytes(self.orig + b"\x00")
        with self.assertRaises(ValueError):
            prefs.read_language(self.path)
        with self.assertRaises(ValueError):
            prefs.set_selected_language(self.path, 0)
        self.assertEqual(self.path.read_bytes(), self.orig + b"\x00")


class IntegerWidths(unittest.TestCase):
    def setUp(self):
        self.dir = h.new_dir(self)

    def write(self, data: bytes) -> Path:
        p = self.dir / "preferences.json"
        p.write_bytes(data)
        return p

    def test_every_integer_encoding_keeps_its_width(self):
        cases = [(None, 12), (0xCC, 12), (0xCD, 12), (0xCE, 12), (0xCF, 12), (0xD0, 12), (0xD1, 12), (0xD2, 12),
                 (0xD3, 12)]
        for header, old in cases:
            with self.subTest(header=header):
                enc = mp_int(old, header)
                p = self.write(document(lang_map(selected=enc)))
                before = p.read_bytes()
                self.assertEqual(prefs.read_language(p)["_selectedLanguage"], old)
                self.assertEqual(prefs.set_selected_language(p, 0), old)
                after = p.read_bytes()
                self.assertEqual(len(after), len(before))
                start = before.index(enc + mp_str("_selectedAudioLanguage"))
                self.assertEqual(after[:start], before[:start])
                self.assertEqual(after[start:start + len(enc)], mp_int(0, header))
                self.assertEqual(after[start + len(enc):], before[start + len(enc):])
                self.assertEqual(prefs.read_language(p)["_selectedLanguage"], 0)
                self.assertEqual(prefs.set_selected_language(p, 12), 0)
                self.assertEqual(p.read_bytes(), before)

    def test_large_values_in_wide_encodings(self):
        for header, value in [(0xCC, 255), (0xCD, 65535), (0xCE, 2 ** 32 - 1), (0xCF, 2 ** 64 - 1),
                              (0xD0, -128), (0xD1, -32768), (0xD2, -2 ** 31), (0xD3, -2 ** 63)]:
            with self.subTest(header=hex(header)):
                p = self.write(document(lang_map(selected=mp_int(0, header))))
                prefs.set_selected_language(p, value)
                self.assertEqual(prefs.read_language(p)["_selectedLanguage"], value)

    def test_fixint_cannot_grow(self):
        p = self.write(document(lang_map(selected=b"\x0c")))
        before = p.read_bytes()
        for value in (128, 300, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    prefs.set_selected_language(p, value)
                self.assertEqual(p.read_bytes(), before)

    def test_negative_fixint_encoding_refused(self):
        p = self.write(document(lang_map(selected=b"\xff")))  # -1
        self.assertEqual(prefs.read_language(p)["_selectedLanguage"], -1)
        with self.assertRaises(ValueError) as cm:
            prefs.set_selected_language(p, 12)
        self.assertIn("0xFF", str(cm.exception))

    def test_value_out_of_range_for_the_width_is_refused_and_file_untouched(self):
        for header, value in [(0xCC, 256), (0xCC, -1), (0xD0, 200), (0xCD, 70000)]:
            with self.subTest(header=hex(header), value=value):
                p = self.write(document(lang_map(selected=mp_int(1, header))))
                before = p.read_bytes()
                with self.assertRaises((ValueError, struct.error)):
                    prefs.set_selected_language(p, value)
                self.assertEqual(p.read_bytes(), before)

    def test_wide_maps_arrays_and_strings_parse(self):
        long_key = "_" + "k" * 40  # str8
        longer = "v" * 300           # str16
        lang = lang_map(extra=[(mp_str(long_key), mp_str(longer)), (mp_str("bin"), b"\xc4\x02ab"),
                               (mp_str("ext"), b"\xd4\x01\x00"), (mp_str("ext8"), b"\xc7\x02\x05xy"),
                               (mp_str("nil"), NIL), (mp_str("f64"), b"\xcb" + struct.pack(">d", 1.5)),
                               (mp_str("neg"), b"\xe0")])
        p = self.write(document(lang, wide=True))
        got = prefs.read_language(p)
        self.assertEqual(got[long_key], longer)
        self.assertEqual(got["bin"], b"ab")
        self.assertEqual(got["nil"], None)
        self.assertEqual(got["f64"], 1.5)
        self.assertEqual(got["neg"], -32)
        self.assertEqual(prefs.set_selected_language(p, 0), 12)
        self.assertEqual(prefs.read_language(p)["_selectedLanguage"], 0)


class UnexpectedLayouts(unittest.TestCase):
    def setUp(self):
        self.dir = h.new_dir(self)

    def check_refused(self, data: bytes, *, exc=ValueError, contains: str = "", read_too: bool = True):
        p = self.dir / "preferences.json"
        p.write_bytes(data)
        if read_too:
            with self.assertRaises(exc):
                prefs.read_language(p)
        with self.assertRaises(exc) as cm:
            prefs.set_selected_language(p, 0)
        if contains:
            self.assertIn(contains, str(cm.exception))
        self.assertEqual(p.read_bytes(), data)  # never written

    def test_root_not_an_array(self):
        self.check_refused(mp_map([(mp_str("a"), b"\x01")]), contains="layout")

    def test_root_array_too_short(self):
        self.check_refused(mp_arr([b"\x02", b"\x00"]), contains="layout")

    def test_no_language_preferences(self):
        self.check_refused(mp_arr([b"\x02", b"\x00", mp_map([(mp_str("_gamePreferences"), mp_arr([]))])]),
                           contains="layout")

    def test_language_preferences_not_an_array(self):
        self.check_refused(mp_arr([b"\x02", b"\x00", mp_map([(mp_str("_languagePreferences"), lang_map())])]),
                           contains="layout")

    def test_language_preferences_wrapper_too_short(self):
        self.check_refused(mp_arr([b"\x02", b"\x00", mp_map([(mp_str("_languagePreferences"), mp_arr([b"\x02"]))])]),
                           contains="layout")

    def test_selected_language_not_an_integer(self):
        for enc in (mp_str("12"), TRUE, NIL, b"\xca" + struct.pack(">f", 12.0)):
            with self.subTest(enc=enc):
                self.check_refused(document(lang_map(selected=enc)), contains="not an integer", read_too=False)

    def test_has_expected_platform_language_missing_or_false(self):
        for has in (None, FALSE, b"\x01"):
            with self.subTest(has=has):
                self.check_refused(document(lang_map(has_expected=has)), contains="launch the game once", read_too=False)

    def test_selected_language_missing(self):
        # KeyError rather than ValueError; still nothing is written
        p = self.dir / "preferences.json"
        data = document(lang_map(drop="_selectedLanguage"))
        p.write_bytes(data)
        self.assertNotIn("_selectedLanguage", prefs.read_language(p))
        with self.assertRaises((KeyError, ValueError)):
            prefs.set_selected_language(p, 0)
        self.assertEqual(p.read_bytes(), data)

    def test_unsupported_type_byte(self):
        self.check_refused(document(lang_map(extra=[(mp_str("x"), b"\xc1")])), contains="0xC1")

    def test_trailing_data_and_leading_garbage(self):
        self.check_refused(document() + b"\x90", contains="trailing")
        self.check_refused(b"\x00" + document())

    def test_truncated_file_never_written(self):
        full = document()
        p = self.dir / "preferences.json"
        for cut in (0, 1, 5, len(full) // 2, len(full) - 1):
            with self.subTest(cut=cut):
                data = full[:cut]
                p.write_bytes(data)
                with self.assertRaises(Exception):
                    prefs.set_selected_language(p, 0)
                self.assertEqual(p.read_bytes(), data)


class CSharpParity(unittest.TestCase):
    """The C# reader (Preferences.cs) was hardened by the C1 fixes (CS-03/CS-04/FZ-03/FZ-04/FZ-12): a repeated key,
    nesting deeper than 32 and lengths beyond the file are refused. The Python port (used by switch_lang.py)
    must refuse the same documents, or at least never write them."""

    def setUp(self):
        self.dir = h.new_dir(self)

    def never_written(self, data: bytes, exc=Exception):
        p = self.dir / "preferences.json"
        p.write_bytes(data)
        with self.assertRaises(exc):
            prefs.set_selected_language(p, 0)
        self.assertEqual(p.read_bytes(), data)
        return p

    def test_deep_nesting_is_refused_without_writing(self):
        deep = b"\x91" * 100000 + b"\x90"  # [[[...[]...]]]
        data = document(lang_map(extra=[(mp_str("deep"), deep)]))
        self.never_written(data)
        with self.assertRaises(Exception):
            prefs.read_language(self.dir / "preferences.json")

    def test_lengths_beyond_the_file_are_refused_without_writing(self):
        for name, enc in (("str32", b"\xdb\xff\xff\xff\xff" + b"x"), ("bin32", b"\xc6\xff\xff\xff\xff"),
                          ("array32", b"\xdd\xff\xff\xff\xff\x01"), ("map32", b"\xdf\xff\xff\xff\xff"),
                          ("ext32", b"\xc9\xff\xff\xff\xff\x01"), ("str16", b"\xda\xff\xff" + b"y")):
            with self.subTest(name=name):
                lang = lang_map(extra=[(mp_str("_z"), enc)])
                self.never_written(document(lang))

    def test_repeated_key_is_refused(self):
        """A repeated _selectedLanguage: the C# reader refuses the file ("同じ項目…が重複しています").
        Was finding pytests-PF1 (the second value was rewritten, the first kept); fixed in round 2."""
        dup = lang_map(extra=[(mp_str("_selectedLanguage"), b"\x03")])
        p = self.never_written(document(dup), exc=ValueError)
        with self.assertRaisesRegex(ValueError, "repeated key '_selectedLanguage'"):
            prefs.set_selected_language(p, 0)
        with self.assertRaisesRegex(ValueError, "repeated key"):  # switch_lang.py status reads it the same way
            prefs.read_language(p)
        self.assertEqual(p.read_bytes(), document(dup))

    def test_repeated_key_in_another_map_is_refused(self):
        root_map = mp_map([(mp_str("_languagePreferences"), mp_arr([b"\x02", b"\x06", lang_map()])),
                           (mp_str("_gamePreferences"), b"\x01"), (mp_str("_gamePreferences"), b"\x02")])
        self.never_written(mp_arr([b"\x02", b"\x00", root_map]), exc=ValueError)

    def test_repeated_key_anywhere_in_the_document_is_refused(self):
        """Other repeated keys: _hasExpectedPlatformLanguage (decides whether the game applies the language) and a
        key in a map nested inside _gamePreferences, which the language rewrite never looks at (C# refuses both)."""
        dup_flag = lang_map(extra=[(mp_str("_hasExpectedPlatformLanguage"), FALSE)])
        self.never_written(document(dup_flag), exc=ValueError)
        nested = mp_arr([b"\x02", b"\x01", mp_map([(mp_str("_inner"), mp_map([(mp_str("a"), b"\x01"),
                                                                                (mp_str("a"), b"\x02")]))])])
        root_map = mp_map([(mp_str("_gamePreferences"), nested),
                           (mp_str("_languagePreferences"), mp_arr([b"\x02", b"\x06", lang_map()]))])
        self.never_written(mp_arr([b"\x02", b"\x00", root_map]), exc=ValueError)

    def test_same_key_in_different_maps_is_not_a_repeat(self):
        """Only a repeat inside one map is refused: the same name in sibling maps is normal."""
        game = mp_arr([b"\x02", b"\x01", mp_map([(mp_str("_version"), b"\x01")])])
        lang = lang_map(extra=[(mp_str("_version"), b"\x02")])
        root_map = mp_map([(mp_str("_gamePreferences"), game),
                           (mp_str("_languagePreferences"), mp_arr([b"\x02", b"\x06", lang]))])
        p = self.dir / "preferences.json"
        doc = mp_arr([b"\x02", b"\x00", root_map])
        p.write_bytes(doc)
        self.assertEqual(prefs.set_selected_language(p, 0), 12)
        self.assertEqual(p.read_bytes(), doc.replace(mp_str("_selectedLanguage") + b"\x0c", mp_str("_selectedLanguage") + b"\x00"))


if __name__ == "__main__":
    unittest.main()
