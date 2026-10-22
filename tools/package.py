"""Build the release: translation data -> patcher exe (data and library embedded) -> dist/TpcJpPatch-<version>.zip

usage: python tools/package.py --version 1.0.1 [--status draft,reviewed,edited] [--force]
The --status filter is passed to build_patch.py (e.g. "reviewed,edited" for a reviewed-only release).
patcher/README.txt must have a changelog line starting with "・<version>". An existing release of the same
version is not overwritten unless --force is given.
"""
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tpcloc import paths  # noqa: E402

PATCHER = paths.PROJECT_ROOT / "patcher"
PROJECT = PATCHER / "TpcJpPatch" / "TpcJpPatch.csproj"
OUTPUT = PATCHER / "TpcJpPatch" / "bin" / "Release" / "net48"
DIST = paths.PROJECT_ROOT / "dist"
DATA = DIST / "data" / "TpcJpPatch.data.tsv"  # build_patch.py's default output, embedded by the csproj
LICENSE = paths.PROJECT_ROOT / "LICENSE"


def opt(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name) + 1
        if i >= len(sys.argv) or sys.argv[i].startswith("--"):
            sys.exit(f"{name} needs a value")
        return sys.argv[i]
    return default


def run(cmd):
    print(">", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True)


def check_release(version: str | None, force: bool) -> tuple[Path, Path]:
    """Stop before building when the version is missing/invalid, already built, or not in the README changelog."""
    if not version:
        sys.exit("usage: python tools/package.py --version <x.y.z> [--status ...] [--force]  (--version is required)")
    if not re.fullmatch(r"\d+(\.\d+){1,3}", version):
        sys.exit(f"--version {version!r} is not a version number like 1.0.1")
    out_dir = DIST / f"TpcJpPatch-{version}"
    archive = DIST / f"TpcJpPatch-{version}.zip"
    existing = [p for p in (out_dir, archive) if p.exists()]
    if existing and not force:
        sys.exit(f"{', '.join(str(p) for p in existing)} already exists; raise --version or pass --force to rebuild it")
    readme = (PATCHER / "README.txt").read_text("utf-8")
    if not re.search(rf"^・{re.escape(version)}(?![0-9.])", readme, re.M):
        sys.exit(f"patcher/README.txt has no changelog line starting with '・{version}'; add one under ■ 更新履歴")
    return out_dir, archive


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    version = opt("--version")
    out_dir, archive = check_release(version, "--force" in sys.argv)
    status = opt("--status", "draft,reviewed,edited")
    try:
        run([sys.executable, str(Path(__file__).with_name("build_patch.py")), "--version", version, "--status", status])
    except subprocess.CalledProcessError:
        sys.exit("build_patch.py failed; nothing was built")
    run(["dotnet", "build", str(PROJECT), "-c", "Release", f"-p:Version={version}"])

    game_version = json.loads((paths.WORK_DIR / "extract" / "strings.json").read_text("utf-8"))["game_version"]
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    shutil.copy2(OUTPUT / "TpcJpPatch.exe", out_dir)
    shutil.copy2(OUTPUT / "TpcJpPatch.exe.config", out_dir)
    readme = (PATCHER / "README.txt").read_text("utf-8")
    readme = readme.replace("{VERSION}", version).replace("{GAME_VERSION}", re.sub(r"\+.*", "", game_version))
    # CRLF + BOM so that every Windows text viewer shows it correctly
    (out_dir / "README.txt").write_bytes(b"\xef\xbb\xbf" + readme.replace("\n", "\r\n").encode("utf-8"))
    notices = (PATCHER / "THIRD-PARTY-NOTICES.txt").read_text("utf-8")
    (out_dir / "THIRD-PARTY-NOTICES.txt").write_bytes(notices.replace("\n", "\r\n").encode("utf-8"))
    license_text = LICENSE.read_text("utf-8")  # the copyright holder's name is not ASCII: BOM like README.txt
    (out_dir / "LICENSE.txt").write_bytes(b"\xef\xbb\xbf" + license_text.replace("\n", "\r\n").encode("utf-8"))

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(out_dir.iterdir()):
            zf.write(f, f"{out_dir.name}/{f.name}")
    entries = re.search(r"^#entries\t(\d+)", DATA.read_text("utf-8"), re.M)
    print(f"-> {archive} ({archive.stat().st_size:,} bytes, {entries.group(1) if entries else '?'} entries)")


if __name__ == "__main__":
    main()
