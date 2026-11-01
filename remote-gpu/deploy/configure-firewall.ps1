param(
    [Parameter(Mandatory=$true)][string]$PeerAddress,
    [int]$Port = 8765,
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $PSScriptRoot
$exePath = Join-Path $packageRoot 'RemoteGpu.exe'
if (-not (Test-Path -LiteralPath $exePath)) { throw '请在完整发布包内运行此脚本。' }
$peerIp = [System.Net.IPAddress]::Parse($PeerAddress)
if ($peerIp.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { throw '首版脚本使用对方的具体 IPv4 地址。' }
$names = @('RemoteGpu-Signaling', 'RemoteGpu-Media')
foreach ($name in $names) { Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule }
if ($Remove) { Write-Host '已移除远程 GPU 防火墙规则。'; exit }
New-NetFirewallRule -Name $names[0] -DisplayName 'Remote GPU signaling (one peer)' -Direction Inbound -Action Allow -Program $exePath -Protocol TCP -LocalPort $Port -RemoteAddress $PeerAddress -Profile Any | Out-Null
New-NetFirewallRule -Name $names[1] -DisplayName 'Remote GPU media (one peer)' -Direction Inbound -Action Allow -Program $exePath -Protocol UDP -RemoteAddress $PeerAddress -Profile Any | Out-Null
Write-Host "已允许来自 $PeerAddress 的程序信令与 UDP 媒体。两端分别运行，填入对方的 Tailscale IP。"
