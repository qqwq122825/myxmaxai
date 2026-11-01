"""Create a full zip and a verified additive overlay for an existing local release."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import zipfile


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for data in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def files(root):
    return {str(p.relative_to(root)).replace("\\", "/"): p for p in root.rglob("*")
            if p.is_file() and p.name != "SHA256SUMS"}


def main():
    project = Path(__file__).resolve().parent.parent
    version, baseline = sys.argv[1:3]
    for value in (version, baseline):
        if any(c not in "0123456789." for c in value):
            raise ValueError("Invalid version")
    current = project / "dist" / version / "RemoteGpu"
    previous = project / "dist" / baseline / "RemoteGpu"
    if not current.is_dir() or not previous.is_dir():
        raise ValueError("Build and baseline folders must exist")
    new, old = files(current), files(previous)
    hashes = {name: sha(path) for name, path in new.items()}
    (current / "SHA256SUMS").write_text("\n".join(f"{hashes[name]}  {name}" for name in sorted(new)) + "\n", encoding="utf-8")
    changed = {name: path for name, path in new.items() if name not in old or sha(old[name]) != hashes[name]}
    # Prove that the additive overlay supplies every differing/new release file.
    for name in new:
        candidate = changed.get(name, old.get(name))
        assert candidate is not None and sha(candidate) == hashes[name], name
    full_zip = project / "dist" / f"RemoteGpu-{version}-win-x64.zip"
    patch_zip = project / "dist" / f"RemoteGpu-{baseline}-to-{version}-update.zip"
    with zipfile.ZipFile(full_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path in sorted(files(current).items()):
            archive.write(path, "RemoteGpu/" + name)
        archive.write(current / "SHA256SUMS", "RemoteGpu/SHA256SUMS")
    manifest = {"from": baseline, "to": version, "mode": "additive-overlay",
                "requires_complete_baseline": True, "files": {name: hashes[name] for name in sorted(changed)}}
    with zipfile.ZipFile(patch_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path in sorted(changed.items()):
            archive.write(path, "RemoteGpu/" + name)
        archive.write(current / "SHA256SUMS", "RemoteGpu/SHA256SUMS")
        archive.writestr("update-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("更新说明.txt", (
            f"仅适用于已有完整 {baseline} 的 RemoteGpu 文件夹。\n"
            "先关闭旧程序，把压缩包里的 RemoteGpu 文件夹合并覆盖到原 RemoteGpu，保留原有 _internal 目录中的其余文件。\n"
            f"更新到 {version} 后，本地启动 RemoteGpuLocal.exe，服务器启动 RemoteGpuServer.exe。\n"
            "这是两个独立界面，配置分别保存。服务器需重新运行 deploy\\开放直连端口.ps1，使入站规则指向新版服务器 EXE。\n"
            "如果旧目录不完整或不是指定基线，请用完整发布包。详细步骤见 docs\\08-独立双端与窗口采集.md。\n"))
    # Validate the archives themselves, not only the source folder.
    for archive_path in (full_zip, patch_zip):
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.testzip() is None
            expected = new if archive_path == full_zip else changed
            for name in expected:
                assert hashlib.sha256(archive.read("RemoteGpu/" + name)).hexdigest() == hashes[name]
    result = {"version": version, "full_zip": str(full_zip), "full_mb": round(full_zip.stat().st_size / 1024**2, 2),
              "patch_zip": str(patch_zip), "patch_mb": round(patch_zip.stat().st_size / 1024**2, 2),
              "changed_files": len(changed), "overlay_verified": True,
              "archive_sha256": {p.name: sha(p) for p in (full_zip, patch_zip)}}
    reports = project / "reports"
    reports.mkdir(exist_ok=True)
    (reports / f"release-{version}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
