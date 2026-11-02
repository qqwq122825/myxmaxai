"""Read FaceLive Camera #2 the way a face-swap program would.

The server writes this virtual camera for other software. This checker only opens and
displays it, so it can run beside the server and show whether that input actually works.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
import multiprocessing as mp
import sys
import threading
import time
import tkinter as tk

import cv2
import numpy as np

TARGET_NAME = "FaceLive Camera #2"


def launched_as_checker() -> bool:
    stem = Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]).stem
    return stem.casefold() in {"vcamcheck", "checkvcam"} or ("检查" in stem and "虚拟摄像头" in stem)


def frame_kind(frame) -> str:
    """Classify a BGR frame: empty, black, placeholder (driver waiting), or picture."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return "empty"
    if float(frame.mean()) < 12:
        return "black"
    pixels = frame[::max(1, frame.shape[0] // 120), ::max(1, frame.shape[1] // 160)].reshape(-1, 3).astype(np.float32)
    blue, green, red = pixels.mean(axis=0)
    red_spread = float(pixels[:, 2].std())
    if green > 200 and blue < 30 and red_spread > 40:
        return "placeholder"
    if blue > 200 and green < 30 and red_spread > 40:
        return "placeholder"
    if green > 200 and red < 40 and blue < 40:
        return "placeholder"
    return "picture"


def describe(device_names, opened, frame, fps, moving) -> tuple[str, str]:
    """Return (ok|warn|fail, message) for whether other programs can use Camera #2."""
    listed = TARGET_NAME in device_names
    shown = "、".join(device_names) if device_names else "（没有列出任何摄像头）"
    if not listed:
        return "fail", (
            f"没有找到 {TARGET_NAME}。当前能看到的设备：{shown}。\n"
            "请在服务器上以管理员运行 deploy\\install-driver.ps1，然后点击「启动输入虚拟摄像头并自检」。"
        )
    if not opened:
        return "fail", (
            f"系统已经列出 {TARGET_NAME}，但这个检查程序打不开它。\n"
            f"当前设备：{shown}"
        )
    if frame is None:
        return "fail", f"{TARGET_NAME} 能打开，但读不到任何画面。"
    height, width = frame.shape[:2]
    kind = frame_kind(frame)
    rate = "—" if fps is None else f"{fps:.0f} FPS"
    if kind == "black":
        return "warn", (
            f"其他程序已经打开 {TARGET_NAME}（{width}×{height}，{rate}），但现在是黑画面。\n"
            "摄像头设备可用。服务器端请确认已启动输入虚拟摄像头，并且已经收到要送进去的画面。"
        )
    if kind == "placeholder":
        return "warn", (
            f"其他程序已经打开 {TARGET_NAME}（{width}×{height}），看到的是驱动自己的等待画面。\n"
            "画面疑似驱动等待图，请确认服务器已开启输入虚拟摄像头，并在收到原画后重试。"
        )
    motion = "画面在变化" if moving else "画面暂时没有变化"
    return "ok", (
        f"成功：本检查程序通过 DirectShow 读到了 {TARGET_NAME}，{width}×{height}，{rate}，{motion}。\n"
        "请对照原画确认内容；换脸软件的输入选择这一路，兼容性仍需在该软件中确认。"
    )


def open_target(devices):
    """Use the exact DirectShow device ID; indices differ between capture backends."""
    import av
    from .media import select_camera
    device = select_camera(TARGET_NAME, devices)
    return av.open(f"video={device.id}", format="dshow", options={"rtbufsize": "16M"}, timeout=(8, 5))


def _capture_worker(connection):
    """Keep potentially blocking native driver calls outside the UI process."""
    capture = None
    try:
        from .media import list_cameras
        devices = list_cameras()
        names = [device.name for device in devices]
        connection.send(("devices", names))
        if TARGET_NAME not in names:
            connection.send(("error", describe(names, False, None, None, False)[1]))
            return
        capture = open_target(devices)
        connection.send(("opened", True))
        stamps = deque(maxlen=45)
        previous = None
        motion = deque(maxlen=12)
        for video in capture.decode(video=0):
            if video.width * video.height > 3840 * 2160:
                raise ValueError("摄像头返回的画面超过 4K。")
            frame = video.to_ndarray(format="bgr24")
            now = time.monotonic()
            stamps.append(now)
            fps = (len(stamps) - 1) / (now - stamps[0]) if len(stamps) > 1 and now > stamps[0] else None
            sample = cv2.resize(frame, (80, 60))
            if previous is not None:
                motion.append(float(cv2.absdiff(previous, sample).mean()))
            previous = sample
            moving = bool(motion) and sum(motion) / len(motion) > 2
            level, message = describe(names, True, frame, fps, moving)
            # Only a small preview crosses the process boundary; the verdict uses the original size.
            connection.send(("frame", (_fit(frame, 800, 430), level, message)))
        raise RuntimeError("摄像头视频流已结束。")
    except Exception as ex:
        try:
            connection.send(("error", f"读取 {TARGET_NAME} 失败：{ex}"))
        except (OSError, EOFError):
            pass
    finally:
        if capture is not None:
            capture.close()
        connection.close()


class _Reader(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="Virtual camera checker")
        self.stop = threading.Event()
        self.restart = threading.Event()
        self.lock = threading.Lock()
        self.frame = None
        self.level = "info"
        self.message = f"正在查找 {TARGET_NAME} ……"
        self.devices = ""

    def run(self):
        while not self.stop.is_set():
            self.restart.clear()
            self._publish(None, "info", f"正在查找 {TARGET_NAME} ……", [])
            self._read_session()
            # Keep the failure visible until the operator explicitly retries.
            while not self.stop.is_set() and not self.restart.wait(.1):
                pass

    def _read_session(self):
        context = mp.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=_capture_worker, args=(send,), daemon=True)
        names = []
        last = time.monotonic()
        try:
            process.start()
            send.close()
            while not self.stop.is_set() and not self.restart.is_set():
                if receive.poll(.1):
                    kind, value = receive.recv()
                    last = time.monotonic()
                    if kind == "devices":
                        names = value
                    elif kind == "opened":
                        self._publish(None, "info", f"已打开 {TARGET_NAME}，等待读取画面……", names)
                    elif kind == "frame":
                        frame, level, message = value
                        self._publish(frame, level, message, names)
                    elif kind == "error":
                        self._publish(None, "fail", value, names)
                        return
                elif not process.is_alive():
                    raise RuntimeError("摄像头读取进程已退出。")
                elif time.monotonic() - last > 8:
                    raise TimeoutError("8 秒内没有收到画面，驱动可能无响应。可点「重新检查」。")
        except Exception as ex:
            if not self.stop.is_set() and not self.restart.is_set():
                self._publish(None, "fail", f"无法读取 {TARGET_NAME}：{ex}", names)
        finally:
            receive.close()
            send.close()
            if process.pid is not None:
                process.join(.2)
                if process.is_alive():
                    process.terminate()
                    process.join(2)
                if not process.is_alive():
                    process.close()

    def _publish(self, frame, level, message, names):
        shown = "、".join(names) if names else "（没有列出任何摄像头）"
        with self.lock:
            self.frame = None if frame is None else frame.copy()
            self.level = level
            self.message = message
            self.devices = shown


class CheckerWindow(tk.Tk):
    def __init__(self, start_reader=True):
        super().__init__()
        self.title("检查虚拟摄像头 · FaceLive Camera #2")
        self.geometry("860x700")
        self.minsize(720, 560)
        self.configure(bg="#f3f5f9")
        self.option_add("*Font", "{Microsoft YaHei UI} 10")
        self.reader = _Reader()
        self.photo = None
        wrap = tk.Frame(self, bg="#f3f5f9", padx=16, pady=14)
        wrap.pack(fill="both", expand=True)
        tk.Label(wrap, text="模拟换脸软件，只读取服务器的输入虚拟摄像头", bg="#f3f5f9", fg="#7d8ba1").pack(anchor="w")
        self.status = tk.Label(wrap, text="", justify="left", anchor="w", bg="#ffffff", fg="#25354b",
                               padx=12, pady=10, wraplength=800)
        self.status.pack(fill="x", pady=(8, 8))
        self.preview = tk.Label(wrap, bg="#10141c")
        self.preview.pack(fill="both", expand=True)
        self.devices = tk.Label(wrap, text="", bg="#f3f5f9", fg="#7d8ba1", anchor="w")
        self.devices.pack(fill="x", pady=(8, 0))
        row = tk.Frame(wrap, bg="#f3f5f9")
        row.pack(fill="x", pady=(10, 0))
        tk.Button(row, text="重新检查", command=self.reader.restart.set).pack(side="left")
        tk.Button(row, text="关闭", command=self.destroy).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        if start_reader:
            self.reader.start()
        self.after(60, self._refresh)

    def destroy(self):
        self.reader.stop.set()
        if self.reader.is_alive():
            self.reader.join(3)
        super().destroy()

    def _refresh(self):
        if not self.winfo_exists():
            return
        with self.reader.lock:
            frame = None if self.reader.frame is None else self.reader.frame
            level, message, devices = self.reader.level, self.reader.message, self.reader.devices
        colors = {"ok": "#1f8a4c", "warn": "#9a6b12", "fail": "#c0392b", "info": "#25354b"}
        self.status.configure(text=message, fg=colors.get(level, "#25354b"))
        self.devices.configure(text=f"系统当前摄像头：{devices}" if devices else "系统当前摄像头：正在列举……")
        if frame is not None:
            shown = _fit(frame, 800, 430)
            ok, encoded = cv2.imencode(".png", shown)
            if ok:
                import base64
                self.photo = tk.PhotoImage(data=base64.b64encode(encoded.tobytes()))
                self.preview.configure(image=self.photo)
        elif self.photo is not None:
            self.preview.configure(image="")
            self.photo = None
        self.after(60, self._refresh)


def _fit(frame, width, height):
    scale = min(width / frame.shape[1], height / frame.shape[0], 1)
    if scale >= 1:
        return frame
    return cv2.resize(frame, (max(1, int(frame.shape[1] * scale)), max(1, int(frame.shape[0] * scale))))


def launch_checker(smoke_report=None):
    window = CheckerWindow()
    if smoke_report:
        window.withdraw()
        from rgpu.doctor import save_report
        deadline = time.monotonic() + 12
        try:
            while window.reader.level == "info" and time.monotonic() < deadline:
                window.update()
                time.sleep(.02)
            with window.reader.lock:
                result = {"ok": window.reader.level != "info", "title": window.title(),
                          "camera_read_ok": window.reader.level == "ok", "level": window.reader.level,
                          "message": window.reader.message, "devices": window.reader.devices}
        finally:
            window.destroy()
        result["reader_stopped"] = not window.reader.is_alive()
        save_report(smoke_report, result)
        return
    window.mainloop()
