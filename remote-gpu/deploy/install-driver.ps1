param([switch]$Uninstall, [switch]$DiagnoseOnly, [string]$ReportDirectory,
      [ValidateRange(2,3)][int]$DeviceCount = 2)
# ASCII source works in Windows PowerShell 5.1 without a BOM.
$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $PSScriptRoot
$driverSource = Join-Path $packageRoot 'VirtualCam'
$driverTarget = Join-Path $env:ProgramData 'FaceLive\VirtualCam'
if (-not $ReportDirectory) { $ReportDirectory = Join-Path $env:LOCALAPPDATA 'FaceLiveRemoteGpu\reports' }
New-Item -ItemType Directory -Path $ReportDirectory -Force | Out-Null
$runId = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$reportPath = Join-Path $ReportDirectory "driver-install-$runId.json"
$result = [ordered]@{ version = 2; time = (Get-Date -Format o); status = 'starting';
    script = $PSCommandPath; source = $driverSource; target = $driverTarget;
    diagnose_only = [bool]$DiagnoseOnly; uninstall = [bool]$Uninstall;
    device_count = $DeviceCount;
    source_files = @(); before = @(); after = @(); registrar = @(); backups = @(); error = $null }
$exitCode = 0

function Get-DriverFileHash([string]$LiteralPath, [string]$Algorithm = 'SHA256') {
    # Avoid dependence on Get-FileHash module auto-loading in stripped server shells.
    $stream = [IO.File]::OpenRead($LiteralPath)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return [pscustomobject]@{ Hash = ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-', '') } }
    finally { $hash.Dispose(); $stream.Dispose() }
}

function File-Info([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @{ path = $Path; exists = $false } }
    return @{ path = $Path; exists = $true; sha256 = (Get-DriverFileHash -LiteralPath $Path -Algorithm SHA256).Hash }
}

function Driver-Registrations {
    foreach ($arch in @('x64', 'x86')) {
        $view = if ($arch -eq 'x64') { [Microsoft.Win32.RegistryView]::Registry64 } else { [Microsoft.Win32.RegistryView]::Registry32 }
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::ClassesRoot, $view)
        try {
            $ids = if ($arch -eq 'x64') { @('10', '12') } else { @('20', '22') }
            if ($DeviceCount -eq 3) { $ids += $(if ($arch -eq 'x64') { '13' } else { '23' }) }
            foreach ($ending in $ids) {
                $clsid = '{5C2CD55C-92AD-4999-8666-912BD3E700' + $ending + '}'
                $key = $base.OpenSubKey("CLSID\$clsid\InprocServer32")
                try {
                    $path = if ($null -ne $key) { [string]$key.GetValue('') } else { '' }
                    $file = if ($path) { File-Info $path } else { @{ path = ''; exists = $false } }
                    [pscustomobject]@{ architecture = $arch; clsid = $clsid; registered_path = $path; file = $file }
                } finally { if ($null -ne $key) { $key.Dispose() } }
            }
        } finally { $base.Dispose() }
    }
}

