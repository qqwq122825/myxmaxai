param(
    [string]$MSBuild = 'D:\AI-FaceSwap\tools\BuildTools\MSBuild\Current\Bin\MSBuild.exe',
    [string]$WindowsSdk = '10.0.26100.0'
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $MSBuild)) { throw '请通过 -MSBuild 指定 VS Build Tools 的 MSBuild.exe。' }
foreach ($arch in @('x64', 'Win32')) {
    & $MSBuild (Join-Path $PSScriptRoot 'UnityCaptureFilter.vcxproj') '/p:Configuration=Release' "/p:Platform=$arch" '/p:PlatformToolset=v143' "/p:WindowsTargetPlatformVersion=$WindowsSdk" '/v:minimal' '/nologo'
    if ($LASTEXITCODE -ne 0) { throw "$arch 摄像头驱动编译失败。" }
}
Write-Host '驱动已编译到 vcam\Build；本脚本不安装或注册驱动。'
