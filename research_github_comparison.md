# Codex 中转切换项目对比

调研日期：2026-10-13

本报告把当前项目（`relay_switcher.pyw`、`relay_core.py`、`relay_models.py`、`relay_catalog.py`）与 GitHub 上公开项目的 README/源码说明进行功能对比。GitHub 项目的宣传功能没有在本机安装或逐项运行，因此只把它们作为“已公开实现/设计”，不把 README 文字当成实际兼容性证明。

## 结论

你的项目不是功能太少，而是定位偏向“安全地编辑全局配置的 Windows GUI”。它在本机环境适配、中文 GUI、DPAPI 站点库、模型列表获取、TOML 无关设置保留、失败回滚方面比简单脚本更完整。

最需要补齐的是运行时闭环：现在主要验证“配置文件写成功”，没有验证“Codex 重新加载了配置、请求真的到达所选中转、工具调用和思考强度真的可用”。同类项目已经把会话索引、重启、诊断和实际请求验证作为核心功能。

## 对比项目

### 1. tuolaji996/codex-provider-switcher

来源：[GitHub README](https://github.com/tuolaji996/codex-provider-switcher)

该项目最值得借鉴的设计是始终保留同一个 provider ID（README 中称为 `model_provider = "OpenAI"`），只替换其后端设置。它明确指出 Codex 会把 provider ID 写入线程索引，改变 provider ID 可能造成历史会话分桶变化或“会话消失”。它还提供：

- Windows 原生 WPF 界面；
- Windows Credential Manager 保存 API Key，`config.toml` 只保存 token broker 命令；
- 每次写配置前创建时间戳备份；
- 选定地址和模型后进行真实 Responses API 兼容性测试；
- 插件工具的 function-call 往返测试、图像生成测试和官方主机诊断；
- provider 切换后的状态页、回滚和更新检查。

你的项目目前的差距：

- 当前程序将默认 provider 设置为 `relay_switcher`，并同步 `custom` 等旧入口；这解决了旧地址残留，但仍然比“稳定 provider ID、只换后端”的方案更容易触发会话索引问题。
- API Key 最终写入活动 `config.toml` 的 `experimental_bearer_token`；DPAPI 只保护站点资料和备份，不能保护当前活动配置。
- 只有 GET `/models` 成功检查，没有真实的 `/responses` 文本、工具调用或图像能力检查。
- 没有时间戳备份历史、诊断页和当前路由验证。

### 2. YizukiAme/codex-switcher

来源：[GitHub README](https://github.com/YizukiAme/codex-switcher)

这是一个 PowerShell 终端配置切换器，功能侧重会话和启动流程：

- 多 provider/profile 管理；
- 启动时异步探测每个中转站；
- 浏览、恢复和 fork 历史会话；
- 用当前 profile 恢复或 fork 会话；
- 最近工作目录选择；
- 在新的 Windows Terminal 标签页启动 Codex；
- `status`、`reload`、`re-probe` 等运维入口。

你的项目优点是站点编辑、密钥保护、模型获取和模型目录生成更完整，GUI 也更适合日常操作。缺口是没有“选择 profile 后启动/恢复会话”的闭环，因此用户需要自己完全退出并重新打开 Codex；也没有会话列表、工作目录和实际探测状态。

### 3. ryoreonhz-dev/codex-provider-switcher

来源：[GitHub README](https://github.com/ryoreonhz-dev/codex-provider-switcher)

该项目把切换看作一套可诊断的运维流程：

- OpenAI 与第三方 provider 的基线配置分离；
- 可选启动、停止和重启本地桥接服务；
- `status`、`doctor`、`logs` 命令；
- 切换后重启 Codex Desktop App；
- 通过日志检查 `actual_model`，而不是只看配置文件；
- 支持 provider registry 扩展模型。

你的项目的回滚和配置保留更细致，但缺少实际路由日志、健康检查、Codex 重启编排以及“切换后验证真实模型”的报告。这正是此前出现“当前对话仍走 Bajie、新对话走 36524”时最难判断的部分。

### 4. nvco/codex-cli-openrouter

来源：[GitHub README](https://github.com/nvco/codex-cli-openrouter)

该项目专门解决模型目录问题：从 Codex 二进制提取有效目录项作为模板，再从 OpenRouter 获取模型元数据，生成符合 Codex 隐式 schema 的目录；它还提供余额、用量和按模型统计。它使用独立 profile，尽量不改用户的主 `config.toml`。

你的 `relay_catalog.py` 的优点是完全不依赖 npm/pip，并且在本机已有目录、缓存和 bundled catalog 之间有回退路径。缺点是：中转站 `/models` 通常只有模型 ID，未知模型会进入 `_fallback()`，其中工具、reasoning、上下文和模态能力是保守猜测；随后又标记为 `supported_in_api = true`。这能让模型显示出来，但不等于它真的兼容 Codex 的 Responses、工具或图像请求。

## 你的项目的主要缺点

### 高优先级：活动 API Key 明文写入配置

`relay_core.py:278-284` 将 Key 写入 `experimental_bearer_token`。站点资料和备份使用 DPAPI，但活动 `config.toml` 仍可直接读取。这是当前最重要的安全差距。

建议：优先改成环境变量或系统凭据引用（前提是当前 Codex 版本的 provider auth 支持该方式）；如果必须写 token，至少在 GUI 中明确提示“活动配置含明文 Key”，并提供清理旧 token 的迁移动作。

### 高优先级：允许把 Key 发送到任意 HTTP 地址

`relay_core.py:318` 起的校验允许 `http` 和 `https`，`relay_models.py:81-84` 会向该地址发送 Bearer Key。非回环 HTTP 地址存在被网络观察者截获 Key 的风险。

建议：默认只允许 HTTPS；仅对明确的 `127.0.0.1`、`localhost` 或 `::1` 放行 HTTP，并在界面显示“本地明文 HTTP”。

### 高优先级：没有真正的切换后验证

现在验证重点是 TOML 写入和备份回滚。GET `/models` 只能证明列表端点可访问，不能证明 `/responses`、SSE、工具调用、图像或特定 reasoning effort 可用。

建议添加“连接诊断”：对所选模型执行最小文本请求，按需执行一次工具调用往返；把 endpoint、模型、HTTP 状态、请求耗时和响应类型记录为不含 Key 的本地诊断结果。

### 中优先级：provider 别名同步规则可能误伤

`relay_core.py:204-229` 会无条件纳入当前非内置 provider，并把名为 `custom` 且 wire API 缺省或为 `responses` 的 provider 当作旧中转入口；之后 `relay_core.py:253-284` 删除并重建这些表。这可能覆盖用户原来配置的 `auth`、`env_key` 或其他 provider 专属字段。

建议：给受程序管理的 provider 写入明确的管理标记，或保存 provider ID 与 endpoint 的来源记录；应用前展示“将修改哪些 provider”，对未确认的 provider 不自动覆盖。

### 中优先级：模型能力元数据仍然是猜测

`relay_catalog.py:75-104` 的 fallback 只知道模型 ID，却需要填充 Codex 目录的工具、上下文、reasoning 和模态字段；`relay_catalog.py:141-146` 还会统一设置 `supported_in_api = true`。这可能造成模型能显示但工具调用失败，或思考强度在中转端被忽略。

建议把状态分成“已发现模型 ID”和“已验证 Codex 兼容模型”，默认只把前者作为可选项；允许用户为中转站保存能力模板，并在诊断通过后再标记工具/图像/推理能力。

### 中优先级：会话和重启没有纳入产品流程

`relay_switcher.pyw:481-491` 写完配置后只提示用户重启。程序不检查 Codex 进程、不重启 Codex，也不提供会话恢复或新会话启动。这样配置文件已经变化，但正在运行的线程仍可能使用旧 provider。

建议至少提供：检测 Codex 是否仍在运行、显示“配置已写入但当前进程未重新加载”、可选的安全重启按钮、重启后读取有效配置并显示当前 provider。更进一步再做会话浏览和恢复。

### 中优先级：测试面偏窄

当前 `test_relay_core.py` 主要覆盖 `_new_config()` 的合成 TOML，共 4 项测试。缺少真实 Windows 临时目录下的 manager switch/restore、DPAPI 失败、并发修改、模型目录生成、HTTP 重定向/TLS、响应体限制和 GUI 工作流测试。

## 建议的实现顺序

1. **先修凭据和地址安全**：HTTPS/loopback 校验；支持 `env_key` 或 Windows Credential Manager 引用；迁移并清理活动配置中的明文 token。
2. **再修稳定路由**：使用固定的 managed provider ID；不要根据 `custom` 名称猜测；应用前显示变更摘要。
3. **增加诊断闭环**：最小 `/responses` 测试、SSE 解析、工具调用测试、模型/endpoint/实际状态记录。
4. **完善运行时流程**：检测并安全重启 Codex，重启后重新读取有效配置；提供“新建对话”入口，避免旧线程沿用旧 provider。
5. **完善模型目录**：区分发现和验证，增加能力模板和验证状态，不再把“能列出”解释为“完全兼容”。
6. **扩展测试**：把现有单元测试扩展为 Windows 集成测试，再覆盖 GUI 的切换、失败回滚和恢复冲突。
7. **最后做运维体验**：时间戳备份列表、状态/doctor/logs、会话浏览和可选工作目录启动。

## 参考的官方配置边界

OpenAI 官方文档说明用户级默认配置位于 `~/.codex/config.toml`，配置优先级还包括 CLI、项目配置和 profile；`model_provider` / `model_providers` 是 provider 路由配置。官方文档没有把“编辑全局 config.toml”定义为正在运行线程的热切换机制，因此切换器必须把“文件写成功”和“当前线程已换路由”分开报告。

来源：[Codex Config Basics](https://learn.chatgpt.com/docs/config-file/config-basic)、[Codex Config Reference](https://learn.chatgpt.com/docs/config-file/config-reference)。
