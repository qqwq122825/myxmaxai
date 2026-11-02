"""Run with the bundled Python 3.8. No physical devices or GPU model required."""
import ctypes as ct
from ctypes import wintypes as wt
import importlib.util
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch
import uuid

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'payload/apps/DeepFaceLive/backend'
pkg = types.ModuleType('facelive_patch_test')
pkg.__path__ = [str(BACKEND)]
sys.modules[pkg.__name__] = pkg
spec = importlib.util.spec_from_file_location('facelive_patch_test.FaceLiveVirtualCamera', BACKEND / 'FaceLiveVirtualCamera.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from facelive_patch_test.FaceLiveUnity import UnityCamera, kernel32


def until(predicate, seconds=3):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        time.sleep(.01)
    assert predicate()


class ResultCameraTests(unittest.TestCase):
    def test_float_conversion_does_not_mutate_source(self):
        original = np.full((32, 48, 3), .5, np.float32)
        shown = module.to_bgr8(original)
        self.assertEqual(shown.dtype, np.uint8)
        self.assertEqual(int(shown[0, 0, 0]), 127)
        self.assertTrue((original == .5).all())

    def test_missing_driver_does_not_start_background_writer(self):
        output = module.ResultCamera()
        with patch.object(module, 'require_result_camera', side_effect=RuntimeError('missing Camera #3')):
            with self.assertRaises(RuntimeError): output.start()
        self.assertFalse(output.active)

    def test_thread_restart_resize_and_stale_frame_blackout(self):
        seen, closed = [], []
        class Camera:
            def __init__(self, width, height, channel):
                self.width, self.height = width, height
                assert channel == 2
            def send(self, image):
                seen.append((image.shape, float(image.mean())))
                return True
            def close(self): closed.append(True)
        with patch.object(module, 'require_result_camera'), patch.object(module, 'UnityCamera', Camera):
            output = module.ResultCamera(stale_seconds=.1)
            try:
                output.start()
                thread = output._thread
                output.start()
                self.assertIs(output._thread, thread)
                output.submit(np.full((32, 48, 3), 180, np.uint8))
                until(lambda: any(value == 180 for _, value in seen))
                output.submit(np.full((48, 32, 3), 80, np.uint8))
                until(lambda: any(shape == (48, 32, 3) and value == 80 for shape, value in seen))
                until(lambda: '超时' in output.status)
                self.assertEqual(seen[-1][1], 0)
                output.stop()
                self.assertFalse(output.active)
                output.start()
                self.assertTrue(output.active)
            finally:
                output.stop()
            self.assertGreaterEqual(len(closed), 2)

    def test_real_private_shared_memory_hd_color_orientation(self):
        width, height = 1080, 1920
        prefix = 'DeepFaceLiveResultTest_' + uuid.uuid4().hex
        k = kernel32()
        k.CreateFileMappingW.restype = wt.HANDLE
        k.CreateFileMappingW.argtypes = [wt.HANDLE, ct.c_void_p, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR]
        handles = [k.CreateMutexW(None, False, prefix + '_Mutx2'),
                   k.CreateEventW(None, False, False, prefix + '_Sent2')]
        mapping = k.CreateFileMappingW(wt.HANDLE(-1), None, 4, 0, 32 + width * height * 4, prefix + '_Data2')
        handles.append(mapping)
        view = k.MapViewOfFile(mapping, 2, 0, 0, 0)
        self.assertTrue(view)
        header = (ct.c_int32 * 8).from_address(view)
        header[0] = width * height * 4
        output = module.ResultCamera()
        def factory(w, h, channel):
            return UnityCamera(w, h, channel, prefix=prefix, set_resolution=False)
        try:
            with patch.object(module, 'require_result_camera'), patch.object(module, 'UnityCamera', factory):
                output.start()
                image = np.zeros((height, width, 3), np.uint8)
                image[:height // 2] = (10, 50, 200)
                output.submit(image)
                self.assertEqual(k.WaitForSingleObject(handles[1], 2000), 0)
                self.assertEqual(tuple(header[1:4]), (width, height, width))
                pixels = np.frombuffer((ct.c_uint8 * (width * height * 4)).from_address(view + 32), np.uint8).reshape(height, width, 4)
                self.assertEqual(tuple(pixels[-1, 0]), (200, 50, 10, 255))
                self.assertEqual(tuple(pixels[0, 0]), (0, 0, 0, 255))
        finally:
            output.stop()
            k.UnmapViewOfFile(view)
            for handle in handles: k.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
