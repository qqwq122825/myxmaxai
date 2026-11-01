from __future__ import annotations

import asyncio
import tempfile
import time

from aiohttp import web

from .config import VideoSettings
from .direct import DirectAgent, connect_direct
from .pairing import load_identity


async def run_direct_selftest(duration=6, device="test", settings=None, status=lambda _: None):
    settings = settings or VideoSettings()
    with tempfile.TemporaryDirectory(prefix="remote-gpu-tls-test-") as temporary:
        identity = load_identity(temporary)
        agent = DirectAgent(identity, status=status)
        runner = web.AppRunner(agent.application(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=identity.ssl_context())
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        stop = asyncio.Event()
        samples, colors = [], []
        first_active = None
        def statistics(sample):
            nonlocal first_active
            samples.append(sample)
            if sample["frames"] > 0 and first_active is None:
                first_active = time.monotonic()
            if first_active and time.monotonic() - first_active >= duration:
                stop.set()
        def preview(image):
            colors.append(float(image[:40, :40, 1].mean()) if device == "test" else float(image.std()))
        try:
            start = time.monotonic()
            result = await asyncio.wait_for(connect_direct(identity.pairing("127.0.0.1", port), device, settings,
                                                           stop, "none", preview=preview, statistics=statistics,
                                                           status=status), duration + 25)
            if result["frames"] < 20 or not any(c > (110 if device == "test" else 5) for c in colors):
                raise AssertionError("加密直连回环未通过帧数或图像验证。")
            active = [s for s in samples if s["return_fps"] > 0]
            for _ in range(50):
                if not agent.busy:
                    break
                await asyncio.sleep(0.02)
            if agent.busy:
                raise AssertionError("停止后直连会话未释放。")
            return {"ok": True, "transport": "TLS/TCP", "single_port": True, "account_required": False,
                    "frames": result["frames"], "seconds": round(time.monotonic() - start, 2),
                    "mean_return_fps": round(sum(s["return_fps"] for s in active) / len(active), 2),
                    "resolution": f"{settings.width}x{settings.height}", "target_fps": settings.fps,
                    "certificate_pinned": True, "session_released": not agent.busy,
                    "scope": "localhost only; synthetic source" if device == "test" else "localhost only; actual camera source",
                    "encoder": "libx264 (CPU)"}
        finally:
            await runner.cleanup()
