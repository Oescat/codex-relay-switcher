# 项目状态

更新时间：2026-10-28

## 当前目标

刷新并使用 OpenAI 官方 Codex 模型能力目录，使新模型按官方元数据获得推理强度选项。

## 已完成

- 语言模型中转站切换、默认模型、主模型默认思考强度和 Auto Review 模型。
- 子代理推理强度已支持 `light`、`medium`、`high`、`xhigh`、`max`、`ultra`，默认 `max`，子代理模型已支持独立选择。
- 生图中转页、独立图片密钥、MCP 工具和 `D:\codex-\image` 保存路径。
- Tkinter 界面已完成浅色主题、较粗字体和两页布局。

## 本次已完成

- 子代理模型与子代理推理强度已独立成一行。
- 子代理模型写入 `[agents].default_subagent_model`；留空时移除该覆盖项。
- 语言模型、子代理模型、子代理强度和 Auto Review 均按站点保存。
- 完成后端与 GUI 回归测试。
- 获取语言模型列表时在后台刷新 OpenAI 官方 Codex 模型目录，并缓存于 `<CODEX_HOME>\relay-switcher\official_model_catalog.json`；该公开请求不携带中转站密钥。
- 应用站点时优先按模型 ID 使用官方目录能力，其次使用 Codex 缓存和项目内置目录；官方刷新失败时保留已有缓存。
- 已直接验证官方目录当前包含 `gpt-6.1-terra`，Codex 界面强度为 `low`、`medium`、`high`、`xhigh`、`max`、`ultra`。

## 待处理

- 暂无必须完成的开发事项；第三方中转站的模型调用兼容性仍需按实际站点测试。

## 提交与上传

- 功能代码已提交为 `f537ebe`（Sync official model capabilities and add subagent model settings），并推送至 GitHub `Oescat/codex-relay-switcher` 的 `main` 分支。
- 本状态文件作为随后的文档提交同步；密钥、生产配置、DPAPI、验证输出与 ZIP 均未纳入提交。

## 验证结果

- `python -m unittest`：79 项通过。
- `python -m py_compile relay_core.py relay_switcher.pyw relay_catalog.py relay_images.py relay_image_mcp.py`：通过。
- `git diff --check`：通过；Git 仅提示新增/编辑文件采用 LF，工作区规则可能在下次写入时转换为 CRLF。
- GUI 960x750、790x660：通过 Tkinter 主事件循环测量语言模型页和生图页的可见控件，两种尺寸均未超出窗口边界；尚未进行截图视觉审查。

## 关键约定

- 子代理模型对应 `[agents].default_subagent_model`；留空时删除该覆盖项，让 Codex 自行选择。
- 子代理强度对应 `[agents].default_subagent_reasoning_effort`；界面的 `light` 写入 Codex 的 `low`。
- 主模型设置与子代理设置互相独立。
- 配置和凭据只使用合成测试数据，不读取生产密钥。
- Codex 官方目录决定桌面端菜单能力；API 文档中的 reasoning 参数集合可能不同，第三方中转站能否执行请求仍取决于其接口实现。
- 单次官方目录刷新失败不会阻止站点模型列表获取。
