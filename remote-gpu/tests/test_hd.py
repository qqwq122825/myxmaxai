"""1080p negotiation, real H.264 pixels, and phone -> server -> PC relay."""
import asyncio
import pytest

from rgpu.bridge import ResultSource
from rgpu.config import VideoSettings, VIDEO_PRESETS, check_remote_video_size, video_limits
from rgpu.direct import connect_direct, connect_viewer
from rgpu.direct_codec import H264Encoder, H264Decoder
from rgpu.media import PatternTrack
from tests.test_managed import server, until


@pytest.mark.parametrize("size", [(1920, 1080), (1080, 1920)])
async def test_full_hd_real_codec_keeps_size_colors_and_orientation(size):
    settings = VideoSettings(*size, 30, 8000)
    encoder, decoder = H264Encoder(settings), H264Decoder(settings)
    source = PatternTrack(settings)
    try:
        frames = []
        for _ in range(4):
            for packet in encoder.encode(await source.recv()):
                frames.extend(decoder.decode(packet))
        assert frames
        image = frames[-1].to_ndarray(format="bgr24")
        assert image.shape == (size[1], size[0], 3)
        assert image[:40, :40, 1].mean() > 110
        assert frames[-1].pts > frames[0].pts
    finally:
        source.stop()


@pytest.mark.parametrize("size", [(1920, 1080), (1080, 1920)])
async def test_phone_full_hd_reaches_server_and_separate_pc_without_downscaling(tmp_path, size):
    agent, runner, pair = await server(tmp_path)
    settings = VideoSettings(*size, 30, 8000)
    stop_phone, stop_pc = asyncio.Event(), asyncio.Event()
    server_frames, pc_frames, phone_frames = [], [], []
    agent.preview = lambda image: server_frames.append(image.shape)
    pc = asyncio.create_task(connect_viewer(pair, stop_pc, "none", media_timeout=15,
        preview=lambda image: pc_frames.append((image.shape, float(image[:40, :40, 1].mean())))))
    phone = asyncio.create_task(connect_direct(pair, "test", settings, stop_phone, "none",
        role="source", media_timeout=15, preview=lambda _: phone_frames.append(True)))
    try:
        await until(lambda: agent.receiving and agent.viewer_busy, seconds=15)
        assert agent.settings == settings
        await agent.start_capture(ResultSource("echo"))
        assert agent.capture.settings == settings
        await agent.set_return(True)
        await until(lambda: len(pc_frames) >= 5, seconds=20)
        expected = (size[1], size[0], 3)
        assert expected in server_frames
        assert all(shape == expected and green > 110 for shape, green in pc_frames)
        assert not phone_frames
        assert not phone.done() and not pc.done()
    finally:
        stop_phone.set()
        stop_pc.set()
        await asyncio.gather(phone, pc, return_exceptions=True)
        await agent.stop_capture()
        await runner.cleanup()


def test_hd_presets_and_old_server_error_are_explicit():
    assert VIDEO_PRESETS["1080P 横屏 · 1920×1080"] == (1920, 1080)
    assert VIDEO_PRESETS["1080P 竖屏 · 1080×1920"] == (1080, 1920)
    for size in ((1920, 1080), (1080, 1920)):
        settings = VideoSettings(*size)
        check_remote_video_size({"video_limits": video_limits()}, settings)
        with pytest.raises(ValueError, match="服务器.*1080p"):
            check_remote_video_size({}, settings)
    check_remote_video_size({}, VideoSettings())  # Existing 720p servers still connect.


@pytest.mark.parametrize("size", [(1920, 1920), (2560, 1440), (1080, 1922), (1919, 1080)])
def test_hd_does_not_remove_size_and_pixel_layout_limits(size):
    with pytest.raises(ValueError):
        VideoSettings(*size)


async def test_hd_client_refuses_legacy_server_before_opening_camera(tmp_path, monkeypatch):
    from aiohttp import web
    import rgpu.direct as direct
    from rgpu.pairing import load_identity
    identity = load_identity(tmp_path)
    async def legacy_health(request):
        return web.json_response({"protocol": "direct-1", "version": "0.4.2", "busy": False})
    legacy = web.Application()
    legacy.router.add_get("/health", legacy_health)
    legacy_runner = web.AppRunner(legacy, access_log=None)
    await legacy_runner.setup()
    site = web.TCPSite(legacy_runner, "127.0.0.1", 0, ssl_context=identity.ssl_context())
    await site.start()
    legacy_pair = identity.pairing("127.0.0.1", site._server.sockets[0].getsockname()[1])
    monkeypatch.setattr(direct, "CameraTrack", lambda *_: pytest.fail("Camera must stay closed"))
    try:
        with pytest.raises(ValueError, match="服务器.*1080p"):
            await connect_direct(legacy_pair, "unused-camera", VideoSettings(1080, 1920), asyncio.Event(), "none")
    finally:
        await legacy_runner.cleanup()
