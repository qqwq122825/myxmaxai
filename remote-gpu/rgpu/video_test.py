"""Local file playback and an isolated DirectShow camera for the standalone tester."""
from __future__ import annotations

import contextlib
import ctypes as ct
from ctypes import wintypes as wt
import hashlib
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import cv2
import numpy as np

from .unity import UnityCamera

CAMERA_NAME = "Sony ZV-E10"
MEMORY_PREFIX = "FaceLiveVideoTest"
SETTINGS_KEY = r"Software\FaceLive\VideoTest"
CLSID64 = "{2C64A7E9-8531-4F79-9CC1-A32267080010}"
CLSID32 = "{2C64A7E9-8531-4F79-9CC1-A32267080020}"


def driver_directory():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "VideoTestDriver"
    return Path(__file__).resolve().parents[2] / "vcam" / "Build" / "VideoTestDriver"


def driver_installed():
    import winreg
    for clsid, view in ((CLSID64, winreg.KEY_WOW64_64KEY), (CLSID32, winreg.KEY_WOW64_32KEY)):
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"CLSID\\{clsid}\\InprocServer32",
                                0, winreg.KEY_READ | view) as key:
                if not Path(winreg.QueryValueEx(key, "")[0]).is_file():
                    return False
            category = "CLSID\\{860BB310-5D01-11D0-BD3B-00A0C911CE86}\\Instance\\"
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, category + clsid,
                                0, winreg.KEY_READ | view) as key:
                if winreg.QueryValueEx(key, "FriendlyName")[0] != CAMERA_NAME:
                    return False
        except OSError:
            return False
    return True


