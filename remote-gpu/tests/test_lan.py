from urllib.parse import parse_qs, urlsplit

import cv2
import pytest

from rgpu.lan import (checked_port, is_lan_ip, pairing_url, parse_addresses,
                      qr_image, rotate_key)
from rgpu.lan_selftest import exercise
from rgpu.pairing import ConnectionKey, load_identity


@pytest.mark.parametrize("address", ["192.168.1.9", "10.10.1.2", "172.16.0.5", "172.31.255.2", "169.254.1.2"])
def test_accept_lan_addresses(address):
    assert is_lan_ip(address)


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0", "100.123.190.3", "8.8.8.8", "172.32.0.1", "localhost", "192.168.1.2:8767"])
def test_do_not_advertise_public_vpn_or_loopback_addresses(address):
    assert not is_lan_ip(address)


@pytest.mark.parametrize("sample", range(5))
def test_phone_qr_schema_and_actual_decode(tmp_path, sample):
    identity = load_identity(tmp_path)
    addresses = parse_addresses("192.168.1.3,10.0.0.5,192.168.1.3")
    url = pairing_url(addresses, 8767, identity.connection_key, "工作室电脑")
    assert urlsplit(url).scheme == "aimoyan" and urlsplit(url).netloc == "connect"
    query = parse_qs(urlsplit(url).query)
    assert query["addr"] == ["192.168.1.3:8767,10.0.0.5:8767"]
    assert query["key"] == [identity.connection_key] and query["mode"] == ["lan"]
    assert query["name"] == ["工作室电脑"]
    assert cv2.QRCodeDetector().detectAndDecode(qr_image(url, size=500))[0] == url
    assert qr_image(url).shape[0] <= 270


@pytest.mark.parametrize("port", ["abc", 0, 65536, 80, None])
def test_invalid_port(port):
    with pytest.raises(ValueError):
        checked_port(port)


def test_rotation_invalidates_old_key_but_keeps_certificate(tmp_path):
    identity = load_identity(tmp_path)
    updated = rotate_key(identity)
    assert updated.fingerprint == identity.fingerprint and updated.key != identity.key
    assert ConnectionKey.decode(updated.connection_key).key == updated.key
    assert load_identity(tmp_path).key == updated.key


@pytest.mark.parametrize("size", [(720, 1280), (1080, 1920)])
async def test_phone_protocol_tls_decode_auth_busy_and_reconnect(tmp_path, size):
    result = await exercise(tmp_path, *size)
    assert result["ok"] and result["input_frames"] == 6


async def test_reconnecting_phone_replaces_silent_connection_but_not_live_one(tmp_path):
    import asyncio
    from dataclasses import asdict

    import aiohttp
    from aiohttp import web

    from rgpu.config import VideoSettings
    from rgpu.lan import LanReceiver

    identity = load_identity(tmp_path)
    disconnects = []
    agent = LanReceiver(identity, on_disconnect=lambda: disconnects.append(True))
    agent.stale_after = 0.5
    runner = web.AppRunner(agent.application(), access_log=None, shutdown_timeout=3)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=identity.ssl_context())
    await site.start()
    endpoint = f"https://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    pin = aiohttp.Fingerprint(bytes.fromhex(identity.fingerprint))
    hello = {"type": "start", "protocol": "direct-1", "role": "source",
             "video": asdict(VideoSettings(720, 1280, 30, 6000))}
    try:
        async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + identity.key}) as http:
            old = await http.ws_connect(endpoint + "/stream", ssl=pin, autoping=False, heartbeat=None)
            await old.send_json(hello)
            assert (await old.receive_json(timeout=3))["type"] == "ready"

            # Live (just spoke): a second phone is still refused and health reports busy.
            async with http.get(endpoint + "/health", ssl=pin) as response:
                assert (await response.json())["busy"]
            async with http.ws_connect(endpoint + "/stream", ssl=pin) as second:
                await second.send_json(hello)
                assert (await second.receive_json(timeout=3))["type"] == "error"

            # The old phone goes silent (Wi-Fi dropped without a close): health stops reporting busy
            # and the reconnecting phone takes over within a moment instead of waiting 10-20 s.
            await asyncio.sleep(0.8)
            async with http.get(endpoint + "/health", ssl=pin) as response:
                assert not (await response.json())["busy"]
            async with http.ws_connect(endpoint + "/stream", ssl=pin) as new:
                await new.send_json(hello)
                assert (await new.receive_json(timeout=8))["type"] == "ready"
                assert agent.busy and agent.ws is not None and len(disconnects) == 1
                await new.send_json({"type": "ping", "time": 5})
                assert await new.receive_json(timeout=3) == {"type": "pong", "time": 5}
            await old.close()
    finally:
        await runner.cleanup()
