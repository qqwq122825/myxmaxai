"""FaceLive Camera #3 output integration, modified 2026-11-02, GPL-3.0.

Independent of the bridge's Camera #2 input and Camera #1/local output.
"""
import os
import threading
import time

import numpy as np

from .FaceLiveUnity import UnityCamera


def require_result_camera():
    if os.name != 'nt':
        raise RuntimeError('结果虚拟摄像头仅支持 Windows')
    import winreg
    path = r'CLSID\{5C2CD55C-92AD-4999-8666-912BD3E70013}\InprocServer32'
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path) as key:
            driver = winreg.QueryValueEx(key, None)[0]
        if not os.path.isfile(driver):
            raise OSError('DLL missing')
    except OSError:
        raise RuntimeError('未安装 FaceLive Camera #3；请以管理员运行 install-result-camera.cmd')


def to_bgr8(image):
    if image.ndim != 3 or image.shape[2] not in (1, 3, 4):
        raise ValueError('结果画面必须是 HWC，1/3/4 通道')
    if image.dtype != np.uint8:
        if not np.issubdtype(image.dtype, np.floating):
            raise ValueError('结果画面须为 uint8 或浮点图像')
        image = np.clip(np.nan_to_num(image) * 255.0, 0, 255).astype(np.uint8)
    if image.shape[2] == 1:
        image = np.repeat(image, 3, axis=2)
    return np.ascontiguousarray(image[:, :, :3])


class ResultCamera:
    def __init__(self, fps=30, stale_seconds=1.0):
        self.fps, self.stale_seconds = fps, stale_seconds
        self._lock, self._exit = threading.Lock(), threading.Event()
        self._latest, self._thread = None, None
        self._message = '结果虚拟摄像头未启动'

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive() and not self._exit.is_set()

    @property
    def status(self):
        with self._lock:
            return self._message

    def _status(self, text):
        with self._lock:
            self._message = text

    def start(self):
        if self.active:
            return
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError('结果摄像头仍在停止，请稍后重试')
        require_result_camera()
        self._exit = threading.Event()
        self.clear()
        self._status('Camera #3 已启用，等待结果画面')
        self._thread = threading.Thread(target=self._run, name='DeepFaceLive result camera', daemon=True)
        self._thread.start()

    def submit(self, image):
        if self.active:
            item = (image.copy(), time.monotonic())
            with self._lock:
                self._latest = item

    def clear(self):
        with self._lock:
            self._latest = None

    def stop(self):
        self._exit.set()
        if self._thread is not None:
            self._thread.join(3)
            if self._thread.is_alive():
                raise RuntimeError('结果摄像头线程尚未停止')
        self.clear()
        self._status('结果虚拟摄像头已关闭')

    def _run(self):
        camera, black, converted, previous = None, None, None, None
        deadline = time.monotonic()
        try:
            while not self._exit.is_set():
                now = time.monotonic()
                with self._lock:
                    item = self._latest
                fresh = item is not None and now - item[1] <= self.stale_seconds
                if fresh:
                    if item is not previous:
                        converted, previous = to_bgr8(item[0]), item
                    height, width = converted.shape[:2]
                    if (not 16 <= width <= 1920 or not 16 <= height <= 1920 or
                            width * height > 1920 * 1080 or width % 4 or height % 2):
                        raise ValueError('尺寸不支持；请选择单独合成画面，最大 1920×1080 / 1080×1920')
                    if camera is None or (camera.width, camera.height) != (width, height):
                        if camera is not None:
                            camera.close()
                        camera = UnityCamera(width, height, channel=2)
                        black = np.zeros_like(converted)
                    image = converted
                else:
                    image = black
                if camera is not None:
                    sent = camera.send(image)
                    message = ('Camera #3 · {}×{} · {}'.format(camera.width, camera.height,
                        '正在输出' if sent else '等待读取端')) if fresh else '结果输入超时，Camera #3 输出黑画面'
                    self._status(message)
                deadline += 1.0 / self.fps
                now = time.monotonic()
                if deadline <= now:
                    deadline = now
                self._exit.wait(max(.001, deadline - now))
        except Exception as ex:
            self._status('结果摄像头失败：' + str(ex))
        finally:
            if camera is not None:
                try:
                    camera.send(black)
                except Exception:
                    pass
                camera.close()
