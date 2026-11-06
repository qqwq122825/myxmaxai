from types import SimpleNamespace
import json
import sys

import rgpu.lan_camera as camera


def test_first_start_registers_once_and_later_start_reuses_device(monkeypatch):
    ready, installed = [], []
    monkeypatch.setattr(camera, "driver_installed", lambda: bool(ready))
    monkeypatch.setattr(camera.ct.windll.shell32, "IsUserAnAdmin", lambda: True)
    def install():
        installed.append(True)
        ready.append(True)
    monkeypatch.setattr(camera, "install_driver", install)
    camera.ensure_installed()
    camera.ensure_installed()
    assert installed == [True]


def test_elevated_helper_uses_own_entry_point(monkeypatch):
    checked, commands = [], []
    def ready():
        checked.append(True)
        return len(checked) > 1
    monkeypatch.setattr(camera, "driver_installed", ready)
    monkeypatch.setattr(camera.ct.windll.shell32, "IsUserAnAdmin", lambda: False)
    monkeypatch.setattr(camera, "run_elevated", lambda program, arguments: commands.append((program, arguments)))
    camera.ensure_installed()
    assert commands[0][0] == sys.executable
    assert commands[0][1][-1] == "--install-lan-camera"


def test_installer_registers_only_own_x64_and_x86_files_and_skips_recopy(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for bits in (64, 32):
        (source / f"LanCaptureFilter{bits}.dll").write_bytes(b"MZ fake LAN driver " + str(bits).encode())
    monkeypatch.setenv("ProgramData", str(tmp_path / "programdata"))
    monkeypatch.setenv("SystemRoot", str(tmp_path / "windows"))
    monkeypatch.setattr(camera, "data_directory", lambda: tmp_path / "userdata")
    monkeypatch.setattr(camera, "driver_directory", lambda: source)
    monkeypatch.setattr(camera.ct.windll.shell32, "IsUserAnAdmin", lambda: True)
    monkeypatch.setattr(camera, "driver_installed", lambda: True)
    calls = []
    monkeypatch.setattr(camera.subprocess, "run", lambda command, **kw: (calls.append(command) or SimpleNamespace(returncode=0)))
    camera.install_driver()
    report = json.loads((tmp_path / "userdata/reports/camera-install.json").read_text(encoding="utf-8"))
    assert report["status"] == "ok"
    assert len(calls) == 2
    assert calls[0][0].endswith("System32\\regsvr32.exe")
    assert calls[1][0].endswith("SysWOW64\\regsvr32.exe")
    assert all("LanCamera" in command[-1] and "LanCaptureFilter" in command[-1] for command in calls)
    monkeypatch.setattr(camera.shutil, "copy2", lambda *args: (_ for _ in ()).throw(AssertionError("Should reuse unchanged DLLs")))
    camera.install_driver()


def test_camera_writer_uses_isolated_name_and_resolution_key(monkeypatch):
    import winreg
    from contextlib import nullcontext
    keys, values, opened = [], [], []
    monkeypatch.setattr(winreg, "CreateKey", lambda root, path: (keys.append(path) or nullcontext(path)))
    monkeypatch.setattr(winreg, "SetValueEx", lambda key, name, _, kind, value: values.append((name, value)))
    monkeypatch.setattr(camera, "UnityCamera", lambda *args, **kw: (opened.append((args, kw)) or "writer"))
    assert camera.open_camera(1080, 1920) == "writer"
    assert keys == [r"Software\FaceLive\LanCamera"]
    assert values == [("Width", 1080), ("Height", 1920)]
    assert opened == [((1080, 1920, 0), {"prefix": "FaceLiveLan", "set_resolution": False})]
