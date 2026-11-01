# Chat2Local Plus

基于 [mmc-cloud/Chat2Local](https://github.com/mmc-cloud/Chat2Local) 0.3.0（含上游 master `852be7d`） 的非官方扩展，提供图片与文档预览、通用文件传输、断点续传及目录打包。原项目由 mmc-cloud 开发，使用 MIT License；本分支保留原版权与许可。

本分支版本为 `0.3.0.post1`，同步至上游 master [`852be7d`](https://github.com/mmc-cloud/Chat2Local/commit/852be7d6d0a1f7864edb906a7484028f52cb6773)。后续上游修复仍需维护者同步。

2026-11-01，使用者在 ChatGPT 刷新工具列表后完成真实聊天附件导入验收。此项验收确认附件可以传到本地；不代表所有客户端、文件格式和完整实验报告流程均已验证。

本版保留上游浏览器能力、本地 Web UI、Handoff 可视化与并发/进程修复，同时保留六个 Plus 文件工具以兼容现有连接。另提供 `file.preview` 能力，作为不扩展上游公共工具数量的小范围贡献。

## 支持能力

原有的文件读取、搜索、patch、命令执行、进程管理、设备选择与 Handoff 工具继续保留。新增六个工具：

| 工具 | 功能 |
|---|---|
| `import_chat_file` | 通过官方文件参数接收聊天附件，下载到指定设备工作区 |
| `upload_file` | 文本、base64、HTTPS 来源上传；支持分块续传 |
| `download_file` | 内联内容、分段读取、短时下载链接 |
| `preview_file` | 图片与文本预览；PDF、Office 页面图片及所选页面正文 |
| `transfer_status` | 查看续传位置、会话状态与配额 |
| `transfer_directory` | 目录清单、ZIP 打包、逐文件解包 |

共 18 个 MCP 工具（上游 12 个 + Plus 6 个）。图片支持 PNG、JPEG、GIF、WebP、BMP；SVG 返回文本。PDF 使用 Poppler；DOCX、XLSX、PPTX 使用 LibreOffice 导出 PDF 后渲染。旧版 DOC/XLS/PPT 与 ODF 格式也可交给 LibreOffice，当前实测覆盖 PDF、DOCX、XLSX、PPTX。

## 从源码安装

要求 Python 3.12+。获取 Plus 分支：

```powershell
git clone --branch plus/0.3.0 https://github.com/renji1hao8/Chat2Local.git Chat2Local-Plus
cd Chat2Local-Plus
```

进入本仓库目录，使用独立虚拟环境，避免与原版同名的 `chat2local` Python 模块冲突。

```powershell
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -e .
.venv/Scripts/c2l-plus.exe --workspace C:/Projects/example
```

Linux/macOS 使用 `python3 -m venv .venv`，随后运行 `.venv/bin/python -m pip install -e .` 和 `.venv/bin/c2l-plus`。已使用 uv 的用户也可以执行 `uv tool install --editable .`。本仓库未提供扩展版桌面 EXE；上游的安装包不包含这些新增工具。

## 上游新增功能

Core 自带本地 Web UI，默认从 `http://127.0.0.1:8767/` 起选择可用端口；实际地址以启动日志为准。可管理配置、设备、日志和 Handoff。浏览器功能需要上游可选 Chrome 扩展，默认不启用浏览器监听；请按上游 [浏览器说明](browser-extension/README.md) 配置，或先查询 `capability_discover` 的可用状态。

通过 `capability_discover` 查询 `file.preview` 并使用 `capability_execute` 可查看工作区图片；详见 [图片预览能力](docs/file-preview.md)。完整 PDF/Office 预览继续使用 Plus 的 `preview_file`。

## 从聊天附件导入模板

`import_chat_file` 声明官方 `_meta["openai/fileParams"]`，由支持此机制的 ChatGPT 客户端提供附件对象。你可以说：“把我上传的模板原件导入工作区的 templates/template.docx，随后运行 MATLAB，并在副本中填写实验结果。”

工具需要 `file` 和明确的目标 `path`，可传 `device`、`workspace`、`sha256`、`overwrite`。`file` 由客户端提供 `download_url` 和 `file_id`，可带 `mime_type`、`file_name`。附件名不会作为落盘路径，原有文件默认不覆盖。目标电脑通过现有 URL 拉取机制下载原始字节，不需要额外的 OpenAI API key，也不需要模型生成 base64。

Core 更新后需要重新加载服务，并在 ChatGPT 中刷新该连接的工具列表。确认“写入操作”出现 **Import chat file**，再新开聊天测试。

建议先说：“将附件原件导入工作区的 templates 文件夹，返回落盘路径、大小和 SHA-256，暂不运行实验或修改模板。”确认文件可读后，再运行 MATLAB，将数据、图片、日志和模板副本保存在同一工作区。

若客户端只传文件 ID、`sandbox:` 路径或未交付有效临时 URL，请重新附加/选择文件；不要编造 URL。链接失效时需要客户端重新提供引用。仅导入文件不等于自动执行 MATLAB 或填写报告，这些是后续操作。

## 文档预览

在运行 Core 的设备安装 LibreOffice 和 Poppler，并让 `soffice`、`pdfinfo`、`pdftoppm`、`pdftotext` 位于 PATH。也可以在 `~/.chat2local/files.yaml` 中配置它们的完整路径。

```json
{"path":"reports/example.docx","mode":"both","page":1,"page_count":2}
```

将这些参数交给 `preview_file`：`both` 同时返回页面和正文，`pages` 只返回图片，`text` 只提取正文。默认第一页，每次最多三页；后续页面通过增加 `page` 获取。Excel 页码对应打印分页，PowerPoint 页码对应导出后的幻灯片页面。返回图片会按传输预算缩放。

扫描版 PDF 可显示页面，但没有 OCR。加密文件、缺少转换器、超时或超限时，工具返回具体原因和可用的下载链接。转换在临时目录进行，不改写源文件。Office 布局可能因字体与 LibreOffice 兼容性而不同。

## 配置与访问边界

文件传输配置独立保存在 `~/.chat2local/files.yaml`，完整接口见 [文件传输文档](docs/FILE_TRANSFER.md)。所有文件访问受 workspace、`allowed_roots` 和 `deny_globs` 限制；调用参数只能收紧服务端限额。

默认下载链接指向目标设备本机地址。跨设备使用时，为目标设备设置 `download_base_url`，并让该地址路由到该设备的 `/files`。Hub 不会代理 Agent 的下载链接。链接中的随机 token 是下载凭证，默认 900 秒有效；持有者可下载绑定文件，MCP OAuth 不覆盖该入口。

命令执行与文档转换使用当前 OS 账户权限，不是系统沙箱。LibreOffice 使用独立 profile，禁用宏及加载时的外链刷新；需要强隔离时应使用独立账户或容器。

## 测试

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -p test_regressions.py -v
.venv/Scripts/python.exe tests/smoke_service.py
.venv/Scripts/python.exe tests/mcp_e2e.py
.venv/Scripts/python.exe tests/devices_e2e.py
.venv/Scripts/python.exe tests/resilience_e2e.py
```

图像与端到端测试需要 Pillow；开发环境可用 `uv sync --group dev` 按锁文件安装，或使用项目解释器执行 `python -m pip install -e . Pillow`。脚本使用单独分配的临时目录、配置和测试进程。测试结果与真实客户端验证范围见 [验收记录](docs/TEST_RESULTS.md)，本次修复见 [变更记录](CHANGELOG.md)。

## 发布与许可

发布前使用独立环境构建并检查 wheel；排除 `.venv`、本机配置、凭据、日志和运行状态。保留 LICENSE 和上游来源，使用扩展版自己的包名、安装说明与测试结果。MIT 许可全文见 [LICENSE](LICENSE)。

上游贡献：[PR #1：通过现有能力接口预览工作区图片](https://github.com/mmc-cloud/Chat2Local/pull/1)。该草稿 PR 只包含有界图片预览，完整附件和传输功能保留在 Plus 分支。
