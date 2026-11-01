from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from dataclasses import asdict
from typing import Callable

import aiohttp
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription

from . import __version__
from .config import VideoSettings, check_token, configure_h264, ice_configuration, prefer_h264, signaling_url
from .media import CameraTrack, FrameSink, LatestTrack, PatternTrack

log = logging.getLogger(__name__)
PROTOCOL = 1


class AgentSession:
    def __init__(self, agent, settings):
        self.agent = agent
        self.id = secrets.token_urlsafe(18)
        self.pc = RTCPeerConnection(agent.ice)
        self.settings = settings
        self.sink = None
        self.source = None
        self.incoming = None
        self.tasks = set()
        self.closed = False
        self.close_task = None
        self.created = time.monotonic()
        self.last_output = self.created
        self.last_count = 0

        @self.pc.on("connectionstatechange")
        async def connection_changed():
            agent.status(f"远程媒体状态：{self.pc.connectionState}")
            if self.pc.connectionState in ("failed", "closed"):
                await self.close()

        @self.pc.on("track")
        def track_received(track):
            if track.kind != "video" or self.incoming is not None:
                self.spawn(self.close())
                return
            self.incoming = LatestTrack(track)
            if agent.mode == "echo":
                self.source = self.incoming
            else:
                self.source = CameraTrack(agent.output_device, settings)
                self.spawn(self.consume_input())
            self.pc.addTrack(self.source)

        @self.pc.on("datachannel")
        def datachannel(channel):
            @channel.on("message")
            def message_received(message):
                if not isinstance(message, str) or len(message) > 1024:
                    return
                try:
                    data = json.loads(message)
                    if data.get("type") == "ping" and isinstance(data.get("time"), (int, float)):
                        channel.send(json.dumps({"type": "pong", "time": data["time"],
                                                 "input_frames": self.sink.count if self.sink else self.incoming.count if self.incoming else 0,
                                                 "output_frames": self.source.count if self.source else 0,
                                                 "input_camera_writes": self.sink.written if self.sink else 0,
                                                 "mode": agent.mode}))
                except (ValueError, TypeError, AttributeError):
                    pass

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def prepare(self):
        if self.agent.mode == "bridge":
            self.sink = FrameSink(self.settings, "unity:1", preview=self.agent.preview)
            await self.sink.wait_ready()
        self.spawn(self.watchdog())

    async def consume_input(self):
        try:
            while not self.closed:
                frame = await self.incoming.recv()
                self.sink.submit(frame)
                if self.sink.error:
                    raise self.sink.error
        except asyncio.CancelledError:
            pass
        except Exception as ex:
            self.agent.status(f"远程输入停止：{ex}")
            await self.close()

    async def watchdog(self):
        while not self.closed:
            await asyncio.sleep(1)
            now = time.monotonic()
            if self.pc.connectionState != "connected":
                if now - self.created > self.agent.connect_timeout:
                    self.agent.status("连接未完成或已经断开，释放当前会话。")
                    await self.close()
                    return
                continue
            count = self.source.count if self.source else 0
            if count != self.last_count:
                self.last_count, self.last_output = count, now
            if now - self.last_output > self.agent.media_timeout:
                self.agent.status("结果摄像头无画面，释放会话。请检查换脸软件输出。")
                await self.close()
                return
            if self.sink and self.sink.error:
                self.agent.status(f"虚拟输入摄像头写入失败：{self.sink.error}")
                await self.close()
                return

    async def close(self):
        if self.close_task is None:
            self.closed = True
            self.close_task = asyncio.create_task(self._close())
        # Multiple callers (DTLS close, DELETE, watchdog) must await the same
        # completed cleanup, not merely observe a flag set at its beginning.
        await asyncio.shield(self.close_task)

    async def _close(self):
        # Free the single-session slot only after devices and transports close.
        others = list(self.tasks)
        for task in others:
            task.cancel()
        for track in (self.source, self.incoming):
            if track:
                track.stop()
        await self.pc.close()
        if self.sink:
            await self.sink.close()
        if isinstance(self.source, CameraTrack):
            await self.source.wait_closed()
        if others:
            await asyncio.gather(*others, return_exceptions=True)
        if self.agent.session is self:
            self.agent.session = None
        self.agent.status("远程端待连接。")


