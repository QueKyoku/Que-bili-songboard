@echo off
chcp 936 >nul
cd /d "%~dp0"

echo ============================================
echo   哔哩哔哩点歌板
echo ============================================
echo.

rem ---------- 1) 有没有 Python ----------
where python >nul 2>&1
if errorlevel 1 goto nopython

rem ---------- 2) 版本够不够（代码要求 3.10+）----------
python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 goto oldpython

rem ---------- 3) 依赖装了没（缺了就直接装）----------
rem websockets 是硬依赖：缺了连演示模式都起不来。
python -c "import websockets" >nul 2>&1
if errorlevel 1 goto fixdeps

rem cryptography 是可选的：缺了只用不了网易云的搜索/查时长。
python -c "import cryptography" >nul 2>&1
if errorlevel 1 (
    echo   [提示] 没装 cryptography，网易云的搜索和查时长会不可用。
    echo          想装的话执行：python -m pip install cryptography
    echo.
)

goto menu

:fixdeps
echo   检测到缺少依赖，正在自动安装（需要联网，可能要等一会儿）...
echo.
python -m pip install --disable-pip-version-check websockets
echo.
python -c "import websockets" >nul 2>&1
if errorlevel 1 goto depsfailed
echo   依赖装好了。
echo.
goto menu

:depsfailed
echo   [x] 自动安装失败。请手动执行这一条：
echo.
echo         python -m pip install websockets cryptography
echo.
echo   如果提示 pip 不存在，先执行：
echo         python -m ensurepip --upgrade
echo.
pause
exit /b 1

:nopython
echo   [x] 没找到 python 命令。
echo.
echo       请先安装 Python 3.10 或更新版本，安装时记得勾选
echo       "Add python.exe to PATH"，装完重开一个窗口再运行本文件。
echo.
echo       下载地址：https://www.python.org/downloads/
echo.
pause
exit /b 1

:oldpython
echo   [x] Python 版本太旧，本项目需要 3.10 或更新版本。当前版本：
python -V
echo.
pause
exit /b 1

:menu
echo   [1] 演示模式（离线看效果；会自己造模拟弹幕，正式开播别用）
echo   [2] 连直播间（需要填房间号）
echo   [3] 自检（离线测试，验证代码没坏）
echo   [4] 扫码登录网易云（设置 cookie，不用手抄）
echo.
set /p choice=请选择 [1/2/3/4]:

if "%choice%"=="1" goto demo
if "%choice%"=="2" goto live
if "%choice%"=="3" goto test
if "%choice%"=="4" goto qrlogin
goto demo

:demo
echo.
echo 正在以演示模式启动...
python -m songboard --mode demo --open
goto end

:live
echo.
set /p room=请输入直播间号（不是 UID）:
echo.
echo 正在连接直播间 %room% ...
python -m songboard --room %room% --open
goto end

:test
echo.
python selftest.py
pause
goto end

:qrlogin
rem 转给专门的扫码脚本：它会检查 Python / tkinter / qrcode 并自动补上
call "%~dp0扫码登录.bat"
goto end

:end
echo.
echo 已退出。
pause
