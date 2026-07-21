@echo off
rem grok-cli-gateway：把本机 grok CLI 包装成 OpenAI 兼容 API（默认 http://127.0.0.1:8100）
rem 按需修改下面的环境变量，或直接用命令行参数：python server.py --command ... --port ...
cd /d %~dp0

if "%GROK_CLI_COMMAND%"=="" set GROK_CLI_COMMAND=%USERPROFILE%\.grok\bin\grok.exe
if "%GROK_CLI_PORT%"=="" set GROK_CLI_PORT=8100

python server.py %*
