"""Explicit Windows/Tk check; fake writer, no camera, network or user settings."""
import json
import time
from types import SimpleNamespace

import rgpu.gui as gui
import rgpu.unity as unity


def main():
    class Camera:
        def __init__(self, *args): pass
        def send(self, image): return True
        def close(self): pass

    gui.load_preferences = lambda _: {}
    gui.save_preferences = lambda *_: None
    gui.Application.refresh_devices = lambda _: None
    gui.driver_update_required = lambda _: True  # A working older driver must not block Start.
    unity.UnityCamera = Camera
    app = gui.Application("local")
    app.geometry("1080x760")
    app.update()
    try:
        assert app.vcam_start.winfo_ismapped() and app.vcam_stop.winfo_ismapped()
        assert app.vcam_start["text"] == "启动虚拟摄像头"
        assert app.vcam_stop.instate(["disabled"])
        app.vcam_start.invoke()
        assert app.virtual_output.enabled
        assert app.vcam_start.instate(["disabled"]) and not app.vcam_stop.instate(["disabled"])
        writer = app.virtual_output
        app.start_virtual()  # Starting twice must not toggle it off or create another writer.
        assert app.virtual_output is writer and writer.enabled
        app.vcam_stop.invoke()
        assert not writer.enabled and writer.thread.is_alive() and app.engine.thread.is_alive()
        assert not app.vcam_start.instate(["disabled"]) and app.vcam_stop.instate(["disabled"])
        app.stop_virtual()
        assert not writer.enabled

        initial, selected = app.canvas.yview()[0], app.camera_box.get()
        app.camera_box.event_generate("<MouseWheel>", delta=-120)
        app.update()
        assert app.canvas.yview()[0] > initial
        assert app.camera_box.get() == selected
        app.scroll_rail._press(SimpleNamespace(y=app.scroll_rail.winfo_height() - 1))
        app.scroll_rail._release(None)
        app.update()
        assert app.canvas.yview()[1] > .99
        assert app.vcam_start.winfo_ismapped() and app.vcam_stop.winfo_ismapped()
        print(json.dumps({"ok": True, "separate_buttons": True, "idempotent": True,
                          "older_driver_allowed": True, "network_loop_preserved": True,
                          "wheel_over_combobox": True, "rail_drag": True, "fixed_actions": True}))
    finally:
        app.close_window()
        end = time.monotonic() + 6
        while app.engine.thread.is_alive() and time.monotonic() < end:
            app.update()
            time.sleep(.02)
        assert not app.engine.thread.is_alive()


if __name__ == "__main__":
    main()