function Assert-DriverArchitecture([string]$Path, [int]$Machine) {
    $stream = [IO.File]::OpenRead($Path)
    $reader = New-Object IO.BinaryReader($stream)
    try {
        if ($reader.ReadUInt16() -ne 0x5A4D) { throw "Invalid DLL (MZ): $Path" }
        $stream.Position = 0x3C
        $offset = $reader.ReadInt32()
        if ($offset -lt 64 -or $offset -gt $stream.Length - 6) { throw "Invalid DLL header: $Path" }
        $stream.Position = $offset
        if ($reader.ReadUInt32() -ne 0x4550 -or $reader.ReadUInt16() -ne $Machine) { throw "Wrong DLL architecture: $Path" }
    } finally { $reader.Dispose(); $stream.Dispose() }
}

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $result['administrator'] = ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $result['before'] = @(Driver-Registrations)
    $names = @('UnityCaptureFilter64.dll', 'UnityCaptureFilter32.dll')
    $result['source_files'] = @($names | ForEach-Object { File-Info (Join-Path $driverSource $_) })
    if ($DiagnoseOnly) { $result['status'] = 'diagnostics' }
    else {
        if (-not $result.administrator) { throw 'Administrator PowerShell is required. Right-click install-driver.cmd and choose Run as administrator.' }
        if (-not $Uninstall) {
            Assert-DriverArchitecture (Join-Path $driverSource $names[0]) 0x8664
            Assert-DriverArchitecture (Join-Path $driverSource $names[1]) 0x014C
            # Check every changed target before replacing either DLL.
            foreach ($name in $names) {
                $target = Join-Path $driverTarget $name
                $source = Join-Path $driverSource $name
                if ((Test-Path -LiteralPath $target) -and (Get-DriverFileHash -LiteralPath $target).Hash -ne (Get-DriverFileHash -LiteralPath $source).Hash) {
                    try {
                        $handle = [IO.File]::Open($target, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
                        $handle.Dispose()
                    } catch { throw "Driver file is in use or not writable: $target. Close DeepFaceLive, Pro, bridge and camera checkers. $($_.Exception.Message)" }
                }
            }
            New-Item -ItemType Directory -Path $driverTarget -Force | Out-Null
            foreach ($name in $names) {
                $target = Join-Path $driverTarget $name
                $source = Join-Path $driverSource $name
                if ((Test-Path -LiteralPath $target) -and (Get-DriverFileHash -LiteralPath $target).Hash -eq (Get-DriverFileHash -LiteralPath $source).Hash) { continue }
                if (Test-Path -LiteralPath $target) {
                    $backup = Join-Path $driverTarget "Backups\$runId\$name"
                    New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
                    Copy-Item -LiteralPath $target -Destination $backup -Force
                    $result['backups'] += $backup
                }
                Copy-Item -LiteralPath $source -Destination $target -Force
                if ((Get-DriverFileHash -LiteralPath $target).Hash -ne (Get-DriverFileHash -LiteralPath $source).Hash) { throw "Copied DLL hash mismatch: $target" }
            }
        }
        foreach ($item in @(@('System32', $names[0]), @('SysWOW64', $names[1]))) {
            $systemDir = $item[0]
            if ($systemDir -eq 'System32' -and [Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) { $systemDir = 'Sysnative' }
            $registrar = Join-Path $env:WINDIR "$systemDir\regsvr32.exe"
            $target = Join-Path $driverTarget $item[1]
            $flags = if ($Uninstall) { '/s /u' } else { '/s /i:UnityCaptureDevices=' + $DeviceCount }
            # Wait for the GUI executable and read its actual exit code.
            $arguments = $flags + ' "' + $target + '"'
            $process = Start-Process -FilePath $registrar -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
            $result['registrar'] += @{ executable = $registrar; driver = $target; exit_code = $process.ExitCode }
            if ($process.ExitCode -ne 0) { throw "regsvr32 failed: $target, exit code $($process.ExitCode)" }
        }
        $result['after'] = @(Driver-Registrations)
        if (-not $Uninstall) {
            foreach ($registration in $result.after) {
                $name = if ($registration.architecture -eq 'x64') { $names[0] } else { $names[1] }
                $expected = Join-Path $driverTarget $name
                if (-not [string]::Equals($registration.registered_path, $expected, [StringComparison]::OrdinalIgnoreCase)) { throw "Registry path mismatch: $($registration.clsid) -> $($registration.registered_path), expected $expected" }
                if (-not $registration.file.exists -or $registration.file.sha256 -ne (Get-DriverFileHash -LiteralPath (Join-Path $driverSource $name)).Hash) { throw "Registered DLL hash mismatch: $($registration.clsid)" }
            }
        }
        $result['status'] = if ($Uninstall) { 'uninstalled' } else { 'verified' }
        Write-Host 'Driver registration completed and verified. Restart camera applications.'
    }
} catch {
    $result['status'] = 'failed'
    $result['error'] = $_.Exception.Message
    $exitCode = 1
    Write-Host ("FAILED: " + $result.error) -ForegroundColor Red
} finally {
    try { $result['after'] = @(Driver-Registrations) } catch { $result['registry_read_error'] = $_.Exception.Message }
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $reportPath -Encoding UTF8
    Write-Host "Report: $reportPath"
}
exit $exitCode
