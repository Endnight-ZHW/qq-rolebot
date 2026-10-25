# 面板入口与登录

面板默认只需要管理员密码登录，固定用户名为 `admin`。普通会话 12 小时，可选记住登录 30 天；退出撤销当前会话，改密码撤销全部会话。

生产 Cookie 保持 Secure、HttpOnly、SameSite=Strict，并启用 CSRF、限频和显式来源校验。网关全站不缓存；`/onebot/*`、`/health/*`、`/data/*`、`/internal/*` 和 `/metrics*` 不公开。

`PUBLIC_HOST` 是不含协议的域名，`PUBLIC_BASE_URL` 是浏览器访问的完整 HTTPS 来源。Caddy 示例自动申请公共证书，需要域名正确指向入口且 ACME 验证可达；直接部署通常开放 80/443 并设置 `PANEL_BIND=0.0.0.0`。若通过自己的反向代理，请按实际网络配置端口、TLS 和受信来源，保持 HTTPS 与无缓存认证响应。

仓库不附带域名、路由器规则、云平台配置、项目 CA 或证书。额外入口须明确填入 `ADMIN_ADDITIONAL_ORIGINS`，不要从请求转发头自动扩展受信来源。只有回环地址本地开发才使用 `COOKIE_SECURE=false`。

公网只读验证：设置 `PUBLIC_BASE_URL`、`ADMIN_PASSWORD`，运行 `python ops/verify_public.py`。前端只读验证另外设置 `QQROLEBOT_TEST_SCOPE` 后执行 `npm --prefix frontend run test:public`。