class Agent:
    def __init__(self, token: str, mode="echo", output_device="", ice_path=None,
                 status: Callable[[str], None] = log.info, preview=None,
                 connect_timeout=30, media_timeout=20):
        if mode not in ("echo", "bridge"):
            raise ValueError("远程模式必须为 echo 或 bridge。")
        if mode == "bridge" and not output_device:
            raise ValueError("桥接模式必须选择换脸软件的结果摄像头。")
        self.token = check_token(token)
        self.mode = mode
        self.output_device = output_device
        self.ice = ice_configuration(ice_path)
        self.status, self.preview = status, preview
        self.connect_timeout, self.media_timeout = connect_timeout, media_timeout
        self.session = None
        self.lock = asyncio.Lock()

    def application(self):
        @web.middleware
        async def authorization(request, handler):
            supplied = request.headers.get("Authorization", "")
            if not secrets.compare_digest(supplied.encode("utf-8"), ("Bearer " + self.token).encode("ascii")):
                return web.json_response({"error": "连接密钥不正确。"}, status=401)
            return await handler(request)

        app = web.Application(middlewares=[authorization], client_max_size=256 * 1024)
        app.router.add_get("/health", self.health)
        app.router.add_post("/offer", self.offer)
        app.router.add_delete("/sessions/{id}", self.end_session)

        async def cleanup(_):
            if self.session:
                await self.session.close()
        app.on_shutdown.append(cleanup)
        return app

    async def health(self, request):
        return web.json_response({"version": __version__, "protocol": PROTOCOL,
                                  "mode": self.mode, "busy": self.session is not None,
                                  "encoder": "H264 / libx264 (CPU)"})

    async def offer(self, request):
        try:
            data = await request.json()
            if not isinstance(data, dict) or data.get("protocol") != PROTOCOL:
                raise ValueError("协议版本不兼容，请使用同一版本的两端程序。")
            if data.get("type") != "offer" or not isinstance(data.get("sdp"), str) or len(data["sdp"]) > 200_000:
                raise ValueError("无效的 WebRTC offer。")
            settings = VideoSettings(**data["video"])
        except (ValueError, TypeError, KeyError) as ex:
            return web.json_response({"error": str(ex)}, status=400)

        async with self.lock:
            if self.session is not None:
                return web.json_response({"error": "远程端已有连接，请先停止旧连接或重启远程端。"}, status=409)
            session = AgentSession(self, settings)
            self.session = session
        try:
            configure_h264(settings.bitrate_kbps)
            await session.prepare()
            await session.pc.setRemoteDescription(RTCSessionDescription(sdp=data["sdp"], type="offer"))
            if session.incoming is None:
                raise ValueError("连接没有提供视频轨道。")
            prefer_h264(session.pc)
            await session.pc.setLocalDescription(await session.pc.createAnswer())
            return web.json_response({"sdp": session.pc.localDescription.sdp, "type": "answer",
                                      "session_id": session.id, "mode": self.mode, "protocol": PROTOCOL})
        except asyncio.CancelledError:
            await session.close()
            raise
        except Exception as ex:
            log.exception("无法建立远程会话")
            await session.close()
            return web.json_response({"error": f"远程会话初始化失败：{ex}"}, status=400)

    async def end_session(self, request):
        session = self.session
        if session and secrets.compare_digest(request.match_info["id"], session.id):
            await session.close()
            return web.json_response({"closed": True})
        return web.json_response({"error": "会话不存在。"}, status=404)


async def peer_stats(pc):
    report = await pc.getStats()
    transports = [s for s in report.values() if s.type == "transport"]
    return {"sent_bytes": sum(s.bytesSent for s in transports),
            "received_bytes": sum(s.bytesReceived for s in transports)}


