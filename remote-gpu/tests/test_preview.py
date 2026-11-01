import asyncio

import aiohttp
import aiortc
import pytest

from rgpu.config import VideoSettings
from rgpu.media import CameraDevice, select_camera
from rgpu.preview import preview_local


def test_frozen_reports_use_user_directory_not_launcher_cwd(monkeypatch, tmp_path):
    import sys
    from rgpu.paths import reports_directory
    launcher = tmp_path / "launcher"
    launcher.mkdir()
    monkeypatch.chdir(launcher)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "user"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert reports_directory() == tmp_path / "user" / "FaceLiveRemoteGpu" / "reports"
    assert not (launcher / "reports").exists()


def test_yy_selection_uses_moniker_and_rejects_ambiguous_name():
    cameras = [CameraDevice("YY开播", "@device_yyanchorvcam"), CameraDevice("Camera", "one"), CameraDevice("Camera", "two")]
    assert select_camera("yy", cameras).id == "@device_yyanchorvcam"
    assert select_camera("two", cameras).name == "Camera"
    with pytest.raises(ValueError):
        select_camera("Camera", cameras)


async def test_preview_reads_frames_without_network_or_virtual_output(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Local preview must not create a network session")
    monkeypatch.setattr(aiohttp, "ClientSession", forbidden)
    monkeypatch.setattr(aiortc, "RTCPeerConnection", forbidden)
    seen = []
    result = await preview_local("test", VideoSettings(320, 240, 20, 1000), asyncio.Event(),
                                 seconds=0.2, on_frame=lambda f: seen.append((f.width, f.height, f.pts)))
    assert result["ok"] and result["frames"] >= 4
    assert not result["network_used"] and not result["virtual_camera_written"]
    assert all((w, h) == (320, 240) for w, h, pts in seen)
    assert all(a[2] < b[2] for a, b in zip(seen, seen[1:]))


async def test_cancel_preview_stops_its_output_worker(monkeypatch):
    from rgpu import preview
    original = preview.FrameSink
    sinks = []
    def sink(*args, **kwargs):
        result = original(*args, **kwargs)
        sinks.append(result)
        return result
    monkeypatch.setattr(preview, "FrameSink", sink)
    frames_seen = asyncio.Event()
    count = 0
    def frame_received(frame):
        nonlocal count
        count += 1
        if count >= 3:
            frames_seen.set()
    task = asyncio.create_task(preview.preview_local("test", VideoSettings(320, 240, 20, 1000),
                                                     asyncio.Event(), on_frame=frame_received))
    await asyncio.wait_for(frames_seen.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sinks and all(not output.thread.is_alive() for output in sinks)
