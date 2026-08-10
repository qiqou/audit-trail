# 审迹 Windows 一键打包脚本
#
# 用法（推荐）：双击项目根目录 build-windows.bat
# 或命令行：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1 [-SkipTests]
#
# 流程：环境检查 → 虚拟环境 → Python/前端依赖 → 前端构建 → 门禁 → PyInstaller → 清单 → ZIP → 冒烟
# 要求：已安装 Python 3.11（https://www.python.org/downloads/，安装时勾选 Add python.exe to PATH）
#
# 参数：
#   -SkipTests  跳过 ruff + pytest 门禁（紧急打包用，不推荐）

param(
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
# 子进程（python/pyinstaller）输出按 UTF-8 解码，防中文乱码
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "    [失败] $msg" -ForegroundColor Red }

# 项目根目录（本脚本位于 scripts/ 下）
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venvScripts = Join-Path $root ".venv\Scripts"
$pyVenv    = Join-Path $venvScripts "python.exe"
$pyiExe    = Join-Path $venvScripts "pyinstaller.exe"
$ruffExe   = Join-Path $venvScripts "ruff.exe"
$pytestExe = Join-Path $venvScripts "pytest.exe"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " 审迹 Windows 一键打包" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# [1/7] 检查 Python 环境
Write-Step "[1/7] 检查 Python 环境"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Fail "未找到 python 命令。请先安装 Python 3.11：https://www.python.org/downloads/"
    Write-Host '        安装时务必勾选 "Add python.exe to PATH"，装完重开命令行再运行本脚本。'
    exit 1
}
$pyVer = (& python -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
Write-Host "    检测到 Python: $pyVer ($($py.Source))"
if ($pyVer -ne '3.11') {
    Write-Host "    [提示] CI 使用 Python 3.11，当前 $pyVer 可能不兼容；建议安装 3.11 后重试（脚本继续尝试）。" -ForegroundColor Yellow
}

# [2/7] 虚拟环境
$venvDir = Join-Path $root ".venv"
if (-not (Test-Path $pyVenv) -and (Test-Path $venvDir)) {
    # .venv 存在但没有 Scripts\python.exe → 无效 venv（典型：从 macOS/Linux 整目录拷贝，
    # 里面是 bin/lib unix 结构，Windows 的 python -m venv 无法复用，直接写文件会 Permission denied）
    Write-Host "    [提示] 检测到无效的 .venv（缺少 Scripts\python.exe，可能是从其他系统拷贝而来），自动删除重建..." -ForegroundColor Yellow
    try {
        Remove-Item -Path $venvDir -Recurse -Force -ErrorAction Stop
        Write-Ok "旧 .venv 已删除"
    } catch {
        Write-Fail "无法删除旧 .venv（文件被占用或只读）。请先关闭占用它的程序，然后在项目目录手动执行："
        Write-Host "    attrib -r -s -h .venv /s /d" -ForegroundColor Yellow
        Write-Host "    rmdir /s /q .venv" -ForegroundColor Yellow
        Write-Host "    然后重新运行本脚本。" -ForegroundColor Yellow
        exit 1
    }
}
if (-not (Test-Path $pyVenv)) {
    Write-Step "[2/7] 创建虚拟环境 .venv"
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "虚拟环境创建失败（python -m venv .venv）。若目录存在残留请先删除 .venv 再重试。"
        exit 1
    }
    Write-Ok "虚拟环境已创建"
} else {
    Write-Step "[2/7] 使用已有虚拟环境 .venv"
}

# [3/7] 依赖
Write-Step "[3/7] 安装依赖（requirements + pyinstaller + 门禁工具）"
& $pyVenv -m pip install --disable-pip-version-check -r requirements.txt -r requirements-dev.txt pyinstaller
if ($LASTEXITCODE -ne 0) {
    Write-Fail "依赖安装失败。请检查网络/代理后重试（pip install 可重复执行，幂等）。"
    exit 1
}
if (-not (Test-Path $pyiExe)) {
    Write-Fail "pyinstaller 未安装成功：找不到 $pyiExe"
    exit 1
}
Write-Ok "依赖就绪"

# [4/7] V3 前端构建
Write-Step "[4/7] 构建 V3 前端（pnpm + TypeScript + Vite）"
$pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
if (-not $pnpm) {
    Write-Fail "未找到 pnpm。请先安装 Node.js 22+，再执行：corepack enable；corepack prepare pnpm@11.5.0 --activate"
    exit 1
}
& pnpm --dir frontend-v3 install --frozen-lockfile
if ($LASTEXITCODE -ne 0) {
    Write-Fail "V3 前端依赖安装失败。请检查网络/代理后重试。"
    exit 1
}
& pnpm --dir frontend-v3 build
if ($LASTEXITCODE -ne 0) {
    Write-Fail "V3 前端构建失败。请修复 TypeScript/Vite 报错后重试。"
    exit 1
}
Write-Ok "V3 前端构建完成"

