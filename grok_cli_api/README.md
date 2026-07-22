# grok_cli — 本地 OpenAI 兼容 API 网关

把本机 **grok CLI**（订阅额度）包装成一个标准的 OpenAI 格式 HTTP 服务。任何按
OpenAI 协议调用的客户端都能直接使用，HeySure 服务器把它当成普通 API 模型预设即可，
服务端不再需要任何 CLI 特判逻辑。

```
HeySure AI Runtime ──HTTP──► grok-cli-gateway (127.0.0.1:8100) ──subprocess──► grok.exe
```

## 启动

纯 Python 标准库实现，无第三方依赖。

### Windows

```bat
device\grok_cli\run.bat
:: 或
python server.py --command C:\Users\admin\.grok\bin\grok.exe --port 8100
```

### Linux 服务器（推荐 `run.sh`）

```bash
cd device/grok_cli
chmod +x run.sh

# 交互菜单：依赖 / 装 CLI / 登录 / 代理 / 启停
./run.sh

# 或分步：
./run.sh deps            # 安装 python3、curl（缺包时需 sudo）
./run.sh proxy           # 交互配置系统代理（网址 + 端口）
./run.sh install-cli     # curl https://x.ai/cli/install.sh | bash
./run.sh login           # 检查登录；未登录引导 grok login 或填 XAI_API_KEY
./run.sh start           # 后台启动网关（日志 runtime/gateway.log）
./run.sh status
./run.sh logs -f
./run.sh restart
./run.sh stop
```

### 系统代理（服务器出网）

装 CLI、登录、推理（`grok` 子进程）都会继承代理环境变量。配置写入 `.env.proxy`，`start` / `install-cli` 自动加载。

```bash
# 交互：填代理 IP/域名 + 端口（可选协议与账号）
./run.sh proxy

# 非交互
./run.sh proxy set --host 127.0.0.1 --port 7890
./run.sh proxy set --host 127.0.0.1 --port 1080 --scheme socks5
./run.sh proxy set --url http://user:pass@10.0.0.2:8080
./run.sh proxy set 127.0.0.1:7890
./run.sh proxy test      # 探测外网连通性
./run.sh proxy show
./run.sh proxy clear     # 清除

# 改完代理后若网关已在跑，需重启生效
./run.sh restart
```

会导出：`http_proxy` / `https_proxy` / `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`，以及 `NO_PROXY=localhost,127.0.0.1,::1`（本机健康检查不走代理）。

对外暴露示例（同机 HeySure 可继续用 `127.0.0.1`）：

```bash
export GROK_CLI_HOST=0.0.0.0
export GROK_CLI_API_KEY='your-gateway-secret'   # 强烈建议设置
./run.sh start
```

### 无 GUI 服务器怎么「真正登录」（订阅账号）

`grok login` 是浏览器 OAuth，token 写在 `~/.grok/auth.json`。服务器没桌面时，**正确做法是本机登录再拷凭证**：

```text
有浏览器的电脑                          无 GUI 服务器
─────────────────                      ────────────────
1. 安装 grok CLI
2. grok login  （浏览器授权）
3. 得到 ~/.grok/auth.json
        │
        └── scp ─────────────────────►  ~/.grok/auth.json
                                       ./run.sh login   # 检测 OK
                                       ./run.sh start
```

**在你自己的 Windows 上：**

```powershell
# 安装（若尚未安装）
irm https://x.ai/cli/install.ps1 | iex

grok login
# 确认：
dir $env:USERPROFILE\.grok\auth.json

# 拷到服务器（改 IP / 用户）
scp $env:USERPROFILE\.grok\auth.json root@服务器IP:~/.grok/auth.json
```

**在服务器上：**

```bash
mkdir -p ~/.grok && chmod 700 ~/.grok
chmod 600 ~/.grok/auth.json
cd /path/to/device/grok_cli
./run.sh login    # 应显示凭证 OK
./run.sh start
```

也可 `./run.sh login` 选 **1) 拷贝凭证登录**，按屏幕提示操作。

> 备选：服务器上 `./run.sh login` 选 2，尝试把登录 URL 打到终端用手机打开——若 OAuth 回调是 `127.0.0.1` 往往会失败，仍应改用上面的 scp 方式。  
> API Key（`XAI_API_KEY` / console.x.ai）是按量计费兜底，**不是**订阅 OAuth 登录。

