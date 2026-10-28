# Codex Relay Switcher 项目指导

- 这是 Windows Tkinter 桌面程序，用于切换 Codex 语言模型中转站、模型、主模型与子代理设置，并独立管理生图 MCP。
- 主要入口：`relay_switcher.pyw`；核心配置逻辑：`relay_core.py`；模型目录：`relay_catalog.py`；生图模块：`relay_images.py`、`relay_image_mcp.py`。
- 开始或恢复任务时先读取 `PROJECT_STATUS.md`，再检查相关代码和 Git 状态。
- 不读取、打印或提交密钥、`auth.json`、`config.toml`、DPAPI 文件、验证输出或临时 ZIP。
- 修改后使用 `python -m unittest` 和 `git diff --check` 验证；GUI 改动还要检查 960x750 与 790x660 布局。
- 保留现有语言/生图两页、默认强度和 MCP 行为；不要顺手重构无关代码。
