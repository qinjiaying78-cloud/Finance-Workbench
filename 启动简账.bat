
@echo off
setlocal
cd /d "%~dp0"
title 简账 - 本地财务工作台

echo ========================================
echo       简账 - 本地财务工作台
echo ========================================
echo.

echo [检查] 检测 Python 环境...

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python，请安装 Python 3.10 或以上版本。
    pause
    exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo [错误] Python 版本低于 3.10。
    pause
    exit /b 1
)

python --version

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [1/4] 创建 Python 虚拟环境...
    python -m venv .venv
    if errorlevel 1 goto :fail
)

echo.
echo [2/4] 安装或检查后端依赖...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail

".venv\Scripts\python.exe" -m pip install -r backend\requirements.txt
if errorlevel 1 goto :fail

echo.
echo [3/4] 启动 FastAPI 后端...
start "简账 FastAPI" /B ".venv\Scripts\python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

echo.
echo [4/4] 等待服务就绪并打开浏览器...

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ok=$false; for($i=0;$i -lt 40;$i++){ try { $r=Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/api/health -TimeoutSec 2; if($r.StatusCode -eq 200){$ok=$true;break} } catch {}; Start-Sleep -Milliseconds 500 }; if($ok){Start-Process 'http://127.0.0.1:8000/app'} else {Write-Error '服务未能在 20 秒内就绪'; exit 1}"

if errorlevel 1 goto :fail

echo.
echo ========================================
echo 简账已启动！
echo 浏览器地址：http://127.0.0.1:8000/app
echo ========================================
echo.
echo 关闭此窗口不会自动停止后台服务。
pause
exit /b 0

:fail
echo.
echo [启动失败] 请检查上方错误信息、依赖安装和网络连接。
pause
exit /b 1
