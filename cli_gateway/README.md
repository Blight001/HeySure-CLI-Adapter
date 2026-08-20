# cli_gateway 统一运行时

两个 CLI 网关现在遵循同一层次：

```text
agent.py                          统一对外 API 与模型路由
server.py --platform <name>       私有子网关入口
  └─ cli_gateway/backends/        平台差异
       ├─ grok.py                 headless / ACP / streaming JSON
       ├─ grok_acp.py             Grok ACP 传输实现
       └─ antigravity.py          agy CLI / 旧 direct OAuth
  └─ cli_gateway/shared.py        无平台差异的纯函数
```

`grok_cli_api/server.py`、`antigravity_cli_api/server.py` 只用于兼容旧启动命令和旧测试导入。新增公共能力不得再
复制进两个 backend，应优先放进 `shared.py`；只有 CLI 参数、认证、流格式或会话语义
确实不同的代码才留在 backend。

统一对外入口直接运行 `agent.py` 并在网页配置，或读取同一份配置直接运行：

```bash
python server.py
```

以下命令只供统一网关拉起私有后端和兼容旧部署：

```bash
python server.py --platform grok --port 8100
python server.py --platform antigravity serve --port 8110
```

对外 HTTP 契约保持一致：`GET /health`、`GET /v1/models`、
`POST /v1/chat/completions`。
