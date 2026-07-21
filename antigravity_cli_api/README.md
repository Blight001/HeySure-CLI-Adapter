# Antigravity CLI → OpenAI API

把 Google 官方 Antigravity CLI（命令 `agy`）包装成 OpenAI 兼容接口：

```text
POST http://127.0.0.1:8110/v1/chat/completions
GET  http://127.0.0.1:8110/v1/models
GET  http://127.0.0.1:8110/health
```

默认链路是：

```text
HeySure → Python 网关 → 当前运行用户的 agy → Google Antigravity
```

网关不要求你创建 Google Cloud OAuth Client，也不把 Google token、账号或登录
信息保存到源码、`.env`、Git 仓库。登录由官方 `agy` 完成，用户数据由它保存在
运行用户自己的本地凭据目录/系统密钥存储中。Python 只启动该用户的 `agy` 子进程。

## Linux 快速开始

```bash
chmod +x run.sh

# root 可以运行；脚本会创建并使用 antigravity-api 普通用户
./run.sh deps
./run.sh install-cli
./run.sh login
./run.sh auth-status
./run.sh config
./run.sh start
./run.sh status
```

`./run.sh login` 也会在未找到 `agy` 时自动安装。SSH 服务器没有浏览器也没关系：
官方 CLI 会显示授权网址，在你自己的电脑浏览器打开并登录 Google，然后把页面给出的
授权码粘贴回 SSH 终端。

无参数运行 `./run.sh` 会显示交互菜单。

Windows 可使用：

```bat
run.bat install-cli
run.bat login
run.bat auth-status
run.bat
```

Windows 安装命令调用的也是 Google 官方 `install.ps1`；登录数据保存在当前 Windows
用户的 Credential Manager/本地配置中。

### 更新 CLI

```bash
./run.sh install-cli
```

安装器固定从 Google 官方地址下载：
`https://antigravity.google/cli/install.sh`。安装和登录都使用网关运行用户，而不是 root。

### `bash\r`: No such file or directory

Windows 拷到 Linux 后如果文件带 CRLF：

```bash
bash run.sh
# 或
sed -i 's/\r$//' run.sh && ./run.sh
```

脚本会自动修复本目录的 CRLF；仓库也通过 `.gitattributes` / `.editorconfig` 强制脚本使用 LF。

## HeySure 模型配置

运行：

```bash
./run.sh config
```

默认输出大致如下：

```text
显示名称：Antigravity Gemini
模型名：  gemini-3.5-flash-medium
Base URL：http://127.0.0.1:8110/v1/chat/completions
API Key： <脚本生成的本地网关密钥>
接口协议：OpenAI 兼容
工具协议：文本 MCP Call
```

已实测可用的模型 ID：

| 模型 ID | agy 显示名称 |
| --- | --- |
| `gemini-3.5-flash-medium` | Gemini 3.5 Flash (Medium) |
| `gemini-3.5-flash-high` | Gemini 3.5 Flash (High) |
| `gemini-3.5-flash-low` | Gemini 3.5 Flash (Low) |
| `gemini-3.1-pro-low` | Gemini 3.1 Pro (Low) |
| `gemini-3.1-pro-high` | Gemini 3.1 Pro (High) |

账号最终可用模型仍以 `./run.sh auth-status`（即 `agy models`）为准。可在 `.env` 覆盖：

```bash
export ANTIGRAVITY_MODELS='gemini-3.5-flash-medium,gemini-3.1-pro-low'
```

请求中的 `model: "auto"` 会选择列表中的第一个模型。

## 调用测试

```bash
curl http://127.0.0.1:8110/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <ANTIGRAVITY_API_KEY>' \
  -d '{
    "model": "gemini-3.5-flash-medium",
    "messages": [{"role":"user","content":"只回复 ok"}]
  }'
```

支持 `stream: true` 的 OpenAI SSE 响应格式。由于一次 `agy --print` 调用完成后网关才取得
最终文本，当前 CLI 后端会在命令完成后分块输出 SSE，不是逐 token 实时流。

## 长对话与 Linux 参数上限

HeySure 会在 `X-HeySure-Session-ID` 请求头中发送匿名、稳定的会话 ID（其他客户端
也可使用 OpenAI `user` 字段）。网关据此为每个会话建立独立工作目录：

- 第一次请求启动新的 `agy` 会话；
- 后续请求用 `agy --continue`，只发送上次回复后的新增消息；
- HeySure 清空、压缩或改写历史后，网关自动换一个新的本地 `agy` 会话；
- 同一请求因网络问题重试时返回本地缓存，不重复调用模型；
- 同一会话串行执行，不同会话可并行，避免 `--continue` 串线。

