param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = Split-Path -Parent $projectRoot
Push-Location $projectRoot
try {
    $releaseVersion = & $Python -c 'from rgpu import __version__; print(__version__)'
    if ($LASTEXITCODE -ne 0 -or $releaseVersion -notmatch '^\d+\.\d+\.\d+$') { throw '无法读取发布版本。' }
    $releaseRoot = Join-Path $projectRoot "dist\$releaseVersion"
    & $Python -m PyInstaller --noconfirm --windowed --onedir --name RemoteGpu --distpath $releaseRoot --collect-all av --collect-all aiortc --collect-all cv2 --collect-all windows_capture remote_gpu.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 打包失败。' }
    $output = Join-Path $releaseRoot 'RemoteGpu'
    Copy-Item -LiteralPath (Join-Path $output 'RemoteGpu.exe') -Destination (Join-Path $output 'RemoteGpuLocal.exe') -Force
    Copy-Item -LiteralPath (Join-Path $output 'RemoteGpu.exe') -Destination (Join-Path $output 'RemoteGpuServer.exe') -Force
    Copy-Item -LiteralPath (Join-Path $output 'RemoteGpu.exe') -Destination (Join-Path $output '检查虚拟摄像头.exe') -Force
    New-Item -ItemType Directory -Path (Join-Path $output 'VirtualCam') -Force | Out-Null
    foreach ($name in @('UnityCaptureFilter64.dll', 'UnityCaptureFilter32.dll', 'LICENSE-UnityCaptureFilter.txt')) {
        Copy-Item -LiteralPath (Join-Path $repoRoot "app\VirtualCam\$name") -Destination (Join-Path $output 'VirtualCam') -Force
    }
    Copy-Item -LiteralPath (Join-Path $projectRoot 'deploy') -Destination $output -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot 'docs') -Destination $output -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination $output -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot 'THIRD_PARTY_NOTICES.md') -Destination $output -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot 'requirements.txt') -Destination $output -Force
    foreach ($name in @('启动本地端.cmd', '启动远程端.cmd', '预览YY画面.cmd', '检查虚拟摄像头.cmd')) {
        Copy-Item -LiteralPath (Join-Path $projectRoot "deploy\$name") -Destination $output -Force
    }
    & $Python (Join-Path $projectRoot 'deploy\collect-licenses.py') (Join-Path $output 'licenses')
    if ($LASTEXITCODE -ne 0) { throw '许可证收集失败。' }
    Write-Host "打包完成：$output"
} finally { Pop-Location }
