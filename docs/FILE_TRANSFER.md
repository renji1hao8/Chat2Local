# 文件传输与预览接口

版本 `0.3.0.post1`；基线 Chat2Local `0.3.0`，上游 master `852be7d`。

## 路由与错误

六个文件工具均支持 `device`。省略时使用 MCP Server 所在设备；远程设备的路径策略、配置、临时文件和转换器均以该设备为准。除 `transfer_status` 外均支持 `workspace`，路径必须在授权工作区内。

传输错误提供稳定的 `error.code`，如 `outside_workspace`、`path_denied`、`exists`、`sha256_mismatch`、`session_expired`。文档转换失败作为预览元数据中的 `reason` / `preview_error` 返回，保留可用的下载方式。

## import_chat_file

官方附件入口，声明 `_meta["openai/fileParams"] = ["file"]`。文件对象严格声明四个 string 属性，只要求 `download_url` 和 `file_id`，`mime_type`、`file_name` 可省略。官方契约见 https://developers.openai.com/plugins/reference#define-file-inputs 。

请求还需明确的 `path`，可带 `device`、`workspace`、`sha256`、`overwrite`。内部调用目标设备已有的 `upload_file(source_url=...)`，沿用 HTTPS/SSRF、大小、摘要、覆盖和路径策略。附件名称仅作为元数据，不能改变目的路径。返回本地路径、摘要和 `imported_file_id`，不回显签名下载 URL。不使用 file_id 调用 OpenAI Files API，不需要 API key。

临时 URL 必须能被目标设备访问。过期、权限拒绝或附件未正确交付时返回错误；客户端需刷新引用。文件 ID、sandbox 路径、模型猜测的 URL 均不能代替真实附件下载地址。服务端协议已测试，2026-11-01 使用者已在 ChatGPT 报告实际附件导入成功。

## upload_file

`action=auto` 接受且仅接受一个来源：`content_text`、`content_base64` 或 `source_url`。来源 URL 默认只允许解析到公网地址的 HTTPS，每次跳转重新检查。DNS 检查没有固定实际连接 IP，不能视为完整的 SSRF 隔离。

大文件使用 `begin → chunk → commit`：

1. `begin` 传 `path`、`size`，可传完整文件 `sha256`，返回 `session_id` 和 `next_offset`。
2. `chunk` 传 `session_id`、`path`、`offset`、`data_base64`，可传 `chunk_sha256`。推荐每块 256 KiB；一致的重复块可重发。
3. `commit` 传 `session_id`、`path`，完成大小、摘要检查后原子替换目标。

`status` 查看断点，`abort` 放弃会话。`overwrite=true` 还需要服务端 `allow_overwrite=true`。`idempotency_key` 的具体覆盖范围以操作实现为准；超时后先检查状态，不盲目重放。对于聊天附件，优先使用 import_chat_file；不支持文件参数桥接的客户端仍需手动放入工作区或提供真实 HTTPS 下载地址。

## download_file

`mode=auto` 返回有界内容和可用的下载链接；`inline` 仅返回有界内容；`url` 仅生成链接。`offset` / `length` 支持分段读取，实际长度不超过服务端及传输预算。按 `next_offset` 继续，直到 `complete=true`。`sha256=true` 计算完整文件摘要。

默认下载 token 为 256 位随机值，有效期 900 秒，绑定路径、大小和修改时间。它是 bearer credential，不是依赖 MCP OAuth 的授权请求。不要将 token 放入公开日志或帖子。

单次链接在服务端发完完整响应后失效，发送失败时解除占用；这不保证浏览器已保存文件。HEAD 和标记为 prefetch 的请求不消耗链接。单次链接拒绝 Range，返回 416 且保留 token；续传请使用 `download_file` 的 offset，或按需配置有效期内可重复使用的链接。无法识别为 prefetch 的普通 GET 仍会被当作下载。

## preview_file

| 参数 | 含义 |
|---|---|
| `path` | 工作区内文件 |
| `max_bytes` | 本次内容预算，只能收紧服务端上限 |
| `mode` | 文档使用 `both`（默认）、`pages`、`text` |
| `page` | 从 1 开始的页码，默认 1 |
| `page_count` | 默认 1，最多 3，受服务端配置限制 |