当单轮新增内容仍超过安全字节数时，网关把完整内容临时写入该会话工作目录，命令行
只向 `agy -p` 传递短的文件引用，并明确要求使用工作区内自动授权的 `read_file`，
对应模型实际可见的 `view_file` 工具（传入绝对路径）；不会开放 `command` 或危险的
全工具免确认权限。`agy` 完成读取后临时文件立即删除。
因此 Linux 单参数约 128 KiB 的限制不会再迫使网关截断模型上下文。

部分 `agy` 版本在无头 `-p` 模式下偶尔会生成成功却丢失 stdout。每次调用使用独立
CLI 日志定位本轮 conversation；仅当退出码为 0 且 stdout/stderr 都为空时，网关才从
官方本地 `transcript.jsonl` 恢复最后一条已完成回复。临时调用日志随后删除。

会话状态位于 `runtime/cli-sessions/`（root 部署时是服务目录下的 runtime），只用于
本地续接和幂等重试，已被 Git 忽略。

## 本地凭据放在哪里

- root 执行管理脚本时，默认运行用户是 `antigravity-api`。
- `agy` 安装在该用户目录，Google 登录数据由官方 CLI 管理。
- 网关进程使用完全相同的 Linux 用户及 `HOME`，因此能读取同一份本地登录状态。
- 代码目录只可能出现已忽略的 `.env`（网关监听/API Key）和 `runtime/`（PID/日志），不保存 Google token。
- 不要删除运行用户的 home/密钥存储；删除后需要重新 `./run.sh login`。

如果希望使用自己的普通用户：

```bash
export ANTIGRAVITY_RUN_USER=myuser
./run.sh login
./run.sh start
```

登录和启动必须始终使用同一个 `ANTIGRAVITY_RUN_USER`。

## 管理命令

```text
./run.sh deps
./run.sh install-cli
./run.sh login
./run.sh auth-status
./run.sh config
./run.sh proxy http://127.0.0.1:7890
./run.sh proxy clear
./run.sh start|stop|restart|status
./run.sh logs
./run.sh logs -f
./run.sh fg
```

默认只监听 `127.0.0.1`。需要 Docker、局域网或外部机器访问时：

```bash
./run.sh expose on              # 监听 0.0.0.0，并自动生成网关 API Key
./run.sh expose on 'my-secret'  # 指定网关 API Key
./run.sh expose show
./run.sh expose off
```

开放后请在云安全组/防火墙控制 8110 端口，并始终使用网关 API Key。

## 配置项

Linux 可写入同目录 `.env`：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ANTIGRAVITY_BACKEND` | `cli` | `cli` 使用官方 agy；`direct` 是旧直连兼容模式 |
| `ANTIGRAVITY_CLI_COMMAND` | 自动发现 `agy` | agy 可执行文件路径 |
| `ANTIGRAVITY_CLI_ARG_SAFE_BYTES` | `98304` | 超过此 UTF-8 字节数改用临时文件，不进入单个命令行参数 |
| `ANTIGRAVITY_CLI_SESSIONS_DIR` | `runtime/cli-sessions` | 本地会话映射、工作目录与重试状态 |
| `ANTIGRAVITY_MODELS` | 上表 5 个模型 ID | `/v1/models` 返回值，逗号分隔 |
| `ANTIGRAVITY_HOST` | `127.0.0.1` | 网关监听地址 |
| `ANTIGRAVITY_PORT` | `8110` | 网关端口 |
| `ANTIGRAVITY_TIMEOUT` | `600` | 单次 agy 调用超时秒数 |
| `ANTIGRAVITY_API_KEY` | 首次 config/start 自动生成 | 调用本地网关使用的密钥；不是 Google API Key |
| `ANTIGRAVITY_RUN_USER` | `antigravity-api` | Linux 安装、登录和运行 agy 的普通用户 |
| `ANTIGRAVITY_PUBLIC_BASE_URL` | 空 | 反向代理后的完整 `/v1/chat/completions` URL |

### 旧 direct 模式

只有确实需要保留旧的内部 HTTP 适配时才使用：

```bash
export ANTIGRAVITY_BACKEND=direct
export ANTIGRAVITY_OAUTH_CLIENT_ID='...'
export ANTIGRAVITY_OAUTH_CLIENT_SECRET='...'
```

默认 `cli` 模式完全不读取这两个 OAuth Client 变量，也不会创建
`runtime/antigravity-auth.json`。

## 官方资料

- [安装 Antigravity CLI](https://antigravity.google/docs/cli-install)
- [Antigravity CLI 实践教程](https://codelabs.developers.google.com/antigravity-cli-hands-on)
