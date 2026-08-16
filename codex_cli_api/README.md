# Codex CLI 本地 OpenAI 兼容 API 网关

把本机已登录的 Codex CLI 包装成 `POST /v1/chat/completions`，供 HeySure 或其他
OpenAI 兼容客户端调用。它使用本机 Codex 登录/订阅，不需要修改 HeySure 服务端。

```text
HeySure AI Runtime -> HTTP :8120 -> codex exec --json -> OpenAI/Codex
```

## 安装与启动

前置条件：Python 3.9+、已安装 Codex CLI，并完成登录。

```powershell
codex --version
codex login                 # 或 device\cli_to_api\codex_cli_api\run.bat login
codex login status

cd device\cli_to_api\codex_cli_api
run.bat
```

Linux 服务器使用与 `grok_cli_api` 相同的管理方式：

```bash
cd device/cli_to_api/codex_cli_api
chmod +x run.sh
./run.sh                 # 进入交互管理菜单

# 或按步骤执行
./run.sh deps            # Python、curl、Node.js/npm
./run.sh install-cli     # npm 安装/更新 @openai/codex
./run.sh login           # device auth / OAuth / 拷凭证 / API Key
./run.sh expose on       # 0.0.0.0 + 自动生成网关密钥，写入 .env
./run.sh start           # 后台启动
./run.sh status
```

无 GUI/SSH 服务器可使用两种订阅登录方式：

- `1) Device Auth`：推荐方式，在浏览器输入一次性代码。
- `2) 浏览器 OAuth`：脚本让 `codex login` 在服务器后台等待；浏览器授权后会停在
  `http://localhost:1455/auth/callback?...`。复制地址栏中的完整 URL 并粘回脚本提示，
  脚本会严格校验地址，再从服务器本机把回调交给 Codex。

callback URL 含一次性授权码，不要发给他人。也可以事先使用
`ssh -L 1455:127.0.0.1:1455 用户@服务器IP` 建立端口转发，此时浏览器会自动完成回调。

默认仍只监听 `127.0.0.1`。需要让 Docker、局域网或远程 HeySure 访问时，使用
`expose on` 显式开放；它会强制设置网关密钥，并在服务已运行时自动重启：

```bash
./run.sh expose on                  # 自动生成强随机密钥
./run.sh expose on '你的强随机密钥' # 指定密钥
./run.sh expose show

# 收回到仅本机监听
./run.sh expose off
```

其他管理命令：

```bash
./run.sh proxy                       # 交互配置代理
./run.sh proxy set --host 127.0.0.1 --port 7890
./run.sh logs -f
./run.sh restart
./run.sh stop
./run.sh autostart on                # 创建并启用 systemd 服务
./run.sh autostart status
```

健康检查：

```bash
curl http://127.0.0.1:8120/health
curl http://127.0.0.1:8120/v1/models

# 从其他机器访问 Linux 服务器
curl http://服务器IP:8120/health
```

调用示例：

```bash
curl http://127.0.0.1:8120/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-HeySure-Session-ID: demo-session" \
  -d '{"model":"codex-default","messages":[{"role":"user","content":"你好"}]}'
```

## 在 HeySure 中配置

- Base URL：同机进程使用 `http://127.0.0.1:8120/v1/chat/completions`；远端 HeySure
  使用 `http://服务器IP:8120/v1/chat/completions`
- API Key：任意非空值；若网关配置了 `CODEX_CLI_API_KEY`，两边需一致
- 模型：从网关 `/v1/models` 返回值中选择；使用 `codex-default` 或不传模型时沿用
  Codex CLI 自己的默认配置

HeySure 发送的 `X-HeySure-Session-ID` 会映射为独立 Codex thread。正常追问只把新增
消息发给 `codex exec resume`；相同请求重试直接返回缓存；历史被清空、压缩或改写时自动
创建新 thread。状态保存在 `runtime/sessions/`，该目录不应提交。

## 配置

