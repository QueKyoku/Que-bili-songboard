@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   哔哩哔哩点歌板
echo ============================================
echo.
echo   [1] 演示模式（不连直播间，先看点歌板效果）
echo   [2] 连直播间（需要填房间号）
echo   [3] 自检（离线测试，验证代码没坏）
echo.
set /p choice=请选择 [1/2/3]:

if "%choice%"=="1" goto demo
if "%choice%"=="2" goto live
if "%choice%"=="3" goto test
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

:end
echo.
echo 已退出。
pause
