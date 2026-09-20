@echo off
chcp 936 >nul
cd /d "%~dp0"

echo ============================================
echo   启动外部媒体信息源（Metabox-Nexus-PlayerCap）
echo ============================================
echo.
echo   它会做两件事：
echo     1) 监听 127.0.0.1:8766，提供"正在播放 + 精确进度"接口
echo     2) 如果网易云没带调试参数运行，会关掉它并用调试参数重开
echo.
echo   注意：它需要管理员权限，且启动时会上报遥测（详见 README）
echo.

net session >nul 2>&1
if errorlevel 1 (
    echo   [!] 当前不是管理员。请右键本文件"以管理员身份运行"。
    echo.
    pause
    exit /b 1
)

if not exist "tools\playercap\Metabox-Nexus-PlayerCap.exe" (
    echo   [x] 找不到 tools\playercap\Metabox-Nexus-PlayerCap.exe
    echo       请先按 README 下载并校验。
    pause
    exit /b 1
)

cd /d "%~dp0tools\playercap"
echo   正在启动 PlayerCap（Ctrl+C 可停止）...
echo.
Metabox-Nexus-PlayerCap.exe
