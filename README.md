# WB Gateway

Python 多账号模型网关，支持国内 / 国际账号、WebUI、余额与签到、模型倍率、调用记录和分区 API Key。不依赖原版桌面客户端或 Node.js 后端。

非官方项目，仅用于你有权使用的账号与接口。

## 运行

已在 Windows + Python 3.12 验证：

```powershell
py -3.12 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe app.py
```

默认端口为 `8787`。在图形窗口打开控制台，复制管理密钥后登录。也可使用 `启动网关.pyw`。

无图形模式：

```powershell
./.venv/Scripts/python.exe app.py --headless --host 127.0.0.1 --port 8787
```

headless 模式的管理密钥可通过 `WB_ADMIN_KEY` 环境变量指定，未指定则自动生成并保存于私有 `data/keys.json`。其他系统需使用 headless 模式，本次未做其他系统实机验证。

## 接入

- 控制台：`http://127.0.0.1:8787/ui/`
- OpenAI 风格地址：`http://127.0.0.1:8787/v1`
- Messages 风格地址：`http://127.0.0.1:8787`

在控制台添加账号、完成官方网页授权，再创建调用 Key。客户端使用调用 Key，不使用管理密钥。

通过 `GET /v1/models` 获取实际模型，保留 `cn/` 或 `gl/` 前缀。思考强度、输出上限和工具由客户端设置，并遵循模型实际能力。支持 Chat Completions、无状态 Responses、Messages 和 SSE。

## 注意

- 默认仅监听本机；远程部署需自行配置 HTTPS 与访问控制。
- 工具由客户端执行；不支持的能力及损坏参数会明确拒绝，不保证所有 Agent 的复杂工作流。

许可证尚未指定，发布前请自行选择并补充 `LICENSE`。
