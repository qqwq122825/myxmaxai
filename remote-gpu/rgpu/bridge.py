"""A listening server whose capture/return can be started and stopped independently."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict, dataclass
import time

import aiohttp
from aiohttp import web

from .config import VideoSettings
from .direct import (Counters, DirectAgent, ReceivedTrack, ReceiveClock, SendChannel,
                     parse_control, send_video, wait_for_streams)
from .direct_codec import H264Decoder, MAX_MESSAGE
from .media import CameraTrack, FrameSink, PatternTrack
from .window_capture import WindowDevice, WindowTrack


@dataclass(frozen=True)
class ResultSource:
    kind: str
    device: str = ""
    window: WindowDevice | None = None
    method: str = "wgc"
    crop: tuple = (0, 0, 0, 0)


class ResultCapture:
    def __init__(self, spec, settings, preview, failed):
        self.spec, self.settings, self.failed = spec, settings, failed
        if spec.kind == "window":
            if spec.window is None:
                raise ValueError("请先选择结果窗口。")
            self.source = WindowTrack(spec.window, settings, spec.method, spec.crop)
        elif spec.kind == "camera":
            self.source = CameraTrack(spec.device, settings)
        elif spec.kind == "test":
            self.source = PatternTrack(settings)
        elif spec.kind == "echo":
            self.source = ReceivedTrack()
        else:
            raise ValueError("未知结果来源。")
        self.frames = ReceivedTrack()
        self.preview = FrameSink(settings, "none", preview)
        self.ready, self.error = False, None
        self.count = 0
        self.task = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            await self.preview.wait_ready()
            while True:
                frame = await self.source.recv()
                self.frames.frames.put(frame)
                self.preview.submit(frame)
                self.count += 1
                self.ready = True
        except asyncio.CancelledError:
            pass
        except Exception as ex:
            self.error = ex
            self.ready = False
            self.failed(str(ex))
        finally:
            self.source.stop()
            if hasattr(self.source, "wait_closed"):
                await self.source.wait_closed()

    async def wait_ready(self):
        deadline = time.monotonic() + 12
        while not self.ready:
            if self.error:
                raise self.error
            if time.monotonic() >= deadline:
                raise TimeoutError("结果来源没有返回首帧，请检查所选窗口或摄像头。")
            await asyncio.sleep(0.05)

    def feed_input(self, frame):
        if self.spec.kind == "echo":
            self.source.frames.put(frame)

    async def close(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.source.stop()
        self.frames.stop()
        await self.preview.close()
        if hasattr(self.source, "wait_closed"):
            await self.source.wait_closed()


class ManagedAgent(DirectAgent):
    def __init__(self, identity, status=lambda _: None, preview=None, result_preview=None,
                 statistics=None, on_settings=None):
        super().__init__(identity, status=status, preview=preview)
        self.mode = "managed"
        self.result_preview, self.statistics, self.on_settings = result_preview, statistics, on_settings
        self.settings = VideoSettings()
        self.capture = None
        self.return_enabled = False
        self.return_changed = asyncio.Event()
        self.capture_lock = asyncio.Lock()
        self.receiving = False

    async def health(self, request):
        response = await super().health(request)
        # Existing protocol framing, with an optional independently controlled return stream.
        data = __import__('json').loads(response.text)
        data["features"] = ["independent-return", "window-capture"]
        return web.json_response(data)

    def capture_failed(self, error):
        self.return_enabled = False
        self.return_changed.set()
        self.status(f"结果采集已停止；原画接收可继续：{error}")

    async def start_capture(self, spec, settings=None):
        async with self.capture_lock:
            await self._stop_capture()
            settings = settings or self.settings
            self.capture = ResultCapture(spec, settings, self.result_preview, self.capture_failed)
            try:
                await self.capture.wait_ready()
            except BaseException:
                await self._stop_capture()
                raise
            self.status("结果采集预览已开启；点击“开始回传”才发送结果。")

    async def _stop_capture(self):
        self.return_enabled = False
        self.return_changed.set()
        capture, self.capture = self.capture, None
        if capture:
            await capture.close()

    async def stop_capture(self):
        async with self.capture_lock:
            await self._stop_capture()
        self.status("结果采集和回传已停止；服务器接收保持当前状态。")

    async def set_return(self, enabled):
        if enabled:
            if not self.busy or not self.receiving:
                raise ValueError("请先启动接收并等待本地画面接入。")
            if not self.capture or not self.capture.ready or self.capture.error:
                raise ValueError("请先选择结果来源，并成功预览结果。")
            if self.capture.settings != self.settings:
                raise ValueError("输入尺寸已变化，请重新预览结果后回传。")
        self.return_enabled = bool(enabled)
        self.return_changed.set()
        self.status("结果回传已开启。" if enabled else "结果回传已停止；原画接收继续。")

    async def _return_loop(self, channel, settings, counters):
        origin = time.monotonic()
        while True:
            self.return_changed.clear()
            capture = self.capture
            active = bool(self.return_enabled and capture and capture.ready and not capture.error)
            await channel.send({"type": "return_state", "active": active})
            if not active:
                await self.return_changed.wait()
                continue
            sender = asyncio.create_task(send_video(capture.frames, channel, settings, counters, time_origin=origin))
            changed = asyncio.create_task(self.return_changed.wait())
            try:
                done, _ = await asyncio.wait([sender, changed], return_when=asyncio.FIRST_COMPLETED)
                if sender in done:
                    try:
                        await sender
                    except (ConnectionError, TimeoutError):
                        raise
                    except Exception as ex:
                        self.capture_failed(str(ex))
            finally:
                sender.cancel()
                changed.cancel()
                await asyncio.gather(sender, changed, return_exceptions=True)

    async def stream(self, request):
        async with self.lock:
            if self.busy:
                return web.json_response({"error": "已有本地端连接，请先停止旧连接。"}, status=409)
            self.busy = True
        ws = web.WebSocketResponse(heartbeat=5, receive_timeout=20, max_msg_size=MAX_MESSAGE,
                                   writer_limit=65536, compress=False)
        tasks, sink = [], None
        try:
            await ws.prepare(request)
            self.ws = ws
            first = await asyncio.wait_for(ws.receive(), 10)
            if first.type != aiohttp.WSMsgType.TEXT:
                raise ValueError("缺少视频会话参数。")
            hello = parse_control(first.data)
            if hello.get("type") != "start" or hello.get("protocol") != "direct-1":
                raise ValueError("两端协议不兼容，请使用同一版本。")
            settings = VideoSettings(**hello["video"])
            if self.capture and self.capture.settings != settings:
                await self.stop_capture()
                self.status("本地端改变了视频规格，请重新预览结果后回传。")
            self.settings = settings
            if self.on_settings:
                self.on_settings(settings)
            channel, counters = SendChannel(ws), Counters(last_video=time.monotonic())
            sink = FrameSink(settings, "none", self.preview)
            await sink.wait_ready()
            decoder, clock = H264Decoder(settings), ReceiveClock()
            await channel.send({"type": "ready", "protocol": "direct-1", "mode": "managed",
                                "return_active": False, "video": asdict(settings)})
            self.return_enabled = False
            self.status("已连接本地端，等待原画；结果回传需单独开启。")
            async def receive():
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        counters.received_bytes += len(message.data)
                        for frame in await asyncio.to_thread(decoder.decode, message.data):
                            clock.check(frame)
                            counters.received_frames += 1
                            counters.last_video = time.monotonic()
                            self.receiving = True
                            sink.submit(frame)
                            if self.capture:
                                self.capture.feed_input(frame)
                    elif message.type == aiohttp.WSMsgType.TEXT:
                        data = parse_control(message.data)
                        if data.get("type") == "ping" and type(data.get("time")) in (float, int):
                            await channel.send({"type": "pong", "time": data["time"],
                                                "return_active": self.return_enabled, "mode": self.mode})
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise ConnectionError("视频通道异常。")
            async def report():
                previous_time = time.monotonic()
                previous_input = previous_output = previous_sent = previous_received = 0
                while True:
                    await asyncio.sleep(1)
                    now = time.monotonic()
                    if now - counters.last_video > 15:
                        raise TimeoutError("长时间未收到本地上传视频。")
                    if sink.error:
                        raise sink.error
                    dt = max(.001, now - previous_time)
                    if self.statistics:
                        self.statistics({"input_fps": (counters.received_frames - previous_input) / dt,
                            "return_fps": (counters.sent_frames - previous_output) / dt,
                            "upload_mbps": (counters.sent_bytes - previous_sent) * 8 / dt / 1e6,
                            "download_mbps": (counters.received_bytes - previous_received) * 8 / dt / 1e6,
                            "return_active": self.return_enabled, "rtt_ms": None})
                    previous_time, previous_input, previous_output = now, counters.received_frames, counters.sent_frames
                    previous_sent, previous_received = counters.sent_bytes, counters.received_bytes
            tasks = [asyncio.create_task(receive()), asyncio.create_task(report()),
                     asyncio.create_task(self._return_loop(channel, settings, counters))]
            await wait_for_streams(tasks)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            self.status(f"视频连接停止：{ex}")
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.send_json({"type": "error", "message": str(ex)}), 1)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if sink:
                await sink.close()
            with contextlib.suppress(Exception):
                await ws.close()
            self.return_enabled = self.receiving = self.busy = False
            self.ws = None
            self.status("本地端已断开；服务器等待新连接。")
        return ws
