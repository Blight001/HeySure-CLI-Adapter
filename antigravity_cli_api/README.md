# Antigravity Python API 网关

把个人 Google 账号的 Antigravity OAuth 会话转换为 OpenAI 兼容接口：

```text
POST http://127.0.0.1:8110/v1/chat/completions
```

实现只依赖 Python 标准库，不安装 Node.js/npm，也不启动 Gemini CLI 或
Antigravity 桌面程序。OAuth、令牌刷新、请求格式转换和流式响应都由
`server.py` 完成。

> 注意：这不是 Google 官方服务端 SDK。实现参考 CLIProxyAPI 使用的
> Antigravity OAuth 与 `v1internal` 接口；Google 随时可能修改接口、模型名、
> OAuth 客户端或使用规则。请只用于自己的账号，并遵守 Google 服务条款。

## Linux 服务器快速使用

```bash
chmod +x run.sh
./run.sh deps
./run.sh login
./run.sh config
./run.sh start
./run.sh status
```

无参数执行 `./run.sh` 会显示交互菜单。root 可以直接运行管理脚本，但脚本会
创建 `antigravity-api` 普通用户；OAuth 凭证与网关进程不会以 root 身份运行。

登录时程序会打印 Google 授权链接。远程服务器有两种完成回调的方式：

首次执行 `./run.sh login` 时，脚本会要求输入 Google OAuth 桌面应用的
Client ID 和 Client Secret，并保存到不会提交 Git 的 `.env`。请在 Google Cloud
Console 的“API 和服务 → 凭据”中创建 `Desktop app` 类型 OAuth Client；如果
OAuth consent screen 处于测试状态，还要把自己的 Google 账号加入测试用户。

1. 推荐先在自己的电脑建立隧道，再打开授权链接：

   ```bash
   ssh -L 51121:127.0.0.1:51121 root@服务器地址
   ```

2. 如果浏览器回调页面打不开，复制浏览器地址栏中的完整
   `http://localhost:51121/oauth-callback?...` 地址，粘贴回服务器终端。

每次必须使用本次登录生成的新链接；旧 authorization code 只能使用一次，
重复提交会出现 `invalid_grant`。服务器时间严重不准也会导致授权失败。

常用命令：

```bash
./run.sh auth-status
./run.sh config
./run.sh logs
./run.sh logs -f
./run.sh restart
./run.sh stop
./run.sh proxy http://127.0.0.1:7890
./run.sh proxy clear
```

## Windows

```bat
run.bat login --open-browser
run.bat auth-status
run.bat
```

无参数 `run.bat` 会以前台模式启动网关。

## API 调用

```bash
curl http://127.0.0.1:8110/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gemini-pro-agent",
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

支持：

- 普通响应与 `stream: true` SSE 流式响应
- system/developer/user/assistant/tool 消息
- OpenAI `tools`、`tool_choice` 和原生 `tool_calls`
- 文本、data URL/HTTP 图片输入及内联图片输出
- temperature、top_p、max_tokens、stop 和 reasoning_effort
- `GET /health` 与 `GET /v1/models`

首次登录或启动时，`run.sh` 会自动生成 `ANTIGRAVITY_API_KEY`，保存到权限为
`0600` 的 `.env`。执行下面的命令可以直接显示 HeySure 页面需要填写的全部字段：

```bash
./run.sh config
```

也可以自行覆盖这个本地鉴权密码：

```bash
export ANTIGRAVITY_API_KEY='请设置一个随机长字符串'
./run.sh restart
```

调用方随后携带 `Authorization: Bearer <ANTIGRAVITY_API_KEY>`。这个值只保护本地
网关，不是 Google API Key。

## 配置

Linux 可把配置写入同目录 `.env`，格式为 shell 的 `export KEY=value`。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ANTIGRAVITY_HOST` | `127.0.0.1` | 监听地址；需要外部访问时才改为 `0.0.0.0` |
| `ANTIGRAVITY_PORT` | `8110` | 监听端口 |
| `ANTIGRAVITY_TIMEOUT` | `600` | 上游请求超时秒数 |
| `ANTIGRAVITY_AUTH_FILE` | `runtime/antigravity-auth.json` | OAuth 凭证文件 |
| `ANTIGRAVITY_MODELS` | 见 `server.py` | `/v1/models` 返回的逗号分隔模型列表 |
| `ANTIGRAVITY_API_KEY` | `run.sh` 自动生成 | 本地网关 Bearer 鉴权；不是 Google API Key |
| `ANTIGRAVITY_PUBLIC_BASE_URL` | 自动按监听地址生成 | 仅供 `run.sh config` 显示反代后的公网完整接口地址 |
| `ANTIGRAVITY_RUN_USER` | `antigravity-api` | root 部署时使用的普通用户 |
| `ANTIGRAVITY_OAUTH_CLIENT_ID` | 必填 | Google OAuth Desktop app Client ID；由 `run.sh login` 提示录入 |
| `ANTIGRAVITY_OAUTH_CLIENT_SECRET` | 必填 | Google OAuth Desktop app Client Secret；仅保存于 `.env` |
| `ANTIGRAVITY_OAUTH_CALLBACK_PORT` | `51121` | OAuth 本地回调端口 |
| `ANTIGRAVITY_BASE_URLS` | daily、prod | 逗号分隔的上游地址及回退顺序 |
| `ANTIGRAVITY_USER_AGENT` | 自动发现 | 覆盖 Antigravity User-Agent |

token endpoint、authorization endpoint 和 userinfo endpoint 也都能通过同名前缀
环境变量覆盖，具体名称见 `server.py` 的 `Config`。

凭证文件包含 refresh token，程序以 `0600` 权限保存。不要提交、分享或放到 Web
目录。`.gitignore` 已忽略 `runtime/` 和 `.env`。

## 学生权益与费用

这个网关使用登录账号在 Antigravity/Google Code Assist 侧已有的权益，不会创建
Google AI Studio API Key，也没有“自动购买额度”或 AI credits 回退逻辑。学生验证
是否包含某个模型、具体限额和到期时间由 Google 当前权益决定；网关不能把学生
订阅转换成独立 Gemini API 免费额度。建议在 Google 账号订阅页确认权益，并观察
服务返回的 quota/rate-limit 错误。

## 测试

测试使用本地模拟 Google OAuth/Antigravity 上游，不消耗账号额度：

```bash
python -m unittest -v test_server.py
```
