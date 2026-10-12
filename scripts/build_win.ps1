<#
  打包「大司农」Windows 端为独立可执行程序（PyInstaller）。

  用法：
    powershell -ExecutionPolicy Bypass -File scripts\build_win.ps1
    powershell -ExecutionPolicy Bypass -File scripts\build_win.ps1 -OneFile -Clean
    powershell -ExecutionPolicy Bypass -File scripts\build_win.ps1 -Console   # 带控制台，便于看报错

  产物：
    dist\win\大司农\大司农.exe        （默认 -onedir）
    dist\win\大司农.exe               （-OneFile，单文件，启动较慢）
    dist\大司农-windows.zip           （onedir 模式的压缩包）
#>
[CmdletBinding()]
param(
    [switch]$OneFile,      # 打单文件 exe
    [switch]$Console,      # 保留控制台窗口（排错用）
    [switch]$Clean,        # 先清理 build/ 中间产物
    [switch]$SkipZip       # 不生成 zip
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent $PSScriptRoot
$WinDir   = Join-Path $RepoRoot "大司农-win"
$Backend  = Join-Path $WinDir "后端"
$UiDir    = Join-Path $WinDir "UI"
$IconSrc  = Join-Path $RepoRoot "软件图标\大司农.jpg"
$BuildDir = Join-Path $RepoRoot "build\win"
$DistWin  = Join-Path $RepoRoot "dist\win"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

if (-not (Test-Path $WinDir))    { throw "找不到目录：$WinDir" }
if (-not (Test-Path $IconSrc))   { Write-Warning "缺少图标：$IconSrc（将不带图标打包）" }

Step "检查依赖"
python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "未检测到 PyInstaller，正在安装..." -ForegroundColor Yellow
    python -m pip install --upgrade pyinstaller
}
python -c "import PySide6, psutil" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "缺少运行依赖，安装 requirements.txt..." -ForegroundColor Yellow
    python -m pip install -r (Join-Path $WinDir "requirements.txt")
}

if ($Clean) {
    Step "清理中间产物"
    foreach ($d in @($BuildDir, $DistWin, (Join-Path $RepoRoot "dist\大司农-windows.zip"))) {
        if (Test-Path $d) { Remove-Item -Recurse -Force $d }
    }
}

New-Item -ItemType Directory -Force -Path $BuildDir, $DistWin | Out-Null

Step "结束可能占用文件的旧进程"
# onedir 模式会派生子进程，若上次打包的程序还在运行，会锁定 dist 下的 dll/pyd 导致打包失败
$running = Get-Process -Name "大司农" -ErrorAction SilentlyContinue
if ($running) {
    foreach ($proc in $running) {
        Write-Host "  结束残留进程 PID=$($proc.Id)" -ForegroundColor Yellow
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 800
}

# ---------------------------------------------------------------- 生成 .ico
$IcoOut = Join-Path $BuildDir "大司农.ico"
if (Test-Path $IconSrc) {
    Step "由 JPG 生成多尺寸 ICO"
    $icoCode = @'
import sys
from PIL import Image
src, dst = sys.argv[1], sys.argv[2]
im = Image.open(src).convert("RGBA")
w, h = im.size
side = max(w, h)
canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
canvas.paste(im, ((side - w) // 2, (side - h) // 2))
canvas.save(dst, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("ico:", dst)
'@
    $icoCode | python - $IconSrc $IcoOut
    if ($LASTEXITCODE -ne 0) { Write-Warning "ICO 生成失败，将不带图标打包" }
}

# ---------------------------------------------------------------- PyInstaller
Step "PyInstaller 打包（首次可能较慢）"
$pyiArgs = @(
    "--noconfirm",
    "--clean",
    "--name", "大司农",
    "--paths", $Backend,
    "--paths", $UiDir,
    "--hidden-import", "dasinong_ui.app",
    "--hidden-import", "dasinong_core.network.transfer",
    "--hidden-import", "dasinong_core.network.gateway_rendezvous",
    "--distpath", $DistWin,
    "--workpath", (Join-Path $BuildDir "pyi"),
    "--specpath", $BuildDir
)
if ($OneFile) { $pyiArgs += "--onefile" } else { $pyiArgs += "--onedir" }
if (-not $Console) { $pyiArgs += "--windowed" }
if (Test-Path $IconSrc) {
    $pyiArgs += @("--add-data", "$IconSrc;.")
    if (Test-Path $IcoOut) { $pyiArgs += @("--icon", $IcoOut) }
}

# 注意：不要用 *> 重定向本机命令的输出。在 $ErrorActionPreference='Stop' 下，
# PyInstaller 写入 stderr 的进度日志会被 PowerShell 当作终止性错误而中断脚本。
# 直接输出到控制台，靠 $LASTEXITCODE 判断成败。
python -m PyInstaller @pyiArgs (Join-Path $WinDir "run.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败（上方为 PyInstaller 日志）" }

# ---------------------------------------------------------------- 压缩
if (-not $SkipZip -and -not $OneFile) {
    Step "生成压缩包"
    $zipPath = Join-Path $RepoRoot "dist\大司农-windows.zip"
    if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
    Compress-Archive -Path (Join-Path $DistWin "大司农\*") -DestinationPath $zipPath
    Write-Host "zip: $zipPath" -ForegroundColor Green
}

Step "完成"
Get-ChildItem $DistWin | ForEach-Object { Write-Host ("  " + $_.FullName) }
Write-Host "运行：dist\win\大司农\大司农.exe" -ForegroundColor Green
exit 0