| 环境变量 / 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `GROK_CLI_COMMAND` / `--command` | `grok` | CLI 命令名或完整路径（Windows `run.bat` 默认 `%USERPROFILE%\.grok\bin\grok.exe`；Linux `run.sh` 自动探测 `~/.grok/bin/grok`） |
| `GROK_CLI_HOST` / `--host` | `127.0.0.1` | 监听地址 |
| `GROK_CLI_PORT` / `--port` | `8100` | 监听端口 |
| `GROK_CLI_TIMEOUT` / `--timeout` | `600` | 单次推理超时（秒），超时杀进程 |
| `GROK_CLI_API_KEY` / `--api-key` | 空（不校验） | 设置后要求请求携带 `Authorization: Bearer <key>` |
| `GROK_CLI_MODELS` / `--models` | `grok-4.5` | `GET /v1/models` 展示的模型 id（逗号分隔，仅展示用） |
| `GROK_CLI_ACP` | `1` | 请求携带 `tools[]` 时启用有状态 ACP 会话；设为 `0` 回退 headless 文本协议 |
| `GROK_CLI_SESSION_TTL` | `1800` | ACP 会话空闲回收时间（秒） |
| `GROK_CLI_MAX_SESSIONS` | `6` | 并存 ACP 会话上限（LRU 淘汰） |
| `XAI_API_KEY` | 空 | 给 **grok CLI** 用的 xAI 密钥（无浏览器环境）；与网关 `GROK_CLI_API_KEY` 不是同一个 |
| `GROK_CLI_PROXY_HOST` | 空 | 代理主机（IP/域名） |
| `GROK_CLI_PROXY_PORT` | 空 | 代理端口 |
| `GROK_CLI_PROXY_SCHEME` | `http` | `http` / `https` / `socks5` / `socks5h` |
| `GROK_CLI_PROXY_URL` | 空 | 完整代理 URL（与 host+port 二选一，URL 优先） |
| `GROK_CLI_PROXY_USER` / `PASS` | 空 | 代理认证（可选） |
| `GROK_CLI_NO_PROXY` | `localhost,127.0.0.1,::1` | 不走代理的主机列表 |

## 接口

- `POST /v1/chat/completions` — OpenAI 格式，支持 `stream: true`（SSE）与非流式。
  - `model` 原样透传给 CLI 的 `-m`。
  - 推理增量以 `delta.reasoning_content` 输出，正文以 `delta.content` 输出。
  - **工具语法归一化**：grok 滑回私有格式
    `<xai:function_call name="..."><parameter ...>` 时，网关把完整块重写为规范
    `<mcp-call>{"tool":...,"arguments":{...}}</mcp-call>` 再输出（流式下疑似
    块开头的尾部会短暂扣住，块补完或推理结束时放行，正文只延迟不丢失）。
  - 响应携带 `system_fingerprint: "grok-cli-gateway"`，调用方可据此识别网关。
  - 消息里的图片块（`image_url` data URL / http(s) / 本地路径）会落盘到 `runtime/`
    临时文件，并提示模型用 grok 内置 `read_file` 查看像素。
- `GET /v1/models` — 展示配置的模型列表。
- `GET /` — 健康检查。

## 在 HeySure 中使用

系统设置 → 模型预设，新增一条普通 API 预设：

- **Base URL**：`http://127.0.0.1:8100/v1/chat/completions`
- **API Key**：任意非空值（除非配置了 `GROK_CLI_API_KEY`）
- **模型**：如 `grok-4.5`（透传给 `grok -m`）

> 网关必须与 grok CLI 在同一台机器上运行；HeySure 服务器只需能通过 HTTP 访问该
> 端口（本机部署填 127.0.0.1；服务器在别处时用 `--host 0.0.0.0` 并配置 `--api-key`）。

## 实现要点（grok CLI 的坑）

- 带 `tools[]` 的 HeySure 请求默认走 ACP。HeySure 在
  `X-HeySure-Session-ID` 中发送匿名、稳定的会话 ID，网关据此跨用户轮次
  复用同一个 `session/prompt`，只发送上轮之后的新增消息。
- 同一请求因网络问题重试时返回内存缓存，不重复调用 GROK；同一会话
  串行执行，不同会话仍可并行。
- HeySure 历史被清空、压缩或改写，或者网关重启/会话 TTL 过期时，自动
  新建 ACP 会话并全量重放当前历史。
- 不带 `tools[]` 或禁用 ACP 的请求仍走 headless：每次启动
  `grok --prompt-file <tmp> --output-format streaming-json ...`，并将完整对话
  （含 system prompt）序列化进 prompt 文件。
- grok 拒绝创建零内置工具的会话，最少保留 `--tools todo_write,read_file`
  （`read_file` 同时用于图片输入）；`--tools ""` 与 `--disallowed-tools "*"` 均不生效。
- prompt 不支持 stdin，必须用 `--prompt-file`。
- `--max-turns` 会把 thinking 也算一轮，不要使用。
