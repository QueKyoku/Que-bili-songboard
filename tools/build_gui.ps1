# 把「扫码登录.pyw」打包成单个 exe，给不想碰命令行的人用。
#
#     powershell -ExecutionPolicy Bypass -File tools\build_gui.ps1
#
# 产物：dist\网易云扫码登录.exe
#
# ⚠️ 打好之后**要把 exe 放到项目根目录**（和 config.json 同一个文件夹）再双击 ——
#    程序是按 exe 所在目录去找 config.json 的。
#
# 不想装 PyInstaller（约几十 MB）的话，其实直接双击「扫码登录.pyw」也能用，
# 效果一样，只是要求机器上装了 Python 和 qrcode。

# ⚠️ 不能用 "Stop"：下面要用 `python -c "import 某模块"` 探测依赖，
#    模块没装时 Python 会往 stderr 写 traceback，而 PowerShell 5.1 在
#    ErrorActionPreference=Stop 下会把它当成致命错误、直接中断脚本
#    （实测踩过：脚本在"检查 PyInstaller"那一步就退出了）。
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Test-Module($name) {
    python -c "import $name" *> $null
    return $LASTEXITCODE -eq 0
}

Write-Host "=== 1/3 检查依赖 ===" -ForegroundColor Cyan
if (-not (Test-Module "tkinter")) {
    Write-Host "  [x] 这个 Python 没有 tkinter，没法打包图形界面" -ForegroundColor Red
    Write-Host "      用官方安装包装一次 Python 就自带 tkinter。" -ForegroundColor Gray
    exit 1
}
Write-Host "  [v] tkinter 可用"

if (-not (Test-Module "qrcode")) {
    Write-Host "  没装 qrcode，正在装（界面里也能装，这里顺手装掉）..."
    python -m pip install --disable-pip-version-check qrcode
    if (-not (Test-Module "qrcode")) {
        Write-Host "  [x] qrcode 装不上，检查一下网络" -ForegroundColor Red
        exit 1
    }
}
Write-Host "  [v] qrcode 可用"

if (-not (Test-Module "PyInstaller")) {
    Write-Host "  没装 PyInstaller，正在装（几十 MB，会慢一点）..."
    python -m pip install --disable-pip-version-check pyinstaller
    if (-not (Test-Module "PyInstaller")) {
        Write-Host "  [x] PyInstaller 装不上。" -ForegroundColor Red
        Write-Host "      其实不打包也能用：直接双击「扫码登录.pyw」就行。" -ForegroundColor Yellow
        exit 1
    }
}
Write-Host "  [v] PyInstaller 可用"

Write-Host ""
Write-Host "=== 2/3 打包（大约 30~60 秒）===" -ForegroundColor Cyan
# --windowed ：双击时不弹黑色控制台窗口
# --onefile  ：打成单个 exe
# --collect-submodules songboard ：把 songboard 包下所有子模块都收进来
#                （防止有动态导入没被分析到；PyInstaller 里**没有**
#                 --collect-subdirs 这个参数，我第一版就写错了）
# --paths .  ：让 PyInstaller 能找到 songboard
python -m PyInstaller `
    --noconfirm --clean `
    --onefile --windowed `
    --name "网易云扫码登录" `
    --hidden-import qrcode `
    --collect-submodules songboard `
    --paths . `
    "扫码登录.pyw"

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[x] 打包失败。也可以直接用「扫码登录.pyw」—— 双击就能跑，不需要 exe。" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "=== 3/3 结果 ===" -ForegroundColor Cyan
$exe = Join-Path $root "dist\网易云扫码登录.exe"
if (Test-Path $exe) {
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "  [v] 打好了：$exe（$mb MB）" -ForegroundColor Green
    Write-Host ""
    Write-Host "  下一步：把它复制到项目根目录再双击（要在那儿读 config.json）：" -ForegroundColor Yellow
    Write-Host "      Copy-Item `"$exe`" `"$root\`"" -ForegroundColor Gray
}
else {
    Write-Host "  [!] 没找到产物，看看上面的日志" -ForegroundColor Yellow
}
