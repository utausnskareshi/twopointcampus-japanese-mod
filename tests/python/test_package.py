"""tools/package.py: check_release() and option parsing only (nothing is ever built).

The module is loaded in-process and its DIST / PATCHER folders are pointed at a temp folder.
"""
import sys
import unittest
from unittest import mock

from . import helpers as h

README = """Two Point Campus 日本語化パッチ {VERSION}

■ 更新履歴
・1.0.10　十番目
・1.0.0　最初の版
・2.0 大きな版
・3.1.4.1　四つの数
"""


class CheckRelease(unittest.TestCase):
    def setUp(self):
        self.root = h.new_dir(self)
        self.pkg = h.load_tool("package")
        (self.root / "patcher").mkdir()
        (self.root / "patcher" / "README.txt").write_text(README, "utf-8")
        (self.root / "dist").mkdir()
        for name, value in (("DIST", self.root / "dist"), ("PATCHER", self.root / "patcher")):
            p = mock.patch.object(self.pkg, name, value)
            p.start()
            self.addCleanup(p.stop)
        # nothing may be run or built from these tests
        for name in ("run", "main"):
            p = mock.patch.object(self.pkg, name, side_effect=AssertionError(f"package.{name} called"))
            p.start()
            self.addCleanup(p.stop)

    def refused(self, version, force=False) -> str:
        before = h.snapshot_files(self.root)
        listing = sorted(str(p) for p in self.root.rglob("*"))
        with self.assertRaises(SystemExit) as cm:
            self.pkg.check_release(version, force)
        self.assertEqual(h.snapshot_files(self.root), before)
        self.assertEqual(sorted(str(p) for p in self.root.rglob("*")), listing)  # no folder created either
        return str(cm.exception.code)

    def test_missing_version(self):
        for v in (None, ""):
            with self.subTest(v=v):
                self.assertIn("--version is required", self.refused(v))

    def test_invalid_versions(self):
        for v in ("1", "v1.0.1", "1.0.1-beta", "1.0.1.2.3", "1..0", " 1.0.1", "1.0.1 ", "1.0.1\n", "1,0,1",
                  "1.0.x", "../1.0", "1.0/1"):
            with self.subTest(v=v):
                self.assertIn("is not a version number like 1.0.1", self.refused(v))

    def test_full_width_digits_are_refused(self):
        # \d also matches full-width digits; the changelog check still stops the release
        self.refused("１.０.１")

    def test_valid_versions_with_changelog_line(self):
        for v in ("1.0.0", "1.0.10", "2.0", "3.1.4.1"):
            with self.subTest(v=v):
                out_dir, archive = self.pkg.check_release(v, False)
                self.assertEqual(out_dir, self.root / "dist" / f"TpcJpPatch-{v}")
                self.assertEqual(archive, self.root / "dist" / f"TpcJpPatch-{v}.zip")
                self.assertFalse(out_dir.exists())
                self.assertFalse(archive.exists())

    def test_changelog_line_must_match_exactly(self):
        for v in ("1.0.1", "1.0", "3.1.4", "2.0.0"):  # only prefixes / extensions of existing lines
            with self.subTest(v=v):
                msg = self.refused(v)
                self.assertIn(f"no changelog line starting with '・{v}'", msg)

    def test_existing_release_needs_force(self):
        (self.root / "dist" / "TpcJpPatch-1.0.0").mkdir()
        msg = self.refused("1.0.0")
        self.assertIn("already exists", msg)
        self.assertIn("--force", msg)
        self.assertEqual(self.pkg.check_release("1.0.0", True)[0], self.root / "dist" / "TpcJpPatch-1.0.0")
        (self.root / "dist" / "TpcJpPatch-1.0.0").rmdir()
        (self.root / "dist" / "TpcJpPatch-1.0.0.zip").write_bytes(b"zip")
        self.assertIn("TpcJpPatch-1.0.0.zip already exists", self.refused("1.0.0"))
        self.pkg.check_release("1.0.0", True)
        self.assertEqual((self.root / "dist" / "TpcJpPatch-1.0.0.zip").read_bytes(), b"zip")

    def test_existing_other_version_does_not_block(self):
        (self.root / "dist" / "TpcJpPatch-1.0.0.zip").write_bytes(b"zip")
        self.pkg.check_release("1.0.10", False)

    def test_version_checked_before_existing_files(self):
        (self.root / "dist" / "TpcJpPatch-1.zip").write_bytes(b"zip")
        self.assertIn("is not a version number", self.refused("1"))

    def test_opt(self):
        for argv, want in ((["package.py", "--version", "1.0.0"], "1.0.0"), (["package.py"], None)):
            with self.subTest(argv=argv), mock.patch.object(sys, "argv", argv):
                self.assertEqual(self.pkg.opt("--version"), want)
        for argv in (["package.py", "--version"], ["package.py", "--version", "--force"]):
            with self.subTest(argv=argv), mock.patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit) as cm:
                    self.pkg.opt("--version")
                self.assertIn("--version needs a value", str(cm.exception.code))

    def test_real_readme_has_a_line_for_the_built_version(self):
        real = h.PROJECT / "patcher" / "README.txt"
        if not real.exists() or not h.REAL_DATA.exists():
            self.skipTest("no README / data")
        import re
        version = re.search(r"^#patch_version\t(.*)$", h.REAL_DATA.read_text("utf-8"), re.M).group(1)
        (self.root / "patcher" / "README.txt").write_text(real.read_text("utf-8"), "utf-8")
        self.pkg.check_release(version, True)


if __name__ == "__main__":
    unittest.main()
