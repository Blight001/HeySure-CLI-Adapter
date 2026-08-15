@echo off
cd /d %~dp0
if /i "%~1"=="controller" (
  shift
  python controller.py %*
  exit /b %errorlevel%
)
python ..\manage.py --platform codex %*
