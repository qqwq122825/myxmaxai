"""Single-port TLS/TCP video transport. No VPN, account, ICE or relay service."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict, dataclass
import json
import logging
import secrets
import time

import aiohttp
from aiohttp import web
from aiortc import VideoStreamTrack
from aiortc.mediastreams import MediaStreamError

from . import __version__
from .config import VideoSettings, video_limits, check_remote_video_size
from .direct_codec import H264Decoder, H264Encoder, MAX_MESSAGE
from .media import CameraTrack, FrameSink, LatestQueue, LatestTrack, PatternTrack
from .pairing import Pairing

log = logging.getLogger(__name__)
SEND_TIMEOUT = 1.5


@dataclass
class Counters:
    sent_frames: int = 0
    received_frames: int = 0
    sent_bytes: int = 0
    received_bytes: int = 0
    dropped: int = 0
    last_video: float = 0


class ReceivedTrack(VideoStreamTrack):
    def __init__(self):
        super().__init__()
        self.frames = LatestQueue()

    async def recv(self):
        return await self.frames.get()

    def stop(self):
        super().stop()
        self.frames.close()


class SendChannel:
    def __init__(self, ws):
        self.ws = ws
        self.lock = asyncio.Lock()

    async def send(self, value):
        async def write():
            async with self.lock:
                if isinstance(value, bytes):
                    await self.ws.send_bytes(value)
                else:
                    await self.ws.send_json(value)
        try:
            await asyncio.wait_for(write(), SEND_TIMEOUT)
        except TimeoutError:
            raise TimeoutError("视频发送持续阻塞，已停止连接以避免累积延迟。") from None


async def send_video(source, channel, settings, counters, preview_sink=None, time_origin=None):
    latest = LatestTrack(source)
    encoder = H264Encoder(settings)
    try:
        while True:
            frame = await latest.recv()
            if preview_sink:
                preview_sink.submit(frame)
            if time_origin is not None:
                from fractions import Fraction
                frame.pts = int((time.monotonic() - time_origin) * 90_000)
                frame.time_base = Fraction(1, 90_000)
            for packet in await asyncio.to_thread(encoder.encode, frame):
                await channel.send(packet)
                counters.sent_bytes += len(packet)
            counters.sent_frames += 1
            counters.dropped = latest.frames.dropped
    finally:
        latest.stop()
        await asyncio.gather(latest.pump, return_exceptions=True)


class ReceiveClock:
    """Detect growing receive queues using relative stream time, not wall clocks."""
    def __init__(self):
        self.base_time = self.base_pts = None

    def check(self, frame):
        now = time.monotonic()
        pts = float(frame.pts * frame.time_base)
        if self.base_time is None:
            self.base_time, self.base_pts = now, pts
        lag_growth = now - self.base_time - (pts - self.base_pts)
        if lag_growth > 1.5:
            raise TimeoutError("接收队列延迟持续增加，已断开。请降低分辨率或码率后重新连接。")


def parse_control(message):
    if len(message) > 8192:
        raise ValueError("控制消息过大。")
    data = json.loads(message)
    if not isinstance(data, dict):
        raise ValueError("控制消息格式不正确。")
    return data


async def wait_for_streams(tasks, stop=None):
    stop_task = asyncio.create_task(stop.wait()) if stop else None
    all_tasks = tasks + ([stop_task] if stop_task else [])
    try:
        done, _ = await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
        if stop_task and stop_task in done:
            return
        for task in done:
            await task  # Propagate actual codec / media errors.
        raise ConnectionError("对端已结束视频连接。")
    finally:
        for task in all_tasks:
            task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)


class DirectAgent:
    def __init__(self, identity, mode="echo", output_device="", status=log.info, preview=None):
        if mode not in ("echo", "bridge") or (mode == "bridge" and not output_device):
            raise ValueError("桥接模式需要选择换脸软件的结果摄像头。")
        self.identity, self.mode, self.output_device = identity, mode, output_device
        self.status, self.preview = status, preview
        self.busy = False
        self.lock = asyncio.Lock()
        self.ws = None

    def application(self):
        @web.middleware
        async def authorization(request, handler):
            supplied = request.headers.get("Authorization", "").encode("utf-8")
            expected = ("Bearer " + self.identity.key).encode("ascii")
            if not secrets.compare_digest(supplied, expected):
                return web.json_response({"error": "配对密钥不正确。"}, status=401)
            return await handler(request)
        app = web.Application(middlewares=[authorization], client_max_size=8192)
        app.router.add_get("/health", self.health)
        app.router.add_get("/stream", self.stream)
        async def shutdown(_):
            for ws in (self.ws, getattr(self, "viewer_ws", None)):
                if ws is not None:
                    await ws.close(code=1001, message=b"Server stopping")
        app.on_shutdown.append(shutdown)
        return app

    async def health(self, request):
        return web.json_response({"version": __version__, "protocol": "direct-1", "busy": self.busy,
                                  "mode": self.mode, "transport": "TLS/TCP", "encoder": "libx264 (CPU)",
                                  "video_limits": video_limits()})

    async def stream(self, request):
        async with self.lock:
            if self.busy:
                return web.json_response({"error": "已有本地端连接，请先停止旧连接。"}, status=409)
            self.busy = True
        ws = web.WebSocketResponse(heartbeat=5, receive_timeout=20, max_msg_size=MAX_MESSAGE,
                                   writer_limit=65536, compress=False)
        sink = source = incoming = None
        tasks = []
        try:
            await ws.prepare(request)
            self.ws = ws
            first = await asyncio.wait_for(ws.receive(), 10)
            if first.type != aiohttp.WSMsgType.TEXT:
                raise ValueError("缺少视频会话参数。")
            hello = parse_control(first.data)
            if hello.get("type") != "start" or hello.get("protocol") != "direct-1":
                raise ValueError("直连协议不兼容，请使用同一版本的两端程序。")
            settings = VideoSettings(**hello["video"])
            channel, counters = SendChannel(ws), Counters(last_video=time.monotonic())
            incoming = ReceivedTrack()
            if self.mode == "echo":
                source = incoming
            else:
                sink = FrameSink(settings, "unity:1", self.preview)
                await sink.wait_ready()
                source = CameraTrack(self.output_device, settings)
            decoder, clock = H264Decoder(settings), ReceiveClock()
            await channel.send({"type": "ready", "protocol": "direct-1", "mode": self.mode,
                                "video": asdict(settings)})
            self.status("免账号直连已建立，正在接收本地视频。")

            async def receive():
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        counters.received_bytes += len(message.data)
                        for frame in await asyncio.to_thread(decoder.decode, message.data):
                            clock.check(frame)
                            counters.received_frames += 1
                            counters.last_video = time.monotonic()
                            if sink:
                                sink.submit(frame)
                                if sink.error:
                                    raise sink.error
                            else:
                                incoming.frames.put(frame)
                    elif message.type == aiohttp.WSMsgType.TEXT:
                        data = parse_control(message.data)
                        if data.get("type") == "ping" and type(data.get("time")) in (int, float):
                            await channel.send({"type": "pong", "time": data["time"],
                                                "input_frames": counters.received_frames,
                                                "output_frames": counters.sent_frames,
                                                "input_camera_writes": sink.written if sink else 0,
                                                "mode": self.mode})
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise ConnectionError("直连通道异常。")

            async def watchdog():
                while True:
                    await asyncio.sleep(1)
                    if time.monotonic() - counters.last_video > 15:
                        raise TimeoutError("长时间未收到上传视频。")

            tasks = [asyncio.create_task(receive()),
                     asyncio.create_task(send_video(source, channel, settings, counters)),
                     asyncio.create_task(watchdog())]
            await wait_for_streams(tasks)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            self.status(f"直连会话已停止：{ex}")
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.send_json({"type": "error", "message": str(ex)}), 1)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if source:
                source.stop()
            if incoming:
                incoming.stop()
            if sink:
                await sink.close()
            if source is not None and isinstance(source, CameraTrack):
                await source.wait_closed()
            with contextlib.suppress(Exception):
                await ws.close()
            self.ws = None
            self.busy = False
            self.status("免账号远程端待连接。")
        return ws


async def serve_direct(agent, host, port, stop, on_ready=None):
    runner = web.AppRunner(agent.application(), access_log=None, shutdown_timeout=8)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host, port, ssl_context=agent.identity.ssl_context())
        await site.start()
        if on_ready:
            on_ready()
        agent.status(f"免账号远程端已启动，加密 TCP 端口 {port}。")
        await stop.wait()
    finally:
        await runner.cleanup()


async def connect_direct(pairing_code, device, settings, stop, sink_target="unity:0",
                         status=log.info, preview=None, statistics=None, media_timeout=20,
                         input_preview=None, role="both"):
    """role="both": upload and receive the result (classic). role="source": upload only (like the phone app);
    the server forwards the result to a separate receiver started with connect_viewer()."""
    pairing = Pairing.decode(pairing_code) if isinstance(pairing_code, str) else pairing_code
    source = sink = input_sink = None
    counters = Counters(last_video=time.monotonic())
    tasks = []
    rtt_ms, remote = None, {}
    fingerprint = aiohttp.Fingerprint(bytes.fromhex(pairing.fingerprint))
    headers = {"Authorization": "Bearer " + pairing.key}
    timeout = aiohttp.ClientTimeout(total=15, connect=10)
    status(f"正在连接 {pairing.host}:{pairing.port}，验证服务器证书……")
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout, trust_env=False) as http:
            # Validate the server identity and bearer key before opening a camera.
            async with http.get(pairing.endpoint + "/health", ssl=fingerprint, allow_redirects=False) as response:
                if response.status != 200:
                    raise ConnectionError(f"服务器拒绝连接（HTTP {response.status}），请检查配对码或旧连接。")
                health = await response.json()
                if health.get("protocol") != "direct-1":
                    raise ValueError("两端直连协议不兼容。")
                check_remote_video_size(health, settings)
                if health.get("busy"):
                    raise ConnectionError("远程端已有连接，请先停止旧连接。")
            async with http.ws_connect(pairing.endpoint + "/stream", ssl=fingerprint, heartbeat=5,
                                       timeout=aiohttp.ClientWSTimeout(ws_receive=media_timeout, ws_close=3),
                                       max_msg_size=MAX_MESSAGE, compress=0) as ws:
                channel = SendChannel(ws)
                hello = {"type": "start", "protocol": "direct-1", "video": asdict(settings)}
                if role != "both":
                    hello["role"] = role
                await channel.send(hello)
                message = await asyncio.wait_for(ws.receive(), 10)
                if message.type != aiohttp.WSMsgType.TEXT:
                    raise ConnectionError("服务器未返回直连确认。")
                ready = parse_control(message.data)
                if ready.get("type") == "error":
                    raise RuntimeError(ready.get("message", "远程初始化失败。"))
                if ready.get("type") != "ready" or ready.get("protocol") != "direct-1":
                    raise ValueError("直连确认格式不正确。")
                source = PatternTrack(settings) if device == "test" else CameraTrack(device, settings)
                sink = FrameSink(settings, sink_target, preview)
                await sink.wait_ready()
                if input_preview:
                    input_sink = FrameSink(settings, "none", input_preview)
                    await input_sink.wait_ready()
                decoder, clock = H264Decoder(settings), ReceiveClock()
                return_active = ready.get("return_active", True)
                counters.last_video = time.monotonic()
                status(f"免账号直连成功，模式 {ready['mode']}；视频通过加密 TCP 双向传输。")

                async def receive():
                    nonlocal rtt_ms, remote, return_active, decoder, clock
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.BINARY:
                            counters.received_bytes += len(message.data)
                            for frame in await asyncio.to_thread(decoder.decode, message.data):
                                clock.check(frame)
                                sink.submit(frame)
                                counters.received_frames += 1
                                counters.last_video = time.monotonic()
                        elif message.type == aiohttp.WSMsgType.TEXT:
                            data = parse_control(message.data)
                            if data.get("type") == "pong":
                                rtt_ms = max(0, (time.perf_counter() - data["time"]) * 1000)
                                remote = data
                            elif data.get("type") == "error":
                                raise RuntimeError(data.get("message", "远程处理失败。"))
                            elif data.get("type") == "return_state":
                                return_active = bool(data.get("active"))
                                # Each resumed return stream begins with a fresh H.264 keyframe.
                                decoder, clock = H264Decoder(settings), ReceiveClock()
                                counters.last_video = time.monotonic()
                                status("原画推送中；服务器正在回传结果。" if return_active else
                                       "原画推送中；等待服务器采集并回传结果。")
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            raise ConnectionError("直连媒体通道异常。")

                async def report():
                    previous_time = time.monotonic()
                    previous_frames = previous_sent = previous_received = previous_input = 0
                    while True:
                        await asyncio.sleep(1)
                        now = time.monotonic()
                        if return_active and now - counters.last_video > media_timeout:
                            raise TimeoutError("未收到回传视频，请检查远程结果摄像头或降低视频档位。")
                        if sink.error:
                            raise sink.error
                        await channel.send({"type": "ping", "time": time.perf_counter()})
                        elapsed = max(0.001, now - previous_time)
                        if statistics:
                            statistics({"return_fps": (counters.received_frames - previous_frames) / elapsed,
                                        "input_fps": (counters.sent_frames - previous_input) / elapsed,
                                        "return_active": return_active,
                                        "upload_mbps": (counters.sent_bytes - previous_sent) * 8 / elapsed / 1e6,
                                        "download_mbps": (counters.received_bytes - previous_received) * 8 / elapsed / 1e6,
                                        "rtt_ms": rtt_ms, "frames": counters.received_frames,
                                        "camera_writes": sink.written, "dropped": counters.dropped,
                                        "remote": remote, "encoder": "libx264 (CPU)",
                                        "network_path": "server-direct", "transport": "TLS/TCP"})
                        previous_time, previous_frames = now, counters.received_frames
                        previous_sent, previous_received = counters.sent_bytes, counters.received_bytes
                        previous_input = counters.sent_frames

                tasks = [asyncio.create_task(receive()),
                         asyncio.create_task(send_video(source, channel, settings, counters, input_sink)),
                         asyncio.create_task(report())]
                await wait_for_streams(tasks, stop)
                return {"frames": counters.received_frames, "transport": "TLS/TCP"}
    except aiohttp.ServerFingerprintMismatch:
        raise ConnectionError("服务器证书与配对码不一致。请在服务器重新复制配对码。") from None
    except aiohttp.ClientConnectorError:
        raise ConnectionError("无法连接服务器。请检查公网 IP、端口映射、安全组和服务器防火墙。") from None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if source:
            source.stop()
        if sink:
            await sink.close()
        if input_sink:
            await input_sink.close()
        if source is not None and isinstance(source, CameraTrack):
            await source.wait_closed()


async def connect_viewer(pairing_code, stop, sink_target="unity:0", status=log.info, preview=None,
                         statistics=None, media_timeout=20):
    """Relay receiver (the PC): no camera, no upload. A phone pushes to the server; the server forwards
    the processed result here, which goes to the virtual camera for the streaming software."""
    pairing = Pairing.decode(pairing_code) if isinstance(pairing_code, str) else pairing_code
    fingerprint = aiohttp.Fingerprint(bytes.fromhex(pairing.fingerprint))
    headers = {"Authorization": "Bearer " + pairing.key}
    timeout = aiohttp.ClientTimeout(total=15, connect=10)
    counters = Counters(last_video=time.monotonic())
    tasks, sink = [], None
    rtt_ms, remote = None, {}
    status(f"正在连接 {pairing.host}:{pairing.port}（只接收），验证服务器证书……")
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout, trust_env=False) as http:
            async with http.get(pairing.endpoint + "/health", ssl=fingerprint, allow_redirects=False) as response:
                if response.status != 200:
                    raise ConnectionError(f"服务器拒绝连接（HTTP {response.status}），请检查 Key。")
                health = await response.json()
                if health.get("protocol") != "direct-1":
                    raise ValueError("两端直连协议不兼容。")
                if "relay-roles" not in health.get("features", []):
                    raise ValueError("服务器版本过旧，不支持“手机推流、电脑接收”，请先更新服务器程序。")
                if health.get("viewer_busy"):
                    raise ConnectionError("服务器已有接收端连接，请先停止旧连接。")
            async with http.ws_connect(pairing.endpoint + "/stream", ssl=fingerprint, heartbeat=5,
                                       timeout=aiohttp.ClientWSTimeout(ws_receive=media_timeout, ws_close=3),
                                       max_msg_size=MAX_MESSAGE, compress=0) as ws:
                channel = SendChannel(ws)
                await channel.send({"type": "start", "protocol": "direct-1", "role": "viewer"})
                message = await asyncio.wait_for(ws.receive(), 10)
                if message.type != aiohttp.WSMsgType.TEXT:
                    raise ConnectionError("服务器未返回直连确认。")
                ready = parse_control(message.data)
                if ready.get("type") == "error":
                    raise RuntimeError(ready.get("message", "服务器拒绝接收端。"))
                if ready.get("type") != "ready" or ready.get("role") != "viewer":
                    raise ValueError("直连确认格式不正确。")
                status("已连接为接收端；等待手机推流并由服务器开始回传。")

                state = {"active": False, "settings": None, "decoder": None, "clock": None}

                async def apply_return_state(data):
                    nonlocal sink
                    state["active"] = bool(data.get("active"))
                    video = data.get("video")
                    if state["active"] and video:
                        settings = VideoSettings(**video)
                        if settings != state["settings"]:
                            if sink:
                                await sink.close()
                            sink = FrameSink(settings, sink_target, preview)
                            await sink.wait_ready()
                            state["settings"] = settings
                    # Each resumed return stream begins with a fresh H.264 keyframe.
                    if state["settings"]:
                        state["decoder"], state["clock"] = H264Decoder(state["settings"]), ReceiveClock()
                    counters.last_video = time.monotonic()
                    status(f"正在接收服务器转发的结果（{state['settings'].width}×{state['settings'].height}）。"
                           if state["active"] and state["settings"] else "已连接；等待服务器开始回传结果。")

                async def receive():
                    nonlocal rtt_ms, remote
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.BINARY:
                            counters.received_bytes += len(message.data)
                            decoder = state["decoder"]
                            if decoder is None:
                                continue
                            for frame in await asyncio.to_thread(decoder.decode, message.data):
                                state["clock"].check(frame)
                                sink.submit(frame)
                                counters.received_frames += 1
                                counters.last_video = time.monotonic()
                        elif message.type == aiohttp.WSMsgType.TEXT:
                            data = parse_control(message.data)
                            if data.get("type") == "pong" and type(data.get("time")) in (int, float):
                                rtt_ms = max(0, (time.perf_counter() - data["time"]) * 1000)
                                remote = data
                            elif data.get("type") == "return_state":
                                await apply_return_state(data)
                            elif data.get("type") == "error":
                                raise RuntimeError(data.get("message", "服务器处理失败。"))
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            raise ConnectionError("接收通道异常。")

                async def report():
                    previous_time = time.monotonic()
                    previous_frames = previous_received = 0
                    while True:
                        await asyncio.sleep(1)
                        now = time.monotonic()
                        if state["active"] and now - counters.last_video > media_timeout:
                            raise TimeoutError("服务器显示正在回传，但长时间未收到画面；请检查服务器结果来源。")
                        if sink and sink.error:
                            raise sink.error
                        await channel.send({"type": "ping", "time": time.perf_counter()})
                        elapsed = max(0.001, now - previous_time)
                        if statistics:
                            statistics({"return_fps": (counters.received_frames - previous_frames) / elapsed,
                                        "input_fps": None, "upload_mbps": 0.0,
                                        "download_mbps": (counters.received_bytes - previous_received) * 8 / elapsed / 1e6,
                                        "rtt_ms": rtt_ms, "return_active": state["active"],
                                        "source_connected": remote.get("source_connected"),
                                        "frames": counters.received_frames, "transport": "TLS/TCP"})
                        previous_time, previous_frames = now, counters.received_frames
                        previous_received = counters.received_bytes

                tasks = [asyncio.create_task(receive()), asyncio.create_task(report())]
                await wait_for_streams(tasks, stop)
                return {"frames": counters.received_frames, "transport": "TLS/TCP"}
    except aiohttp.ServerFingerprintMismatch:
        raise ConnectionError("服务器证书与 Key 不一致。请在服务器重新复制 Key。") from None
    except aiohttp.ClientConnectorError:
        raise ConnectionError("无法连接服务器。请检查公网 IP、端口映射、安全组和服务器防火墙。") from None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if sink:
            await sink.close()
