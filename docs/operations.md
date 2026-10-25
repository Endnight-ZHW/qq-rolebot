# 部署、备份与回退

## 容器部署

1. 安装 Docker Compose，构建前端：`npm --prefix frontend ci`、`npm --prefix frontend run build`。
2. 复制 `.env.example` 为 `.env`，填写 `PUBLIC_HOST`、`PUBLIC_BASE_URL`、`NAPCAT_NETWORK`。两项公开地址须一致；额外浏览器来源只能显式配置到 `ADMIN_ADDITIONAL_ORIGINS`。
3. `data` 挂载目录由 UID/GID 1001 写入，Linux 上可用 `install -d -m 700 -o 1001 -g 1001 data` 创建。保持 Caddy 的 `caddy-data` 与 `caddy-config` 持久化。
4. 确认 NapCat 使用已存在的 Docker 网络；将该网络名称写入配置。启用反向 WebSocket，目标为内部 app 的 `/onebot/v11/ws`，使用同一个 access token。
5. 检查 `docker compose config`，再启动 `docker compose up -d --build`。网关默认绑定回环地址，直接公网部署需自行设置 `PANEL_BIND` 并允许所需端口。

`.env` 由 Compose 读取；普通 Python 运维脚本使用进程环境变量。不要把本地凭据写入代码、提交记录或命令行参数。

## 制作服务

作品制作需要独立 Linux 调度器和 Docker 渲染镜像。示例统一使用 `qq-rolebot-render` 服务、`/run/qq-rolebot-render/broker.sock`，应用的 `CREATION_RENDER_SOCKET` 必须指向同一个套接字。按 [制作服务安装](creations.md#制作服务安装) 安装；代码在临时非 root 容器运行，不挂载数据库、密钥或 Docker socket。

## 远程升级

`ops/remote.py` 读取 `QQROLEBOT_SSH_HOST`、`QQROLEBOT_SSH_PORT`（默认 22）、`QQROLEBOT_SSH_USER`、`QQROLEBOT_REMOTE_ROOT`（默认 `/opt/qq-rolebot`）。支持 SSH 密钥或交互密码；提前将核实过的服务器指纹加入本机 known_hosts。未配置目标时拒绝连接。

`python ops/release.py stage` 准备备份与隔离候选；`deploy` 发布已测试的镜像；`rollback` 回退代码。应用升级不得覆盖挂载的 `compose.yaml` 和网关配置；入口变更应独立审阅。渲染协议变化时应用、渲染镜像和调度器必须配套升级。

`ops/creation_release.py` 的激活操作会显式配置指定群的制作权限；普通升级不应重新运行激活。使用前阅读其 `--help`，只传入自己的目标和恢复点。

## 备份

保留应用和渲染镜像、代码、部署配置，以及数据库一致性快照；密钥和权限快照仅保存在受控的私有位置。发布状态位于被忽略的 `artifacts/release.json`。回退代码不恢复旧聊天数据库；权限恢复使用独立快照，避免覆盖无关权限更新。

应用、渲染服务、NapCat 状态均需验收。公网验证使用明确的 URL 与环境中的管理员密码，不忽略证书错误，不向群里发送测试消息。
