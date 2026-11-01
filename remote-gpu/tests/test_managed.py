import asyncio
import time

from aiohttp import web
import pytest

from rgpu.bridge import ManagedAgent, ResultSource
from rgpu.config import VideoSettings
from rgpu.direct import connect_direct
from rgpu.pairing import load_identity
from rgpu.runtime import pairing_from_fields


async def server(tmp_path):
    identity = load_identity(tmp_path)
    agent = ManagedAgent(identity)
    runner = web.AppRunner(agent.application(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=identity.ssl_context())
    await site.start()
    return agent, runner, identity.pairing("127.0.0.1", site._server.sockets[0].getsockname()[1])


async def until(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("Expected media state did not arrive")
        await asyncio.sleep(.05)


async def test_receive_without_return_then_pause_resume_does_not_disconnect(tmp_path):
    agent, runner, pair = await server(tmp_path)
    stop, stats, frames = asyncio.Event(), [], []
    settings = VideoSettings(320, 240, 15, 1000)
    client = asyncio.create_task(connect_direct(pair, "test", settings, stop, "none",
        statistics=stats.append, preview=lambda image: frames.append(float(image[:30, :30, 1].mean())),
        media_timeout=2))
    try:
        await until(lambda: agent.receiving)
        # Wait longer than the configured media deadline. No return is an expected state.
        await asyncio.sleep(2.6)
        assert not client.done()
        assert stats and stats[-1]["return_active"] is False and stats[-1]["input_fps"] > 0
        await agent.start_capture(ResultSource("echo"))
        await agent.set_return(True)
        await until(lambda: any(color > 110 for color in frames))
        await agent.set_return(False)
        await asyncio.sleep(2.6)
        assert not client.done() and agent.receiving
        # New encoder / timestamps must resume successfully on the same TLS connection.
        previous = len([color for color in frames if color > 110])
        await agent.set_return(True)
        await until(lambda: len([color for color in frames if color > 110]) > previous + 5)
        assert not client.done()
        await agent.stop_capture()
        await asyncio.sleep(.3)
        assert not client.done() and agent.receiving
    finally:
        stop.set()
        await asyncio.wait_for(client, 5)
        await agent.stop_capture()
        await runner.cleanup()


async def test_capture_preview_before_connection_and_size_change_stops_old_source(tmp_path):
    agent, runner, pair = await server(tmp_path)
    stop = asyncio.Event()
    try:
        await agent.start_capture(ResultSource("test"))
        assert agent.capture.ready and not agent.return_enabled
        with pytest.raises(ValueError, match="接收"):
            await agent.set_return(True)
        client = asyncio.create_task(connect_direct(pair, "test", VideoSettings(320, 240, 15, 1000), stop, "none"))
        await until(lambda: agent.receiving)
        assert agent.capture is None
        stop.set()
        await asyncio.wait_for(client, 5)
    finally:
        await agent.stop_capture()
        await runner.cleanup()


def test_manual_fields_preserve_certificate_pin_and_reject_plain_http(tmp_path):
    identity = load_identity(tmp_path)
    pair = pairing_from_fields("127.0.0.1:8766", identity.key, identity.fingerprint)
    assert pair == identity.pairing("127.0.0.1", 8766)
    for address in ("http://example.com:8766", "https://example.com/path", "https://user:pass@example.com"):
        with pytest.raises(ValueError):
            pairing_from_fields(address, identity.key, identity.fingerprint)
    with pytest.raises(ValueError, match="Key"):
        pairing_from_fields("127.0.0.1:8766", identity.key, "")


def test_single_key_works_without_pairing_or_fingerprint_and_survives_address_changes(tmp_path):
    from rgpu.pairing import ConnectionKey
    identity = load_identity(tmp_path)
    token = identity.connection_key
    assert token.startswith("RGK1.")
    assert identity.connection_key == load_identity(tmp_path).connection_key
    credential = ConnectionKey.decode(token)
    assert identity.key not in repr(credential)
    for address, host, port in (("127.0.0.1", "127.0.0.1", 8766),
                                ("example.com:24567", "example.com", 24567)):
        pair = pairing_from_fields(address, token)
        assert pair == identity.pairing(host, port)
    with pytest.raises(ValueError) as result:
        ConnectionKey.decode("SENSITIVE_BUT_INVALID_KEY")
    assert "SENSITIVE_BUT_INVALID_KEY" not in str(result.value)


async def test_single_key_tls_connection_and_invalid_pin_rejected(tmp_path):
    from rgpu.pairing import ConnectionKey
    agent, runner, original = await server(tmp_path)
    stop, samples = asyncio.Event(), []
    key = agent.identity.connection_key
    pair = pairing_from_fields(original.endpoint, key)
    def stats(value):
        samples.append(value)
        stop.set()
    try:
        bad_key = ConnectionKey(agent.identity.key, "0" * 64).encode()
        with pytest.raises(ConnectionError, match="证书"):
            await connect_direct(pairing_from_fields(original.endpoint, bad_key), "test",
                                 VideoSettings(320, 240, 15, 1000), stop, "none")
        await asyncio.wait_for(connect_direct(pair, "test", VideoSettings(320, 240, 15, 1000),
                                             stop, "none", statistics=stats), 8)
        assert samples and samples[-1]["input_fps"] > 0
        assert not samples[-1]["return_active"]
    finally:
        await runner.cleanup()
