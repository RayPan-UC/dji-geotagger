"""
Zip the built application and write checksums for the release page.

Run after `pyinstaller packaging/dji-geotagger.spec`. The archive is named for
the version in pyproject, so a release asset can never claim to be a version
it was not built from.
"""

import hashlib
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist_exe" / "dji-geotagger"
OUT = ROOT / "dist_exe"

# Anything the application writes beside itself when it is run from the build
# directory - which is exactly how it gets tested. DGT_output holds a whole
# survey: RINEX, PPK solutions, ephemerides, the lot. Testing the build before
# releasing it is the normal thing to do, so without this the normal thing
# quietly publishes someone's flight data.
RUNTIME_OUTPUT = {"DGT_output"}


def main() -> int:
    if not (DIST / "dji-geotagger.exe").exists():
        print(f"Nothing built at {DIST}. Run PyInstaller first.")
        return 1

    version = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]

    archive = OUT / f"dji-geotagger-{version}-win64.zip"
    if archive.exists():
        archive.unlink()

    # Deflated rather than stored: the payload is mostly compiled extensions
    # and a 9 MB PROJ database, which halve.
    total = 0
    skipped = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(DIST.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(DIST)
            if RUNTIME_OUTPUT.intersection(relative.parts):
                skipped += 1
                continue
            zf.write(path, Path("dji-geotagger") / relative)
            total += 1
    print(f"{archive.name}: {total} files, {archive.stat().st_size/1048576:.0f} MB")
    if skipped:
        # Loud, not silent: a file left over from testing is a thing to notice.
        print(f"  excluded {skipped} run-time output files "
              f"({', '.join(sorted(RUNTIME_OUTPUT))})")

    # Checksums for everything meant to be downloaded, so a truncated or
    # tampered download is detectable rather than merely unlikely.
    lines = []
    for candidate in sorted(OUT.glob("*")):
        if candidate.is_file() and candidate.name != "SHA256SUMS.txt":
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            lines.append(f"{digest}  {candidate.name}")
            print(f"  {digest[:16]}...  {candidate.name}")

    (OUT / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"SHA256SUMS.txt: {len(lines)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