async def connect_client(url, token, device, settings, stop: asyncio.Event,
                         sink_target="unity:0", ice_path=None, status=log.info, preview=None,
                         statistics=None, media_timeout=20):
    url, token = signaling_url(url), check_token(token)
    configure_h264(settings.bitrate_kbps)
    pc = RTCPeerConnection(ice_configuration(ice_path))
    source = None
    sink = None
    tasks = []
    session_id = None
    started = time.monotonic()
    last_video = started
    received_frames = 0
    rtt_ms = None
    remote_stats = {}
    failure = None
    output_received = False
    headers = {"Authorization": "Bearer " + token}

    @pc.on("connectionstatechange")
    async def state_changed():
        nonlocal failure
        status(f"本地媒体状态：{pc.connectionState}")
        if pc.connectionState in ("failed", "closed") and not stop.is_set():
            failure = RuntimeError("媒体连接中断。请检查两端组网、UDP 和防火墙后重新连接。")
            stop.set()

    async def consume(track):
        nonlocal last_video, received_frames, failure
        try:
            while not stop.is_set():
                frame = await asyncio.wait_for(track.recv(), media_timeout)
                sink.submit(frame)
                last_video = time.monotonic()
                received_frames += 1
        except asyncio.CancelledError:
            pass
        except Exception as ex:
            failure = RuntimeError(f"回传画面停止：{ex}")
            stop.set()

    @pc.on("track")
    def track_received(track):
        nonlocal output_received
        if track.kind == "video" and not output_received:
            output_received = True
            tasks.append(asyncio.create_task(consume(track)))

    channel = pc.createDataChannel("status", ordered=False, maxRetransmits=0)

    @channel.on("message")
    def channel_message(message):
        nonlocal rtt_ms, remote_stats
        try:
            if not isinstance(message, str) or len(message) > 4096:
                return
            data = json.loads(message)
            if data.get("type") == "pong":
                rtt_ms = max(0, (time.monotonic() - data["time"]) * 1000)
                remote_stats = data
        except (ValueError, TypeError, KeyError):
            pass

    timeout = aiohttp.ClientTimeout(total=45, connect=10)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as http:
        try:
            # Fail authentication before acquiring the user's camera.
            async with http.get(url + "/health") as response:
                health = await response.json()
                if response.status != 200:
                    raise RuntimeError(health.get("error", f"HTTP {response.status}"))
                if health.get("protocol") != PROTOCOL:
                    raise RuntimeError("两端程序协议版本不一致。")
                if health.get("busy"):
                    raise RuntimeError("远程端已有连接，请先停止旧会话。")
            status(f"远程模式：{health['mode']}；H.264 软件编码，GPU 用于换脸。")
            source = PatternTrack(settings) if device == "test" else CameraTrack(device, settings)
            sink = FrameSink(settings, sink_target, preview)
            await sink.wait_ready()
            pc.addTrack(source)
            prefer_h264(pc)
            status("正在协商加密视频连接……")
            await pc.setLocalDescription(await pc.createOffer())
            payload = {"protocol": PROTOCOL, "sdp": pc.localDescription.sdp,
                       "type": "offer", "video": asdict(settings)}
            async with http.post(url + "/offer", json=payload) as response:
                answer = await response.json()
                if response.status != 200:
                    raise RuntimeError(answer.get("error", f"HTTP {response.status}"))
            session_id = answer["session_id"]
            await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
            started = last_video = time.monotonic()
            prior_time = started
            prior_frames = prior_sent = prior_received = 0
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), 1)
                    break
                except TimeoutError:
                    pass
                now = time.monotonic()
                if now - last_video > media_timeout:
                    raise TimeoutError("未收到回传视频，请检查远程结果摄像头、Tailscale 和防火墙。")
                if sink.error:
                    raise sink.error
                if channel.readyState == "open":
                    channel.send(json.dumps({"type": "ping", "time": now}))
                totals = await peer_stats(pc)
                elapsed = max(0.001, now - prior_time)
                data = {"return_fps": (received_frames - prior_frames) / elapsed,
                        "upload_mbps": (totals["sent_bytes"] - prior_sent) * 8 / elapsed / 1_000_000,
                        "download_mbps": (totals["received_bytes"] - prior_received) * 8 / elapsed / 1_000_000,
                        "rtt_ms": rtt_ms, "frames": received_frames, "camera_writes": sink.written,
                        "dropped": sink.dropped, "remote": remote_stats,
                        "encoder": "libx264 (CPU)", "network_path": "unknown"}
                if statistics:
                    statistics(data)
                prior_time, prior_frames = now, received_frames
                prior_sent, prior_received = totals["sent_bytes"], totals["received_bytes"]
            if failure:
                raise failure
            return {"frames": received_frames, "seconds": time.monotonic() - started}
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            if source:
                source.stop()
            await pc.close()
            await asyncio.gather(*tasks, return_exceptions=True)
            if sink:
                await sink.close()
            if isinstance(source, CameraTrack):
                await source.wait_closed()
            if session_id:
                with contextlib.suppress(Exception):
                    async with http.delete(url + "/sessions/" + session_id,
                                           timeout=aiohttp.ClientTimeout(total=8)) as response:
                        await response.read()


async def serve(agent: Agent, host, port, stop: asyncio.Event, ssl_context=None):
    runner = web.AppRunner(agent.application(), access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host, port, ssl_context=ssl_context)
        await site.start()
        agent.status(f"远程端已启动：{'https' if ssl_context else 'http'}://{host}:{port}，模式 {agent.mode}")
        await stop.wait()
    finally:
        await runner.cleanup()
