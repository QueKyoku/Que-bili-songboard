# 给「完全干净的电脑」自动配好运行环境：装 Python + 装依赖。
#
#     powershell -ExecutionPolicy Bypass -File tools\setup_env.ps1
#     powershell ... -File tools\setup_env.ps1 -DryRun      # 只下载+校验，不安装
#
# 为什么需要它：主播电脑上通常没有 Python，也没有 pip 依赖。
# 光靠 .bat 只能提示"你去官网装一个"，那不叫自动配置环境。
#
# 几个实测结论（都是真的踩过 / 量过，别凭感觉改）：
#
#   1) 镜像不是锦上添花，是必需的。同一台机器实测下 25.7MB 的安装包：
#        华为云 4054 KB/s（6 秒）   npmmirror 2440 KB/s（11 秒）
#        阿里云   84 KB/s（5 分钟） 官方源     74 KB/s（6 分钟）
#      差了 50 倍 —— 走官方源能让主播干等 6 分钟。
#   2) **HEAD 快不等于下载快**。阿里云 HEAD 只花 0.11 秒，真下整包 5 分钟。
#      所以判断"哪个源可用"不能只看握手，得看实际吞吐。
#   3) **不能要求签名证书链有效**。Python 3.12.10 的签名证书 2025-04-11 就过期了，
#      Get-AuthenticodeSignature 返回 Status=Valid（签名本身没问题），
#      但 SignerCertificate.Verify() 是 False。要是按"证书链必须有效"来校验，
#      会把完全合法的官方安装包拒掉。所以判据是：
#         Status 为 Valid  **且**  签名者写着 Python Software Foundation
#   4) InstallAllUsers=0 装到当前用户目录，**不需要管理员权限、不弹 UAC**。
#      主播电脑上让他"以管理员身份运行"通常就卡住了。

param(
    [string]$PythonVersion = "3.12.10",
    [string]$TargetDir = "",          # 留空 = 装到默认位置（当前用户目录）
    [switch]$DryRun,                  # 只下载 + 校验，不安装
    [switch]$Force,                   # 已经装了也重来
    [switch]$SkipDeps                 # 只装 Python，不装 pip 依赖
)

$ErrorActionPreference = "Continue"   # 原生命令写 stderr 很常见，别让它中断脚本

function Say($msg, $color = "Gray") { Write-Host "  $msg" -ForegroundColor $color }
function Step($msg) { Write-Host ""; Write-Host "=== $msg ===" -ForegroundColor Cyan }

# ───────────────────────── 下载源（按实测速度排序）─────────────────────────
$Exe = "python-$PythonVersion-amd64.exe"
$PySources = @(
    @{ Name = "华为云";        Url = "https://mirrors.huaweicloud.com/python/$PythonVersion/$Exe" },
    @{ Name = "npmmirror 淘宝"; Url = "https://registry.npmmirror.com/-/binary/python/$PythonVersion/$Exe" },
    @{ Name = "阿里云";        Url = "https://mirrors.aliyun.com/python-release/windows/$Exe" },
    @{ Name = "官方 python.org"; Url = "https://www.python.org/ftp/python/$PythonVersion/$Exe" }
)
# pip 源：同样多备几个，国内优先
$PipIndexes = @(
    @{ Name = "阿里云";   Url = "https://mirrors.aliyun.com/pypi/simple/" },
    @{ Name = "清华 TUNA"; Url = "https://pypi.tuna.tsinghua.edu.cn/simple/" },
    @{ Name = "腾讯云";   Url = "https://mirrors.cloud.tencent.com/pypi/simple/" },
    @{ Name = "官方 PyPI"; Url = "https://pypi.org/simple/" }
)
$Deps = @("websockets", "cryptography", "qrcode")

