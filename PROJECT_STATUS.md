# 项目状态

更新时间：2026-11-19（America/Los_Angeles）

## 当前目标

当前 Switcher 与 Infinite Canvas 交接说明已整理为无凭据的源码 ZIP，程序、说明和补丁已同步到 Oescat/codex-relay-switcher 的 main。交接文件为 INFINITE_CANVAS_ADAPTATION.md，标题“适配 Infinite Canvas 的说明”。Canvas 程序本体、用户画布、依赖和授权不随包分发。

## 已完成功能

- Windows Tkinter 语言模型/生图两页，主模型、子代理模型与强度分别设置，默认优先 high/max，按已知模型能力限制档位。
- 全局语言模型切换、模型目录、Auto Review 模型和配置备份恢复；写入 danger-full-access、on-request、auto_review、web_search=live，清理被忽略的 conversationDetailMode。桌面端实际权限预设可能覆盖文件配置。
- 已知能力读取优先外部目录、官方缓存、Codex 缓存、内置目录；保留来源记录。生成目录补齐旧 CLI 必需的 base_instructions，保留新版消息模板。
- 独立图片站点、DPAPI 存储、即时启停、当前图片模型/参数，文字生图与参考图编辑 MCP，每次一张且无自动重试；默认图片路径按程序设置。
- 本地 Canvas 图片连接桥接：同步已应用站点的地址、密钥、显示名；Canvas 仍独立选择模型和参数，MCP 开关不控制 Canvas 直接请求。
- Canvas 的分页历史读取、每画布独立对话及工具归属、未发送空线程恢复、桌面 CLI 选择等源码修改保存在 integrations/infinite-canvas.patch。补丁基线和迁移方法见交接 MD。
- Canva 重新授权、GitHub PAT 环境变量引用和 Agent 重启此前已完成；当时三个状态均 ready，未验证仓库写入或 Canva 上传。当前包不包含凭据或私有绑定。
- README 已纠正“GitHub OAuth 登录成功即服务可用”的旧提示，明确该现场最终使用 PAT。

## 当前验证

- 本轮运行 python -m unittest：136 项通过；git diff --check 通过。
- 25 个公开文件按显式白名单打包，另含 PACKAGE_MANIFEST.json 的逐文件 SHA-256；ZIP CRC 和逐字节内容校验通过。
- 对候选文本检查 GitHub/API/JWT 令牌格式、私钥标记及原用户路径，未发现命中；未读取真实凭据作为扫描样本。仅使用既有演示截图。
- 未使用 Computer Use，未启动真实模型、生图、Canvas 写入或复杂体验测试；没有重新声明历史测试是今天执行。
- 历史验证：Canvas 分页 10 项；配置/HTTP 13 项、Session 31 项、附件切换 5 项合成检查通过；实际历史、模型菜单和分画布聊天曾恢复。完整源码构建与更多版本仍待验证。

## 已知问题和范围

- Canvas 过期同步响应仍可能被生成调用使用；SSE 再次失败后停止重连；同步失败清空连接后上层校验可能妨碍恢复；自动同步可能覆盖未保存渠道编辑草稿。这些均未修复，详见交接 MD。
- 语言模型目录不会自动热更新；切换后需重启空闲 Canvas Codex 后台。runtime 窄补丁可能被重装覆盖，源码应用不等于运行包更新。
- 目录验证仍有缺 description / 模板类型错误边界；外部只改模型目录路径可能使旧备份 expected 哈希不匹配。保留防误覆盖检查。
- 图像编辑/画质/尺寸实际能力由站点决定；未知模型不保证兼容。暂不加入路由、故障转移、聚合、额度查询或视频适配。
- 新电脑重新安装依赖、填写密钥、配对和授权，不能复制原电脑 DPAPI。已经运行的旧父进程不会自动继承新用户环境变量。

## 交付与上传

- 本地目标：codex-relay-switcher-for-new-pc.zip，完整 Switcher 源码和适配说明，不是包含 Python 或 Canvas 的一键安装器。
- 上传前基线 main 为 7c768b28e3c7bd650828defb1b55eb42579f5494。程序及交接说明已通过 GitHub 插件上传为 ad760d250ff327d998cd736ef6188ffbcf01437e；已核对其 Git tree 与本地提交一致。普通 Git push 遇到连接重置，因此使用插件上传，没有强推或覆盖远端新增提交。本次日期与状态记录随后同步。
- ZIP 下载附件发布目标为 GitHub Release portable-2026-11-19；文件上传和最终远端核验结果以本次交付回复为准，发布中断时不得只凭本说明认定附件已存在。
- 新 ZIP 已替换同名旧包；最终来源提交、SHA-256 和上传结果以 ZIP 清单及本次交付回复为准。

## 关键决定与下一步

- 配置与凭据留在用户配置目录，项目目录仅放程序和公开说明。上传不包含 config.toml、auth.json、DPAPI、备份、日志、.verification、依赖缓存和旧 ZIP。
- 分享 Canvas 补丁及交接 MD，不收集或迁移私人会话、画布和配对令牌。
- 源码和说明已同步；继续完成同版 ZIP 附件发布及远端校验。复杂使用体验仍由用户确认。
