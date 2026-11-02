"""Reader-only verdicts for the server virtual camera checker."""
import numpy as np
import pytest

from rgpu.vcam_probe import TARGET_NAME, describe, frame_kind, open_target, _Reader
from rgpu.media import CameraDevice


def test_driver_waiting_patterns_are_not_called_a_picture():
    black = np.zeros((48, 64, 3), np.uint8)
    green = np.zeros_like(black)
    green[:, :] = (0, 254, 0)
    stripes = np.zeros_like(black)
    stripes[:, :, 1] = 255
    stripes[:, :, 2] = np.tile(np.linspace(0, 255, 64, dtype=np.uint8), (48, 1))
    picture = np.zeros_like(black)
    picture[:, :] = (40, 80, 160)
    assert frame_kind(None) == "empty"
    assert frame_kind(black) == "black"
    assert frame_kind(green) == "placeholder"
    assert frame_kind(stripes) == "placeholder"
    assert frame_kind(picture) == "picture"


def test_missing_camera_tells_the_operator_what_to_start():
    level, message = describe(["Integrated Camera"], False, None, None, False)
    assert level == "fail"
    assert TARGET_NAME in message
    assert "install-driver.ps1" in message


def test_live_picture_is_success_for_face_swap_software():
    frame = np.full((720, 1280, 3), (30, 90, 140), np.uint8)
    level, message = describe([TARGET_NAME], True, frame, 28.2, True)
    assert level == "ok"
    assert "成功" in message
    assert "1280×720" in message
    assert "28 FPS" in message


def test_open_uses_exact_device_id_and_never_another_camera(monkeypatch):
    calls = []
    sentinel = object()
    def fake_open(url, **kwargs):
        calls.append((url, kwargs))
        return sentinel
    monkeypatch.setattr("av.open", fake_open)
    devices = [CameraDevice("Physical Camera", "physical-id"), CameraDevice(TARGET_NAME, "exact-camera-2-id")]
    assert open_target(devices) is sentinel
    assert calls[0][0] == "video=exact-camera-2-id"
    assert calls[0][1]["format"] == "dshow"
    with pytest.raises(ValueError):
        open_target(devices[:1])
    with pytest.raises(ValueError):
        open_target(devices + [CameraDevice(TARGET_NAME, "duplicate-id")])
    assert len(calls) == 1


def test_disconnect_clears_the_previous_successful_frame(monkeypatch):
    frame = np.full((48, 64, 3), 100, np.uint8)
    class Connection:
        def __init__(self):
            self.events = iter([("devices", [TARGET_NAME]), ("opened", True),
                                ("frame", (frame, "ok", "成功")), ("error", "视频流已结束")])
        def poll(self, timeout): return True
        def recv(self): return next(self.events)
        def close(self): pass
    class Process:
        pid = 123
        def start(self): pass
        def join(self, timeout): pass
        def is_alive(self): return False
        def close(self): pass
    class Context:
        def Pipe(self, **kwargs): return Connection(), Connection()
        def Process(self, **kwargs): return Process()
    monkeypatch.setattr("rgpu.vcam_probe.mp.get_context", lambda method: Context())
    reader = _Reader()
    reader._read_session()
    assert reader.frame is None
    assert reader.level == "fail"
    assert "已结束" in reader.message


def test_local_output_checker_selects_channel_zero_without_falling_back(monkeypatch):
    calls = []
    monkeypatch.setattr("av.open", lambda url, **kwargs: calls.append(url))
    devices = [CameraDevice("FaceLive Camera", "output-id"), CameraDevice(TARGET_NAME, "input-id")]
    open_target(devices, "FaceLive Camera")
    assert calls == ["video=output-id"]
    with pytest.raises(ValueError):
        open_target(devices[1:], "FaceLive Camera")
    frame = np.full((240, 320, 3), 100, np.uint8)
    level, message = describe(["FaceLive Camera"], True, frame, 30, True, "FaceLive Camera")
    assert level == "ok" and "Camera #2" not in message
