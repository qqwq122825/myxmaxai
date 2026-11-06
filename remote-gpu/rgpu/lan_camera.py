"""One fixed LAN camera, independent of all FaceLive and video-test devices."""
import ctypes as ct
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .lan import data_directory, run_elevated
from .unity import UnityCamera

CAMERA_NAME = "xxx3"
MEMORY_PREFIX = "FaceLiveLan"
SETTINGS_KEY = r"Software\FaceLive\LanCamera"
CLSID64 = "{7BE4E5C1-BEF4-48E5-A8AE-8E56C8100010}"
CLSID32 = "{7BE4E5C1-BEF4-48E5-A8AE-8E56C8100020}"


def driver_directory():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "LanCameraDriver"
    return Path(__file__).resolve().parents[2] / "vcam/Build/LanCameraDriver"


def driver_installed():
    import winreg
    for bits, clsid, view in ((64, CLSID64, winreg.KEY_WOW64_64KEY), (32, CLSID32, winreg.KEY_WOW64_32KEY)):
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"CLSID\\{clsid}\\InprocServer32", 0, winreg.KEY_READ | view) as key:
                installed = Path(winreg.QueryValueEx(key, "")[0])
            bundled = driver_directory() / f"LanCaptureFilter{bits}.dll"
            if not installed.is_file() or not bundled.is_file():
                return False
            if hashlib.sha256(installed.read_bytes()).digest() != hashlib.sha256(bundled.read_bytes()).digest():
                return False
            category = r"CLSID\{860BB310-5D01-11D0-BD3B-00A0C911CE86}\Instance"
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, category + "\\" + clsid, 0, winreg.KEY_READ | view) as key:
                if winreg.QueryValueEx(key, "FriendlyName")[0] != CAMERA_NAME:
                    return False
        except OSError:
            return False
    return True


def install_driver():
    """Called by an elevated helper on first start. Registers only xxx3."""
    report = data_directory() / "reports/camera-install.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    result = {"camera": CAMERA_NAME, "status": "starting", "registered": [], "error": None}
    try:
        if not ct.windll.shell32.IsUserAnAdmin():
            raise PermissionError("首次启动 xxx3 需要管理员授权注册摄像头。")
        files = [driver_directory() / f"LanCaptureFilter{bits}.dll" for bits in (64, 32)]
        digests = [hashlib.sha256(path.read_bytes()).hexdigest() for path in files]
        version = hashlib.sha256("".join(digests).encode("ascii")).hexdigest()[:16]
        # A new DLL gets a new stable folder, so updates never overwrite an in-use DLL.
        target = Path(os.environ["ProgramData"]) / "FaceLive/LanCamera" / version
        target.mkdir(parents=True, exist_ok=True)
        result["target"] = str(target)
        system = Path(os.environ["SystemRoot"])
        for source, digest, folder in zip(files, digests, ("System32", "SysWOW64")):
            destination = target / source.name
            if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                shutil.copy2(source, destination)
            completed = subprocess.run([str(system / folder / "regsvr32.exe"), "/s", str(destination)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30)
            result["registered"].append({"dll": str(destination), "sha256": digest, "exit_code": completed.returncode})
            if completed.returncode:
                raise RuntimeError(f"{source.name} 注册失败（{completed.returncode}）。")
        if not driver_installed():
            raise RuntimeError("xxx3 注册后的设备名／文件校验未通过。")
        result["status"] = "ok"
    except Exception as ex:
        result["status"], result["error"] = "failed", str(ex)
        raise
    finally:
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_installed():
    if driver_installed():
        return
    if ct.windll.shell32.IsUserAnAdmin():
        install_driver()
    else:
        arguments = ["--install-lan-camera"]
        if not getattr(sys, "frozen", False):
            arguments.insert(0, str(Path(__file__).resolve().parents[1] / "lan_receiver.py"))
        run_elevated(sys.executable, arguments)
    if not driver_installed():
        raise RuntimeError("xxx3 未完成注册，请查看 %LOCALAPPDATA%\\FaceLiveLan\\reports\\camera-install.json。")


def open_camera(width, height, channel=0):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY) as key:
        winreg.SetValueEx(key, "Width", 0, winreg.REG_DWORD, width)
        winreg.SetValueEx(key, "Height", 0, winreg.REG_DWORD, height)
    return UnityCamera(width, height, 0, prefix=MEMORY_PREFIX, set_resolution=False)
