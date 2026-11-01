import ctypes as ct
from ctypes import wintypes as wt
import os
import uuid

import numpy as np
import pytest

from rgpu.unity import UnityCamera, kernel32

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows shared-memory protocol")


def test_real_shared_memory_channel_isolation_color_orientation_and_single_writer():
    k = kernel32()
    k.CreateFileMappingW.restype = wt.HANDLE
    k.CreateFileMappingW.argtypes = [wt.HANDLE, ct.c_void_p, wt.DWORD, wt.DWORD, wt.DWORD, wt.LPCWSTR]
    prefix = "RemoteGpuTest_" + uuid.uuid4().hex
    handles, views, writers = [], [], []
    try:
        for suffix in ("", "1"):
            handles.append(k.CreateMutexW(None, False, prefix + "_Mutx" + suffix))
            handles.append(k.CreateEventW(None, False, False, prefix + "_Sent" + suffix))
            mapping = k.CreateFileMappingW(wt.HANDLE(-1), None, 4, 0, 32 + 4 * 4 * 4, prefix + "_Data" + suffix)
            handles.append(mapping)
            view = k.MapViewOfFile(mapping, 2, 0, 0, 0)
            assert view
            views.append(view)
            (ct.c_int32 * 8).from_address(view)[0] = 4 * 4 * 4
        for channel in (0, 1):
            writers.append(UnityCamera(4, 4, channel, prefix=prefix, set_resolution=False))
        with pytest.raises(RuntimeError, match="已有"):
            UnityCamera(4, 4, 0, prefix=prefix, set_resolution=False)
        image = np.zeros((4, 4, 3), np.uint8)
        image[:2] = (10, 20, 220)
        assert writers[1].send(image)
        header0, header1 = ((ct.c_int32 * 8).from_address(view) for view in views)
        assert header0[1] == 0  # Channel 0 untouched.
        assert tuple(header1[1:8]) == (4, 4, 4, 0, 1, 0, 1000)
        pixels = np.frombuffer((ct.c_uint8 * 64).from_address(views[1] + 32), dtype=np.uint8).reshape(4, 4, 4)
        assert tuple(pixels[-1, 0]) == (220, 20, 10, 255)  # BGR -> RGBA, bottom-up.
        assert tuple(pixels[0, 0]) == (0, 0, 0, 255)
    finally:
        for writer in writers:
            writer.close()
        for view in views:
            k.UnmapViewOfFile(view)
        for handle in handles:
            if handle:
                k.CloseHandle(handle)
