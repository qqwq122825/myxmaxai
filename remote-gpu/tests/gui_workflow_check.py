"""Run explicitly: two real Tk applications, real local TLS/video, no user camera or settings."""
import json
from pathlib import Path
import socket
import tempfile
import time
import tkinter as tk

import rgpu.gui as gui
from rgpu.pairing import load_identity


def main(resolution="360P 流畅 · 640×360"):
    with tempfile.TemporaryDirectory() as directory:
        identity = load_identity(directory)
        gui.load_preferences = lambda _: {}
        gui.save_preferences = lambda *_: None
        gui.load_identity = lambda: identity
        server, local = gui.Application("agent"), gui.Application("local")
        server.public_host.set("127.0.0.1")
        server.use_input_vcam.set(False)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server.listen_port.set(str(port))
        server.public_port.set(str(port))
        server.withdraw()
        local.withdraw()
        local.resolution.set(resolution)
        local.fps.set("15")
        local.bitrate.set("1000")
        state, deadline, result = 0, time.monotonic() + 30, {}
        try:
            while time.monotonic() < deadline:
                server.update()
                local.update()
                if state == 0 and server.agent and server.generated_code.get() and local.devices:
                    pair = identity.pairing("127.0.0.1", port)
                    local.address.set(pair.endpoint)
                    local.key.set(server.key.get())
                    local.fingerprint.set("")
                    local.camera_box.current(0)
                    server.start()
                    state = 1
                elif state == 1 and "已启动" in server.status.get():
                    local.start()
                    state = 2
                elif state == 2 and server.agent.receiving:
                    server.source_type.set("原画回环（测试网络）")
                    server.change_source_type()
                    server.preview_result()
                    state = 3
                elif state == 3 and server.capture_ready:
                    server.toggle_return()
                    state = 4
                elif state == 4 and local.metric_vars["return_fps"].get() not in ("—", "0.0"):
                    image = local.frames["output"]
                    if image is not None and float(image[:40, :40, 1].mean()) > 110:
                        expected = local.video_settings()
                        assert image.shape[:2] == (expected.height, expected.width)
                        assert server.settings == expected
                        result = {"ok": True, "local_title": local.title(), "server_title": server.title(),
                                  "return_fps": local.metric_vars["return_fps"].get(),
                                  "actual_frame_size": [image.shape[1], image.shape[0]],
                                  "role_isolation": not hasattr(local, "window_box") and not hasattr(server, "camera_box"),
                                  "separate_config_keys": [local.role, server.role],
                                  "tls_roundtrip": True}
                        result["address_and_key_only"] = local.key.get().startswith("RGK1.") and not local.fingerprint.get()
                        break
                time.sleep(.02)
            if not result:
                raise RuntimeError(f"GUI workflow timed out at {state}; local={local.status.get()}, server={server.status.get()}")
        except Exception as ex:
            result = {"ok": False, "state": state, "error": str(ex)}
        finally:
            server.close_window()
            local.close_window()
            end = time.monotonic() + 20
            while time.monotonic() < end and (server.engine.thread.is_alive() or local.engine.thread.is_alive()):
                for app in (server, local):
                    try:
                        app.update()
                    except tk.TclError:
                        pass
                time.sleep(.03)
            result["loops_closed"] = not server.engine.thread.is_alive() and not local.engine.thread.is_alive()
        Path("reports").mkdir(exist_ok=True)
        from rgpu import __version__
        size = result.get("actual_frame_size", [0, 0])
        Path(f"reports/gui-workflow-{__version__}-{size[0]}x{size[1]}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        if not result.get("ok") or not result.get("loops_closed"):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
