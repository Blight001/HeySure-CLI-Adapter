@echo off
cd /d %~dp0
python ..\manage.py --platform codex %*