def install_driver():
    """Only registers this tester's separate CLSIDs and persistent driver files."""
    if not ct.windll.shell32.IsUserAnAdmin():
        raise PermissionError("安装摄像头需要管理员权限。")
    target = Path(os.environ["ProgramData"]) / "FaceLive" / "VideoTest"
    target.mkdir(parents=True, exist_ok=True)
    system = Path(os.environ["SystemRoot"])
    for bits, directory in ((64, "System32"), (32, "SysWOW64")):
        name = f"VideoTestFilter{bits}.dll"
        source, destination = driver_directory() / name, target / name
        same = destination.is_file() and hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(destination.read_bytes()).digest()
        if not same:
            shutil.copy2(source, destination)
        result = subprocess.run([str(system / directory / "regsvr32.exe"), "/s", str(destination)],
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        if result.returncode:
            raise RuntimeError(f"{bits} 位摄像头驱动注册失败（{result.returncode}）。")
    if not driver_installed():
        raise RuntimeError("摄像头注册未通过检查。")


def install_elevated():
    """Elevate this executable, wait in a worker thread, and detect cancellation."""
    if ct.windll.shell32.IsUserAnAdmin():
        install_driver()
        return

    class SHELLEXECUTEINFO(ct.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("fMask", ct.c_ulong), ("hwnd", wt.HWND),
                    ("lpVerb", wt.LPCWSTR), ("lpFile", wt.LPCWSTR), ("lpParameters", wt.LPCWSTR),
                    ("lpDirectory", wt.LPCWSTR), ("nShow", ct.c_int), ("hInstApp", wt.HINSTANCE),
                    ("lpIDList", ct.c_void_p), ("lpClass", wt.LPCWSTR), ("hkeyClass", wt.HKEY),
                    ("dwHotKey", wt.DWORD), ("hIcon", wt.HANDLE), ("hProcess", wt.HANDLE)]

    command = ["--install-driver"]
    if not getattr(sys, "frozen", False):
        command.insert(0, str(Path(__file__).resolve().parents[1] / "video_camera_test.py"))
    info = SHELLEXECUTEINFO()
    info.cbSize, info.fMask = ct.sizeof(info), 0x40  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb, info.lpFile = "runas", sys.executable
    info.lpParameters, info.nShow = subprocess.list2cmdline(command), 0
    shell = ct.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteExW.argtypes, shell.ShellExecuteExW.restype = [ct.POINTER(SHELLEXECUTEINFO)], wt.BOOL
    if not shell.ShellExecuteExW(ct.byref(info)):
        error = ct.get_last_error()
        if error == 1223:
            raise RuntimeError("已取消安装摄像头；可再次点击启动。")
        raise ct.WinError(error)
    k = ct.WinDLL("kernel32", use_last_error=True)
    k.WaitForSingleObject.argtypes, k.WaitForSingleObject.restype = [wt.HANDLE, wt.DWORD], wt.DWORD
    k.GetExitCodeProcess.argtypes = [wt.HANDLE, ct.POINTER(wt.DWORD)]
    k.CloseHandle.argtypes = [wt.HANDLE]
    try:
        if k.WaitForSingleObject(info.hProcess, 120_000) != 0:
            raise RuntimeError("摄像头安装等待超时，请稍后重新启动。")
        code = wt.DWORD()
        if not k.GetExitCodeProcess(info.hProcess, ct.byref(code)) or code.value or not driver_installed():
            raise RuntimeError("摄像头安装失败，请查看 %LOCALAPPDATA%\\FaceLiveVideoTest\\install-error.txt。")
    finally:
        k.CloseHandle(info.hProcess)


def fit_frame(image, width, height):
    h, w = image.shape[:2]
    scale = min(width / w, height / h)
    dw, dh = max(1, round(w * scale)), max(1, round(h * scale))
    resized = cv2.resize(image, (dw, dh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    result = np.zeros((height, width, 3), np.uint8)
    x, y = (width - dw) // 2, (height - dh) // 2
    result[y:y + dh, x:x + dw] = resized
    return result


class LoopingVideo:
    def __init__(self, path):
        self.path = str(path)
        self.capture = cv2.VideoCapture(self.path)
        self.loops = 0
        if not self.capture.isOpened():
            self.close()
            raise ValueError("无法打开视频，请选择有效的 MP4、MOV、MKV 或 AVI 文件。")
        fps = self.capture.get(cv2.CAP_PROP_FPS)
        self.fps = fps if math.isfinite(fps) and 1 <= fps <= 240 else 30.0

    def read(self):
        ok, image = self.capture.read()
        if not ok:
            # Reopen if the decoder cannot seek (some containers do not have an index).
            self.capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, image = self.capture.read()
            if not ok:
                self.capture.release()
                self.capture = cv2.VideoCapture(self.path)
                ok, image = self.capture.read()
            if not ok:
                raise ValueError("视频没有可解码的画面，或素材文件已不可用。")
            self.loops += 1
        return image

    def close(self):
        self.capture.release()


def open_camera(width, height):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY) as key:
        winreg.SetValueEx(key, "Width", 0, winreg.REG_DWORD, width)
        winreg.SetValueEx(key, "Height", 0, winreg.REG_DWORD, height)
    return UnityCamera(width, height, prefix=MEMORY_PREFIX, set_resolution=False)


class VideoPlayback:
    def __init__(self, path, width, height, frame=lambda _: None, status=lambda _: None,
                 camera_factory=open_camera):
        self.path, self.width, self.height = path, width, height
        self.frame, self.status, self.camera_factory = frame, status, camera_factory
        self.stop_event = threading.Event()
        self.frames, self.written, self.loops = 0, 0, 0
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True, name="Video file virtual camera")

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _run(self):
        source = camera = None
        try:
            source = LoopingVideo(self.path)
            image = source.read()  # Validate the file before acquiring the camera channel.
            camera = self.camera_factory(self.width, self.height)
            next_tick, report_at = time.monotonic(), 0
            while not self.stop_event.is_set():
                output = fit_frame(image, self.width, self.height)
                connected = camera.send(output)
                self.written += bool(connected)
                self.frames += 1
                self.loops = source.loops
                self.frame(output)
                now = time.monotonic()
                if now >= report_at:
                    detail = "正在输出" if connected else "已启动，等待其他软件打开摄像头"
                    self.status(f"{detail} · {CAMERA_NAME} · 已循环 {self.loops} 次")
                    report_at = now + .5
                next_tick += 1 / source.fps
                # Do not burst all overdue frames when the machine stalls.
                next_tick = max(next_tick, time.monotonic())
                if self.stop_event.wait(max(0, next_tick - time.monotonic())):
                    break
                image = source.read()
        except Exception as ex:
            self.error = str(ex)
            self.status("输出失败：" + self.error)
        finally:
            if camera:
                with contextlib.suppress(Exception):
                    camera.send(np.zeros((self.height, self.width, 3), np.uint8))
                camera.close()
            if source:
                source.close()
