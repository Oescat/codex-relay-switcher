# 适配 Infinite Canvas 的说明

本文件供下一位 Codex 接手当前适配。只记录可公开的结构、问题、修复和操作边界，不包含密钥、PAT、OAuth 令牌、配对链接、私人配置、聊天内容或线程 ID。

## 安装包和版本

ZIP 包含完整 Switcher 源码、内置模型目录、测试、启动入口、使用说明、本文件、Canvas 源码补丁和可选重新授权脚本。它是源码便携包，需要 Windows、Python 3.11+ 和 Tkinter，不是自带 Python 的安装器。

**不包含 Infinite Canvas 程序本体**、Node/npm 依赖、已安装 runtime、浏览器数据、画布素材、Codex 会话或用户授权。新电脑需要独立安装 Canvas，重新填写站点密钥并授权；DPAPI 文件与 Windows 用户/电脑关联，不应复制进包或假定跨电脑可解密。

整理日期：2026-11-20。运行验证来自先前记录，不能当作今天重新测试了所有功能。

| 项目 | 基线 |
| --- | --- |
| Canvas 源码 | basketikun/infinite-canvas，v0.20.0 |
| 补丁基础提交 | `9cd3deacbcc598361807afc16c059a80dd18b6f9` |
| Canvas Agent | @basketikun/canvas-agent 0.6.0 |
| 原依赖 Codex CLI | 0.146.0 |
| 解决认证时的桌面 CLI | 0.167.0-alpha.4.1；后续更新可能改变 |

路径使用占位符：`<SWITCHER_ROOT>` 是本包解压目录；`<CANVAS_ROOT>` 是 Canvas 源码根目录；`<CODEX_HOME>` 是实际 Codex 配置目录；`<NODE_EXE>`、`<PYTHON_EXE>`、`<CODEX_EXE>` 是目标电脑可执行文件；`<CANVAS_AGENT_ENTRY>` 是实际 Agent 的 dist/index.js。新装软件默认放到 `D:\AI-Tools\<软件名>`。不要复制原电脑用户名或桌面 CLI 的哈希路径。

## 两个程序如何配合

| 配置 | 归属和生效方式 |
| --- | --- |
| 语言模型站点、模型、子代理和强度 | Switcher 写入 Codex 全局配置，Canvas 独立 Codex 子进程启动时读取；没有自动热更新 |
| 图片站点地址、密钥和显示名 | 在 Switcher 生图页应用后，同步到本地 Canvas 固定的“Switcher 生图”渠道 |
| 图片模型、画质、尺寸、张数、脚本 | Canvas 自己选择保存，不从 Switcher 同步 |
| Switcher 生图 MCP 开关 | 只控制 relay_switcher_image，不关闭 Canvas 直接请求 |
| 图片文件 | Switcher MCP 使用其图片路径；Canvas 继续自己的存储/下载方式，不自动共用图片目录 |
| 画布、素材和配对 | Canvas 浏览器业务数据与 Agent 配对独立保存，本包不迁移 |

同步链路：`Switcher 已应用图片连接 → relay_canvas.py → Vite 同源接口 → Canvas 专用渠道 → 图片服务`。

`relay_canvas.py` 只返回已应用站点的名称、URL 和密钥，不返回模型或语言模型配置；没有图片配置时返回空，不创建配置。**不要在终端直接打印 `--connection-json` 输出。**

Vite 的 `GET /__relay_switcher/image-connection` 限制本机回环、同源和专用请求头，响应禁止缓存。配置变化通知不含凭据。生成、编辑、获取模型前再次读取，失败时阻止当前请求。Canvas 仍会将渠道保存到浏览器本地，因此图片密钥有一份独立于 Switcher DPAPI 的浏览器副本。

该桥接仅适用于本地 Vite 开发服务，不作用于在线 Canvas 或普通静态部署。

## 遇到的问题和解决方法

### 模型目录导致初始化失败

Canvas 原 CLI 0.146.0 要求 `base_instructions`，新版目录可能只有 `model_messages.instructions_template`。Switcher 的 relay_catalog.py 保留新版结构，从模板补齐缺失或非字符串的旧字段，已有合法旧值不覆盖，不改模型能力。重新在 Switcher 应用语言模型配置后才生成新目录。

此前独立合成配置初始化和实际侧栏恢复通过；这不保证兼容所有 CLI 或畸形目录。

### 侧栏模型列表未更新

长期运行的 Canvas Codex 子进程持有旧模型目录。只改全局配置、刷新网页不一定更新后台。确认空闲后重启 Canvas Codex 子进程或 Agent，再连接网页；此前实际菜单恢复。没有硬编码前端模型，也没有实现语言模型热更新。

### 旧历史报分页接口错误

报错：`paginated threads do not support thread/read(includeTurns=true)`。

