param([ValidateRange(1024,65535)][int]$Port = 8767,
      [Parameter(Mandatory=$true)][string]$ProgramPath,
      [Parameter(Mandatory=$true)][string]$ReportDirectory)
# ASCII source for Windows PowerShell 5.1. No shell-built network rules.
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Path $ReportDirectory -Force | Out-Null
$reportPath = Join-Path $ReportDirectory 'lan-firewall.json'
$result = [ordered]@{ status = 'starting'; port = $Port; program = $ProgramPath;
    profile = 'Private'; remote_address = 'LocalSubnet'; error = $null }
try {
    if (-not (Test-Path -LiteralPath $ProgramPath -PathType Leaf)) { throw 'Program does not exist.' }
    $name = "FaceLiveLan-$Port"
    $existing = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
    if ($existing) { $existing | Remove-NetFirewallRule }
    New-NetFirewallRule -Name $name -DisplayName "FaceLive LAN Receiver TCP $Port" `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
        -Program $ProgramPath -Profile Private -RemoteAddress LocalSubnet | Out-Null
    $result.status = 'ok'
} catch {
    $result.status = 'failed'
    $result.error = $_.Exception.Message
} finally {
    $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $reportPath -Encoding UTF8
}
if ($result.status -ne 'ok') { exit 1 }
