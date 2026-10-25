# 模型配置与参考项目

后端仍使用 Python / FastAPI / SQLite。参考 RuoYi-AI 的供应商与模型分层设计，以及 DeepSeek Harness 的模型目录、只写凭据和推理等级，独立实现本项目需要的轻量配置与适配层。

## 后台配置

在「模型管理」统一维护供应商、模型和聊天 / 决策 / 记忆三类任务。供应商 ID 稳定，模型另有内部 ID 与真实接口模型 ID；同一接口模型可以归属于不同供应商。

- Chat Completions：官方 API 地址 `https://api.deepseek.com` 或 `https://api.deepseek.com/v1`。
- 自建网关支持 HTTPS；内网 HTTP 地址须直接使用私有 IPv4（10/8、172.16/12、192.168/16），具体地址由部署者配置。密钥仍通过只写字段保存。
- DeepSeek Messages（Harness）：官方 API 地址 `https://api.deepseek.com/anthropic`，请求自动拼接 `/v1/messages`；显式 `/v1` 不会重复拼接。
- 思考等级：`off / low / high / max`。Messages 使用 `thinking` 与 `output_config.effort`；Chat Completions 使用 `thinking` 与 `reasoning_effort`。
- 内置目录：`deepseek-flash`（显示名 DeepSeek-V4.1-Flash，支持文字与图片）、`deepseek-v4-pro`（文字）。目录容量来自所参考的 Harness 版本，并非账号可用性的保证；可手工修正或使用「读取接口模型目录」发现模型。
- 输入上下文预算保留在基础设置中，并受当前模型容量限制；每种任务分别配置输出上限，不会将 Harness 的最大容量直接当成机器人每次请求的输出量。思考也占用输出额度。
- 搜索 / 网页读取使用单独选择的供应商，要求支持 DeepSeek 原生 Search/Fetch；不会使用其他供应商的密钥尝试请求。
- 测试使用已保存的配置，会产生模型费用；估算仍使用「设置」中明确展示的统一费率，没有费用停机限制。接口模型目录不返回能力时，需要人工核对图片与容量配置。

密钥留空表示保留；勾选「保存时清除密钥」才会删除。密钥仅在服务器 `data/secrets.json` 中保存（权限 0600），模型目录、API 返回、校验错误和调用记录不包含密钥内容。停用模型或供应商后，后续请求被拒绝。

## 升级与协议行为

供应商、模型、输出预算和思考等级统一由模型目录管理。基础设置保留输入预算和费用费率，不再通过旧模型字段覆盖目录。升级保留现有目录、路由和密钥；新安装创建默认目录。

每次请求读取配置快照。工具链中如果协议、模型或思考方式发生变化，旧链立即停止，下轮使用新配置。Messages 思考块及签名按原样回传，正文与内部思考分离；并行工具结果合并成一条用户消息。两种协议均有模拟测试。Messages 的 JSON 输出使用系统指令约束，并由应用解析校验，不宣称接口强制 JSON schema。

模型声明不支持图片时，图片替换为“内容未知、无法识别”的明确说明，并记录能力降级事件。模型容量检测使用保守字符估算，图片采用固定 token 预留；不等同于供应商精确 tokenizer。

只实现本项目需要的 Chat Completions 和 DeepSeek Messages。未引入 RuoYi 的 Java 服务、账户体系或其其他业务模块，也未将 Harness 作为服务进程运行。自定义网关须兼容对应协议和 DeepSeek 思考参数。

## 参考来源

- [ruoyi-element-ai](https://github.com/element-plus-x/ruoyi-element-ai)，参考提交 `81aea3bc5ce672ce001c312eb50266426fa363f2`。该项目指向 RuoYi-AI 后端。
- [RuoYi-AI](https://github.com/ageerle/ruoyi-ai)，参考提交 `8ab36380129db655d7c960713839e151b5747ce7`：ChatProvider / ChatModel 分层、只写 API Key、DeepseekServiceImpl。
- [DeepSeek Harness 供应商配置](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/providers.zh.md)，参考提交 `687f282e8625c6b8d11ff58211a90524be2e4317`：配置字段、模型目录与凭据管理。
- [Harness DeepSeek 适配器](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/llm/llm-deepseek)：models.ts、config.ts、serialize.ts、messages-api.ts、translate.ts。

以上是架构和协议参考，本项目代码独立实现；线上行为还需结合实际供应商版本与账号能力验证。

协议与工具链由隔离模拟测试持续验证。实际账号的连通性可在「模型管理」检查；测试会产生供应商费用。
