param([int]$Port = 8766, [switch]$Remove)
$ErrorActionPreference = 'Stop'
if ($Port -lt 1024 -or $Port -gt 65535) { throw '端口须为 1024–65535。' }
$packageRoot = Split-Path -Parent $PSScriptRoot
$exePath = Join-Path $packageRoot 'RemoteGpuServer.exe'
if (-not (Test-Path -LiteralPath $exePath)) { $exePath = Join-Path $packageRoot 'RemoteGpu.exe' }
if (-not (Test-Path -LiteralPath $exePath)) { throw '请在完整发布包内运行本脚本。' }
$ruleName = 'RemoteGpu-Direct-TLS'
Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
if ($Remove) { Write-Host '已移除免账号直连入站规则。'; exit }
New-NetFirewallRule -Name $ruleName -DisplayName 'Remote GPU encrypted direct TCP' -Direction Inbound -Action Allow -Program $exePath -Protocol TCP -LocalPort $Port -Profile Any | Out-Null
Write-Host "已允许此程序的 TCP $Port 入站。租机平台的端口映射 / 安全组需另外开放相应公网端口。"
