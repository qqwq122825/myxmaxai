"""Relay mode: a phone uploads ("source"), the server processes, a PC receives ("viewer")."""
import asyncio

import pytest

from rgpu.bridge import ResultSource
from rgpu.config import VideoSettings
from rgpu.direct import connect_direct, connect_viewer
from tests.test_managed import server, until


async def test_phone_pushes_and_pc_receives_forwarded_result(tmp_path):
    agent, runner, pair = await server(tmp_path)
    phone_stop, pc_stop = asyncio.Event(), asyncio.Event()
    pc_frames, pc_stats, phone_frames = [], [], []
    settings = VideoSettings(320, 240, 15, 1000)
    pc = asyncio.create_task(connect_viewer(pair, pc_stop, "none", statistics=pc_stats.append,
        preview=lambda image: pc_frames.append(float(image[:30, :30, 1].mean())), media_timeout=3))
    phone = asyncio.create_task(connect_direct(pair, "test", settings, phone_stop, "none", role="source",
        preview=lambda image: phone_frames.append(1), media_timeout=3))
    try:
        await until(lambda: agent.viewer_busy and agent.receiving)
        # Both slots are taken by different devices at the same time.
        assert agent.busy and agent.viewer_busy
        await agent.start_capture(ResultSource("echo"))
        await agent.set_return(True)
        # The phone's test pattern comes back on the PC (bright green patch in the corner).
        await until(lambda: len([c for c in pc_frames if c > 110]) > 10)
        await until(lambda: pc_stats and pc_stats[-1].get("source_connected") is True)
        assert pc_stats[-1]["return_fps"] > 0
        # The phone never receives the processed picture.
        assert phone_frames == []
        assert not phone.done() and not pc.done()

        # Phone drops (network blip): the PC stays connected and the return stays enabled.
        phone_stop.set()
        await asyncio.wait_for(phone, 5)
        await until(lambda: not agent.busy)
        assert agent.viewer_busy and agent.return_enabled and not pc.done()

        # Phone reconnects with the same size; frames flow to the PC again without touching the server.
        phone_stop = asyncio.Event()
        phone = asyncio.create_task(connect_direct(pair, "test", settings, phone_stop, "none", role="source", media_timeout=3))
        before = len(pc_frames)
        await until(lambda: len(pc_frames) > before + 10)
    finally:
        phone_stop.set()
        pc_stop.set()
        await asyncio.gather(phone, pc, return_exceptions=True)
        await agent.stop_capture()
        await runner.cleanup()


async def test_second_receiver_and_second_phone_are_refused(tmp_path):
    agent, runner, pair = await server(tmp_path)
    stops = [asyncio.Event() for _ in range(2)]
    settings = VideoSettings(320, 240, 15, 1000)
    pc = asyncio.create_task(connect_viewer(pair, stops[0], "none"))
    phone = asyncio.create_task(connect_direct(pair, "test", settings, stops[1], "none", role="source"))
    try:
        await until(lambda: agent.viewer_busy and agent.receiving)
        with pytest.raises(ConnectionError, match="接收端"):
            await connect_viewer(pair, asyncio.Event(), "none")
        with pytest.raises((ConnectionError, RuntimeError), match="已有"):
            await connect_direct(pair, "test", settings, asyncio.Event(), "none", role="source")
        # A classic push-and-receive client needs both slots, so it is refused too.
        with pytest.raises((ConnectionError, RuntimeError), match="已有"):
            await connect_direct(pair, "test", settings, asyncio.Event(), "none")
    finally:
        for stop in stops:
            stop.set()
        await asyncio.gather(pc, phone, return_exceptions=True)
        await runner.cleanup()


async def test_old_server_without_relay_is_reported_clearly(tmp_path):
    from aiohttp import web
    from rgpu.bridge import ManagedAgent
    from rgpu.pairing import load_identity

    class OldServer(ManagedAgent):          # behaves like a 0.3.x server: no relay feature
        async def health(self, request):
            response = await super().health(request)
            import json
            data = json.loads(response.text)
            data["features"] = ["independent-return", "window-capture"]
            return web.json_response(data)

    identity = load_identity(tmp_path)
    agent = OldServer(identity)
    runner = web.AppRunner(agent.application(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=identity.ssl_context())
    await site.start()
    pair = identity.pairing("127.0.0.1", site._server.sockets[0].getsockname()[1])
    try:
        with pytest.raises(ValueError, match="服务器版本过旧"):
            await connect_viewer(pair, asyncio.Event(), "none")
    finally:
        await runner.cleanup()
