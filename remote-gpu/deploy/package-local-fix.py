"""Verified local-only overlay for a complete 0.4.7 package."""
import hashlib
import json
from pathlib import Path
import zipfile


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    root = Path(__file__).resolve().parents[1]
    old = root / "dist/0.4.7/RemoteGpu"
    new = root / "dist/0.4.8/RemoteGpu"
    before = {p.relative_to(old).as_posix(): p for p in old.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
    current = {p.relative_to(new).as_posix(): p for p in new.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
    runtimes = {name for name in current if name.startswith("_internal/")}
    assert runtimes == {name for name in before if name.startswith("_internal/")}
    changed = {name: current[name] for name in runtimes if sha(current[name]) != sha(before[name])}
    for name in ("RemoteGpuLocal.exe", "docs/19-本地输出分辨率实时应用.md"):
        changed[name] = current[name]
    hashes = {name: sha(path) for name, path in changed.items()}
    merged = {**before, **changed}
    checksums = "".join(f"{sha(path)}  {name}\n" for name, path in sorted(merged.items()))
    destination = root / "dist/RemoteGpu-0.4.7-local-fix-0.4.8.zip"
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path in sorted(changed.items()):
            archive.write(path, "RemoteGpu/" + name)
        archive.writestr("RemoteGpu/SHA256SUMS", checksums)
        archive.writestr("local-fix-manifest.json", json.dumps({"from": "0.4.7", "local_to": "0.4.8",
            "server_exe_unchanged": True, "requires_complete_baseline": True,
            "files": hashes}, ensure_ascii=False, indent=2))
        archive.writestr("更新说明.txt", (new / "docs/19-本地输出分辨率实时应用.md").read_text(encoding="utf-8"))
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        for name, expected in hashes.items():
            assert hashlib.sha256(archive.read("RemoteGpu/" + name)).hexdigest() == expected
    result = {"path": str(destination), "size_mb": round(destination.stat().st_size / 1024**2, 2),
              "overlay_verified": True, "changed_files": len(changed), "sha256": sha(destination)}
    (root / "reports/local-fix-048.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