| 环境变量 / 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `CODEX_CLI_COMMAND` / `--command` | `codex` | CLI 命令或完整路径 |
| `CODEX_CLI_HOST` / `--host` | `127.0.0.1` | HTTP 监听地址；`expose on` 持久化为 `0.0.0.0` |
| `CODEX_CLI_PORT` / `--port` | `8120` | HTTP 监听端口 |
| `CODEX_CLI_TIMEOUT` / `--timeout` | `900` | 单次调用超时秒数 |
| `CODEX_CLI_API_KEY` / `--api-key` | 空 | 网关 Bearer 鉴权密钥 |
| `CODEX_CLI_MODELS` / `--models` | 空 | 可选人工覆盖；默认通过 `codex debug models` 动态发现并缓存 5 分钟 |
| `CODEX_CLI_SANDBOX` / `--sandbox` | `read-only` | Codex 本机工具沙箱 |
| `CODEX_CLI_CWD` / `--workspace` | 空 | 可选固定项目工作目录；设置后 Codex 直接在该目录读取或修改文件 |
| `CODEX_CLI_SESSIONS_DIR` / `--sessions-dir` | `runtime/sessions` | thread 映射与缓存目录 |

推荐用与 Grok 相同的持久化开放命令：

```bash
./run.sh expose on
./run.sh start
```

还需在云服务器安全组和系统防火墙中放行 TCP `8120`。如果 HeySure 与网关位于同一台
服务器的不同 Docker 容器中，优先通过宿主机网关地址或 Compose 服务名访问，不必把
`8120` 开放给整个公网。

端口 `8120` 默认只提供明文 HTTP，不能填写成 `https://服务器IP:8120`。需要 HTTPS 时，
应由 Nginx/Caddy 使用域名和证书终止 TLS，再反向代理到
`http://127.0.0.1:8120`；HeySure 中填写反向代理后的
`https://域名/v1/chat/completions`。如果 HTTPS 被误发到 8120，网关只记录一条协议提示，
不再把 TLS 二进制内容写成 journald blob。

## 接口和限制

- `POST /v1/chat/completions`：支持非流式和 `stream: true` SSE。
- `GET /v1/models`、`GET /health`、`GET /`。
- Codex CLI 的 `--json` 输出以完成事件为主，因此 SSE 是 OpenAI 格式兼容的分块响应，
  不是逐 token 实时流。
- HeySure 的 `tools[]` 被写入 prompt，模型应以
  `<mcp-call>{"tool":"...","arguments":{...}}</mcp-call>` 返回；网关不会把平台工具注册成
  本机 Codex 工具。
- 默认 `read-only`，防止通用聊天请求改动网关所在机器。只有明确需要 Codex 操作本地工作区
  时才应改成 `workspace-write`，不要在公网服务中使用 `danger-full-access`。

运行测试：

```bash
python -m unittest -v test_server.py
```

## 作为 HeySure 外部数字成员常驻对话

服务器支持 `heysure.claim_message` / `heysure.reply_message` 后，本机可以主动领取
网页或机器人发给外部成员的消息。此模式只建立电脑到服务器的出站 HTTPS 连接，不开放
本机端口；每个 HeySure 会话映射为一个持续的 Codex thread。

先把成员控制文档中的 Token 放入环境变量，再启动：

```powershell
$env:HEYSURE_CONTROLLER_TOKEN = '控制文档中一次性显示的 Token'
run.bat controller --endpoint https://你的域名/mcp/external
```

控制文档使用成员专属环境变量名时，用 `--token-env` 指定：

```powershell
run.bat controller --endpoint https://你的域名/mcp/external `
  --token-env HEYSURE_CONTROLLER_TOKEN_19
```

需要让外部成员在一个本地项目中持续排查和修改代码时，显式指定工作区、会话目录和
最小写权限。会话目录包含对话历史，必须加入项目的 `.gitignore`：

```powershell
run.bat controller --endpoint https://你的域名/mcp/external `
  --token-env HEYSURE_CONTROLLER_TOKEN_19 `
  --workspace D:\path\to\project `
  --sessions-dir D:\path\to\project\.heysure-codex-sessions `
  --sandbox workspace-write
```

桥接器领取消息时使用 30 分钟租约，成功后恰好一次写回助手消息；失败会把 turn 置为
终态并记录脱敏错误。Codex CLI 必须是普通终端可执行的独立安装版本；仅能由 Windows
Store 应用内部启动、在 PowerShell 中显示“拒绝访问”的内嵌可执行文件不能用作桥接器。
