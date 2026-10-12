<#
  一键打包「大司农」两端。
  用法：powershell -ExecutionPolicy Bypass -File scripts\build_all.ps1 [-Release] [-Clean]
  产物：dist\win\... 与 dist\android\...
#>
[CmdletBinding()]
param(
    [switch]$Release,
    [switch]$Clean,
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

# 用哈希表 splat：数组 splat 到 [CmdletBinding()] 脚本会被当成位置参数而报错
$winParams = @{}
if ($Clean)   { $winParams.Clean = $true }
if ($OneFile) { $winParams.OneFile = $true }

$androidParams = @{}
if ($Release) { $androidParams.Release = $true }
if ($Clean)   { $androidParams.Clean = $true }

Write-Host "=========== 1/2 打包 Windows 端 ===========" -ForegroundColor Magenta
& (Join-Path $PSScriptRoot "build_win.ps1") @winParams

Write-Host "`n=========== 2/2 打包 Android 端 ===========" -ForegroundColor Magenta
& (Join-Path $PSScriptRoot "build_android.ps1") @androidParams

Write-Host "`n全部完成，产物在 dist\ 下。" -ForegroundColor Green
