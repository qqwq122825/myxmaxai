param(
    [string]$Python = 'python',
    [string]$MSBuild = 'D:\AI-FaceSwap\tools\BuildTools\MSBuild\Current\Bin\MSBuild.exe',
    [string]$WindowsSdk = '10.0.26100.0'
)
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath $Python -PathType Leaf) { $Python = (Resolve-Path -LiteralPath $Python).Path }
$projectRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = Split-Path -Parent $projectRoot
$driverRoot = Join-Path $repoRoot 'vcam\Build\VideoTestDriver'
New-Item -ItemType Directory -Path $driverRoot -Force | Out-Null
foreach ($arch in @('x64', 'Win32')) {
    & $MSBuild (Join-Path $repoRoot 'vcam\UnityCaptureFilter.vcxproj') '/p:Configuration=Release' "/p:Platform=$arch" '/p:PlatformToolset=v143' "/p:WindowsTargetPlatformVersion=$WindowsSdk" '/p:FaceLiveVideoTest=true' '/v:minimal' '/nologo'
    if ($LASTEXITCODE -ne 0) { throw "Failed to build test camera driver: $arch" }
    $bits = if ($arch -eq 'x64') { '64' } else { '32' }
    Copy-Item -LiteralPath (Join-Path $repoRoot "vcam\Build\Release-VideoTestFilter$bits\VideoTestFilter$bits.dll") -Destination $driverRoot -Force
}
Push-Location $projectRoot
try {
    $output = Join-Path $repoRoot 'dist\VideoCameraTest'
    & $Python -m PyInstaller --noconfirm --clean --onefile --windowed --name VideoCameraTest --distpath $output --workpath (Join-Path $projectRoot 'build\video-test') --specpath (Join-Path $projectRoot 'build') --collect-all cv2 --exclude-module aiortc --exclude-module av --exclude-module windows_capture --add-data "$driverRoot;VideoTestDriver" --add-data "$(Join-Path $repoRoot 'app\VirtualCam\LICENSE-UnityCaptureFilter.txt');licenses" video_camera_test.py
    if ($LASTEXITCODE -ne 0) { throw 'Failed to package VideoCameraTest.exe' }
    & $Python (Join-Path $PSScriptRoot 'collect-licenses.py') (Join-Path $output 'licenses')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to collect dependency licenses' }
    Copy-Item -LiteralPath (Join-Path $repoRoot 'app\VirtualCam\LICENSE-UnityCaptureFilter.txt') -Destination (Join-Path $output 'licenses') -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot 'docs\20-独立视频摄像头测试.md') -Destination (Join-Path $output '使用说明.md') -Force
    Write-Host "Built: $output\VideoCameraTest.exe"
} finally { Pop-Location }
