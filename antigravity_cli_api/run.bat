@echo off
rem Pure-Python Antigravity OAuth -> OpenAI-compatible gateway.
cd /d %~dp0

if "%ANTIGRAVITY_HOST%"=="" set ANTIGRAVITY_HOST=127.0.0.1
if "%ANTIGRAVITY_PORT%"=="" set ANTIGRAVITY_PORT=8110

if "%~1"=="" (
  python server.py serve
) else (
  python server.py %*
)
