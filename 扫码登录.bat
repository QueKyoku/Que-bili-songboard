@echo off
chcp 936 >nul
cd /d "%~dp0"

echo ============================================
echo   网易云扫码登录（给点歌板设置 cookie）
echo ============================================
echo.

rem ---------- 1) 有没有 Python ----------
rem 这一步必须在 .bat 里做：没装 Python 的话，
rem 「扫码登录.pyw」双击根本不会被执行（Windows 只会问"用什么打开"），
rem 所以检测逻辑不能写在 .pyw 里面。
where python >nul 2>&1
if errorlevel 1 goto nopython

rem ---------- 2) 版本够不够（代码要求 3.10+）----------
python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 goto oldpython

rem ---------- 3) 有没有 tkinter（图形界面靠它）----------
python -c "import tkinter" >nul 2>&1
if errorlevel 1 goto notk

rem ---------- 4) qrcode 装了没，缺了就自动装 ----------
python -c "import qrcode" >nul 2>&1
if errorlevel 1 goto fixdeps

goto launch

:fixdeps
echo   正在安装 qrcode（画二维码用的，纯 Python 小库，需要联网）...
echo.
python -m pip install --disable-pip-version-check qrcode
echo.
python -c "import qrcode" >nul 2>&1
if errorlevel 1 goto depsfailed
echo   装好了。
echo.
goto launch

:depsfailed
echo   [x] 自动安装失败。请手动执行这一条：
echo.
echo         python -m pip install qrcode
echo.
echo   如果提示 pip 不存在，先执行：
echo         python -m ensurepip --upgrade
echo.
pause
exit /b 1

:nopython
echo   [x] 这台电脑上没找到 python 命令。
echo.
echo       两条路，挑一条：
echo.
echo       1) 装一个 Python 3.10 或更新版本
echo          下载： https://www.python.org/downloads/
echo          **安装时务必勾选 "Add python.exe to PATH"**，
echo          装完重开一个窗口，再双击本文件。
echo.
echo       2) 直接用打包好的 exe（不需要装 Python）
echo          在项目目录里执行一次：
echo              powershell -ExecutionPolicy Bypass -File tools\build_gui.ps1
echo          把 dist\网易云扫码登录.exe 放到项目根目录，双击它就行。
echo.
pause
exit /b 1

:oldpython
echo   [x] Python 版本太旧，本项目需要 3.10 或更新版本。当前版本：
python -V
echo.
pause
exit /b 1

:notk
echo   [x] 这个 Python 没带 tkinter（图形界面要用它）。
echo.
echo       多半是用了精简版/特殊发行版。用官方安装包装一次就有了：
echo       https://www.python.org/downloads/
echo.
echo       不想换 Python 的话，改成命令行版扫码：
echo           python tools\login_qrcode.py
echo       （在终端里画二维码，效果一样）
echo.
pause
exit /b 1

:launch
echo   环境没问题，正在打开扫码窗口...
rem 用 pythonw 启动，不留黑色控制台窗口；没有 pythonw 就退回 python
where pythonw >nul 2>&1
if errorlevel 1 (
    python "%~dp0tools\扫码登录.pyw"
) else (
    start "" pythonw "%~dp0tools\扫码登录.pyw"
)
exit /b 0