# ───────────────────────── 1. 已经有了吗 ─────────────────────────
Step "1/5 检查现有环境"
$existing = $null
$cmd = Get-Command python -ErrorAction SilentlyContinue
if ($cmd) { $existing = $cmd.Source }
if ($existing -and -not $Force) {
    $v = & python -c "import sys; print(sys.version.split()[0])" 2>$null
    Say "已经装了 Python $v ：$existing" "Green"
    # 别写成 `$hasTk = (& python -c "..." 2>$null; $LASTEXITCODE -eq 0)` ——
    # 括号里塞分号在 PS 5.1 里容易解析出问题，分开写最省事。
    & python -c "import tkinter" 2>$null
    $hasTk = ($LASTEXITCODE -eq 0)
    if ($hasTk) { Say "tkinter 也在（图形界面能用）" "Green" }
    else { Say "但这个 Python 没有 tkinter（图形界面用不了）" "Yellow" }
    if (-not $SkipDeps) {
        Step "2/5 检查 pip 依赖"
        $need = @()
        foreach ($d in $Deps) {
            & python -c "import $d" 2>$null
            if ($LASTEXITCODE -ne 0) { $need += $d }
        }
        if ($need.Count -eq 0) { Say "依赖都齐了：$($Deps -join ', ')" "Green" }
        else { Say "缺：$($need -join ', ')（稍后自动装）" "Yellow" }
        $script:NeedDeps = $need
    }
    if (-not $script:NeedDeps -or $script:NeedDeps.Count -eq 0) {
        Say "环境没问题，不用装任何东西。" "Green"
        exit 0
    }
    Say "先自动补依赖…" "Yellow"
    $skipPython = $true
}
else {
    # 区分"真没装"和"-Force 要求重来"—— 之前两种都打印"没找到 python 命令"，
    # 用 -Force 测试时看着很误导。
    if ($existing) { Say "已经装了（$existing），但 -Force 要求重新走一遍安装。" "Yellow" }
    else { Say "这台电脑上没有找到 python 命令 —— 需要装一个。" "Yellow" }
    $skipPython = $false
}

