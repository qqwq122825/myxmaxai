param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw '请在管理员 PowerShell 中运行本脚本。先关闭使用 FaceLive 摄像头的所有程序。'
}
$packageRoot = Split-Path -Parent $PSScriptRoot
$driverSource = Join-Path $packageRoot 'VirtualCam'
$driverTarget = Join-Path $env:ProgramData 'FaceLive\VirtualCam'
if (-not $Uninstall) {
    foreach ($name in @('UnityCaptureFilter64.dll', 'UnityCaptureFilter32.dll')) {
        if (-not (Test-Path -LiteralPath (Join-Path $driverSource $name))) { throw "驱动文件缺失：$name。请使用完整发布包。" }
    }
    New-Item -ItemType Directory -Force -Path $driverTarget | Out-Null
    $backupRoot = Join-Path $driverTarget ('Backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
    foreach ($name in @('UnityCaptureFilter64.dll', 'UnityCaptureFilter32.dll')) {
        $installed = Join-Path $driverTarget $name
        if ((Test-Path -LiteralPath $installed) -and ((Get-FileHash -LiteralPath $installed).Hash -ne (Get-FileHash -LiteralPath (Join-Path $driverSource $name)).Hash)) {
            New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
            Copy-Item -LiteralPath $installed -Destination (Join-Path $backupRoot $name) -Force
        }
    }
    Copy-Item -LiteralPath (Join-Path $driverSource 'UnityCaptureFilter64.dll') -Destination $driverTarget -Force
    Copy-Item -LiteralPath (Join-Path $driverSource 'UnityCaptureFilter32.dll') -Destination $driverTarget -Force
}
foreach ($item in @(@('System32', 'UnityCaptureFilter64.dll'), @('SysWOW64', 'UnityCaptureFilter32.dll'))) {
    $registrar = Join-Path $env:WINDIR ($item[0] + '\regsvr32.exe')
    $driver = Join-Path $driverTarget $item[1]
    if ($Uninstall) { & $registrar '/s' '/u' $driver }
    else { & $registrar '/s' '/i:UnityCaptureDevices=2' $driver }
    if ($LASTEXITCODE -ne 0) { throw "虚拟摄像头注册失败：$driver，退出码 $LASTEXITCODE" }
}
if ($Uninstall) { Write-Host '已卸载 FaceLive 虚拟摄像头。原 FaceLive 程序也共用此驱动，需重新安装后才能使用。' }
else { Write-Host '已注册 FaceLive Camera（本地输出）和 FaceLive Camera #2（远程输入）。请重新打开使用摄像头的软件。' }