# [5/7] 门禁
if ($SkipTests) {
    Write-Host "    [跳过] 门禁检查（-SkipTests）" -ForegroundColor Yellow
} else {
    Write-Step "[5/7] 门禁检查（ruff + pytest）"
    & $ruffExe check .
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "ruff 静态检查未通过，请修复代码后重试；紧急打包可加参数 -SkipTests。"
        exit 1
    }
    & $pytestExe tests/ -q --disable-warnings
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "pytest 回归测试未通过，请修复后重试；紧急打包可加参数 -SkipTests。"
        exit 1
    }
    Write-Ok "门禁全绿"
}

# [6/7] 打包
Write-Step "[6/7] PyInstaller 打包（约 1-3 分钟，请耐心等待）"
& $pyiExe --noconfirm 审迹.spec
if ($LASTEXITCODE -ne 0) {
    Write-Fail "PyInstaller 打包失败，请查看上方详细日志（关键报错一般在最后 30 行）。"
    exit 1
}
$exePath = Join-Path $root "dist\审迹\审迹.exe"
if (-not (Test-Path $exePath)) {
    Write-Fail "未找到打包产物：$exePath（打包可能未完成）。"
    exit 1
}
Write-Ok "exe 已生成：$exePath"

# [7/7] 安装包 + 发布清单 + 冒烟
Write-Step "[7/7] 生成安装包 + 发布清单 + 冒烟测试"

# 版本号 + 时间戳 → 安装包名（多次打包不覆盖，符合"输出不覆盖"约定）
$verLine = Get-Content -Path (Join-Path $root "backend\version.py") | Where-Object { $_ -match 'APP_VERSION\s*=\s*"([^"]+)"' } | Select-Object -First 1
$appVer = "unknown"
if ($verLine -match 'APP_VERSION\s*=\s*"([^"]+)"') { $appVer = $Matches[1] }
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$zipName = "审迹-windows-v$appVer-$stamp.zip"
$zipPath = Join-Path $root "dist\$zipName"
Compress-Archive -Path (Join-Path $root "dist\审迹") -DestinationPath $zipPath -Force
if (-not (Test-Path $zipPath)) {
    Write-Fail "安装包压缩失败：$zipPath"
    exit 1
}
Write-Ok "安装包已生成：$zipName"

# 发布清单（zip 生成后扫描，安装包也入清单；显式列出产物名，避免 dist 里历史 mac 产物混入）
& $pyVenv scripts\release_manifest.py dist "审迹" $zipName
if ($LASTEXITCODE -ne 0) {
    Write-Fail "发布清单生成失败（scripts\release_manifest.py）。"
    exit 1
}

# 冒烟：启动 exe，按进程 PID 发现动态端口后请求首页。
Write-Host "    启动 exe 冒烟测试（最长 60 秒）..."
$p = Start-Process -FilePath $exePath -PassThru
$ok = $false
$smokeUrl = ""
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 1
    if ($p.HasExited) { break }
    $ports = @(Get-NetTCPConnection -OwningProcess $p.Id -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @('127.0.0.1', '::1') } |
        Select-Object -ExpandProperty LocalPort -Unique)
    foreach ($port in $ports) {
        $url = "http://127.0.0.1:$port/"
        try {
            $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { $ok = $true; $smokeUrl = $url; break }
        } catch { }
    }
    if ($ok) { break }
}
if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
if ($ok) {
    Write-Ok "冒烟通过：$smokeUrl 返回 200"
} else {
    Write-Fail "冒烟失败：exe 启动后未发现可访问的动态本地端口（进程已退出：$($p.HasExited)）。"
    $logDir = Join-Path $env:USERPROFILE ".shenji\logs"
    if (Test-Path $logDir) {
        Write-Host "    最近崩溃日志：" -ForegroundColor Yellow
        Get-ChildItem $logDir -Filter "crash_*.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 3 | ForEach-Object {
            Write-Host "    === $($_.Name) ===" -ForegroundColor Yellow
            Get-Content $_.FullName -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "    $_" }
        }
    }
    exit 1
}

# 汇总
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " 打包完成！" -ForegroundColor Green
Write-Host "   exe   : dist\审迹\审迹.exe"
Write-Host "   安装包: dist\$zipName"
Write-Host "   清单  : dist\manifest.txt"
Write-Host " 注意：dist\审迹\ 是 onedir 形态，分发时整个目录一起拷走（含 _internal\）"
Write-Host "========================================" -ForegroundColor Green
