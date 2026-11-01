from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass
from fractions import Fraction

import av
import cv2
import numpy as np
from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError

from .config import VideoSettings

log = logging.getLogger(__name__)
_camera_enumeration_lock = threading.Lock()


class LatestQueue:
    """One decoded frame only. May be published by a device capture thread."""
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=1)
        self.dropped = 0
        self.closed = False
        self._gate = threading.Lock()
        self._pending = None
        self._scheduled = False

    def put(self, item):
        if self.closed:
            return
        if self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
        self.queue.put_nowait(item)

    def from_thread(self, item):
        with self._gate:
            if self.closed:
                return
            if self._scheduled:
                self.dropped += 1
            self._pending = item
            if not self._scheduled:
                self._scheduled = True
                try:
                    self.loop.call_soon_threadsafe(self._deliver)
                except RuntimeError:
                    self._scheduled = False

    def _deliver(self):
        with self._gate:
            item = self._pending
            self._pending = None
            self._scheduled = False
        self.put(item)

    def close(self):
        self.put(MediaStreamError())
        self.closed = True

    async def get(self, timeout=15):
        item = await asyncio.wait_for(self.queue.get(), timeout)
        if isinstance(item, BaseException):
            raise item
        return item


def fit_bgr(image: np.ndarray, width: int, height: int) -> np.ndarray:
    sh, sw = image.shape[:2]
    if sw == width and sh == height:
        return np.ascontiguousarray(image)
    scale = min(width / sw, height / sh)
    dw, dh = max(1, round(sw * scale)), max(1, round(sh * scale))
    resized = cv2.resize(image, (dw, dh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    result = np.zeros((height, width, 3), np.uint8)
    x, y = (width - dw) // 2, (height - dh) // 2
    result[y:y + dh, x:x + dw] = resized
    return result


@dataclass(frozen=True)
class CameraDevice:
    name: str
    id: str


def list_cameras() -> list[CameraDevice]:
    with _camera_enumeration_lock:
        return _list_cameras()


def _list_cameras() -> list[CameraDevice]:
    # FFmpeg's DirectShow enumeration prints metadata without opening video.
    previous = av.logging.get_level()
    if previous is None:
        previous = av.logging.ERROR
    try:
        av.logging.set_level(av.logging.INFO)
        with av.logging.Capture(local=True) as entries:
            try:
                av.open("dummy", format="dshow", options={"list_devices": "true"})
            except av.FFmpegError:
                pass  # Listing intentionally exits with an error.
        # FFmpeg emits the name, media type and newline as separate log calls.
        return parse_devices("".join(text for _, component, text in entries if component == "dshow"))
    finally:
        av.logging.set_level(previous)


def parse_devices(text):
    result = []
    video = False
    for line in text.splitlines():
        if "DirectShow video devices" in line:
            video = True
        if "DirectShow audio devices" in line:
            video = False
        match = re.search(r'"([^"\r\n]+)"\s*\((video|audio|none)\)', line)
        if match:
            video = match[2] == "video"
            if video:
                result.append(CameraDevice(match[1], match[1]))
        alternative = re.search(r'Alternative name "([^"\r\n]+)"', line)
        if alternative and video and result:
            result[-1] = CameraDevice(result[-1].name, alternative[1])
    return result


def select_camera(value: str, devices=None) -> CameraDevice:
    devices = list_cameras() if devices is None else devices
    if value.lower() == "yy":
        matches = [d for d in devices if "yyanchorvcam" in d.id.lower()]
    else:
        matches = [d for d in devices if d.id == value]
        if not matches:
            matches = [d for d in devices if d.name == value]
    if len(matches) != 1:
        raise ValueError(f"摄像头匹配到 {len(matches)} 个设备。请刷新设备列表并选择唯一的名称或设备 ID。")
    return matches[0]


class CameraTrack(VideoStreamTrack):
    """DirectShow device, owned entirely by its capture thread."""
    def __init__(self, device: str, settings: VideoSettings):
        super().__init__()
        self.frames = LatestQueue()
        self.settings = settings
        self.device = device
        self.exit = threading.Event()
        self.count = 0
        self.input_width = self.input_height = 0
        self.input_format = ""
        self.thread = threading.Thread(target=self._capture, daemon=True, name="Camera capture")
        self.thread.start()

    def _capture(self):
        try:
            options = {"video_size": f"{self.settings.width}x{self.settings.height}",
                       "framerate": str(self.settings.fps), "rtbufsize": "16M"}
            with av.open(f"video={self.device}", format="dshow", options=options, timeout=(10, 5)) as container:
                start = time.monotonic()
                previous = -1
                for frame in container.decode(video=0):
                    if self.exit.is_set():
                        break
                    if frame.width * frame.height > 3840 * 2160:
                        raise ValueError("采集设备返回的画面超过 4K。")
                    self.input_width, self.input_height = frame.width, frame.height
                    self.input_format = frame.format.name
                    image = fit_bgr(frame.to_ndarray(format="bgr24"), self.settings.width, self.settings.height)
                    output = av.VideoFrame.from_ndarray(image, format="bgr24")
                    output.pts = max(previous + 1, int((time.monotonic() - start) * 90_000))
                    output.time_base = Fraction(1, 90_000)
                    previous = output.pts
                    self.frames.from_thread(output)
        except Exception as ex:
            if not self.exit.is_set():
                self.frames.from_thread(RuntimeError(f"无法读取摄像头 {self.device}：{ex}"))

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
        await asyncio.to_thread(self.thread.join, 6)
        if self.thread.is_alive():
            log.warning("摄像头驱动尚未退出读取；再次连接前如设备被占用，请关闭本程序。")


class PatternTrack(VideoStreamTrack):
    """Synthetic source for testing without touching a camera."""
    def __init__(self, settings: VideoSettings):
        super().__init__()
        self.settings = settings
        self.start = None
        self.count = 0

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError()
        now = time.monotonic()
        if self.start is None:
            self.start = now
        target = self.count / self.settings.fps
        await asyncio.sleep(max(0, self.start + target - now))
        s = self.settings
        image = np.full((s.height, s.width, 3), (24, 160, 48), np.uint8)
        x = (self.count * 11) % max(1, s.width - 24)
        image[s.height // 2:, x:x + 24] = (240, 240, 240)
        cv2.putText(image, f"REMOTE GPU TEST {self.count}", (12, s.height // 2), cv2.FONT_HERSHEY_SIMPLEX,
                    max(0.45, s.width / 1280), (255, 255, 255), 2)
        cv2.putText(image, time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}",
                    (12, s.height - 20), cv2.FONT_HERSHEY_SIMPLEX, max(0.45, s.width / 1280), (255, 255, 255), 2)
        frame = av.VideoFrame.from_ndarray(image, format="bgr24")
        frame.pts = round(self.count * 90_000 / s.fps)
        frame.time_base = Fraction(1, 90_000)
        self.count += 1
        return frame


class LatestTrack(VideoStreamTrack):
    """Drain incoming decoded video continuously, avoiding relay queue growth."""
    def __init__(self, source):
        super().__init__()
        self.source = source
        self.frames = LatestQueue()
        self.count = 0
        self.pump = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            while True:
                self.frames.put(await self.source.recv())
        except asyncio.CancelledError:
            pass
        except Exception as ex:
            self.frames.put(ex)

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError()
        frame = await self.frames.get()
        self.count += 1
        return frame

    def stop(self):
        super().stop()
        self.pump.cancel()
        self.frames.close()


class FrameSink:
    """Bounded worker for conversions, virtual camera output and preview."""
    def __init__(self, settings: VideoSettings, target="none", preview=None, stale_seconds=1.0):
        self.settings = settings
        self.target = target
        self.preview = preview
        self.stale_seconds = stale_seconds
        self.count = 0
        self.written = 0
        self.dropped = 0
        self.error = None
        self.last_frame = 0.0
        self._latest = None
        self._gate = threading.Lock()
        self.exit = threading.Event()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="Virtual camera output")
        self.thread.start()

    def submit(self, frame):
        if frame.width * frame.height > 1920 * 1080:
            raise ValueError("收到的视频超过本原型的 1080p 限制。")
        with self._gate:
            if self._latest is not None:
                self.dropped += 1
            self._latest = frame
            self.last_frame = time.monotonic()
            self.count += 1

    def _run(self):
        camera = None
        try:
            if self.target.startswith("unity:"):
                from .unity import UnityCamera
                camera = UnityCamera(self.settings.width, self.settings.height, int(self.target.split(":")[1]))
            elif self.target != "none":
                raise ValueError("输出目标必须为 unity:0、unity:1 或 none。")
            self.ready.set()
            black = np.zeros((self.settings.height, self.settings.width, 3), np.uint8)
            image = black
            interval = 1 / self.settings.fps
            while not self.exit.is_set():
                started = time.monotonic()
                with self._gate:
                    frame, self._latest = self._latest, None
                    age = started - self.last_frame
                if frame is not None:
                    image = fit_bgr(frame.to_ndarray(format="bgr24"), self.settings.width, self.settings.height)
                elif age > self.stale_seconds:
                    image = black
                if camera:
                    self.written += bool(camera.send(image))
                if self.preview:
                    self.preview(image)
                self.exit.wait(max(0, interval - (time.monotonic() - started)))
        except Exception as ex:
            self.error = ex
            log.exception("视频输出停止")
        finally:
            self.ready.set()
            if camera:
                try:
                    camera.send(np.zeros((self.settings.height, self.settings.width, 3), np.uint8))
                finally:
                    camera.close()

    async def wait_ready(self):
        if not await asyncio.to_thread(self.ready.wait, 5):
            raise TimeoutError("初始化虚拟摄像头超时。")
        if self.error:
            raise self.error

    async def close(self):
        self.exit.set()
        await asyncio.to_thread(self.thread.join, 5)
