"""Writer for the existing MIT UnityCapture / FaceLive shared-memory protocol.

Protocol: vcam/shared.inl (Bernhard Schelling). No driver registration is
performed here. Channel 1 uses *1 names and is isolated from the default
UnityCapture / FaceLive output channel 0.
"""
from __future__ import annotations

import ctypes as ct
import os
from ctypes import wintypes as wt

import cv2
import numpy as np


def kernel32():
    k = ct.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateMutexW": (wt.HANDLE, [ct.c_void_p, wt.BOOL, wt.LPCWSTR]),
        "OpenMutexW": (wt.HANDLE, [wt.DWORD, wt.BOOL, wt.LPCWSTR]),
        "CreateEventW": (wt.HANDLE, [ct.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]),
        "OpenEventW": (wt.HANDLE, [wt.DWORD, wt.BOOL, wt.LPCWSTR]),
        "OpenFileMappingW": (wt.HANDLE, [wt.DWORD, wt.BOOL, wt.LPCWSTR]),
        "MapViewOfFile": (ct.c_void_p, [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ct.c_size_t]),
        "UnmapViewOfFile": (wt.BOOL, [ct.c_void_p]),
        "WaitForSingleObject": (wt.DWORD, [wt.HANDLE, wt.DWORD]),
        "ReleaseMutex": (wt.BOOL, [wt.HANDLE]),
        "SetEvent": (wt.BOOL, [wt.HANDLE]),
        "CloseHandle": (wt.BOOL, [wt.HANDLE]),
    }
    for name, (result, args) in signatures.items():
        method = getattr(k, name)
        method.restype, method.argtypes = result, args
    return k


class UnityCamera:
    def __init__(self, width, height, channel=0, prefix="UnityCapture", set_resolution=True):
        if os.name != "nt":
            raise RuntimeError("FaceLive 虚拟摄像头输出只支持 Windows。")
        if not 0 <= channel <= 2:
            raise ValueError("支持的摄像头通道为 0、1、2。")
        self.width, self.height = width, height
        self.k = kernel32()
        self.mutex = self.want = self.sent = self.mapping = self.view = 0
        self.suffix = "" if channel == 0 else str(channel)
        self.prefix = prefix
        self.writer = self.k.CreateMutexW(None, False, f"RemoteGpuWriter_{prefix}_{channel}")
        if not self.writer:
            raise ct.WinError(ct.get_last_error())
        if ct.get_last_error() == 183:
            self.k.CloseHandle(self.writer)
            self.writer = 0
            raise RuntimeError("已有远程传输程序在写入这个摄像头通道。")
        if set_resolution:
            try:
                import winreg
                path = r"Software\FaceLive\VirtualCam"
                if channel:
                    path += rf"\Channels\{channel}"
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
                    winreg.SetValueEx(key, "Width", 0, winreg.REG_DWORD, width)
                    winreg.SetValueEx(key, "Height", 0, winreg.REG_DWORD, height)
            except Exception:
                self.close()
                raise

    def _open(self):
        if self.view:
            return True
        p, s, k = self.prefix, self.suffix, self.k
        if not self.mutex:
            self.mutex = k.OpenMutexW(0x00100001, False, f"{p}_Mutx{s}")
        if not self.mutex:
            return False  # The capture consumer creates the mapping.
        if not self.want:
            self.want = k.CreateEventW(None, False, False, f"{p}_Want{s}")
        if not self.sent:
            self.sent = k.OpenEventW(0x0002, False, f"{p}_Sent{s}")
        if not self.mapping:
            self.mapping = k.OpenFileMappingW(0x0002, False, f"{p}_Data{s}")
        if self.mapping and self.want and self.sent:
            self.view = k.MapViewOfFile(self.mapping, 0x0002, 0, 0, 0)
        return bool(self.view)

    def send(self, bgr):
        if not self._open():
            return False
        if bgr.shape != (self.height, self.width, 3) or bgr.dtype != np.uint8:
            raise ValueError("摄像头图像形状或类型不正确。")
        rgba = np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)[::-1])
        result = self.k.WaitForSingleObject(self.mutex, 20)
        if result not in (0, 0x80):
            return False
        try:
            header = (ct.c_int32 * 8).from_address(self.view)
            if ct.c_uint32(header[0]).value < rgba.nbytes:
                raise RuntimeError("虚拟摄像头共享内存不足。")
            header[1:8] = (self.width, self.height, self.width, 0, 1, 0, 1000)
            ct.memmove(self.view + 32, rgba.ctypes.data, rgba.nbytes)
        finally:
            self.k.ReleaseMutex(self.mutex)
        self.k.SetEvent(self.sent)
        return True

    def close(self):
        if self.view:
            self.k.UnmapViewOfFile(self.view)
            self.view = 0
        for name in ("mapping", "sent", "want", "mutex", "writer"):
            handle = getattr(self, name, 0)
            if handle:
                self.k.CloseHandle(handle)
                setattr(self, name, 0)
