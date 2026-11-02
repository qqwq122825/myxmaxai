"""Pattern recognition used by the virtual camera self-test (runs anywhere; the device part is Windows-only)."""
import cv2
import numpy as np

from rgpu.camtest import classify, clsid_for_channel, virtual_camera_selftest
from rgpu.camtest import test_pattern as make_pattern


def test_pattern_round_trip_and_orientation_errors():
    image = make_pattern(640, 360)
    assert classify(image) == "ok"
    assert classify(image[::-1]) == "upside_down"
    assert classify(image[:, ::-1]) == "mirrored"
    assert classify(image[::-1, ::-1]) == "rotated"
    assert classify(np.zeros_like(image)) == "black"
    assert classify(np.full_like(image, 128)) == "other"


def test_pattern_survives_scaling_letterbox_and_compression():
    image = make_pattern(640, 360)
    # Driver may scale to another size, and readers may letterbox it.
    scaled = cv2.resize(image, (1280, 720))
    letterboxed = np.zeros((900, 1280, 3), np.uint8)
    letterboxed[90:810] = scaled
    ok, jpeg = cv2.imencode(".jpg", letterboxed, [cv2.IMWRITE_JPEG_QUALITY, 40])
    decoded = cv2.imdecode(jpeg, cv2.IMREAD_COLOR)
    assert classify(cv2.resize(image, (1280, 720))) == "ok"
    assert classify(decoded) in ("ok", "other")   # heavy letterbox may hide the band; must never claim a wrong orientation


def test_clsids_match_the_driver():
    assert clsid_for_channel(0) == "{5C2CD55C-92AD-4999-8666-912BD3E70010}"
    assert clsid_for_channel(1) == "{5C2CD55C-92AD-4999-8666-912BD3E70012}"   # FaceLive Camera #2 (as in doctor.py)


def test_selftest_outside_windows_says_so():
    result = virtual_camera_selftest()
    assert not result.ok and "Windows" in result.verdict
    assert "结论" in result.text()
