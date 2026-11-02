"""Small driver overlay for complete 0.4.2 installations. No runtime, models or identities."""
import hashlib
import json
from pathlib import Path
import zipfile


def main():
    project = Path(__file__).resolve().parent.parent
    repo = project.parent
    entries = {"VirtualCam/" + name: repo / "app" / "VirtualCam" / name for name in
               ("UnityCaptureFilter64.dll", "UnityCaptureFilter32.dll", "LICENSE-UnityCaptureFilter.txt")}
    entries["deploy/install-driver.ps1"] = project / "deploy" / "install-driver.ps1"
    entries["docs/14-横线花屏驱动补丁.md"] = project / "docs" / "14-横线花屏驱动补丁.md"
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in entries.items()}
    destination = project / "dist" / "RemoteGpu-0.4.2-camera-fix-0.4.3.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path in entries.items():
            archive.write(path, "RemoteGpu/" + name)
        archive.writestr("driver-update-manifest.json", json.dumps({"requires": "complete RemoteGpu 0.4.2",
            "driver_fix": "0.4.3", "exe_unchanged": True, "files": hashes}, ensure_ascii=False, indent=2))
        archive.writestr("更新说明.txt", (project / "docs" / "14-横线花屏驱动补丁.md").read_text(encoding="utf-8"))
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        for name, expected in hashes.items():
            assert hashlib.sha256(archive.read("RemoteGpu/" + name)).hexdigest() == expected
    result = {"path": str(destination), "size_kb": round(destination.stat().st_size / 1024, 1),
              "archive_verified": True, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
    (project / "reports").mkdir(exist_ok=True)
    (project / "reports" / "camera-fix-043.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
