# HeySure CLI Adapter

`agent.py` 是 Grok、Antigravity 两套本地 CLI 网关的统一入口。部署时只运行
一个 Adapter；它自带本地管理网页，具体平台、HeySure 登录、CLI 路径、模型、工作
目录和超时均在网页中设置，保存后实时生效。

代码实现也已统一到 `cli_gateway/`：`agent.py` 管理统一对外 API，`server.py --platform ...`
只负责启动本机私有后端，两个 `*_cli_api/server.py` 仅保留为旧命令兼容壳。公共的配置、JSON、会话指纹和内容
处理位于 `cli_gateway/shared.py`，平台差异集中在 `cli_gateway/backends/`。

## 启动

先在本机安装并登录至少一种 CLI，再执行：

```powershell
pip install -r requirements.txt
.\run.bat
```

管理页默认监听 `0.0.0.0:8130`。启动后使用 `http://服务器IP:8130/` 完成设置；如需
限制为仅本机访问，可设置 `HEYSURE_CLI_CONTROL_HOST=127.0.0.1`。Linux 直接运行 `./run.sh` 进入服务管理
菜单，首次选择“安装 / 更新依赖”会创建项目独立的 `.venv` 并安装 `python-socketio`；
“启动服务”也会在发现依赖缺失时自动执行安装。

Linux 也支持非交互子命令：

```bash
./run.sh deps             # 安装 / 更新依赖
./run.sh start            # 后台启动
./run.sh stop             # 停止
./run.sh restart          # 重启
./run.sh status           # 运行状态与自启状态
./run.sh logs             # 最近 100 行日志
./run.sh logs-follow      # 持续查看日志
./run.sh autostart-on     # 安装 systemd 服务、开机自启并立即启动
./run.sh autostart-off    # 关闭自启并停止 systemd 服务
./run.sh foreground       # 前台调试
```

root 用户安装系统级 systemd 服务，普通用户安装 user service。OpenCloudOS、CentOS、
RHEL、Debian、Ubuntu 和 Alpine 的常见包管理器均可自动处理 Python/pip 依赖。
HeySure 配置存放在本机 `control_runtime/config.json`（已忽略提交，权限尽量收紧为
仅当前用户可读）；Grok/Antigravity 的登录资料仍由各 CLI 保存在本机，不上传
HeySure 服务器。

## 管理页能力

页面分为四个互相独立的顶级栏目：“HeySure 接入”、“统一 API”、“CLI 平台”、“状态与测试”。栏目
切换不会清空尚未保存的表单内容，平台切换也不会覆盖其它 CLI 的独立配置。
HeySure 设置使用“保存并登录 HeySure”同步验证服务器地址和账号密码；状态页会显示 Socket
连接阶段及最近一次连接错误。服务器地址可填写完整 URL，也可填写 `域名或IP:端口` 自动补全 HTTP。
三个设置栏目分别局部保存：HeySure 栏目只提交账号与设备信息，统一 API 栏目只提交
监听、路由与鉴权信息，CLI 栏目只提交当前选中的平台。保存或执行某个平台操作时不会
连带覆盖其他栏目尚未保存的草稿。

“统一 API”栏目集中设置访问范围、监听地址、端口、默认路由平台、公网展示地址和唯一
API Key。可一键生成高强度随机 Key并复制。服务器模式监听 `0.0.0.0`，页面会优先使用
当前管理页的访问域名/IP 生成 Base URL；监听所有网卡不等于自动获得公网 IP，云安全组、
系统防火墙以及可能存在的 NAT 端口映射仍需在服务器环境中配置。

两个平台各自保存独立配置，切换平台不会覆盖其它平台：

- 通用：CLI 路径、默认模型、模型目录、工作目录、会话目录、调用超时、代理；
- Grok：ACP 开关、工具收集窗口、会话 TTL/上限、可选 xAI API Key；
- Antigravity：官方 `agy` 或旧 `direct` 后端、参数安全字节数、OAuth 与 API 地址；
- 运维：探测 CLI 版本、重启内部网关、读取启动日志、刷新模型目录；
- 测试：选择模型并从网页直接发起一次真实聊天调用。

“平台基础设置”支持一键自动识别。它会定位已安装的 CLI 可执行文件并读取版本；优先从
正在运行的后端获取真实模型目录；
无法由 CLI 报告的内容使用平台推荐值。工作目录和各平台独立会话目录也会生成建议值，
所有结果先在弹窗中预览，只有点击“应用识别结果”并保存后才生效。

每个平台只提供子平台维护入口：安装系统依赖、安装/更新 CLI、登录与登录检查以及
应用/清除/测试代理。启动、停止、重启、整体状态、聚合日志和开机自启全部归入“统一
API”栏目，由主网关一次性控制所有已勾选启用的 CLI 子平台；不再提供前台调试按钮。
安装与登录作为交互作业运行，也可以从网页向
作业 stdin 发送授权码、菜单选项或确认内容。代理不再占用常驻设置卡片，点击“设置代理”
后在弹窗中保存、测试或清除；安装、登录和各项运维操作同样使用带明确进度与结果反馈的
弹窗。登录输出中的 HTTP(S) 授权地址会成为可点击按钮，并尝试直接打开新的浏览器页面。

对外只提供一个 OpenAI 兼容地址，默认是 `http://127.0.0.1:8140/v1`。模型目录由所有已启用
平台合并，聊天请求先按已配置模型名匹配，再按 `grok-*`、`gemini-*`、`gpt-*` 等前缀
路由；调用方也可传 `cli_platform`（`grok` / `antigravity`）强制选择。统一 API
支持普通 JSON 和 SSE 流式响应。“对外开放”会把统一地址切换为 `0.0.0.0`，此时强制要求
网关 API Key。各平台子网关始终使用随机回环端口，避免端口冲突。开机自启
管理的是统一 Adapter（Windows 计划任务 / Linux systemd user service），不会额外启动
一套旧网关。

密码、API Key、OAuth Secret 和可能含认证信息的代理 URL 不会回显到浏览器；留空保存
表示保留原值。内部两个 OpenAI 网关只绑定随机的本机回环端口，不对局域网暴露。

Adapter 首次上线后：

1. 在 Adapter 管理页选择平台、填写 HeySure 连接并启用；
2. 在 HeySure 网页“作坊”中找到 `本机 CLI Adapter`，给它“分配 AI 成员”；
3. 在设备的“MCP 权限”中勾选 `cli.run`、`cli.models`、`cli.status`。

完成后，被绑定的 HeySure AI 会看到这三个工具，并可根据任务主动调用本机 CLI。
接入与回包完全遵循 [`../read.md`](../read.md) 的 `device:register`、`task:dispatch`
和 `task:result/task:error` 契约。

原来的两个 `*_cli_api/server.py` 只保留旧命令兼容能力，新部署无需也不应分别对外启动它们。
