"""Package the standalone LAN EXE, notices and instructions with verified hashes."""
import hashlib
from pathlib import Path
import zipfile

from rgpu.lan import LAN_VERSION


def main():
    project = Path(__file__).resolve().parents[1]
    folder = project / "dist" / f"FaceLiveLan-{LAN_VERSION}"
    executable = folder / "FaceLiveLan.exe"
    if not executable.is_file():
        raise SystemExit("Run deploy/build-lan.ps1 first.")
    sums = []
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            sums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(folder).as_posix()}")
    (folder / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    archive = folder.parent / f"FaceLiveLan-{LAN_VERSION}-win-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(folder.parent))
    with zipfile.ZipFile(archive) as output:
        assert output.testzip() is None
        for row in sums:
            expected, relative = row.split("  ", 1)
            assert hashlib.sha256(output.read(folder.name + "/" + relative)).hexdigest() == expected
    archive.with_suffix(".zip.sha256").write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n", encoding="ascii")
    print(f"Verified: {archive} ({archive.stat().st_size / 1024 / 1024:.1f} MiB)")


if __name__ == "__main__":
    main()
