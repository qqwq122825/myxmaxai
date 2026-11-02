"""Explicit Windows/Tk workflow check with fake devices; never installs or opens cameras."""
import json
import threading
import time

import numpy as np

import rgpu.gui as gui
import rgpu.unity as unity
import rgpu.vcam_probe as probe
from rgpu.camtest import test_pattern


class FakeCamera:
    def __init__(self, *args): pass
    def send(self, image): return True
    def close(self): pass


class FakeReader:
    def __init__(self):
        self.stop, self.lock = threading.Event(), threading.Lock()
        self.frame, self.level, self.message = None, "info", "checking"
    def start(self): pass
    def join(self, timeout): pass


def main():
    gui.load_preferences = lambda _: {}
    gui.save_preferences = lambda *_: None
    gui.list_cameras = lambda: []
    gui.list_windows = lambda: []
    gui.Application.prepare_server = lambda self: None
    gui.Application.detect_ip = lambda *args, **kwargs: None
    # Reproduces a working installed DLL differing from the bundled file.
    gui.input_driver_update_required = lambda: True
    unity.UnityCamera = FakeCamera
    probe._Reader = FakeReader
    app = gui.Application("agent")
    app.withdraw()
    def until(predicate):
        end = time.monotonic() + 4
        while not predicate() and time.monotonic() < end:
            app.update()
            time.sleep(.01)
        assert predicate(), app.input_vcam_status.get()
    try:
        assert app.virtual_output is None and not app.use_input_vcam.get()
        app.start_input_virtual()
        app.vcam_reader.frame = test_pattern(app.settings.width, app.settings.height)
        app.vcam_reader.level = "ok"
        until(lambda: "自检通过" in app.input_vcam_status.get() and not app.vcam_readers)
        assert app.virtual_output.enabled
        assert app.virtual_output._diagnostic is None

        app.virtual_output.submit_image(np.full((app.settings.height, app.settings.width, 3), 160, np.uint8))
        app.check_input_virtual()
        app.vcam_reader.frame = np.full((100, 100, 3), 160, np.uint8)
        app.vcam_reader.level = "ok"
        until(lambda: "读回成功" in app.input_vcam_status.get() and not app.vcam_readers)

        app.check_input_virtual()
        app.vcam_reader.level, app.vcam_reader.message = "fail", "驱动未安装"
        until(lambda: "驱动未安装" in app.input_vcam_status.get() and not app.vcam_readers)
        assert str(app.input_vcam_label.cget("foreground")) == "#c0392b"

        app.check_input_virtual()
        reader = app.vcam_reader
        app.stop_input_virtual()
        app.poll_input_check(reader, 0, 0)  # A late check must not override the stopped state.
        until(lambda: not app.vcam_readers)
        assert "已停止" in app.input_vcam_status.get()
        assert not app.virtual_output.enabled and app.engine.thread.is_alive()
        print(json.dumps({"ok": True, "pattern_readback": True, "real_input": True,
                          "missing_driver_red": True, "stop_cancels_check": True,
                          "different_driver_hash_does_not_block": True}))
    finally:
        app.close_window()
        end = time.monotonic() + 6
        while app.engine.thread.is_alive() and time.monotonic() < end:
            app.update()
            time.sleep(.02)
        assert not app.engine.thread.is_alive()


if __name__ == "__main__":
    main()
