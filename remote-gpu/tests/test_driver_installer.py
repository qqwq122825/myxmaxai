"""Windows PowerShell 5.1 checks. Never install/register real system drivers."""
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell installer")
PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def staged(tmp_path):
    package = tmp_path / "RemoteGpu with space"
    (package / "deploy").mkdir(parents=True)
    (package / "VirtualCam").mkdir()
    installer = package / "deploy" / "install-driver.ps1"
    shutil.copy2(PROJECT / "deploy" / installer.name, installer)
    for name in ("UnityCaptureFilter64.dll", "UnityCaptureFilter32.dll"):
        shutil.copy2(PROJECT.parent / "app" / "VirtualCam" / name, package / "VirtualCam" / name)
    return installer


def run_installer(tmp_path, installer, args=(), arch="System32", harness=None):
    reports = tmp_path / ("reports-" + arch)
    fake_data = tmp_path / "fake-programdata"
    env = dict(os.environ, ProgramData=str(fake_data))
    ps = Path(os.environ["WINDIR"]) / arch / "WindowsPowerShell/v1.0/powershell.exe"
    command = [str(ps), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness or installer)]
    if harness:
        command += ["-Installer", str(installer)]
    command += ["-ReportDirectory", str(reports), *args]
    process = subprocess.run(command, env=env, capture_output=True, timeout=20)
    files = list(reports.glob("driver-install-*.json"))
    assert len(files) == 1, process.stdout.decode(errors="replace") + process.stderr.decode(errors="replace")
    return process.returncode, json.loads(files[0].read_text(encoding="utf-8-sig")), fake_data


@pytest.mark.parametrize("arch", ["System32", "SysWOW64"])
def test_readonly_diagnostics_in_both_windows_powershell_views(tmp_path, staged, arch):
    code, report, target = run_installer(tmp_path, staged, ["-DiagnoseOnly"], arch)
    assert code == 0 and report["status"] == "diagnostics"
    assert len(report["before"]) == 4 and report["after"] == report["before"]
    assert all(entry["exists"] and entry["sha256"] for entry in report["source_files"])
    assert report["registrar"] == [] and not target.exists()


def test_bad_dll_stops_before_copy_or_registration_and_saves_error(tmp_path, staged):
    if not ctypes.windll.shell32.IsUserAnAdmin():
        pytest.skip("Preflight test requires an already elevated test process")
    (staged.parent.parent / "VirtualCam/UnityCaptureFilter64.dll").write_bytes(b"invalid driver")
    code, report, target = run_installer(tmp_path, staged)
    assert code == 1 and report["status"] == "failed"
    assert "Invalid DLL" in report["error"]
    assert report["registrar"] == [] and not target.exists()


def test_third_result_camera_has_separate_clsids_in_both_registry_views(tmp_path, staged):
    code, report, target = run_installer(tmp_path, staged, ["-DiagnoseOnly", "-DeviceCount", "3"])
    assert code == 0 and report["device_count"] == 3
    assert len(report["after"]) == 6
    assert any(item["clsid"].endswith("E70013}") for item in report["after"])
    assert any(item["clsid"].endswith("E70023}") for item in report["after"])
    assert not target.exists()


def test_zero_registrar_exit_does_not_hide_wrong_registered_path(tmp_path, staged):
    if not ctypes.windll.shell32.IsUserAnAdmin():
        pytest.skip("Preflight test requires an already elevated test process")
    harness = tmp_path / "mock-registrar.ps1"
    harness.write_text('''param([string]$Installer, [string]$ReportDirectory)
function Start-Process {
    param($FilePath, $ArgumentList, $WindowStyle, [switch]$Wait, [switch]$PassThru)
    if (-not $Wait -or -not $PassThru -or $WindowStyle -ne 'Hidden') { throw 'Registrar flags missing' }
    # No process is launched and no registry is modified.
    return [pscustomobject]@{ ExitCode = 0 }
}
& $Installer -ReportDirectory $ReportDirectory
exit $LASTEXITCODE
''', encoding="ascii")
    code, report, target = run_installer(tmp_path, staged, harness=harness)
    assert code == 1 and report["status"] == "failed"
    assert "Registry path mismatch" in report["error"]
    assert len(report["registrar"]) == 2
    assert all(item["exit_code"] == 0 for item in report["registrar"])
    assert report["after"] == report["before"]
    assert (target / "FaceLive/VirtualCam/UnityCaptureFilter64.dll").is_file()
