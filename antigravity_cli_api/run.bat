@echo off
rem Official Antigravity CLI (agy) -> OpenAI-compatible gateway.
cd /d %~dp0

if "%ANTIGRAVITY_HOST%"=="" set ANTIGRAVITY_HOST=127.0.0.1
if "%ANTIGRAVITY_PORT%"=="" set ANTIGRAVITY_PORT=8110
if "%ANTIGRAVITY_BACKEND%"=="" set ANTIGRAVITY_BACKEND=cli
set "PATH=%LOCALAPPDATA%\agy\bin;%PATH%"

if /i "%~1"=="install-cli" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://antigravity.google/cli/install.ps1 | iex"
  exit /b %errorlevel%
)

if /i "%~1"=="login" (
  where agy >nul 2>nul || (
    echo [antigravity] 未找到 agy，正在运行 Google 官方安装器……
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://antigravity.google/cli/install.ps1 | iex"
    if errorlevel 1 exit /b 1
  )
)

if "%~1"=="" (
  python server.py serve
) else (
  python server.py %*
)
