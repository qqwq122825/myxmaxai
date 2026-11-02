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
    guide = "09-地址与单Key连接.md" if (current / "docs" / "09-地址与单Key连接.md").exists() else "08-独立双端与窗口采集.md"
    driver_note = ""
    if version == "0.4.4":
        guide = "15-直播伴侣与镜像和30帧.md"
        driver_note = ("本地安装的旧摄像头驱动需更新：关闭直播伴侣、旧桥接端和摄像头检查器，\n"
                       "以管理员在 RemoteGpu 文件夹执行 powershell -ExecutionPolicy Bypass -File .\\deploy\\install-driver.ps1。\n"
                       "然后重开桥接、开启虚拟摄像头；直播伴侣重新选择 FaceLive Camera，同一场景仅保留一路。\n"
                       "服务器若已安装 0.4.3 摄像头补丁，无需重复注册相同驱动。\n")
    if version == "0.4.5":
        guide = "16-独立摄像头按钮与滚动条.md"
        driver_note = "本次主要更新界面；已正常工作的虚拟摄像头不需要重新注册驱动。\n"
    if version == "0.4.6":
        guide = "17-1080p双端升级.md"
        driver_note = ("1080p 需要服务器与本地端同时更新至 0.4.6；手机可继续使用已有的 1080p 选项。\n"
                       "电脑只接收模式的画面尺寸也需选择 1080P，才能通过虚拟摄像头输出完整尺寸。\n"
                       "已安装 0.4.3 驱动补丁无需重复注册；服务器若仍是原版 0.4.2 驱动且自检要求更新，\n"
                       "请关闭换脸软件，以管理员运行 deploy\\install-driver.ps1 后重试。\n")
    if version == "0.4.7":
        guide = "18-服务器驱动自检与安装报告.md"
        driver_note = ("服务器不再因驱动文件哈希不同而阻止写入，请先启动输入虚拟摄像头并做实际读回自检。\n"
                       "需要安装驱动时，先关闭摄像头相关程序，右键 deploy\\install-driver.cmd 以管理员运行；\n"
                       "安装报告保存在 LOCALAPPDATA\\FaceLiveRemoteGpu\\reports\\driver-install-*.json。\n")
    if version == "0.4.8":
        guide = "19-本地输出分辨率实时应用.md"
        driver_note = ("本地只接收模式改选 1080P 会应用到虚拟摄像头；服务器 0.4.7 不必同步升级。\n"
                       "请重开直播伴侣以刷新已经保留的摄像头尺寸，本次不要求重新安装驱动。\n")
    firewall_note = ("服务器需重新运行 deploy\\开放直连端口.ps1，使入站规则指向新版服务器 EXE。\n"
                     if baseline in ("0.1.1", "0.2.0") else "现有入站规则路径未变化，平台公网端口仍需保持开放。\n")
    with zipfile.ZipFile(patch_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path in sorted(changed.items()):
            archive.write(path, "RemoteGpu/" + name)
        archive.write(current / "SHA256SUMS", "RemoteGpu/SHA256SUMS")
        archive.writestr("update-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("更新说明.txt", (
            f"仅适用于已有完整 {baseline} 的 RemoteGpu 文件夹。\n"
            "先关闭旧程序，把压缩包里的 RemoteGpu 文件夹合并覆盖到原 RemoteGpu，保留原有 _internal 目录中的其余文件。\n"
            f"更新到 {version} 后，本地启动 RemoteGpuLocal.exe，服务器启动 RemoteGpuServer.exe。\n"
            "这是两个独立界面，配置分别保存。" + firewall_note +
            driver_note +
            f"如果旧目录不完整或不是指定基线，请用完整发布包。详细步骤见 docs\\{guide}。\n"))
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
