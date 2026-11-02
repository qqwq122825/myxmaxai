"""Keep the actual Tk controls visible after video images change their size."""
import os
import time

import cv2
import numpy as np
import pytest

import rgpu.video_test_gui as gui

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows desktop layout")


def pump(window, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        window.update()
        time.sleep(.01)


def assert_controls_visible(window):
    left, top = window.winfo_rootx(), window.winfo_rooty()
    right, bottom = left + window.winfo_width(), top + window.winfo_height()
    for control in (window.add_button, window.start_button, window.stop_button, window.size_box):
        assert control.winfo_ismapped(), f"{control} disappeared"
        x, y = control.winfo_rootx(), control.winfo_rooty()
        assert left <= x and top <= y
        assert x + control.winfo_width() <= right, f"{control} extends past the window width"
        assert y + control.winfo_height() <= bottom, f"{control} extends below the window"


@pytest.mark.parametrize("size", [(320, 180), (180, 320)])
def test_loading_landscape_or_portrait_keeps_controls_visible(monkeypatch, tmp_path, size):
    monkeypatch.setattr(gui, "driver_installed", lambda: True)
    path = tmp_path / "layout.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30, size)
    assert writer.isOpened()
    for _ in range(5):
        writer.write(np.full((size[1], size[0], 3), (20, 180, 40), np.uint8))
    writer.release()
    window = gui.VideoTestWindow()
    try:
        window.update()
        window.load_video(str(path))
        deadline = time.monotonic() + 5
        while window.busy and time.monotonic() < deadline:
            window.update()
            time.sleep(.01)
        assert window.path == str(path), window.status.get()
        pump(window, .2)
        initial = (window.preview.winfo_width(), window.preview.winfo_height())
        pump(window, 1.2)
        assert_controls_visible(window)
        assert (window.preview.winfo_width(), window.preview.winfo_height()) == initial, "Preview grows on every redraw"
        # Large video frames and repeated window resizing must not move buttons offscreen.
        window.set_frame(np.zeros((1920, 1080, 3), np.uint8))
        for geometry in ("720x600", "1100x780", "840x660", "720x600"):
            window.geometry(geometry)
            pump(window, .2)
            assert_controls_visible(window)
    finally:
        for identifier in window.tk.call("after", "info"):
            window.after_cancel(identifier)
        window.destroy()
