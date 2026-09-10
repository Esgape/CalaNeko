@echo off
chcp 65001 >nul
title CalaNeko

cd /d "%~dp0"

echo ========================================
echo   CalaNeko - 游戏进程优化工具
echo   Web 界面版 (浏览器访问)
echo ========================================
echo.

:: 检查 Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.8+
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

:: 检查 psutil
python -c "import psutil" >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 正在安装依赖 psutil...
    pip install psutil
)

:: 检查管理员权限
net session >nul 2>&1
if %errorlevel% == 0 (
    echo [√] 管理员权限 - 完整功能模式
) else (
    echo [!] 非管理员权限 - 轮询模式（功能受限）
    echo     建议右键此文件 - 以管理员身份运行
    echo.
)

echo.
echo 正在启动 CalaNeko Web 服务...
echo 服务地址: http://127.0.0.1:18765
echo 浏览器将自动打开，关闭此窗口即退出服务
echo.

:: 启动 Web 服务
python "%~dp0src\game_boost_web.py"

pause
