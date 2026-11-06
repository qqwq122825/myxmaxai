param([string]$Python = 'python',
      [string]$MSBuild = 'D:\AI-FaceSwap\tools\BuildTools\MSBuild\Current\Bin\MSBuild.exe',
      [string]$WindowsSdk = '10.0.26100.0')
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath $Python -PathType Leaf) { $Python = (Resolve-Path -LiteralPath $Python).Path }
$projectRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = Split-Path -Parent $projectRoot
$output = Join-Path $projectRoot 'dist\FaceLiveLan-1.0.4'
$virtualCam = Join-Path $repoRoot 'app\VirtualCam'
$driverRoot = Join-Path $repoRoot 'vcam\Build\LanCameraDriver'
New-Item -ItemType Directory -Path $driverRoot -Force | Out-Null
foreach ($arch in @('x64', 'Win32')) {
    & $MSBuild (Join-Path $repoRoot 'vcam\UnityCaptureFilter.vcxproj') '/p:Configuration=Release' "/p:Platform=$arch" '/p:PlatformToolset=v143' "/p:WindowsTargetPlatformVersion=$WindowsSdk" '/p:FaceLiveLan=true' '/v:minimal' '/nologo'
    if ($LASTEXITCODE -ne 0) { throw "Failed to build LAN camera driver: $arch" }
    $bits = if ($arch -eq 'x64') { '64' } else { '32' }
    Copy-Item -LiteralPath (Join-Path $repoRoot "vcam\Build\Release-LanCaptureFilter$bits\LanCaptureFilter$bits.dll") -Destination $driverRoot -Force
}
Push-Location $projectRoot
try {
    & $Python -m PyInstaller --noconfirm --clean --onefile --windowed --name FaceLiveLan `
        --distpath $output --workpath (Join-Path $projectRoot 'build\lan') `
        --specpath (Join-Path $projectRoot 'build') --collect-all av --collect-all cv2 `
        --collect-all aiortc --exclude-module windows_capture `
        --add-data "${driverRoot};LanCameraDriver" `
        --add-data "${virtualCam}\LICENSE-UnityCaptureFilter.txt;VirtualCam" `
        --add-data "$(Join-Path $PSScriptRoot 'enable-lan-firewall.ps1');deploy" `
        lan_receiver.py
    if ($LASTEXITCODE -ne 0) { throw 'Failed to package FaceLiveLan.exe' }
    & $Python (Join-Path $PSScriptRoot 'collect-licenses.py') (Join-Path $output 'licenses')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to collect licenses' }
    Copy-Item -LiteralPath (Join-Path $virtualCam 'LICENSE-UnityCaptureFilter.txt') -Destination (Join-Path $output 'licenses') -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot 'THIRD_PARTY_NOTICES.md') -Destination $output -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot 'docs\22-局域网接收端.md') -Destination (Join-Path $output '使用说明.md') -Force
    & $Python -m deploy.package-lan
    if ($LASTEXITCODE -ne 0) { throw 'Failed to verify the LAN archive' }
    Write-Host "Built: $output\FaceLiveLan.exe"
} finally { Pop-Location }
