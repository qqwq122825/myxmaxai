import json
import socket
import time
from urllib.parse import parse_qs, urlsplit

import numpy as np

from rgpu.config import VideoSettings
import rgpu.lan_gui as gui
from rgpu.pairing import load_identity


def pump(window, predicate=lambda: False, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        window.update()
        if predicate():
            return
        time.sleep(.01)


def prepare(tmp_path, monkeypatch, addresses):
    identity = load_identity(tmp_path / "identity")
    monkeypatch.setattr(gui.LanWindow, "_prepare", lambda _: (identity, addresses))
    monkeypatch.setattr(gui, "data_directory", lambda: tmp_path)
    monkeypatch.setattr(gui, "discover_addresses", lambda: addresses)
    return identity


def close(window):
    window.close()
    pump(window, lambda: not window.thread.is_alive(), timeout=12)
    if window.winfo_exists():
        pump(window, timeout=.1)


def test_automatic_reception_two_buttons_preview_resize_and_camera_lifetime(tmp_path, monkeypatch):
    identity = prepare(tmp_path, monkeypatch, ["192.168.1.9", "10.0.0.5"])
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    (tmp_path / "settings.json").write_text(json.dumps({"port": port}), encoding="utf-8")
    cameras = []
    class FakeOutput:
        def __init__(self, settings, channel, status, camera_factory=None):
            self.settings, self.channel, self.closed = settings, channel, False
            self.image, self.enabled = None, False
            cameras.append(self)
        def submit_image(self, image):
            self.image = image.copy()
        def close(self):
            self.closed = True
        def clear(self):
            self.image = None
    monkeypatch.setattr(gui, "VirtualOutput", FakeOutput)
    registered = []
    monkeypatch.setattr(gui.lan_camera, "ensure_installed", lambda: registered.append(True))
    window = gui.LanWindow()
    try:
        pump(window, lambda: window.active)
        assert window.identity is identity and window.active
        assert window.connection_link.startswith("aimoyan://connect?") and window.qr_picture.width() <= 270
        buttons = [w for w in window.controls.winfo_children() if w.winfo_class() == "TButton"]
        assert [button.cget("text") for button in buttons] == ["启动虚拟摄像头", "关闭虚拟摄像头"]
        window.output_start.invoke()
        pump(window, lambda: bool(cameras))
        assert registered == [True] and "xxx3" in window.output_status.get()
        window.emit("settings", VideoSettings(1080, 1920, 30, 12000))
        pump(window, lambda: window.settings.width == 1080)
        assert cameras[0].closed and cameras[-1].settings.width == 1080
        image = np.zeros((1920, 1080, 3), np.uint8)
        image[:, :540] = (0, 0, 200)
        window.receive_frame(image)
        pump(window, timeout=.2)
        assert window.picture is not None and cameras[-1].image[100, 100, 2] == 200
        for size in ("820x660", "1180x920", "820x660"):
            window.geometry(size)
            pump(window, timeout=.15)
            for button in buttons:
                assert button.winfo_ismapped()
                assert button.winfo_rooty() + button.winfo_height() <= window.winfo_rooty() + window.winfo_height()
            assert window.picture.width() <= window.preview.winfo_width()
        window.output_stop.invoke()
        assert cameras[-1].closed and window.active  # Camera stop leaves reception running.
        window.emit("disconnected")
        pump(window, lambda: window.latest is None)
        assert window.latest is None and window.active and window.connection_link
        def cancelled():
            raise RuntimeError("已取消管理员授权")
        monkeypatch.setattr(gui.lan_camera, "ensure_installed", cancelled)
        window.output_start.invoke()
        pump(window, lambda: not window.output_starting)
        assert "已取消" in window.output_status.get()
        assert "disabled" not in window.output_start.state()
    finally:
        close(window)
    assert not window.thread.is_alive()


def test_no_network_then_auto_recovery_and_busy_port_fallback(tmp_path, monkeypatch):
    prepare(tmp_path, monkeypatch, [])
    with socket.socket() as occupied:
        occupied.bind(("0.0.0.0", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        (tmp_path / "settings.json").write_text(json.dumps({"port": port}), encoding="utf-8")
        window = gui.LanWindow()
        try:
            pump(window, lambda: window.identity is not None)
            assert not window.active and not window.connection_link
            window.emit("addresses", ["192.168.1.9"])
            pump(window, lambda: window.active)
            assert window.active and window.port > port
            addresses = parse_qs(urlsplit(window.connection_link).query)["addr"][0]
            assert addresses == f"192.168.1.9:{window.port}"
        finally:
            close(window)
