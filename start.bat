@echo off
chcp 65001 >nul
title 重庆旅游知识库系统

echo ============================================================
echo 重庆旅游知识库系统 - 启动脚本
echo ============================================================
echo.

REM 检查Python是否安装
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] Python未安装或未添加到PATH
    echo [提示] 请从 https://www.python.org/downloads/ 下载安装Python 3.11+
    pause
    exit /b 1
)

REM 检查Python版本（需要 >=3.11）
for /f "tokens=2" %%V in ('python --version 2^>^&1') do set "PY_VER=%%V"
echo [信息] Python版本: %PY_VER%

REM 切换到脚本所在目录
cd /d "%~dp0"

echo [信息] 当前目录: %cd%
echo.

REM 检查虚拟环境
set "VENV_ACTIVATED=0"
if exist "TraeAI-4\Scripts\activate.bat" (
    echo [信息] 激活虚拟环境 TraeAI-4...
    call TraeAI-4\Scripts\activate.bat
    set "VENV_ACTIVATED=1"
) else if exist "venv\Scripts\activate.bat" (
    echo [信息] 激活虚拟环境 venv...
    call venv\Scripts\activate.bat
    set "VENV_ACTIVATED=1"
) else (
    echo [警告] 未找到 TraeAI-4 或 venv 虚拟环境
    echo [提示] 如需创建虚拟环境: python -m venv venv ^&^& venv\Scripts\activate ^&^& pip install -r requirements.txt
)

echo.

REM 检查 Playwright/CloakBrowser 浏览器二进制（首次部署常见问题）
echo [检查] 浏览器二进制...
where chrome.exe >nul 2>&1
if not errorlevel 1 (
    echo [成功] 检测到系统 Chrome
) else (
    where msedge.exe >nul 2>&1
    if not errorlevel 1 (
        echo [成功] 检测到系统 Edge
    ) else (
        echo [警告] 未检测到系统 Chrome 或 Edge，浏览器启动可能失败
        echo [提示] 请安装 Chrome: https://www.google.com/chrome/ 或 Edge: https://www.microsoft.com/edge
    )
)

REM 检查 CloakBrowser 浏览器缓存
if exist "%LOCALAPPDATA%\cloakbrowser\cache" (
    echo [成功] CloakBrowser 浏览器缓存存在
) else (
    echo [提示] CloakBrowser 浏览器缓存未初始化，首次启动将自动下载
    echo [提示] 如离线部署，请预热缓存或设置 CLOAKBROWSER_BINARY_PATH 指向本地浏览器
)

echo.

REM 检查Ollama服务
echo [检查] Ollama服务状态...
curl -s http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo [警告] Ollama服务未运行
    echo [提示] 请先启动Ollama服务: ollama serve
    echo [提示] 或者系统将使用云端API作为备选
) else (
    echo [成功] Ollama服务正常运行
)

echo.

REM 检查知识库
if exist "data\knowledge_base.json" (
    echo [成功] 知识库文件存在
) else (
    echo [警告] 知识库文件不存在
)

echo.

REM 创建必要的目录
if not exist "data" mkdir data
if not exist "data\app_state" mkdir data\app_state
if not exist "logs" mkdir logs

REM 跨电脑部署提示
if exist "data\browser_user_data" (
    echo [提示] 检测到 data\browser_user_data 目录
    echo [提示] 该目录包含开发机的 Chrome 登录态（DPAPI 加密），跨机器部署后可能无法解密
    echo [提示] 如首次在新机器部署，建议先手动删除该目录让系统自动重建
    echo.
)

set "SERVER_HOST_VALUE=127.0.0.1"
set "SERVER_PORT_VALUE=8023"
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if /i "%%~A"=="SERVER_HOST" set "SERVER_HOST_VALUE=%%~B"
        if /i "%%~A"=="SERVER_PORT" set "SERVER_PORT_VALUE=%%~B"
    )
)
if defined SERVER_HOST set "SERVER_HOST_VALUE=%SERVER_HOST%"
if defined SERVER_PORT set "SERVER_PORT_VALUE=%SERVER_PORT%"
set "ACCESS_HOST=%SERVER_HOST_VALUE%"
if /i "%ACCESS_HOST%"=="0.0.0.0" set "ACCESS_HOST=127.0.0.1"
if /i "%ACCESS_HOST%"=="::" set "ACCESS_HOST=127.0.0.1"

echo [信息] 启动Web服务...
echo [信息] 访问地址: http://%ACCESS_HOST%:%SERVER_PORT_VALUE%
echo.
echo ============================================================
echo 按 Ctrl+C 停止服务
echo ============================================================
echo.

python run_app.py

pause
