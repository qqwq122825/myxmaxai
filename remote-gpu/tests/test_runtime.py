import threading
import time

import numpy as np
import pytest

from rgpu.config import VideoSettings
from rgpu.runtime import VirtualOutput, load_preferences, save_preferences


def test_role_settings_are_isolated_and_tolerate_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    save_preferences("local", {"address": "https://local-example:8766"})
    save_preferences("agent", {"public_host": "server-example"})
    assert load_preferences("local") == {"address": "https://local-example:8766"}
    assert load_preferences("agent") == {"public_host": "server-example"}
    (tmp_path / "FaceLiveRemoteGpu" / "local-settings.json").write_text("broken", encoding="utf-8")
    assert load_preferences("local") == {}
    assert load_preferences("agent")["public_host"] == "server-example"


def test_virtual_camera_toggle_is_independent_and_stale_image_is_cleared(monkeypatch):
    from rgpu import unity
    seen, opened, closed = [], [], []
    class FakeCamera:
        def __init__(self, width, height, channel):
            opened.append(channel)
        def send(self, image):
            seen.append(float(image.mean()))
            return True
        def close(self):
            closed.append(True)
    monkeypatch.setattr(unity, "UnityCamera", FakeCamera)
    output = VirtualOutput(VideoSettings(320, 240, 15, 1000), 0)
    try:
        assert not opened
        output.enabled = True
        deadline = time.monotonic() + 2
        while not opened and time.monotonic() < deadline:
            time.sleep(.02)
        output.submit_image(np.full((240, 320, 3), 150, np.uint8))
        deadline = time.monotonic() + 2
        while not any(value > 100 for value in seen) and time.monotonic() < deadline:
            time.sleep(.02)
        assert any(value > 100 for value in seen)
        output.clear()
        time.sleep(.2)
        assert seen[-1] == 0
        output.enabled = False
        time.sleep(.2)
        assert closed
        assert output.thread.is_alive()  # Its lifetime is independent of network/session toggles.
    finally:
        output.close()
    assert not output.thread.is_alive()


def test_selfcheck_pattern_never_replaces_fresh_input_and_is_removed_after_check(monkeypatch):
    from rgpu import unity
    seen = []
    class FakeCamera:
        def __init__(self, *args): pass
        def send(self, image):
            seen.append(float(image.mean()))
            return True
        def close(self): pass
    monkeypatch.setattr(unity, "UnityCamera", FakeCamera)
    output = VirtualOutput(VideoSettings(320, 240, 30, 1000), 1)
    def until(predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.01)
        assert predicate()
    try:
        output.diagnostic_image(np.full((240, 320, 3), 60, np.uint8))
        output.enabled = True
        until(lambda: bool(seen) and seen[-1] == 60)
        output.submit_image(np.full((240, 320, 3), 180, np.uint8))
        assert output.has_fresh_input()
        until(lambda: seen[-1] == 180)
        output.diagnostic_image()  # Finish selfcheck; keep the real video flowing.
        output.clear()
        until(lambda: seen[-1] == 0)
        assert not output.has_fresh_input()
    finally:
        output.close()
