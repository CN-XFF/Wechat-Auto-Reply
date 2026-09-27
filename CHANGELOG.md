# 版本更新日志

此日志记录每次用户要求的软件修改，便于查明变更内容和回退。当前基准版本为 **1.0**。

## 版本规则

- 每次用户要求修改软件后，分配一个此前未使用的新版本号，并在本文件记录日期、变更摘要、涉及文件和验证结果。
- 当前约定采用补丁递增：`1.0` 之后依次为 `1.0.1`、`1.0.2`、`1.0.3`……；同一轮请求中的多个相关改动合并为一个版本。
- 修改前备份所有将要改动的现有源文件到 `backups/versions/<修改前版本>/`。新建或删除的文件也要在日志中注明，以便回退时恢复或移除。
- 回退时以目标版本之后的日志和对应快照为准，只恢复软件源文件；不得用版本快照覆盖本机配置、日志、聊天记录或其他用户数据。

## 1.0 — 2026-09-27

- 将当前程序版本确立为 1.0，并在主界面标题区域显示版本号。
- 窗口标题保持原样，避免影响现有启动与停止脚本。
- 自动回复、监听、联系人定位和发送逻辑未作修改。
- 快照：`backups/versions/1.0/app.py`。
- 验证：`app.py` 通过本机 Python 语法检查；未启动微信，也未进行真实发送测试。

## 1.0.1 — 2026-09-27

- “使用搜索查找测试”现在先确保微信主窗口位于前台，再通过 Ctrl+F 聚焦搜索框并粘贴联系人名；不再要求 OCR 先定位搜索框坐标。
- 仍需 OCR 确认搜索词进入搜索栏，并只点击联系人结果区里匹配度最高且唯一的联系人行；无法确认时停止，不点击、不发送。
- 普通自动回复和“聊天列表优先”测试流程保持原样。
- 涉及文件：`app.py`、`vendor/wechatauto-replica/wechatauto/guia.py`、`tests/test_wechat_window_diagnosis.py`。
- 修改前备份：`backups/versions/1.0/pre-1.0.1-search-hotkey/`；版本快照：`backups/versions/1.0.1/`。
- 验证：专项测试 78 项通过，语法检查通过；全量测试 195 项通过、1 项 `tests/test_profiles.py` 因本机配置与测试预期不一致而失败（本次未修改配置文件）；未启动微信、未发送真实测试消息。

## 1.0.2 — 2026-09-27

- “使用搜索查找测试”在 Ctrl+F 输入并 OCR 确认搜索词后，改为按 Enter 选择微信搜索首项，绕过搜索结果行的坐标点击。
- 按 Enter 后仍执行联系人会话标题校验；未确认目标会话时停止，不会继续填入或发送测试内容。
- 普通联系人定位和聊天列表优先流程保持原样。
- 涉及文件：`app.py`、`vendor/wechatauto-replica/wechatauto/guia.py`、`tests/test_wechat_window_diagnosis.py`。
- 修改前备份：`backups/versions/1.0.1/pre-1.0.2-search-enter/`；版本快照：`backups/versions/1.0.2/`。
- 验证：联系人搜索及固定测试专项 78 项通过，语法检查通过；全量测试 195 项通过、1 项 `tests/test_profiles.py` 因本机配置与测试预期不一致而失败（本次未修改配置文件）；未启动微信、未发送真实测试消息。

## 1.0.3 — 2026-09-27

- “使用搜索查找测试”在 Ctrl+F 粘贴联系人名后直接按 Enter 进入首项，不再要求 OCR 先识别搜索词或下拉候选。
- 进入后仍严格核对会话标题；标题无法确认或不匹配时停止，不继续填入或发送测试内容。若名称误粘入聊天草稿，仍会拦截 Enter。
- 普通联系人搜索流程保持原有 OCR 确认规则。
- 涉及文件：`app.py`、`vendor/wechatauto-replica/wechatauto/guia.py`、`tests/test_wechat_window_diagnosis.py`、`CHANGELOG.md`。
- 修改前备份：`backups/versions/1.0.2/pre-1.0.3-result-ocr-fallback/`；版本快照：`backups/versions/1.0.3/`。
- 验证：联系人搜索及固定测试专项 80 项通过，语法检查通过；未启动微信、未发送真实测试消息。

## 1.0.4 — 2026-09-27

- 将运行状态提示、关闭提醒和“停止自动回复”按钮移出可滚动设置页，固定在窗口底部；日志与设置仍在上方区域滚动。
- 涉及文件：`app.py`、`tests/test_status_window.py`、`CHANGELOG.md`。
- 修改前备份：`backups/versions/1.0.3/pre-1.0.4-sticky-stop-footer/`；版本快照：`backups/versions/1.0.4/`。
- 验证：界面布局与联系人设置专项 26 项通过，`app.py` 与测试文件语法检查通过；未启动微信或发送消息。

## 1.0.5 — 2026-09-27

- 统一窗口为浅灰背景、白色内容区和绿色强调色，整理标题卡片、折叠分区、输入框、下拉框与滚动条样式。
- 突出“开始测试”和“停止自动回复”按钮，日志改为更清晰的浅底深字显示；不改自动回复或联系人定位逻辑。
- 涉及文件：`app.py`、`tests/test_status_window.py`、`tests/test_contact_checkbox_ui.py`、`CHANGELOG.md`。
- 修改前备份：`backups/versions/1.0.4/pre-1.0.5-ui-polish/`；版本快照：`backups/versions/1.0.5/`。
- 验证：界面布局专项 27 项通过，语法检查通过；当前 Tk 的 clam 下拉框/滚动条样式兼容检查通过；未启动自动回复或微信。

## 1.0.6 — 2026-09-27

- 恢复普通操作按钮的立体边框，保留分区标题按钮的扁平样式，并让主要操作按钮更容易辨认。
- 给联系人自定义风格文本框添加竖向滚动条；鼠标滚轮位于文本框时滚动风格内容，不再带动整个页面。
- 涉及文件：`app.py`、`tests/test_status_window.py`、`tests/test_contact_checkbox_ui.py`、`CHANGELOG.md`。
- 修改前备份：`backups/versions/1.0.5/pre-1.0.6-button-borders-profile-scroll/`；版本快照：`backups/versions/1.0.6/`。
- 验证：界面专项测试 27 项通过，语法检查通过；Tk 验证了普通按钮有边框、折叠标题保留扁平样式；未启动自动回复或微信。
