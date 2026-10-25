# QQ Rolebot

通过 NapCat / OneBot 11 接入 QQ 的可配置群聊机器人。后端使用 Python 3.11、FastAPI 和 SQLite，后台使用 Vue 3、TypeScript 和 Vite。

提供群聊与图片理解、角色配置、记忆管理、工具权限、定时任务、表情管理，以及隔离环境中的图表、GIF、短视频和简单三维制作。

本仓库仅包含通用程序。首次启动创建空白角色与空素材库，不附带账号、人设、素材、凭据、聊天记录或模型调用记录。所有群聊和工具授权都由部署者配置。

## 本地开发

需要 Python 3.11、支持当前 Vite 的 Node.js 22.12+ 和 npm。

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r backend/requirements.txt -r requirements-dev.txt
npm --prefix frontend ci
```

后端：

```powershell
$env:PYTHONPATH='backend'
$env:DATA_DIR='data'
$env:PUBLIC_BASE_URL='http://127.0.0.1:5173'
$env:COOKIE_SECURE='false'
.venv/Scripts/python -m uvicorn rolebot.main:app --host 127.0.0.1 --port 18080
```

前端在另一终端运行 `npm --prefix frontend run dev`，访问 `http://127.0.0.1:5173`。首次密码在本地 `data/bootstrap-password.txt`，也可在首次启动前设置 `ADMIN_PASSWORD`。生产部署保持 Secure Cookie。

在后台配置模型和密钥、角色、管理员与 QQ 连接；空白角色的说明、提示词、别名和关系均可自行填写。OneBot 反向 WebSocket 使用内网地址 `ws://qq-rolebot:8080/onebot/v11/ws` 及本地 `data/secrets.json` 的 `onebot_token`，不要把该文件上传。

## 测试

```powershell
.venv/Scripts/python -m pytest -q
npm --prefix frontend run build
npm --prefix frontend exec playwright install chromium
```

隔离 UI 检查：分别启动 `PYTHONPATH=backend` 下的 `tests/ui_server.py`、Vite，再运行 `npm --prefix frontend run test:ui`。测试使用合成身份和临时几何素材，不连接 QQ、不调用付费模型。测试账号和密码只适用于隔离测试服务。

## 部署与配置

复制 `.env.example` 为 `.env`，填写自己的域名、访问来源和 NapCat 网络。参阅 [部署与回退](docs/operations.md)、[面板与登录](docs/panel-access.md) 和 [作品制作](docs/creations.md)。制作渲染器为可选的独立 Linux/Docker 服务，未安装时不影响普通聊天。

```powershell
npm --prefix frontend run build
docker compose config
docker compose up -d --build
```

启动前创建可由容器 UID/GID 1001 写入的 `data` 目录，并确保指定的外部 NapCat 网络已存在。公开部署还需按文档配置入口和 TLS。

## 代码目录

| 目录 | 内容 |
| --- | --- |
| backend/rolebot | 通用运行逻辑与提示词 |
| frontend/src | 管理后台 |
| rendering | 隔离渲染器与宿主调度器 |
| tests、frontend/tests | 合成数据回归与 UI 检查 |
| ops | 参数化部署、备份和验证工具 |
| docs | 功能、接口与运维说明 |

- [功能与使用](docs/features.md)
- [架构](docs/architecture.md)
- [工具权限](docs/tool-permissions.md)
- [模型配置](docs/models.md)
- [素材管理](docs/art-resources.md)

运行数据、上传素材、证书和密钥保存在 Git 之外。提交前运行 `python ops/check_public_tree.py`；该检查是额外保护，不代替对新增内容的审阅。
