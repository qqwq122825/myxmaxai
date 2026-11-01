"""Local-only camera validation: no network, token, GPU or virtual output."""
from __future__ import annotations

import asyncio
import time

from .media import CameraTrack, FrameSink, PatternTrack


async def preview_local(device, settings, stop, preview=None, status=lambda _: None,
                        statistics=None, seconds=None, on_frame=None):
    source = PatternTrack(settings) if device == "test" else CameraTrack(device, settings)
    sink = FrameSink(settings, target="none", preview=preview)
    count = 0
    first_at = last_at = previous_tick = None
    previous_count = 0
    width = height = 0
    try:
        await sink.wait_ready()
        status("正在本地读取摄像头；不连接服务器、不上传、不写虚拟摄像头。")
        while not stop.is_set():
            frame = await asyncio.wait_for(source.recv(), timeout=12)
            now = time.monotonic()
            if first_at is None:
                first_at = previous_tick = now
            last_at = now
            count += 1
            width, height = frame.width, frame.height
            sink.submit(frame)
            if on_frame:
                on_frame(frame)
            if sink.error:
                raise sink.error
            elapsed = now - first_at
            if statistics and now - previous_tick >= 1:
                statistics({"local_preview": True, "fps": (count - previous_count) / (now - previous_tick),
                            "frames": count, "width": width, "height": height,
                            "native_width": getattr(source, "input_width", width),
                            "native_height": getattr(source, "input_height", height)})
                previous_tick, previous_count = now, count
            if seconds is not None and elapsed >= seconds:
                break
        duration = (last_at - first_at) if first_at is not None and last_at is not None else 0
        return {"ok": count > 0, "frames": count, "seconds": round(duration, 3),
                "fps": round((count - 1) / duration, 2) if count > 1 and duration > 0 else 0,
                "width": width, "height": height,
                "native_width": getattr(source, "input_width", width),
                "native_height": getattr(source, "input_height", height),
                "native_pixel_format": getattr(source, "input_format", "generated"),
                "network_used": False, "virtual_camera_written": False}
    finally:
        source.stop()
        await sink.close()
        if isinstance(source, CameraTrack):
            await source.wait_closed()
