import asyncio
from dataclasses import replace
import secrets
import time

import aiohttp
from aiohttp import web
import av
import numpy as np
import pytest

from rgpu.config import VideoSettings
from rgpu.direct import DirectAgent, connect_direct
from rgpu.direct_codec import H264Decoder, H264Encoder, HEADER
from rgpu.media import PatternTrack
from rgpu.pairing import Pairing, load_identity


def test_pairing_identity_persists_and_code_hides_key_in_repr(tmp_path):
    a = load_identity(tmp_path)
    b = load_identity(tmp_path)
    assert a.fingerprint == b.fingerprint and a.key == b.key
    pair = a.pairing("127.0.0.1", 8766)
    assert Pairing.decode(pair.encode()) == pair
    assert a.key not in repr(pair)
    assert pair.endpoint == "https://127.0.0.1:8766"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "224.0.0.1", "http://example.com", "1.2.3.999", "host/path"])
def test_pairing_invalid_host(host):
    with pytest.raises(ValueError):
        Pairing(host, 8766, "a" * 64, secrets.token_urlsafe(32))


def test_invalid_pairing_error_does_not_echo_pasted_secret():
    secret = "SENSITIVE_INVALID_PAIRING"
    with pytest.raises(ValueError) as result:
        Pairing.decode(secret)
    assert secret not in str(result.value)


async def test_h264_real_encoding_and_decoding_and_timestamp_checks():
    settings = VideoSettings(320, 240, 20, 1000)
    source = PatternTrack(settings)
    encoder, decoder = H264Encoder(settings), H264Decoder(settings)
    decoded = []
    last_packet = None
    try:
        for _ in range(8):
            frame = await source.recv()
            for packet in encoder.encode(frame):
                last_packet = packet
                decoded.extend(decoder.decode(packet))
        assert len(decoded) >= 5
        image = decoded[-1].to_ndarray(format="bgr24")
        assert image[:40, :40, 1].mean() > 110
        with pytest.raises(ValueError, match="时间戳"):
            decoder.decode(last_packet)
        with pytest.raises(ValueError):
            decoder.decode(HEADER.pack(b"RGV1", 99999999, 1920, 1080) + b"invalid")
    finally:
        source.stop()


async def running_agent(tmp_path, mode="echo", output_device=""):
    identity = load_identity(tmp_path)
    agent = DirectAgent(identity, mode, output_device)
    runner = web.AppRunner(agent.application(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=identity.ssl_context())
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return agent, runner, identity.pairing("127.0.0.1", port)


async def test_tls_direct_roundtrip_and_single_port_cleanup(tmp_path):
    agent, runner, pair = await running_agent(tmp_path)
    stop = asyncio.Event()
    samples, colors = [], []
    def stats(sample):
        samples.append(sample)
        if sample["frames"] >= 25:
            stop.set()
    try:
        result = await asyncio.wait_for(connect_direct(pair, "test", VideoSettings(320, 240, 20, 1000), stop,
                                                       "none", statistics=stats,
                                                       preview=lambda im: colors.append(float(im[:40, :40, 1].mean()))), 15)
        assert result["frames"] >= 25
        assert result["transport"] == "TLS/TCP"
        assert any(c > 110 for c in colors)
        assert samples[-1]["upload_mbps"] > 0 and samples[-1]["download_mbps"] > 0
        for _ in range(50):
            if not agent.busy:
                break
            await asyncio.sleep(0.02)
        assert not agent.busy
    finally:
        await runner.cleanup()


async def test_wrong_certificate_rejected_before_camera_access(tmp_path, monkeypatch):
    agent, runner, pair = await running_agent(tmp_path)
    from rgpu import direct
    def forbidden(*args, **kwargs):
        raise AssertionError("Camera must not be acquired before authentication")
    monkeypatch.setattr(direct, "CameraTrack", forbidden)
    try:
        with pytest.raises(ConnectionError, match="证书"):
            await connect_direct(replace(pair, fingerprint="0" * 64), "camera", VideoSettings(), asyncio.Event(), "none")
        assert not agent.busy
    finally:
        await runner.cleanup()


async def test_wrong_key_rejected_before_camera_access(tmp_path, monkeypatch):
    agent, runner, pair = await running_agent(tmp_path)
    from rgpu import direct
    def forbidden(*args, **kwargs):
        raise AssertionError("Camera must not be acquired before authentication")
    monkeypatch.setattr(direct, "CameraTrack", forbidden)
    try:
        with pytest.raises(ConnectionError, match="401"):
            await connect_direct(replace(pair, key=secrets.token_urlsafe(32)), "camera", VideoSettings(), asyncio.Event(), "none")
        assert not agent.busy
    finally:
        await runner.cleanup()


async def test_bad_handshake_and_busy_session_cleanup(tmp_path):
    agent, runner, pair = await running_agent(tmp_path)
    fingerprint = aiohttp.Fingerprint(bytes.fromhex(pair.fingerprint))
    headers = {"Authorization": "Bearer " + pair.key}
    try:
        async with aiohttp.ClientSession(headers=headers) as http:
            async with http.ws_connect(pair.endpoint + "/stream", ssl=fingerprint) as ws:
                async with http.ws_connect(pair.endpoint + "/stream", ssl=fingerprint) as other:
                    raise AssertionError("Busy endpoint must reject second connection")
    except aiohttp.WSServerHandshakeError as ex:
        assert ex.status == 409
    finally:
        await runner.cleanup()
    assert not agent.busy
