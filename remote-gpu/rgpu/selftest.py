from __future__ import annotations

import asyncio
import secrets
import time

import aiohttp
from aiohttp import web

from .config import VideoSettings
from .transport import Agent, connect_client


async def run_selftest(duration=6, status=lambda _: None, settings=None):
    """Actual ICE / DTLS / SRTP / H.264 round trip; no camera or GPU required."""
    token = secrets.token_urlsafe(32)
    agent = Agent(token, status=status)
    runner = web.AppRunner(agent.application(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    stop = asyncio.Event()
    samples = []
    colors = []
    active_started = None

    def preview(image):
        colors.append(float(image[:40, :40, 1].mean()))

    def stats(sample):
        nonlocal active_started
        samples.append(sample)
        if sample["frames"] > 0 and active_started is None:
            active_started = time.monotonic()

    async def deadline():
        start = time.monotonic()
        while time.monotonic() - start < 25:
            await asyncio.sleep(0.1)
            if active_started is not None and time.monotonic() - active_started >= duration:
                stop.set()
                return
        stop.set()

    timer = asyncio.create_task(deadline())
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url + "/health") as response:
                unauthorized = response.status
        settings = settings or VideoSettings()
        result = await connect_client(url, token, "test", settings, stop,
                                      sink_target="none", preview=preview, statistics=stats, status=status)
        if result["frames"] < 20 or not any(value > 110 for value in colors):
            raise AssertionError(f"H.264 回环未通过：收到 {result['frames']} 帧，颜色验证失败或帧数不足。")
        if unauthorized != 401:
            raise AssertionError("连接密钥校验未通过。")
        if agent.session is not None:
            raise AssertionError("停止连接后远程会话未释放。")
        active_samples = [s for s in samples if s["return_fps"] > 0]
        rtts = sorted(s["rtt_ms"] for s in samples if s["rtt_ms"] is not None)
        return {"ok": True, "frames": result["frames"], "seconds": round(result["seconds"], 2),
                "mean_return_fps": round(sum(s["return_fps"] for s in active_samples) / max(1, len(active_samples)), 2),
                "data_channel_rtt_ms_median": round(rtts[len(rtts) // 2], 2) if rtts else None,
                "authentication": unauthorized == 401, "session_released": agent.session is None,
                "codec": "H264 / CPU", "video_color_verified": True,
                "resolution": f"{settings.width}x{settings.height}", "target_fps": settings.fps,
                "scope": "localhost synthetic source; no physical camera, face-swap engine or remote GPU tested"}
    finally:
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)
        await runner.cleanup()