codex-client.ts 只在原接口明确报此错误时，转为读取线程元数据、升序读取全部 thread/turns/list 页面，再逐轮升序读取全部 thread/items/list 页面。保留轮次状态、错误、顺序和消息归属，不替换线程。失败、重复游标或归属异常时拒绝返回半份历史。

此前源码/运行方法共 10 项离线检查通过，实际旧消息和附件恢复。

### 不同画布共用聊天

原逻辑只保存全站活跃线程。现按稳定 projectId 保存当前线程及本画布历史：切换自动恢复，首次创建空聊天，“新对话”只影响当前画布，旧聊天仍在其历史中；离开画布恢复站点作用域的聊天。旧共享聊天曾按用户明确选择绑定到一个已有画布，本包不包含私有绑定或迁移脚本。

后台仍同时只运行一个任务。工具固定到发起画布；附件下载等待后再次核对画布，防止切换期间写错。已有消息的线程恢复失败保留原绑定，**只有明确未发送首条消息的空线程缺少 rollout 时可重建**，发送前清除草稿标记。

此前合成检查通过：13 项配置/HTTP、31 项 Session、5 项附件切换；实机切换分别显示旧消息和空聊天。多窗口长任务、连续发消息后切换等仍待用户体验。

### 重启电脑后网页打不开或 Agent 未连接

原快捷方式只启动 17371 的 Agent，没有启动 3000 的网页。随后本机脚本补为：复用已有 Node/Agent/Vite，后台启动缺失服务，等待网页就绪后打开浏览器，重复启动不重复占端口。

原电脑快捷方式和启动脚本未纳入本包，新电脑按下文重建。另一次现场是网页正常但 Agent 进程已退出；没有证据证明关闭 VPN 是直接原因，未关闭安全校验或改系统代理。重新启动服务、手动连接恢复；自动保活和重连仍有缺陷。

### Canva invalid_client

报错 OAuth refresh `invalid_client: Client not found`，表示服务端拒绝原 OAuth 客户端注册。模型中转密钥和延长超时不能解决。

此前使用桌面 CLI 注销 Canva MCP，再通过 DCR 重新授权，实际服务恢复 ready。不要直接删除 OAuth 存储。包内 `integrations/repair-canvas-mcp-auth.ps1 -Server canva` 是可选辅助入口，执行前确认；会影响同一 Codex 用户下该 MCP 的登录，需用户授权。目标电脑需先装插件，CLI 参数以安装版本为准。

### GitHub 登录成功仍要求 PAT

Canvas 原固定使用旧 CLI。现启动优先级：CANVAS_CODEX_PATH 显式绝对路径 → Windows PATH 中桌面 Codex CLI → 项目 @openai/codex 入口。处理 Windows Path/PATH 和路径分隔符；原生 EXE 直接启动，JS 由 Node 启动。

**换新版 CLI 并未单独解决 GitHub。** 现场 OAuth login 虽退出 0，实际新版 MCP 启动仍要求 PAT。最终由用户本机设置令牌后，在 Codex 配置添加非敏感引用：

```toml
[mcp_servers.github]
url = "https://api.githubcopilot.com/mcp/"
bearer_token_env_var = "CODEX_GITHUB_PERSONAL_ACCESS_TOKEN"
```

令牌只由用户在本机保存，不能写入 TOML、源码、命令参数、MD 或聊天。权限和有效期由用户决定，到期需更新。包内脚本 `-Server github` 只发起 OAuth，不代表解决 PAT 分支。

已经启动的终端、Codex、Explorer 不会自动获得新变量；需重新登录 Windows，或由本机受控启动过程将用户变量仅传给 Agent。此前最终 Agent/GitHub/Canva 均 ready，只确认初始化，没有做仓库写入或 Canva 上传。

### runtime 窄部署引用不存在的函数

源码和已安装 npm 包不完全相同。曾整段替换源码 start 方法，引入运行包没有的 createAgentLogWriter，导致启动失败。

最终保留原运行包的日志/事件实现，只改 CLI 选择和 spawn 参数；分页也只替换目标方法。**源码补丁不等于运行包已更新**，不能无检查地整文件覆盖；重新安装依赖可能覆盖这些窄补丁。

## 下一位 Codex 的接手步骤

1. 先读 Switcher 的 AGENTS.md、PROJECT_STATUS.md 和本文，核对两个目录 Git 状态，保留已有改动。
2. 按 README 运行 Switcher，重新填写站点密钥。现有 BAT 固定查找 Python 3.13 常见用户安装路径；其他位置直接用 `python relay_switcher.pyw`，不搬迁已有 Python。
3. 独立安装 Canvas 源码、Node 和依赖；包中没有程序本体和运行依赖。使用兼容基线或隔离检出，先检查补丁：

   ```powershell
   git -C "<CANVAS_ROOT>" rev-parse HEAD
   git -C "<CANVAS_ROOT>" apply --check "<SWITCHER_ROOT>\integrations\infinite-canvas.patch"
   ```

   确认不覆盖用户改动后执行：

   ```powershell
   git -C "<CANVAS_ROOT>" apply "<SWITCHER_ROOT>\integrations\infinite-canvas.patch"
   ```

   已应用补丁可用 reverse/check 判断；上游不同先人工解决冲突，不强制覆盖。
