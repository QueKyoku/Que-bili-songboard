# 编译 AwooNcmCefBridge.dll（网易云播放队列桥）。
#
# 产物落到 <项目>\bridge\AwooNcmCefBridge.dll，再跑 tools\inject_bridge.py 注入。
#
# ⚠️ 署名：被编译的源码 native\AwooNcmCefBridge.cpp 是**第三方作品**，
#    作者 Enkianssus，来自 https://github.com/Enkianssus/awoo-connectors
#    该仓库**没有 LICENSE 文件**，版权归原作者所有。
#    详见 THIRD_PARTY_NOTICES.md。
#
#    若你不想用仓库里这份拷贝，可以指向自己的上游 clone：
#      powershell -File tools\build_bridge.ps1 `
#        -BridgeSource ..\awoo-connectors\native\Netease\AwooNcmCefBridge.cpp
#
# 依赖：
#   1. Visual Studio 2022 C++ x64 生成工具（找 vswhere + vcvars64）
#   2. CEF 头文件目录 cef-4472\include\（本项目自写的极简 shim，非官方头文件）
#
# 关于 API 哈希：桥 DLL 会调 libcef.dll 的 cef_version_info/cef_api_hash 做
# 硬校验。上游期望 CEF 91.2.2+4472.169 / API 哈希 37d5f9f0… / 306fdfb4…。
# 实测网易云 3.1.40.205461 的 libcef.dll 与这三个值**完全一致**
# （外壳升级了但 CEF 内核没换），所以能通过校验。
# 若哪天网易云换了 CEF，这里会 REFUSED，需要重新对齐版本和哈希。

param(
    [string]$Configuration = "Release",
    [string]$CefRoot = "",
    [string]$BridgeSource = ""
)

$ErrorActionPreference = "Stop"
$toolsDir = $PSScriptRoot
$projRoot = Split-Path -Parent $toolsDir
if (-not $CefRoot)      { $CefRoot = Join-Path $projRoot "cef-4472" }
if (-not $BridgeSource) { $BridgeSource = Join-Path $projRoot "native\AwooNcmCefBridge.cpp" }

$outDir = Join-Path $projRoot "bridge"
$objDir = Join-Path $projRoot "build-obj"
New-Item -ItemType Directory -Force -Path $outDir, $objDir | Out-Null

Write-Output "cefRoot : $CefRoot  (exists=$(Test-Path $CefRoot))"
Write-Output "source  : $BridgeSource  (exists=$(Test-Path $BridgeSource))"

if (-not (Test-Path $BridgeSource)) {
    throw "找不到桥源码：$BridgeSource"
}
if (-not (Test-Path (Join-Path $CefRoot "include\cef_browser.h"))) {
    throw "找不到 CEF 头文件：$CefRoot\include\cef_browser.h"
}

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { throw "找不到 vswhere.exe（未安装 Visual Studio）" }
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if ([string]::IsNullOrWhiteSpace($vs)) { throw "没找到 VC++ x64 生成工具" }
$vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"

$dll = Join-Path $outDir "AwooNcmCefBridge.dll"
$obj = Join-Path $objDir "AwooNcmCefBridge.obj"
$pdb = Join-Path $objDir "AwooNcmCefBridge.pdb"
$opt = if ($Configuration -eq "Debug") { "/Od /Zi" } else { "/O2" }

$compile = @(
    "`"$vcvars`"", "&&", "cl.exe", "/nologo", "/std:c++20",
    "/utf-8",
    "/EHsc", "/W4", "/WX", "/wd4100", "/DUNICODE", "/D_UNICODE", $opt,
    "/I`"$CefRoot`"",
    "/Fo`"$obj`"", "/Fd`"$pdb`"", "/LD", "`"$BridgeSource`"",
    "/link", "/OUT:`"$dll`"", "kernel32.lib", "user32.lib"
) -join " "

& $env:ComSpec /d /s /c $compile
if ($LASTEXITCODE -ne 0) { throw "编译失败，exit=$LASTEXITCODE" }

$hash = (Get-FileHash $dll -Algorithm SHA256).Hash
Write-Output ""
Write-Output "编译成功"
Write-Output "  DLL    : $dll"
Write-Output "  大小   : $((Get-Item $dll).Length) bytes"
Write-Output "  SHA256 : $hash"
Write-Output ""
Write-Output "下一步：python tools\inject_bridge.py"
