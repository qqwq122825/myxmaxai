"""Capture one explicitly selected HWND. Native capture is isolated in a child process."""
from __future__ import annotations

import asyncio
import ctypes as ct
from ctypes import wintypes as wt
from dataclasses import dataclass
from fractions import Fraction
import multiprocessing as mp
from multiprocessing import shared_memory
import os
import queue
import threading
import time

import av
import numpy as np
from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError

from .media import LatestQueue, fit_bgr


def user32():
    if os.name != "nt":
        raise RuntimeError("窗口采集仅支持 Windows。")
    u = ct.WinDLL("user32", use_last_error=True)
    for name, result, args in (
        ("IsWindow", wt.BOOL, [wt.HWND]), ("IsWindowVisible", wt.BOOL, [wt.HWND]),
        ("IsIconic", wt.BOOL, [wt.HWND]),
        ("GetWindowThreadProcessId", wt.DWORD, [wt.HWND, ct.POINTER(wt.DWORD)]),
        ("GetWindowTextLengthW", ct.c_int, [wt.HWND]),
        ("GetWindowTextW", ct.c_int, [wt.HWND, wt.LPWSTR, ct.c_int]),
        ("GetWindowRect", wt.BOOL, [wt.HWND, ct.POINTER(wt.RECT)]),
        ("GetClientRect", wt.BOOL, [wt.HWND, ct.POINTER(wt.RECT)]),
        ("GetDC", wt.HDC, [wt.HWND]), ("ReleaseDC", ct.c_int, [wt.HWND, wt.HDC]),
        ("PrintWindow", wt.BOOL, [wt.HWND, wt.HDC, wt.UINT]),
    ):
        f = getattr(u, name)
        f.restype, f.argtypes = result, args
    return u


@dataclass(frozen=True)
class WindowDevice:
    hwnd: int
    pid: int
    title: str
    minimized: bool = False

    @property
    def label(self):
        return f"{self.title}  [PID {self.pid} / {self.hwnd:X}]" + (" · 已最小化" if self.minimized else "")


def list_windows():
    u, result = user32(), []
    callback_type = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def visit(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        length = u.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title = ct.create_unicode_buffer(min(length, 512) + 1)
        u.GetWindowTextW(hwnd, title, len(title))
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ct.byref(pid))
        if pid.value != os.getpid() and title.value.strip():
            result.append(WindowDevice(int(hwnd), pid.value, title.value, bool(u.IsIconic(hwnd))))
        return True
    callback = callback_type(visit)
    u.EnumWindows.argtypes, u.EnumWindows.restype = [callback_type, wt.LPARAM], wt.BOOL
    if not u.EnumWindows(callback, 0):
        raise ct.WinError(ct.get_last_error())
    return sorted(result, key=lambda item: item.title.casefold())


def validate_window(window):
    u = user32()
    pid = wt.DWORD()
    u.GetWindowThreadProcessId(window.hwnd, ct.byref(pid))
    if not u.IsWindow(window.hwnd) or pid.value != window.pid:
        raise RuntimeError("所选结果窗口已关闭或被替换，请刷新并重新选择。")
    if u.IsIconic(window.hwnd):
        raise RuntimeError("所选结果窗口已最小化。请恢复窗口后重新预览；不会改为采集整个桌面。")


def crop_image(image, crop):
    left, top, right, bottom = crop
    h, w = image.shape[:2]
    if min(crop) < 0 or left + right >= w or top + bottom >= h:
        raise ValueError("裁切边距超出窗口画面，请减小左、上、右、下的裁切像素。")
    return image[top:h - bottom, left:w - right]


class GdiCapture:
    """Compatibility path; PrintWindow never falls back to desktop BitBlt."""
    def __init__(self, hwnd):
        self.hwnd, self.u = hwnd, user32()
        self.g = ct.WinDLL("gdi32", use_last_error=True)
        for name, result, args in (
            ("CreateCompatibleDC", wt.HDC, [wt.HDC]),
            ("CreateCompatibleBitmap", wt.HBITMAP, [wt.HDC, ct.c_int, ct.c_int]),
            ("SelectObject", wt.HANDLE, [wt.HDC, wt.HANDLE]),
            ("DeleteObject", wt.BOOL, [wt.HANDLE]), ("DeleteDC", wt.BOOL, [wt.HDC]),
            ("GetDIBits", ct.c_int, [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ct.c_void_p, ct.c_void_p, wt.UINT]),
        ):
            f = getattr(self.g, name)
            f.restype, f.argtypes = result, args

    def grab(self):
        rect = wt.RECT()
        if not self.u.GetClientRect(self.hwnd, ct.byref(rect)):
            raise RuntimeError("无法获取结果窗口画面区域。")
        w, h = rect.right, rect.bottom
        if not (1 <= w <= 7680 and 1 <= h <= 4320):
            raise ValueError("窗口尺寸无效或过大。")
        dc = self.u.GetDC(self.hwnd)
        mem = bmp = old = None
        try:
            mem = self.g.CreateCompatibleDC(dc)
            bmp = self.g.CreateCompatibleBitmap(dc, w, h)
            if not dc or not mem or not bmp:
                raise RuntimeError("窗口采集无法分配图像资源。")
            old = self.g.SelectObject(mem, bmp)
            if not self.u.PrintWindow(self.hwnd, mem, 3):  # CLIENTONLY | RENDERFULLCONTENT
                raise RuntimeError("该窗口不支持兼容采集，请切换 Windows 图形采集。")
            self.g.SelectObject(mem, old)
            old = None
            # BITMAPINFOHEADER + one spare color entry, top-down BGRA pixels.
            header = (ct.c_int32 * 11)()
            header[0:6] = (40, w, -h, (32 << 16) | 1, 0, w * h * 4)
            pixels = np.empty((h, w, 4), np.uint8)
            if self.g.GetDIBits(mem, bmp, 0, h, pixels.ctypes.data, ct.byref(header), 0) != h:
                raise RuntimeError("无法读取窗口图像。")
            return pixels[:, :, :3]
        finally:
            if old:
                self.g.SelectObject(mem, old)
            if bmp:
                self.g.DeleteObject(bmp)
            if mem:
                self.g.DeleteDC(mem)
            if dc:
                self.u.ReleaseDC(self.hwnd, dc)


