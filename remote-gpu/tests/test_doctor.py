from contextlib import nullcontext
import sys
from types import SimpleNamespace

from rgpu import doctor


def test_local_driver_is_checked_even_when_server_device_is_not_installed(tmp_path, monkeypatch):
    bundle = tmp_path / "VirtualCam"
    bundle.mkdir()
    (bundle / "UnityCaptureFilter64.dll").write_bytes(b"current")
    installed = tmp_path / "installed.dll"
    installed.write_bytes(b"old")
    paths = []
    def open_key(root, path):
        paths.append(path)
        if "E70012}" in path:
            raise FileNotFoundError("Camera #2 is not installed on this PC")
        return nullcontext("local-driver")
    fake = SimpleNamespace(HKEY_CLASSES_ROOT=1, OpenKey=open_key,
                           QueryValueEx=lambda *_: (str(installed), 1))
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(doctor.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "RemoteGpuLocal.exe"))
    assert doctor.driver_update_required(0)
    assert not doctor.input_driver_update_required()
    assert "E70010}" in paths[0] and "E70012}" in paths[1]
    installed.write_bytes(b"current")
    assert not doctor.driver_update_required(0)
