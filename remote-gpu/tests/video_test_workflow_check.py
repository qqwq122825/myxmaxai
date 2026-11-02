"""Opt-in real-driver/UI workflow check, using only generated test media."""
import json
from pathlib import Path
import sys
import time

import cv2
import numpy as np

from rgpu.camtest import classify, test_pattern
from rgpu.media import list_cameras
from rgpu.video_test import CAMERA_NAME, VideoPlayback, driver_installed
from rgpu.video_test_gui import VideoTestWindow


def main():
    assert driver_installed(), "Build/install the isolated test driver first"
    repo = Path(__file__).resolve().parents[2]
    fixture = repo / ".build" / "video-test-smoke.avi"
    fixture.parent.mkdir(exist_ok=True)
    writer = cv2.VideoWriter(str(fixture), cv2.VideoWriter_fourcc(*"MJPG"), 30, (640, 360))
    assert writer.isOpened()
    for _ in range(15):
        writer.write(test_pattern(640, 360))
    writer.release()
    names = [device.name for device in list_cameras()]
    assert CAMERA_NAME in names, names

    # Exercise the real widget callbacks and native writer, then open through FFmpeg.
    window = VideoTestWindow()
    def wait_until(condition, seconds=10):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            window.update()
            time.sleep(.01)
        assert condition(), window.status.get()
    results = {}
    try:
        assert str(window.start_button.cget("state")) == "disabled"
        window.load_video(str(fixture))
        wait_until(lambda: window.path is not None and not window.busy)
        window.size.set("720P 横屏 · 1280×720")
        window.start_camera()
        wait_until(lambda: window.playback and window.playback.loops >= 2)
        assert str(window.add_button.cget("state")) == "disabled"
        import av
        with av.open(f"video={CAMERA_NAME}", format="dshow", options={"video_size": "1280x720"}, timeout=(10, 5)) as camera:
            frames = camera.decode(video=0)
            for _ in range(45):
                image = next(frames).to_ndarray(format="bgr24")
                window.update()
                if classify(image) == "ok":
                    break
            assert image.shape == (720, 1280, 3) and classify(image) == "ok"
            results["readback"] = {"device": CAMERA_NAME, "size": [1280, 720], "orientation": classify(image)}
            results["loops"] = window.playback.loops
            window.stop_camera()
            wait_until(lambda: window.playback is None)
            # The consumer is still open: it must see a black frame after stop.
            for _ in range(30):
                stopped = next(frames).to_ndarray(format="bgr24")
                if stopped.max() == 0:
                    break
            assert stopped.max() == 0
            results["stop_black"] = True
        window.start_camera()
        wait_until(lambda: window.playback and window.playback.frames >= 5)
        playback = window.playback
        window.close_window()
        deadline = time.monotonic() + 5
        while playback.thread.is_alive() and time.monotonic() < deadline:
            window.update()
            time.sleep(.01)
        assert not playback.thread.is_alive() and not playback.error
        # Reacquisition after window exit proves the writer mutex was released.
        again = VideoPlayback(fixture, 1280, 720)
        again.start()
        deadline = time.monotonic() + 5
        while again.frames < 2 and time.monotonic() < deadline:
            time.sleep(.02)
        again.stop()
        again.thread.join(3)
        assert again.frames >= 2 and not again.error
        results.update(restart=True, close_releases_camera=True, fixture=str(fixture))
    finally:
        if window.playback:
            window.playback.stop()
            window.playback.thread.join(3)
        try:
            window.destroy()
        except Exception:
            pass
    report = repo / "remote-gpu" / "reports" / "video-test-workflow.json"
    report.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