def _capture_worker(window, settings, method, crop, output, errors, stop):
    """A stuck PrintWindow / native DLL can be terminated without hanging the GUI."""
    control = memory = None
    try:
        name, frame_lock, frame_ready = output
        memory = shared_memory.SharedMemory(name=name)
        shared_image = np.ndarray((settings.height, settings.width, 3), np.uint8, buffer=memory.buf)
        validate_window(window)
        cached = [None, None]
        def publish(image):
            if image is not cached[0]:
                cached[:] = image, fit_bgr(crop_image(image, crop), settings.width, settings.height)
            if frame_lock.acquire(timeout=.05):
                try:
                    shared_image[:] = cached[1]
                    frame_ready.set()
                finally:
                    frame_lock.release()
        if method == "gdi":
            capture = GdiCapture(window.hwnd)
            while not stop.is_set():
                tick = time.monotonic()
                validate_window(window)
                publish(capture.grab())
                stop.wait(max(0, 1 / settings.fps - (time.monotonic() - tick)))
        else:
            from windows_capture import WindowsCapture
            latest = [None]
            image_lock = threading.Lock()
            capture = WindowsCapture(window_hwnd=window.hwnd, cursor_capture=False, draw_border=None,
                                     minimum_update_interval=None)
            @capture.event
            def on_frame_arrived(frame, capture_control):
                if stop.is_set():
                    capture_control.stop()
                    return
                # Keep a bounded copy: static windows do not generate continuous WGC events.
                with image_lock:
                    latest[0] = frame.frame_buffer[:, :, :3].copy()
            @capture.event
            def on_closed():
                if not stop.is_set():
                    errors.put_nowait("结果窗口采集已结束，请重新选择窗口并预览。")
            control = capture.start_free_threaded()
            while not stop.is_set():
                tick = time.monotonic()
                validate_window(window)
                if control.is_finished():
                    raise RuntimeError("图形采集已结束，请检查窗口和当前远程桌面会话。")
                with image_lock:
                    image = latest[0]
                if image is not None:
                    publish(image)
                stop.wait(max(0, 1 / settings.fps - (time.monotonic() - tick)))
    except Exception as ex:
        with __import__('contextlib').suppress(queue.Full):
            errors.put_nowait(str(ex))
    finally:
        if control:
            control.stop()
        if memory:
            memory.close()
        errors.cancel_join_thread()


class WindowTrack(VideoStreamTrack):
    def __init__(self, window, settings, method="wgc", crop=(0, 0, 0, 0)):
        super().__init__()
        if method not in ("wgc", "gdi"):
            raise ValueError("未知窗口采集方式。")
        validate_window(window)
        self.frames = LatestQueue()
        self.settings, self.started, self.count = settings, time.monotonic(), 0
        context = mp.get_context("spawn")
        self.memory = shared_memory.SharedMemory(create=True, size=settings.width * settings.height * 3)
        self.shared_image = np.ndarray((settings.height, settings.width, 3), np.uint8, buffer=self.memory.buf)
        self.frame_lock, self.frame_ready = context.Lock(), context.Event()
        self.errors = context.Queue(1)
        self.exit, self.finished = context.Event(), threading.Event()
        self._closed = False
        self.process = context.Process(target=_capture_worker, args=(window, settings, method, crop,
                         (self.memory.name, self.frame_lock, self.frame_ready), self.errors, self.exit), daemon=True)
        try:
            self.process.start()
        except BaseException:
            self.memory.close()
            self.memory.unlink()
            raise
        self.thread = threading.Thread(target=self._read, daemon=True, name="Window frame reader")
        self.thread.start()

    def _read(self):
        last = time.monotonic()
        previous = -1
        try:
            while not self.exit.is_set():
                try:
                    error = self.errors.get_nowait()
                except queue.Empty:
                    error = None
                if error:
                    raise RuntimeError(error)
                if not self.frame_ready.wait(.2):
                    if not self.process.is_alive() or time.monotonic() - last > 8:
                        raise RuntimeError("窗口没有返回画面。请保持窗口非最小化；可尝试兼容采集或结果虚拟摄像头。")
                    continue
                if not self.frame_lock.acquire(timeout=.2):
                    continue
                try:
                    image = self.shared_image.copy()
                    self.frame_ready.clear()
                finally:
                    self.frame_lock.release()
                last = time.monotonic()
                frame = av.VideoFrame.from_ndarray(image, format="bgr24")
                frame.pts = max(previous + 1, int((last - self.started) * 90_000))
                frame.time_base = Fraction(1, 90_000)
                previous = frame.pts
                self.frames.from_thread(frame)
        except Exception as ex:
            self.frames.from_thread(ex)
        finally:
            self.finished.set()

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError()
        frame = await self.frames.get()
        self.count += 1
        return frame

    def stop(self):
        super().stop()
        self.exit.set()
        self.frames.close()

    async def wait_closed(self):
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self.process.join, 2)
        if self.process.is_alive():
            self.process.terminate()
            await asyncio.to_thread(self.process.join, 2)
        await asyncio.to_thread(self.thread.join, 1)
        self.errors.cancel_join_thread()
        self.errors.close()
        self.memory.close()
        self.memory.unlink()
        self.process.close()
