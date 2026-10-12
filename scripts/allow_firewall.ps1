<#
  放行「大司农」所需的入站端口（自动请求管理员权限）。

  用法：
    powershell -ExecutionPolicy Bypass -File scripts\allow_firewall.ps1
    powershell -ExecutionPolicy Bypass -File scripts\allow_firewall.ps1 -Remove
    powershell -ExecutionPolicy Bypass -File scripts\allow_firewall.ps1 -UdpPort 37020 -TcpPort 37021

  说明：热点/局域网常被判为「公用」网络，规则必须 profile=any 才能生效。
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    [int]$UdpPort = 0,
    [int]$TcpPort = 0
)

[Console]::OutputEncoding = [Text.Encoding]::UTF8

if (-not $UdpPort) { $UdpPort = if ($env:DASINONG_UDP_PORT) { [int]$env:DASINONG_UDP_PORT } else { 37020 } }
if (-not $TcpPort) { $TcpPort = if ($env:DASINONG_TCP_PORT) { [int]$env:DASINONG_TCP_PORT } else { 37021 } }

# 自动提权
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "需要管理员权限，正在请求提权..." -ForegroundColor Yellow
    $argList = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "-UdpPort", $UdpPort, "-TcpPort", $TcpPort
    )
    if ($Remove) { $argList += "-Remove" }
    Start-Process powershell -Verb RunAs -ArgumentList $argList
    exit 0
}

$udpName = "Dasinong UDP $UdpPort"
$tcpName = "Dasinong TCP $TcpPort"

# 先删同名旧规则，避免重复
foreach ($n in @($udpName, $tcpName)) {
    netsh advfirewall firewall delete rule name="$n" 2>$null | Out-Null
}

if ($Remove) {
    Write-Host "已删除大司农防火墙规则。" -ForegroundColor Green
    exit 0
}

netsh advfirewall firewall add rule name="$udpName" dir=in action=allow protocol=UDP localport=$UdpPort profile=any | Out-Null
# TCP 放行**整段端口池**（主口 + 7 个备用口）：接口池会给每台设备各分一个口
netsh advfirewall firewall add rule name="$tcpName" dir=in action=allow protocol=TCP localport="$TcpPort-$(([int]$TcpPort) + 7)" profile=any | Out-Null

Write-Host "已放行入站：UDP $UdpPort / TCP $TcpPort （profile=any）" -ForegroundColor Green
Write-Host "若仍连不上，请确认两台设备在同一网段，且对方程序也在运行。"
exit 0