# ───────────────────────── 2. 下载安装包 ─────────────────────────
if (-not $skipPython) {
    Step "2/5 下载 Python $PythonVersion（多镜像，自动挑能用的）"
    $work = Join-Path $env:TEMP "python-setup-$PythonVersion"
    New-Item -ItemType Directory -Path $work -Force | Out-Null
    $exePath = Join-Path $work $Exe

    $haveCurl = $null -ne (Get-Command curl.exe -ErrorAction SilentlyContinue)
    $downloaded = $false
    foreach ($s in $PySources) {
        Say "试 $($s.Name) …"
        $t0 = Get-Date
        try {
            if ($haveCurl) {
                # curl 比 Invoke-WebRequest 快得多，也省内存
                & curl.exe -L --fail --silent --show-error --max-time 600 `
                    -o $exePath $s.Url
                $ok = ($LASTEXITCODE -eq 0)
            }
            else {
                # 老系统没有 curl（Win7/8），退回 PowerShell 自带的方式
                $ProgressPreference = 'SilentlyContinue'
                Invoke-WebRequest -Uri $s.Url -OutFile $exePath -UseBasicParsing
                $ok = $true
            }
        }
        catch { $ok = $false }
        $sec = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
        if ($ok -and (Test-Path $exePath)) {
            $mb = [math]::Round((Get-Item $exePath).Length / 1MB, 1)
            Say "  下好了：$mb MB，用时 $sec 秒（$($s.Name)）" "Green"
            $downloaded = $true
            break
        }
        Say "  $($s.Name) 不行（用时 $sec 秒），换下一个" "Yellow"
    }
    if (-not $downloaded) {
        Say "所有镜像都下不动。" "Red"
        Say "请手动装 Python $PythonVersion，装的时候**务必勾选 Add python.exe to PATH**：" "Yellow"
        Say "    https://www.python.org/downloads/" "Yellow"
        exit 1
    }

    # ───────────────────────── 3. 校验 ─────────────────────────
    Step "3/5 校验下载到的安装包"
    $size = (Get-Item $exePath).Length
    if ($size -lt 20MB) {
        Say "文件太小（$([math]::Round($size/1MB,1)) MB），不像安装包 —— 可能下到了一个错误页。" "Red"
        Remove-Item $exePath -Force -ErrorAction SilentlyContinue
        exit 1
    }
    Say "大小 $([math]::Round($size/1MB,1)) MB  正常" "Green"

    $fs = [System.IO.File]::OpenRead($exePath)
    $head = New-Object byte[] 2
    $fs.Read($head, 0, 2) | Out-Null
    $fs.Close()
    if (-not ($head[0] -eq 0x4D -and $head[1] -eq 0x5A)) {   # 'MZ'
        Say "文件头不是 MZ，不是有效的 Windows 可执行文件。" "Red"
        exit 1
    }
    Say "文件头 MZ，是 PE 可执行文件" "Green"

    # ⚠️ 判据只看 Status + 签名者，**不要**加 SignerCertificate.Verify()：
    #    Python 的签名证书已经过期，Verify() 会返回 False，
    #    那样会把完全合法的官方包拒掉（实测踩过）。
    $sig = Get-AuthenticodeSignature $exePath
    # ⚠️ PowerShell 5.1 不支持 `$x = if (...) {...} else {...}`（那是 PS7 的写法），
    #    必须老老实实分开写 —— 否则整个脚本语法直接不过。
    $signer = ""
    if ($sig.SignerCertificate) { $signer = $sig.SignerCertificate.Subject }
    if ($sig.Status -ne "Valid" -or $signer -notlike "*Python Software Foundation*") {
        Say "数字签名不对！" "Red"
        Say "  状态  : $($sig.Status)" "Red"
        Say "  签名者: $signer" "Red"
        Say "从第三方镜像下载可执行文件，签名不对就绝不能装。已删除。" "Red"
        Remove-Item $exePath -Force -ErrorAction SilentlyContinue
        exit 1
    }
    Say "数字签名有效，签名者：Python Software Foundation" "Green"
    if ($sig.SignerCertificate.NotAfter -lt (Get-Date)) {
        Say "（注：这个证书本身已过期，但签名有效 —— 官方安装包就是这样，不影响）" "DarkGray"
    }

    if ($DryRun) {
        Step "DryRun：到此为止，不安装"
        Say "安装包留在：$exePath" "Yellow"
        exit 0
    }

    # ───────────────────────── 4. 静默安装 ─────────────────────────
    Step "4/5 静默安装（装到当前用户目录，不需要管理员权限）"
    # InstallAllUsers=0 → 只给当前用户装，不弹 UAC（主播电脑上这点很重要）
    # PrependPath=1     → 自动加进 PATH，省得用户手动配
    # ⚠️ 变量名不能叫 $args —— 那是 PowerShell 的自动变量，赋值会被忽略。
    $installArgs = @("/quiet", "InstallAllUsers=0", "PrependPath=1",
              "Include_test=0", "Include_launcher=1", "Include_doc=0",
              "Include_tcltk=1")     # tcltk 是 tkinter 用的，图形界面必须要
    if ($TargetDir) { $installArgs += "TargetDir=$TargetDir" }
    Say "参数：$($installArgs -join ' ')"
    $p = Start-Process -FilePath $exePath -ArgumentList $installArgs -Wait -PassThru
    Say "安装程序退出码：$($p.ExitCode)"
    if ($p.ExitCode -ne 0) {
        Say "安装失败（退出码 $($p.ExitCode)）。可以手动双击装：$exePath" "Red"
        exit 1
    }

    # 刷新当前会话的 PATH（不然这个窗口里还是找不到 python）
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "Machine")
    Say "已刷新 PATH" "Green"
}

# ───────────────────────── 5. 装 pip 依赖 ─────────────────────────
if (-not $SkipDeps) {
    Step "5/5 安装依赖（多镜像，自动挑能用的）"
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) {
        Say "装完了但这个窗口里还找不到 python —— 关掉重开一个窗口再试。" "Yellow"
        exit 0
    }
    $need = @($Deps)
    & python -c "import sys; print('  python', sys.version.split()[0], sys.executable)"
    foreach ($idx in $PipIndexes) {
        Say "试 $($idx.Name) …"
        $ok = $true
        foreach ($d in $need) {
            & python -m pip install --disable-pip-version-check --quiet `
                --index-url $idx.Url --trusted-host ([Uri]$idx.Url).Host $d
            if ($LASTEXITCODE -ne 0) { $ok = $false; break }
        }
        if ($ok) { Say "  $($idx.Name) 装好了" "Green"; break }
        Say "  $($idx.Name) 不行，换下一个" "Yellow"
    }
    $missing = @()
    foreach ($d in $Deps) {
        & python -c "import $d" 2>$null
        if ($LASTEXITCODE -ne 0) { $missing += $d }
    }
    if ($missing.Count -eq 0) { Say "依赖齐了：$($Deps -join ', ')" "Green" }
    else {
        Say "还缺：$($missing -join ', ')" "Red"
        Say "手动试试：python -m pip install $($missing -join ' ')" "Yellow"
    }
}

Write-Host ""
Write-Host "=== 完成 ===" -ForegroundColor Green
Say "现在可以双击根目录的 启动.bat 或 扫码登录.bat 了。" "Green"
Say "（如果当前窗口还提示找不到 python，关掉重开一个新窗口）" "Gray"
