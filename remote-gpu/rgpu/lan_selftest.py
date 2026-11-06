"""Loopback-only phone protocol check. Never opens a camera or changes drivers."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile

import aiohttp
from aiohttp import web
import av
import numpy as np

from .config import VideoSettings
from .direct_codec import H264Encoder
from .lan import LanReceiver, pairing_url, qr_image
from .lan_audio import AUDIO_MAGIC, MAX_PCM_BYTES, AudioMonitor
from .pairing import load_identity


async def exercise(directory, width=1080, height=1920):
    identity = load_identity(Path(directory) / "identity")
    qr_image(pairing_url(["192.168.1.23"], 8767, identity.connection_key, "LAN test"))
    frames, statistics, configurations, disconnects = [], [], [], []
    played, resets, closed = [], [], []

    class TestSpeaker:
        def write(self, pcm):
            played.append(pcm)

        def reset(self):
            resets.append(True)

        def close(self):
            closed.append(True)

    monitor = AudioMonitor(lambda _: None, speaker_factory=TestSpeaker)
    def disconnected():
        disconnects.append(True)
        monitor.reset()

    agent = LanReceiver(identity, preview=lambda image: frames.append(image.copy()),
        statistics=statistics.append, on_settings=configurations.append,
        on_disconnect=disconnected, on_audio=monitor.receive)
    runner = web.AppRunner(agent.application(), access_log=None, shutdown_timeout=3)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=identity.ssl_context())
    await site.start()
    endpoint = f"https://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    pin = aiohttp.Fingerprint(bytes.fromhex(identity.fingerprint))
    settings = VideoSettings(width, height, 30, 12000)
    from dataclasses import asdict
    hello = {"type": "start", "protocol": "direct-1", "role": "source", "video": asdict(settings)}

    async def eventually(predicate, timeout=5):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(.02)

    try:
        async with aiohttp.ClientSession() as stranger:
            async with stranger.get(endpoint + "/health", ssl=pin) as response:
                assert response.status == 401
        async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + identity.key}) as http:
            async with http.get(endpoint + "/health", ssl=pin) as response:
                health = await response.json()
                assert response.status == 200 and {"lan-receiver", "lan-mic-pcm"} <= set(health["features"])
                assert not health["busy"]
                assert health["video_limits"]["max_pixels"] == 1920 * 1080
            async with http.ws_connect(endpoint + "/stream", ssl=pin) as ws:
                await ws.send_json(hello)
                ready = await ws.receive_json(timeout=5)
                assert ready["type"] == "ready" and ready["video"]["bitrate_kbps"] == 12000
                assert configurations == [settings]
                async with http.get(endpoint + "/health", ssl=pin) as response:
                    assert (await response.json())["busy"]
                async with http.ws_connect(endpoint + "/stream", ssl=pin) as duplicate:
                    await duplicate.send_json(hello)
                    assert (await duplicate.receive_json(timeout=3))["type"] == "error"
                assert agent.busy and not disconnects
                await ws.send_json({"type": "ping", "time": 123.75})
                assert await ws.receive_json(timeout=3) == {"type": "pong", "time": 123.75}
                pcm = b"\x10\x20" * (MAX_PCM_BYTES // 2)
                await ws.send_bytes(AUDIO_MAGIC + pcm)
                await ws.send_json({"type": "ping", "time": 1})
                assert await ws.receive_json(timeout=3) == {"type": "pong", "time": 1}
                assert not played
                monitor.start()
                await ws.send_bytes(AUDIO_MAGIC + b"\x10")  # Invalid PCM must not reach the decoder.
                for _ in range(6):  # 6 × 20 ms fills the 120 ms jitter buffer, then playback starts
                    await ws.send_bytes(AUDIO_MAGIC + pcm)
                await eventually(lambda: b"".join(played) == pcm * 6)
                encoder = H264Encoder(settings)
                image = np.zeros((height, width, 3), np.uint8)
                image[:height // 2] = (15, 25, 220)
                image[height // 2:] = (220, 30, 10)
                from fractions import Fraction
                for index in range(6):
                    frame = av.VideoFrame.from_ndarray(image, format="bgr24")
                    frame.pts, frame.time_base = index * 3000, Fraction(1, 90000)
                    packets = await asyncio.to_thread(encoder.encode, frame)
                    for packet in packets:
                        await ws.send_bytes(packet)
                    await asyncio.sleep(1 / 30)
                await eventually(lambda: len(frames) > 0)
                assert frames[-1].shape == image.shape
                assert frames[-1][height // 4, width // 2, 2] > 180
                assert frames[-1][3 * height // 4, width // 2, 0] > 180
                await eventually(lambda: len(statistics) > 0)
                assert statistics[-1]["frames"] == 6
                monitor.stop()
                await ws.send_bytes(AUDIO_MAGIC + pcm)
                await ws.send_json({"type": "ping", "time": 2})
                assert await ws.receive_json(timeout=3) == {"type": "pong", "time": 2}
                assert b"".join(played) == pcm * 6 and closed == [True]
                monitor.start()
            await eventually(lambda: not agent.busy)
            assert len(disconnects) == 1 and resets == [True]
            async with http.ws_connect(endpoint + "/stream", ssl=pin) as wrong_role:
                await wrong_role.send_json({**hello, "role": "viewer"})
                assert (await wrong_role.receive_json(timeout=3))["type"] == "error"
            async with http.ws_connect(endpoint + "/stream", ssl=pin) as oversized:
                await oversized.send_json({**hello, "video": {**hello["video"], "width": 3840, "height": 2160}})
                assert (await oversized.receive_json(timeout=3))["type"] == "error"
            async with http.ws_connect(endpoint + "/stream", ssl=pin) as reconnected:
                await reconnected.send_json(hello)
                assert (await reconnected.receive_json(timeout=3))["type"] == "ready"
            await eventually(lambda: not agent.busy)
        return {"ok": True, "width": width, "height": height, "decoded_preview_frames": len(frames),
            "input_frames": 6, "authentication_checked": True, "duplicate_rejected": True,
            "ping_pong_checked": True, "reconnection_checked": True, "oversize_rejected": True, "qr_checked": True,
            "microphone_monitor_checked": True, "speaker": "simulated",
            "physical_camera_used": False, "driver_changed": False, "network": "loopback TLS only"}
    finally:
        await runner.cleanup()
        monitor.stop()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="FaceLiveLanTest-") as directory:
            result = asyncio.run(exercise(directory))
    except Exception as ex:
        result = {"ok": False, "error": str(ex)}
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
