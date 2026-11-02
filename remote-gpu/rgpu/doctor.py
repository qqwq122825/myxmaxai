from __future__ import annotations

import json
import hashlib
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from . import __version__
from .media import list_cameras


def input_driver_update_required():
    """An EXE overlay does not replace the registered DLL in ProgramData."""
    if platform.system() != "Windows":
        return False
    import winreg
    bundle = (Path(sys.executable).parent / "VirtualCam" if getattr(sys, "frozen", False)
              else Path(__file__).resolve().parents[2] / "app" / "VirtualCam") / "UnityCaptureFilter64.dll"
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"CLSID\{5C2CD55C-92AD-4999-8666-912BD3E70012}\InprocServer32") as key:
            installed = Path(winreg.QueryValueEx(key, None)[0])
        if not bundle.is_file() or not installed.is_file():
            return False  # Missing drivers are diagnosed by the reader, rather than called an update.
        def digest(path):
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").digest()
        return digest(bundle) != digest(installed)
    except OSError:
        return False


def run_command(arguments):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"ok": result.returncode == 0, "output": (result.stdout if result.returncode == 0 else result.stderr).strip()}
    except Exception as ex:
        return {"ok": False, "output": str(ex)}


def probe_encoder(name):
    try:
        codec = av.CodecContext.create(name, "w")
        codec.width, codec.height = 320, 240
        codec.pix_fmt = "yuv420p"
        codec.time_base = Fraction(1, 30)
        if name == "h264_nvenc":
            codec.options = {"preset": "p1", "tune": "ull"}
        else:
            codec.options = {"tune": "zerolatency"}
        frame = av.VideoFrame.from_ndarray(np.zeros((240, 320, 3), np.uint8), format="bgr24")
        frame.pts, frame.time_base = 0, Fraction(1, 30)
        packets = list(codec.encode(frame)) + list(codec.encode(None))
        return {"ok": bool(packets), "detail": f"{len(packets)} encoded packet(s)"}
    except Exception as ex:
        return {"ok": False, "detail": str(ex)}


def report(probe_nvenc=False):
    disk = shutil.disk_usage(Path.cwd())
    data = {"app_version": __version__, "platform": platform.platform(),
            "python": platform.python_version(), "pyav": av.__version__,
            "working_directory": str(Path.cwd()), "free_disk_gb": round(disk.free / 1e9, 1),
            "gpu": run_command(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"]),
            "tailscale": run_command(["tailscale", "ip", "-4"]),
            "h264_cpu": probe_encoder("libx264"),
            "nvenc_codec_present": "h264_nvenc" in av.codecs_available,
            "media_encoder_used": "libx264 (CPU)",
            "notes": ["NVENC 检测成功不代表本原型已启用 NVENC。", "Windows Server 2022 的软件、驱动和断开远程桌面行为仍须实际联调。"]}
    if probe_nvenc:
        data["nvenc_probe"] = probe_encoder("h264_nvenc")
    try:
        data["cameras"] = [asdict(device) for device in list_cameras()]
    except Exception as ex:
        data["cameras_error"] = str(ex)
    if platform.system() == "Windows":
        import winreg
        installed = {}
        for name, clsid in (("FaceLive Camera", "{5C2CD55C-92AD-4999-8666-912BD3E70010}"),
                            ("FaceLive Camera #2", "{5C2CD55C-92AD-4999-8666-912BD3E70012}")):
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "CLSID\\" + clsid + "\\InprocServer32") as key:
                    path, _ = winreg.QueryValueEx(key, None)
                    installed[name] = {"path": path, "exists": Path(path).exists()}
            except OSError:
                installed[name] = {"exists": False}
        data["virtual_camera_drivers"] = installed
        data["input_driver_update_required"] = input_driver_update_required()
    return data


def save_report(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def default_bind_address():
    result = run_command(["tailscale", "ip", "-4"])
    if result["ok"]:
        return result["output"].splitlines()[0]
    return "127.0.0.1"
