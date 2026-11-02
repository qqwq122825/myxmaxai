"""Reproduce changing a relay receiver's camera size during an active connection.

Uses the real UnityCamera configuration path with a private shared-memory prefix
and a mocked registry. Does not read/write a user's actual camera or settings.
"""
from contextlib import contextmanager
import json
import time
import uuid
import winreg

import rgpu.gui as gui
import rgpu.unity as unity
from rgpu.config import VideoSettings


def main():
    registry, saved = {}, {}
    @contextmanager
    def create_key(root, path):
        yield path
    winreg.CreateKey = create_key
    winreg.SetValueEx = lambda key, name, _reserved, _kind, value: registry.__setitem__((key, name), value)
    original_camera = unity.UnityCamera
    prefix = "OutputSizeWorkflow_" + uuid.uuid4().hex
    unity.UnityCamera = lambda width, height, channel: original_camera(width, height, channel, prefix=prefix)
    gui.load_preferences = lambda _: {}
    gui.save_preferences = lambda role, values: saved.update(values)
    gui.Application.refresh_devices = lambda _: None
    gui.driver_update_required = lambda _: False
    app = gui.Application("local")
    app.withdraw()
    app.network_active = lambda: True
    app.work_mode.set(gui.LOCAL_MODES[1])
    app.settings = VideoSettings(720, 1280)
    app.resolution.set("720P 竖屏 · 720×1280")
    key = r"Software\FaceLive\VirtualCam"
    def until(predicate):
        end = time.monotonic() + 4
        while not predicate() and time.monotonic() < end:
            app.update()
            time.sleep(.01)
        assert predicate(), app.output_status.get()
    try:
        app.start_virtual()
        until(lambda: registry.get((key, "Width")) == 720)
        app.resolution.set("1080P 竖屏 · 1080×1920")
        app.resolution_box.event_generate("<<ComboboxSelected>>")
        until(lambda: registry.get((key, "Width")) == 1080 and registry.get((key, "Height")) == 1920)
        assert app.virtual_output.enabled and app.network_active()
        assert app.virtual_output.settings.width == 1080
        assert app.settings.width == 720  # Stale connection settings must not override output.
        assert saved["resolution"] == "1080P 竖屏 · 1080×1920"
        assert "1080×1920" in app.output_status.get() and "重新打开" in app.output_status.get()

        app.stop_virtual()
        app.start_virtual()
        assert app.virtual_output.settings.width == 1080

        # Horizontal HD must be applied too; it must never fall back to 1280x720.
        app.resolution.set("1080P 横屏 · 1920×1080")
        app.resolution_box.event_generate("<<ComboboxSelected>>")
        until(lambda: registry.get((key, "Width")) == 1920 and registry.get((key, "Height")) == 1080)

        # Uploads keep their negotiated size until the sender explicitly reconnects.
        current = app.virtual_output
        app.work_mode.set(gui.LOCAL_MODES[0])
        app.resolution.set("720P 横屏 · 1280×720")
        app.resolution_box.event_generate("<<ComboboxSelected>>")
        assert app.virtual_output is current
        assert app.engine.thread.is_alive()
        print(json.dumps({"ok": True, "live_relay_720_to_1080": True,
                          "camera_registry_updated": True, "restart_uses_current_selection": True,
                          "landscape_hd": True, "upload_negotiation_preserved": True}))
    finally:
        app.close_window()
        end = time.monotonic() + 6
        while app.engine.thread.is_alive() and time.monotonic() < end:
            app.update()
            time.sleep(.02)
        assert not app.engine.thread.is_alive()


if __name__ == "__main__":
    main()
