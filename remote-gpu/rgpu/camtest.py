"""Virtual camera self-test: can programs on this machine actually open FaceLive Camera #2?

The server writes a known test picture into the virtual camera, then reads it back the same
ways face-swap software does (FFmpeg/DirectShow, OpenCV DirectShow, OpenCV Media Foundation).
The result says where the chain breaks: driver not registered, not listed, opened but no
picture, picture upside-down / mirrored, or only invisible to Media Foundation programs.

Test picture (as the camera should deliver it):  top 25% green band; below it, the left half
red and the right half blue.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
import threading
import time

import numpy as np

BASE_CLSID = "{5C2CD55C-92AD-4999-8666-912BD3E700%02X}"


def clsid_for_channel(channel: int) -> str:
    # vcam/UnityCaptureFilter.cpp: device #1 = ...10, device #N (N>1) = ...10 + 1 + (N-1)
    return BASE_CLSID % (0x10 if channel == 0 else 0x10 + 1 + channel)


def test_pattern(width: int, height: int) -> np.ndarray:
    """BGR test picture."""
    image = np.zeros((height, width, 3), np.uint8)
    band = height // 4
    image[:band] = (0, 220, 0)                     # green band on top
    image[band:, : width // 2] = (0, 0, 230)       # red left
    image[band:, width // 2:] = (230, 0, 0)        # blue right
    return image


def _mean(region: np.ndarray):
    b, g, r = region.reshape(-1, 3).mean(axis=0)
    return r, g, b


def classify(image: np.ndarray) -> str:
    """Compare a received BGR frame with the test picture.

    Returns "ok", "upside_down", "mirrored", "rotated" (upside-down and mirrored), "black" or "other".
    """
    if image is None or image.size == 0:
        return "other"
    h, w = image.shape[:2]
    if image.mean() < 12:
        return "black"
    def is_green(region):
        r, g, b = _mean(region)
        return g > 120 and r < 90 and b < 90
    def is_red(region):
        r, g, b = _mean(region)
        return r > 120 and g < 90 and b < 90
    def is_blue(region):
        r, g, b = _mean(region)
        return b > 120 and r < 90 and g < 90
    # Sample well inside each area so scaling / letterboxing at the edges does not matter.
    top = image[int(h * .05): int(h * .18), int(w * .3): int(w * .7)]
    bottom = image[int(h * .82): int(h * .95), int(w * .3): int(w * .7)]
    upper_left = image[int(h * .35): int(h * .6), int(w * .1): int(w * .4)]
    upper_right = image[int(h * .35): int(h * .6), int(w * .6): int(w * .9)]
    lower_left = image[int(h * .4): int(h * .65), int(w * .1): int(w * .4)]
    lower_right = image[int(h * .4): int(h * .65), int(w * .6): int(w * .9)]
    if is_green(top):
        if is_red(upper_left) and is_blue(upper_right):
            return "ok"
        if is_blue(upper_left) and is_red(upper_right):
            return "mirrored"
    if is_green(bottom):
        if is_red(lower_left) and is_blue(lower_right):
            return "upside_down"
        if is_blue(lower_left) and is_red(lower_right):
            return "rotated"
    return "other"


ORIENTATION_TEXT = {
    "ok": "画面方向正确",
    "upside_down": "画面上下颠倒",
    "mirrored": "画面左右镜像",
    "rotated": "画面旋转了 180°",
    "black": "只读到黑画面（写入端没有接上）",
    "other": "读到的不是测试画面（可能是驱动的提示画面，说明写入端没有接上）",
}


@dataclass
class Check:
    name: str
    ok: bool | None          # None = informational
    detail: str
    hint: str = ""


@dataclass
class SelfTest:
    channel: int
    device_name: str = ""
    checks: list = field(default_factory=list)
    verdict: str = ""
    ok: bool = False

    def add(self, name, ok, detail, hint=""):
        self.checks.append(Check(name, ok, detail, hint))
        return ok

    def as_dict(self):
        return asdict(self)

    def text(self):
        lines = [f"虚拟摄像头自检（通道 {self.channel}{'：' + self.device_name if self.device_name else ''}）"]
        for c in self.checks:
            mark = "✔" if c.ok else ("✘" if c.ok is False else "•")
            lines.append(f"{mark} {c.name}：{c.detail}")
            if c.hint:
                lines.append(f"    → {c.hint}")
        lines.append("")
        lines.append("结论：" + self.verdict)
        return "\n".join(lines)


def _registry_name(clsid: str, wow64: bool):
    """Returns (registered, friendly name, dll path, dll exists)."""
    import winreg
    root = r"WOW6432Node\CLSID" if wow64 else "CLSID"
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{root}\\{clsid}") as key:
            name, _ = winreg.QueryValueEx(key, None)
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{root}\\{clsid}\\InprocServer32") as key:
            path, _ = winreg.QueryValueEx(key, None)
        return True, name, path, Path(path).exists()
    except OSError:
        return False, "", "", False


class _PatternWriter:
    """Keeps writing the test picture into the virtual camera while readers try to open it."""
    def __init__(self, width, height, channel):
        from .unity import UnityCamera
        self.camera = UnityCamera(width, height, channel)   # also sets the offered resolution
        self.image = test_pattern(width, height)
        self.written = 0
        self.exit = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="Camera self-test writer")
        self.thread.start()

    def _run(self):
        while not self.exit.is_set():
            try:
                self.written += bool(self.camera.send(self.image))
            except Exception:
                pass
            self.exit.wait(1 / 30)

    def close(self):
        self.exit.set()
        self.thread.join(2)
        self.camera.close()


def _read_ffmpeg_dshow(name, width, height, seconds):
    import av
    deadline = time.monotonic() + seconds
    last = None
    options = {"video_size": f"{width}x{height}", "rtbufsize": "16M"}
    with av.open(f"video={name}", format="dshow", options=options, timeout=(10, 5)) as container:
        for frame in container.decode(video=0):
            last = frame.to_ndarray(format="bgr24")
            if classify(last) not in ("black", "other") or time.monotonic() > deadline:
                break
    return last


def _read_opencv(index, backend, seconds):
    import cv2
    capture = cv2.VideoCapture(index, backend)
    try:
        if not capture.isOpened():
            return None, False
        deadline = time.monotonic() + seconds
        last = None
        while time.monotonic() < deadline:
            ok, frame = capture.read()
            if ok and frame is not None:
                last = frame
                if classify(frame) not in ("black", "other"):
                    break
            else:
                time.sleep(.05)
        return last, True
    finally:
        capture.release()


def virtual_camera_selftest(channel: int = 1, width: int = 640, height: int = 360, seconds: float = 5) -> SelfTest:
    result = SelfTest(channel)
    if os.name != "nt":
        result.verdict = "只能在 Windows 上检测虚拟摄像头。"
        return result

    # 1. Driver registration (64-bit and 32-bit programs look in different places).
    clsid = clsid_for_channel(channel)
    reg64 = _registry_name(clsid, wow64=False)
    reg32 = _registry_name(clsid, wow64=True)
    registered = result.add("驱动注册（64 位程序）", reg64[0] and reg64[3],
        f"{reg64[1]} → {reg64[2]}" if reg64[0] else "未注册",
        "" if reg64[0] and reg64[3] else "以管理员身份运行 deploy\\install-driver.ps1（会注册本地输出和 Camera #2 两个设备）。")
    result.add("驱动注册（32 位程序）", reg32[0] and reg32[3], f"{reg32[1]} → {reg32[2]}" if reg32[0] else "未注册",
               "" if reg32[0] and reg32[3] else "只影响 32 位换脸软件；安装脚本会同时注册 32 位。")
    if not registered:
        result.verdict = "虚拟摄像头驱动没有安装，换脸软件不可能看到 Camera #2。请先以管理员身份运行 deploy\\install-driver.ps1，再重新自检。"
        return result
    name = reg64[1]
    result.device_name = name

    # 2. Does DirectShow enumeration list it?
    from .media import list_cameras
    try:
        cameras = list_cameras()
        names = [c.name for c in cameras]
    except Exception as ex:
        cameras, names = [], []
        result.add("系统摄像头列表", False, f"列举失败：{ex}")
    listed = result.add("系统摄像头列表（DirectShow）", name in names,
        "、".join(names) if names else "一个摄像头都没有列出",
        "" if name in names else "驱动已注册但系统列不出：请重新运行安装脚本并重启服务器；确认 VirtualCam 下 DLL 未被杀毒软件删除。")
    index = names.index(name) if name in names else -1

    # 3. Media Foundation (some programs read cameras only through it).
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    mf_files = {f: (system32 / f).exists() for f in ("mfplat.dll", "mfreadwrite.dll", "mf.dll")}
    has_mf = all(mf_files.values())
    result.add("Media Foundation 组件", has_mf if has_mf else None,
               "齐全" if has_mf else "缺少 " + "、".join(f for f, ok in mf_files.items() if not ok),
               "" if has_mf else "Windows Server 需以管理员运行：Install-WindowsFeature Server-Media-Foundation，然后重启。")
    if not listed:
        result.verdict = "驱动已注册，但系统列不出这个摄像头。按上面的提示重新安装驱动并重启服务器。"
        return result

    # 4-6. Write the test picture and read it back three ways.
    try:
        writer = _PatternWriter(width, height, channel)
    except Exception as ex:
        result.add("写入测试画面", False, str(ex),
                   "如果提示已有程序在写入：先在服务器端点“停止服务”，再自检。")
        result.verdict = "无法向虚拟摄像头写入测试画面，见上方原因。"
        return result
    try:
        try:
            frame = _read_ffmpeg_dshow(name, width, height, seconds)
            kind = classify(frame)
            ffmpeg_ok = kind == "ok"
            result.add("DirectShow 读取（FFmpeg，本程序和大多数直播软件的方式）", ffmpeg_ok,
                       ORIENTATION_TEXT[kind] + f"（写入 {writer.written} 帧）")
        except Exception as ex:
            ffmpeg_ok, kind = False, "error"
            result.add("DirectShow 读取（FFmpeg）", False, f"打不开：{ex}")

        try:
            import cv2
            frame, opened = _read_opencv(index, cv2.CAP_DSHOW, seconds)
            ckind = classify(frame) if opened else "error"
            cv_dshow_ok = ckind == "ok"
            result.add("OpenCV DirectShow 读取（Python 换脸软件常用）", cv_dshow_ok,
                       ORIENTATION_TEXT.get(ckind, "打不开") if opened else f"按序号 {index} 打不开")
        except Exception as ex:
            cv_dshow_ok = False
            result.add("OpenCV DirectShow 读取", False, f"出错：{ex}")

        cv_msmf_ok = False
        try:
            import cv2
            found = False
            for i in range(0, max(len(cameras), 1) + 2):
                frame, opened = _read_opencv(i, cv2.CAP_MSMF, 2)
                if opened and classify(frame) == "ok":
                    found = True
                    break
            cv_msmf_ok = found
            result.add("Media Foundation 读取（只支持新式摄像头的软件）", found if found else None,
                       "能读到测试画面" if found else "看不到本虚拟摄像头",
                       "" if found else "这是 DirectShow 虚拟摄像头的正常限制；只用 Media Foundation 的软件（如 Windows 相机）看不到它。")
        except Exception as ex:
            result.add("Media Foundation 读取", None, f"无法检测：{ex}")
    finally:
        writer.close()

    if ffmpeg_ok and cv_dshow_ok:
        result.ok = True
        result.verdict = ("虚拟摄像头工作正常：用 DirectShow / OpenCV 读取都能拿到正确方向的测试画面。"
                          "如果换脸软件仍看不到或打不开 Camera #2，问题在换脸软件这一侧"
                          + ("（它可能只用 Media Foundation 读摄像头，或需要先在软件里刷新设备 / 重启软件）。" if not cv_msmf_ok
                             else "（请在软件里刷新设备或重启软件，并确认选的是 Camera #2）。"))
    elif ffmpeg_ok or cv_dshow_ok:
        result.ok = True
        result.verdict = "虚拟摄像头可以被读取，但不同读取方式结果不一致，见上方各项；换脸软件请优先选择 DirectShow 方式。"
    elif kind in ("upside_down", "mirrored", "rotated"):
        result.verdict = f"虚拟摄像头能读到画面，但{ORIENTATION_TEXT[kind]}。请把这份结果发给开发者修正方向。"
    else:
        result.verdict = "驱动已注册且能被列出，但读不到测试画面。请把这份结果发给开发者。"
    return result
