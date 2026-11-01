import asyncio
import secrets

import aiohttp
from aiohttp import web
import av
import numpy as np
import pytest

from rgpu.config import VideoSettings, private_address, signaling_url
from rgpu.media import FrameSink, LatestQueue, fit_bgr, parse_devices
from rgpu.selftest import run_selftest
from rgpu.transport import Agent, AgentSession


@pytest.mark.parametrize("url", ["http://8.8.8.8:8765", "http://example.com", "http://user:password@127.0.0.1", "http://127.0.0.1/?token=abc"])
def test_signaling_rejects_public_plaintext_and_embedded_credentials(url):
    with pytest.raises(ValueError):
        signaling_url(url)


def test_private_network_validation():
    assert private_address("100.72.1.2")
    assert not private_address("0.0.0.0")
    assert not private_address("169.254.1.1")
    assert signaling_url("https://gpu.example.com:443") == "https://gpu.example.com:443"


@pytest.mark.parametrize("kwargs", [{"width": 4000}, {"fps": 60}, {"width": True}, {"bitrate_kbps": 0}, {"width": 1920, "height": 1920}])
def test_media_limits(kwargs):
    with pytest.raises(ValueError):
        VideoSettings(**kwargs)


async def test_latest_queue_drops_old_frames():
    queue = LatestQueue()
    for number in range(500):
        queue.put(number)
    assert await queue.get() == 499
    assert queue.dropped == 499
    assert queue.queue.qsize() == 0
    queue.close()


def test_fit_preserves_aspect_and_does_not_flip():
    image = np.zeros((40, 80, 3), np.uint8)
    image[:20] = (20, 40, 200)
    result = fit_bgr(image, 80, 80)
    assert result.shape == (80, 80, 3)
    assert not result[:20].any()
    assert tuple(result[20, 0]) == (20, 40, 200)
    assert not result[60:].any()


def test_camera_enumeration_preserves_stable_ids_and_excludes_audio_and_unknown():
    result = parse_devices('"摄像头" (video)\n  Alternative name "@device_one"\n'
                           '"Unknown" (none)\n  Alternative name "@device_unknown"\n'
                           '"麦克风" (audio)\n  Alternative name "@device_audio"\n'
                           '"FaceLive Camera #2" (video)\n  Alternative name "@device_two"\n')
    assert [(d.name, d.id) for d in result] == [("摄像头", "@device_one"), ("FaceLive Camera #2", "@device_two")]


async def test_sink_blacks_out_stale_video_and_stops_worker():
    seen = []
    sink = FrameSink(VideoSettings(320, 240, 30, 1000), preview=lambda image: seen.append(bool(image.any())), stale_seconds=0.1)
    await sink.wait_ready()
    frame = av.VideoFrame.from_ndarray(np.full((240, 320, 3), 150, np.uint8), format="bgr24")
    sink.submit(frame)
    await asyncio.sleep(0.3)
    await sink.close()
    assert True in seen
    assert seen[-1] is False
    assert not sink.thread.is_alive()


async def test_real_h264_webrtc_round_trip_and_cleanup():
    result = await run_selftest(duration=2, settings=VideoSettings(320, 240, 20, 1000))
    assert result["ok"]
    assert result["authentication"]
    assert result["video_color_verified"]
    assert result["frames"] >= 20
    assert result["session_released"]


async def test_bad_offer_does_not_occupy_server_and_wrong_key_is_rejected():
    token = secrets.token_urlsafe(32)
    agent = Agent(token)
    runner = web.AppRunner(agent.application())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(url + "/offer", json={}, headers={"Authorization": "Bearer bad"}) as response:
                assert response.status == 401
            headers = {"Authorization": "Bearer " + token}
            async with http.post(url + "/offer", json={"protocol": 1, "type": "offer", "sdp": "bad",
                                                       "video": {"width": 320, "height": 240, "fps": 20, "bitrate_kbps": 1000}}, headers=headers) as response:
                assert response.status == 400
            assert agent.session is None
            async with http.get(url + "/health", headers=headers) as response:
                assert not (await response.json())["busy"]
    finally:
        await runner.cleanup()


async def test_concurrent_close_waits_until_devices_are_released():
    agent = Agent(secrets.token_urlsafe(32))
    session = AgentSession(agent, VideoSettings())
    agent.session = session
    gate = asyncio.Event()
    original_close = session.pc.close
    async def delayed_close():
        await gate.wait()
        await original_close()
    session.pc.close = delayed_close
    first = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    second = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    assert not second.done()
    assert agent.session is session
    gate.set()
    await asyncio.gather(first, second)
    assert agent.session is None
