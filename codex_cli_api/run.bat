@echo off
rem Codex CLI -> OpenAI-compatible gateway (default http://127.0.0.1:8120)
cd /d %~dp0

if "%CODEX_CLI_COMMAND%"=="" set CODEX_CLI_COMMAND=codex
if "%CODEX_CLI_HOST%"=="" set CODEX_CLI_HOST=127.0.0.1
if "%CODEX_CLI_PORT%"=="" set CODEX_CLI_PORT=8120

if /i "%~1"=="login" (
  codex login
  exit /b %errorlevel%
)

if /i "%~1"=="login-status" (
  codex login status
  exit /b %errorlevel%
)

python server.py %*
