# HeySure CLI Adapter

`agent.py` 是 Codex、Grok、Antigravity 三套本地 CLI 网关的统一入口。部署时只运行
一个 Adapter；它自带本地管理网页，具体平台、HeySure 登录、CLI 路径、模型、工作
目录、超时和 Codex 沙箱均在网页中设置，保存后实时生效。

## 启动

先在本机安装并登录至少一种 CLI，再执行：

```powershell
pip install -r requirements.txt
.\run.bat
```

然后打开 `http://127.0.0.1:8130/` 完成设置。Linux/macOS 运行 `./run.sh`。
HeySure 配置存放在本机 `control_runtime/config.json`（已忽略提交，权限尽量收紧为
仅当前用户可读）；Codex/Grok/Antigravity 的登录资料仍由各 CLI 保存在本机，不上传
HeySure 服务器。

Adapter 首次上线后：

1. 在 Adapter 管理页选择平台、填写 HeySure 连接并启用；
2. 在 HeySure 网页“作坊”中找到 `本机 CLI Adapter`，给它“分配 AI 成员”；
3. 在设备的“MCP 权限”中勾选 `cli.run`、`cli.models`、`cli.status`。

完成后，被绑定的 HeySure AI 会看到这三个工具，并可根据任务主动调用本机 CLI。
接入与回包完全遵循 [`../read.md`](../read.md) 的 `device:register`、`task:dispatch`
和 `task:result/task:error` 契约。

原来的三个 `*_cli_api/server.py` 保留为 Adapter 内部后端，也仍可单独用于兼容旧的
OpenAI API 接入，但新部署无需分别启动它们。