PNG/JPEG/GIF/WebP/BMP 返回 image 内容块。SVG 和文本返回 text 内容块。过大的图片返回元数据和可用链接，不声称已经内联预览。

PDF 使用 `pdfinfo`、`pdftotext`、`pdftoppm`。DOCX/XLSX/PPTX、DOC/XLS/PPT、ODT/ODS/ODP 使用 LibreOffice 转 PDF，再执行同样的页面与正文提取。当前实测格式为 PDF、DOCX、XLSX、PPTX。正文只覆盖所选页；Excel 按打印分页，幻灯片按导出分页。扫描 PDF 没有 OCR。

返回 `total_pages`、`page`、`last_page`、`rendered_pages`、`text_available`、`text_truncated`、`image_truncated`。分页超出范围、加密/损坏文档、缺少转换器、超时及超大输入会返回具体原因。转换使用临时目录、独立 LibreOffice profile，完成后清理；不改动原文件。工具以当前 OS 用户运行，不是系统沙箱。

为适配默认 1 MiB 远程消息限制，普通二进制内联最多 512 KiB，文本最多 96 KiB；文档一批页面图片最多 384 KiB、正文最多 16 KiB。清单也有编码体积上限，截断时返回 `truncated=true`。这些限制在单机也生效，服务端配置和调用参数均不能放大传输预算。

## transfer_directory

`manifest` 列出路径、大小、修改时间及可选 SHA-256；比较两份清单可选择需要传输的文件。超过数量、字节或编码预算时返回截断标记，不应将其当作完整清单。

`download` 将选中的文件打包 ZIP，返回链接及可用的内联资源。`upload` 接受 `zip_path`、`content_base64`、`source_url` 之一，逐文件解包。遍历、打包和解包执行同一套拒绝规则；拒绝路径穿越、符号链接、目录联接及 Windows ADS 名称。解包逐文件提交，部分失败返回 `written` / `failed`，不自动回滚已完成文件。目录操作不保证对并发文件修改提供一致性快照。

## 配置

在目标设备的 `~/.chat2local/files.yaml` 配置；也可用 `CHAT2LOCAL_FILES_CONFIG` 指定文件。配置在进程启动/首次使用时载入，修改后重启 Core。

```yaml
enabled: true
max_file_bytes: 4294967296
max_inline_bytes: 524288
chunk_size: 262144
max_chunk_bytes: 1048576
max_source_bytes: 2147483648
max_preview_bytes: 4194304
max_directory_files: 5000
max_directory_bytes: 2147483648
allow_overwrite: false
allow_source_url: true
allow_insecure_source_url: false
allow_private_source_hosts: false
session_ttl: 3600
max_sessions: 64
max_concurrent_transfers: 4
max_staging_bytes: 2147483648
download_url_enabled: true
download_token_ttl: 900
download_token_single_use: true
# download_base_url: https://files.example.com/device-a
deny_globs: ["**/.git/**", "**/.ssh/**"]
document_preview_enabled: true
max_document_bytes: 33554432
max_preview_pages: 3
preview_timeout: 20
# soffice_path: C:/Program Files/LibreOffice/program/soffice.exe
# pdftoppm_path: C:/Tools/poppler/Library/bin/pdftoppm.exe
# pdftotext_path: C:/Tools/poppler/Library/bin/pdftotext.exe
# pdfinfo_path: C:/Tools/poppler/Library/bin/pdfinfo.exe
```

`download_base_url` 仅接受无用户名、密码、query、fragment 的 HTTP(S) 地址，可含反向代理路径前缀。代理需要将其 `/files/<token>` 路由到生成 token 的那台设备。只转发 Hub 的 `/mcp` 不会让 Agent 下载链接可访问。

默认远程请求超时为 30 秒，文档转换本身合计最多 20 秒；排队、慢磁盘和大目录仍可能导致调用超时。出现超时后先检查实际状态。重启会丢失内存中的下载 token，已落盘的上传会话可按状态恢复。
