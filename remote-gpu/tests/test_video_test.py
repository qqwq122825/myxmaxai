import time

import cv2
import numpy as np
import pytest

from rgpu.video_test import LoopingVideo, VideoPlayback, fit_frame


@pytest.fixture
def short_video(tmp_path):
    path = tmp_path / "循环素材.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 20, (96, 64))
    assert writer.isOpened()
    for color in ((0, 0, 230), (230, 0, 0), (0, 220, 0), (0, 200, 220)):
        writer.write(np.full((64, 96, 3), color, dtype=np.uint8))
    writer.release()
    return path


def wait_until(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(.01)
    assert condition()


def test_video_loops_back_to_first_frame_and_unicode_path(short_video):
    source = LoopingVideo(short_video)
    try:
        frames = [source.read() for _ in range(13)]
        assert source.loops == 3
        assert source.fps == 20
        for i in (4, 8, 12):
            assert np.max(np.abs(frames[0].astype(int) - frames[i].astype(int))) < 3
    finally:
        source.close()


def test_playback_repeats_at_source_rate_then_blacks_and_releases_camera(short_video):
    cameras = []
    class Camera:
        def __init__(self, width, height):
            self.frames = []
            self.closed = False
            cameras.append(self)
        def send(self, frame):
            self.frames.append(frame.copy())
            return True
        def close(self):
            self.closed = True
    playback = VideoPlayback(short_video, 128, 72, camera_factory=Camera)
    started = time.monotonic()
    playback.start()
    wait_until(lambda: playback.loops >= 2)
    playback.stop()
    playback.thread.join(2)
    assert not playback.thread.is_alive()
    assert not playback.error
    assert .3 < time.monotonic() - started < 3
    assert cameras[0].closed and cameras[0].frames[-1].max() == 0
    assert cameras[0].frames[0].shape == (72, 128, 3)
    assert cameras[0].frames[0][:, 0].max() == 0  # Padding at the left edge.
    # A second run can acquire a fresh camera and begins at the first source frame.
    again = VideoPlayback(short_video, 128, 72, camera_factory=Camera)
    again.start()
    wait_until(lambda: again.frames >= 1)
    again.stop()
    again.thread.join(2)
    assert cameras[1].closed
    assert np.array_equal(cameras[0].frames[0], cameras[1].frames[0])


def test_invalid_file_does_not_acquire_camera(tmp_path):
    path = tmp_path / "broken.mp4"
    path.write_bytes(b"invalid video")
    def forbidden(*_):
        pytest.fail("Must validate video before opening camera")
    playback = VideoPlayback(path, 128, 72, camera_factory=forbidden)
    playback.start()
    playback.thread.join(3)
    assert playback.error and not playback.thread.is_alive()


def test_camera_failure_releases_resources(short_video):
    closed = []
    class BrokenCamera:
        def send(self, image):
            raise RuntimeError("test write failure")
        def close(self):
            closed.append(True)
    playback = VideoPlayback(short_video, 128, 72, camera_factory=lambda *_: BrokenCamera())
    playback.start()
    playback.thread.join(3)
    assert playback.error == "test write failure"
    assert closed == [True]


def test_portrait_fitting_preserves_complete_frame():
    image = np.full((100, 50, 3), (0, 220, 0), np.uint8)
    frame = fit_frame(image, 160, 90)
    assert frame.shape == (90, 160, 3)
    assert tuple(frame[45, 80]) == (0, 220, 0)
    assert frame[:, :50].max() == 0 and frame[:, -50:].max() == 0
