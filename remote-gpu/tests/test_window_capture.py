import asyncio
import ctypes as ct
import os
import time

import numpy as np
import pytest

from rgpu.config import VideoSettings
from rgpu.window_capture import WindowDevice, WindowTrack, crop_image


def test_crop_keeps_only_selected_region_and_rejects_empty_image():
    image = np.arange(10 * 20 * 3).reshape((10, 20, 3))
    assert np.array_equal(crop_image(image, (2, 1, 3, 4)), image[1:6, 2:17])
    for crop in ((-1, 0, 0, 0), (20, 0, 0, 0), (0, 5, 0, 5)):
        with pytest.raises(ValueError):
            crop_image(image, crop)


@pytest.mark.skipif(os.name != "nt", reason="Windows native integration")
@pytest.mark.parametrize("method", ["gdi", "wgc"])
async def test_real_selected_window_capture_and_cleanup(method):
    import tkinter as tk
    root = tk.Tk()
    root.title("RemoteGpu capture verification fixture")
    root.geometry("480x320+60+60")
    root.configure(bg="#00bb33")
    root.update()
    u = ct.WinDLL("user32", use_last_error=True)
    u.GetParent.restype, u.GetParent.argtypes = ct.c_void_p, [ct.c_void_p]
    hwnd = int(u.GetParent(root.winfo_id()) or root.winfo_id())
    track = None
    try:
        track = WindowTrack(WindowDevice(hwnd, os.getpid(), root.title()), VideoSettings(320, 240, 15, 1000), method)
        deadline = time.monotonic() + 12
        received = asyncio.create_task(track.recv())
        while not received.done() and time.monotonic() < deadline:
            root.update()
            await asyncio.sleep(.02)
        frame = await asyncio.wait_for(received, 1)
        image = frame.to_ndarray(format="bgr24")
        assert image.shape == (240, 320, 3)
        assert image[70:170, 80:240, 1].mean() > 120
        assert image[70:170, 80:240, 2].mean() < 80
        track.stop()
        await track.wait_closed()
        track = None
    finally:
        if track:
            track.stop()
            await track.wait_closed()
        root.destroy()
