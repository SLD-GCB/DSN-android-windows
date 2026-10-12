<#
  打包「大司农」Android 端 APK。

  用法：
    powershell -ExecutionPolicy Bypass -File scripts\build_android.ps1
    powershell -ExecutionPolicy Bypass -File scripts\build_android.ps1 -Release
    powershell -ExecutionPolicy Bypass -File scripts\build_android.ps1 -Offline   # 不联网

  说明：
    - 自动探测 JAVA_HOME / ANDROID_HOME；
    - Gradle 优先用工程内 gradlew，其次用本机 ~/.gradle/wrapper/dists 缓存的最高版本；
    - -Release 产出的通常是未签名 APK（要装到手机请用 debug，或用 Android Studio 配签名）。

  产物：dist\android\大司农-android-<debug|release>.apk
#>
[CmdletBinding()]
param(
    [switch]$Release,
    [switch]$Offline,
    [switch]$Tests,        # 顺带跑后端 JVM 单测
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$RepoRoot   = Split-Path -Parent $PSScriptRoot
$AndroidDir = Join-Path $RepoRoot "大司农-android"
$DistAndroid = Join-Path $RepoRoot "dist\android"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

if (-not (Test-Path $AndroidDir)) { throw "找不到目录：$AndroidDir" }

# ---------------------------------------------------------------- 环境
Step "检测 JDK / Android SDK"
if (-not $env:JAVA_HOME) {
    $jbr = Get-ChildItem (Join-Path $env:USERPROFILE ".jdks") -Directory -Filter "jbr-*" -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    if ($jbr) { $env:JAVA_HOME = $jbr.FullName }
}
if (-not $env:JAVA_HOME) {
    $javaCmd = Get-Command java -ErrorAction SilentlyContinue
    if ($javaCmd) { $env:JAVA_HOME = Split-Path -Parent (Split-Path -Parent $javaCmd.Source) }
}
if (-not $env:JAVA_HOME) { throw "未找到 JDK。请安装 JDK 17+ 或设置 JAVA_HOME。" }
Write-Host "JAVA_HOME   = $env:JAVA_HOME"

if (-not $env:ANDROID_HOME -and -not $env:ANDROID_SDK_ROOT) {
    $sdk = Join-Path $env:LOCALAPPDATA "Android\Sdk"
    if (Test-Path $sdk) { $env:ANDROID_HOME = $sdk }
}
if (-not $env:ANDROID_HOME -and -not $env:ANDROID_SDK_ROOT) {
    throw "未找到 Android SDK。请设置 ANDROID_HOME。"
}
Write-Host "ANDROID_HOME = $(if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { $env:ANDROID_SDK_ROOT })"

# ---------------------------------------------------------------- Gradle
Step "定位 Gradle"
$gradleCmd = $null
$localWrapper = Join-Path $AndroidDir "gradlew.bat"
if (Test-Path $localWrapper) {
    $gradleCmd = $localWrapper
    Write-Host "使用工程内 gradlew"
} else {
    $dists = Join-Path $env:USERPROFILE ".gradle\wrapper\dists"
    $found = Get-ChildItem $dists -Recurse -Filter "gradle.bat" -ErrorAction SilentlyContinue |
        Where-Object { $_.DirectoryName -like "*\bin" } |
        ForEach-Object {
            $m = [regex]::Match($_.FullName, 'gradle-(\d+(\.\d+){1,2})[\\/]bin[\\/]gradle\.bat$')
            if ($m.Success) {
                [pscustomobject]@{ Path = $_.FullName; Version = [version]$m.Groups[1].Value }
            }
        } | Sort-Object Version -Descending
    if ($found) {
        $gradleCmd = $found[0].Path
        Write-Host "使用缓存 Gradle $($found[0].Version)：$gradleCmd"
    }
}
if (-not $gradleCmd) {
    throw "未找到 Gradle。请用 Android Studio 打开过大司农-android（会自动生成 wrapper），或安装 Gradle 加入 PATH。"
}

# ---------------------------------------------------------------- 构建
$gradleArgs = @("-p", $AndroidDir)
if ($Offline) { $gradleArgs += "--offline" }
if ($Clean)   { $gradleArgs += "clean" }
if ($Tests)   { $gradleArgs += ":backend:testDebugUnitTest" }
$gradleArgs += if ($Release) { ":ui:assembleRelease" } else { ":ui:assembleDebug" }

Step "开始构建：$($gradleArgs -join ' ')"
& $gradleCmd @gradleArgs
if ($LASTEXITCODE -ne 0) { throw "Gradle 构建失败" }

# ---------------------------------------------------------------- 收集产物
Step "收集 APK"
$variant = if ($Release) { "release" } else { "debug" }
$apkDir = Join-Path $AndroidDir "UI\build\outputs\apk\$variant"
$apks = Get-ChildItem $apkDir -Filter "*.apk" -ErrorAction SilentlyContinue
if (-not $apks) { throw "未找到 APK：$apkDir" }

New-Item -ItemType Directory -Force -Path $DistAndroid | Out-Null
foreach ($apk in $apks) {
    $tag = if ($apk.Name -match "unsigned") { "unsigned" } else { $variant }
    $dest = Join-Path $DistAndroid "大司农-android-$tag.apk"
    Copy-Item $apk.FullName $dest -Force
    Write-Host ("  " + $dest) -ForegroundColor Green
}

if ($Release -and ($apks | Where-Object { $_.Name -match "unsigned" })) {
    Write-Warning "release 构建是未签名 APK，无法直接安装。装真机请用默认（debug），或在 Android Studio 里配置 signingConfig。"
}
Step "完成"
exit 0