4. 显式设置非密钥桥接变量，避免补丁中历史默认路径不匹配：

   ```powershell
   $env:RELAY_SWITCHER_ROOT = "<SWITCHER_ROOT>"
   $env:RELAY_SWITCHER_PYTHON = "<PYTHON_EXE>"
   $env:RELAY_SWITCHER_HOME = "<CODEX_HOME>"
   ```

5. 区分源码与 runtime，沿用 Canvas 的启动/构建说明和当地 AGENTS 约束。实际进程必须加载适配代码；本包不自动构建、部署或修补 node_modules。可用 CANVAS_CODEX_PATH 指定目标电脑桌面 CLI，不复制旧哈希目录。
6. Agent 使用本机 127.0.0.1:17371，网页只绑定 127.0.0.1:3000。可通过现有 Node 和 CANVAS_AGENT_ENTRY 启动 Agent，从 Canvas web 目录启动 Vite，并显式传入 `--host 127.0.0.1 --port 3000 --strictPort`。不要使用 Vite 默认的全网卡监听公开桥接入口。后台独立启动，无窗口不等于已设置保活。
7. 使用 Agent 原生配对流程，令牌只在本机内部使用；不要打印 /config 或配对 URL。在线版、本地版、浏览器和端口的存储相互独立。
8. Switcher 应用图片站点后，在 Canvas 专用渠道手动添加和选择图片模型；切换语言模型后重启空闲后台。
9. 新电脑重新授权 Canva、配置 GitHub PAT。覆盖配置前按用户授权本机加密备份，仅改必要字段；不复制原电脑凭据。
10. 用户要求自己体验复杂功能，不使用 Computer Use，不自动做收费模型、生图、仓库写入或媒体上传测试；只做与改动相称的离线和启动确认。

## 仍未解决的问题

| 问题 | 影响及后续方向 |
| --- | --- |
| P1 过期同步结果 | 请求序号只防旧响应覆盖 store，生成仍可能使用旧返回值；应拒绝过期结果或等待最新结果 |
| P2 SSE 重连 | 再次失败后关闭 EventSource，自动连接标记未复位，服务恢复未必自动重连 |
| P2 同步失败后恢复 | 清空连接后，上层完整性检查可能先拦截，无法进入请求前刷新；可能需刷新/focus/重连 |
| P2 未保存编辑草稿 | 同步替换渠道对象可能覆盖尚未保存的模型或脚本草稿 |
| 模型目录格式边界 | 缺档位 description 或模板类型错误的合成目录被接受，但旧 CLI 初始化失败 |
| 配置恢复哈希 | 外部只改目录路径后，旧备份 expected 哈希可能不符；保留防误覆盖校验 |
| Agent 保活 | 没有已验证的自动守护和开机自启；此前进程退出原因证据不足 |
| 语言模型热更新 | 后台需重启；图片同步不代表语言配置热更新 |
| 源码与 runtime | 重装覆盖窄补丁，完整源码构建后的行为仍需目标版本验证 |

按用户既定范围，不扩展多协议、路由、故障转移、额度查询或视频。未知模型不推断中转站能力；图片 edits/mask/扩展尺寸画质需按实际接口验证。

## 文件索引和验证边界

- Switcher 主入口 relay_switcher.pyw；核心 relay_core.py；目录 relay_catalog.py；图片 relay_images.py / relay_image_mcp.py；桥接 relay_canvas.py。
- Canvas 图片同步：web/vite.config.ts、services/api/switcher.ts、渠道 store、图片请求和根初始化。
- Canvas 对话：canvas-agent/src/config.ts、canvas/session.ts、server/http.ts、agent/codex.ts、前端 local-agent-panel.tsx。
- 分页和 CLI 选择：canvas-agent/src/agent/codex-client.ts 及协议/测试。
- Canvas 修改保存在 integrations/infinite-canvas.patch；不包含 node_modules、已安装插件机器路径、配对文件或日志。

此前记录：Switcher 136 项离线测试通过；分页 10 项、配置/HTTP 13 项、Session 31 项、附件切换 5 项合成检查通过；实机历史、画布独立聊天和模型菜单曾恢复，最后 MCP/Agent ready。没有用这些结果宣称已验证真实模型调用、生图、仓库写入、其他 Codex 版本或所有同步竞争问题。

本次打包扫描、ZIP 校验、提交和上传结果见 PROJECT_STATUS.md 及交付回复。公开仓库不保存 .verification、生产配置、备份或任何用户数据。
