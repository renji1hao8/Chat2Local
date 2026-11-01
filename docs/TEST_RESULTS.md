# 验收测试结果

## 2026-11-01 上游同步与真实附件验收

候选版 0.3.0.post1 基于上游 master 852be7d。Python 3.12.14 隔离环境下，Plus MCP E2E、Hub/Agent、附件导入/SSRF/续传恢复通过。10 项回归测试（含 PDF、DOCX、XLSX、PPTX 真实渲染子测试）通过；能力预览 11 项通过、1 项因文件 symlink 权限跳过。

使用者在 ChatGPT 刷新 thm-e 工具列表后，明确反馈真实聊天附件导入成功。没有记录其文件名、内容或截图作为公开测试素材。此反馈不能替代手机/网页各版本的兼容性测试，也不代表 MATLAB 到报告成品全流程已在此次验收。


## 2026-11-01 附件桥接验收

版本 `0.1.3.post3`。真实 MCP tools/list 中 import_chat_file 的 `_meta["openai/fileParams"]` 与四属性 Schema 检查通过；缺少 download_url 的引用被拒绝。

resilience_e2e 使用客户端格式的文件对象，完成 1.5 MB 原始文件下载、摘要核对、签名 URL 不回显、覆盖/路径拒绝、不可用链接和 SSRF 拒绝。devices_e2e 验证指定 Agent 下载落盘且 Hub 无副本，附件名不作为目的路径。已有 MCP、远程图片及续传测试通过。

文件源为隔离测试 HTTP 服务，测试配置仅在临时目录允许本机 HTTP。生产默认的 HTTPS 与公网地址检查保持不变。当时尚未完成真实客户端验收；随后使用者报告实际附件导入成功，见上方更新记录。


## 2026-11-01 修复及文档预览验收

版本 `0.1.3.post2`，Windows 11，项目虚拟环境 Python 3.13.3。未新增运行时 Python 依赖。

| 验证 | 结果 |
|---|---|
| `test_regressions.py` | 10 个测试通过，包含四种真实文档子测试；目录拒绝规则、限额、ZIP 路径、下载链接并发占用/发送失败/HEAD/Range/预取与依赖降级 |
| `smoke_service.py` | 引擎上传、下载、摘要、覆盖拒绝、续传、目录打包/解包通过 |
| `mcp_e2e.py` | 真实 MCP 工具注册、图片内容块、单次链接下载、续传及越权拒绝通过 |
| `devices_e2e.py` | Hub/Agent 设备隔离通过；800 KiB 大图按预算降级；PDF/DOCX/XLSX/PPTX 的图片与正文均通过真实远程协议返回 |
| `resilience_e2e.py` | URL 拉取、SSRF 拒绝、强制结束测试服务后的续传及过期清理通过 |
| 真实文档渲染 | 使用合成 PDF、DOCX、XLSX、PPTX；已查看四张输出图，文字、中文、表格和幻灯片可读；PDF 第二页正文提取通过 |
| 本地构建 | wheel 和 sdist 构建成功；已检查预览模块和 MIT LICENSE，排除虚拟环境与 pyc |

转换器使用本机 LibreOffice 与 Poppler。页面与正文由当前设备实际生成，非仅返回链接。真实渲染测试可通过 `C2L_PREVIEW_FIXTURES` 指向包含 `sample.pdf`（至少两页）、`sample.docx`、`sample.xlsx`、`sample.pptx` 的测试目录；默认无此变量时跳过该可选测试。测试要求第一页含 `Preview`，PDF 第二页含 `Second`，并会在该目录写入验证用 JPEG；不要指向业务文档目录。

本次没有重新执行 MATLAB 验收，也没有测试 ChatGPT 界面按钮和公网跨机器网络。远程测试使用同机独立 Hub/Agent 进程。CI 配置已添加，但尚未在 GitHub 执行；Linux/macOS 未在本次实测。

## 2026-10-27 历史验收

以下记录对应旧版本，当中的工具说明和限制以现行接口文档为准。

日期：2026-10-27 · 环境：Windows 11 Home / Python 3.13.3（uv 托管）/ MATLAB R2024a (`E:\MATLAB-1`)

所有测试都是**真实进程 + 真实 MCP 客户端（`mcp` SDK 2.3.0）+ 真实字节**，
在独立端口与独立 `USERPROFILE`（避开生产实例的单实例锁）上运行。

| 脚本 | 覆盖内容 |
|---|---|
| `tests/smoke_service.py` | 引擎级：内联/文本上传、去重、续传、下载、预览、路径策略、幂等、目录 ZIP/解包 |
| `tests/mcp_e2e.py` | MCP 传输级：工具注册、内容块、签名单次链接、DOCX/PDF/ZIP 摘要、越权、清单 |
| `tests/matlab_e2e.py` | 与 MATLAB 的双向图片闭环 |
| `tests/resilience_e2e.py` | `source_url` 拉取、SSRF 拒绝、进程被强杀后续传、会话过期清理 |
| `tests/devices_e2e.py` | Hub + 远程 Agent 的设备路由与隔离 |

## 与需求逐条对照

| # | 需求 | 结果 | 证据 |
|---|---|---|---|
| 1 | ChatGPT 附件中的 PNG 上传到指定 Windows 目录 | **部分** | 协议侧已验证：同一 PNG 经 `content_base64`（单次/分块）落到 `E:\...\shots\from-chat.png`，磁盘 SHA-256 与发送端一致。**但 ChatGPT 本身无法把附件字节塞进工具参数**（见限制 1），真实附件需走 `source_url`；该通道已在 `resilience_e2e` 用 1.5 MB 文件验证 |
| 2 | 上传后的 PNG 可被 MATLAB `imread()` 读取 | **通过** | `matlab_e2e`: `READ_OK 64 96 3` / `COLORTYPE truecolor` |
| 3 | MATLAB 生成的 PNG 可下载并被预览 | **通过** | `preview_file` 返回 `image` 内容块，base64 与磁盘文件逐字节一致；像素级校验“下载图 = 原图的垂直翻转” |
| 4 | DOCX / PDF / ZIP 传输后 SHA-256 一致 | **通过** | 三种类型各 4 KB：上传摘要核对 + 通过单次链接下载后重算摘要一致 |
| 5 | 大文件中途断开后可恢复 | **通过** | ① 客户端只发 3 块后停止 → `transfer_status.next_offset` 准确，续传后摘要一致；② **服务进程被 `kill`** 后重启，会话从磁盘恢复（`next_offset=32000`），续传提交成功且字节一致 |
| 6 | 未授权目录访问被拒绝 | **通过** | `../..`、`C:/Windows/win.ini`、目录联接（junction）指向工作区外，全部 `outside_workspace`；SSRF 默认拒绝 `127.0.0.1` 源；覆盖被拒 `exists` |
| 7 | 不同设备间路由正确、不串写 | **通过** | Hub `hub-dev` + Agent `agent-dev`：写 agent 只出现在 agent 工作区，写 hub 只出现在 hub 工作区；分块上传同样只落在 agent；agent 侧越权被它自己的策略拒绝 |

## 实际输出摘要

```
tests/mcp_e2e.py
  tools: apply_patch download_file exec_command handoff_get handoff_list handoff_save
         interact_process kill_process list_devices preview_file read search
         transfer_directory transfer_status upload_file
  PASS upload_file succeeds / uploaded sha matches / file on disk
  PASS preview has image block ['text', 'image'] / preview image decodes image/png
  PASS download has embedded resource / downloaded bytes identical
  PASS download_url serves the file / download_url is single-use
  PASS status reports resume point next_offset=60000
  PASS resumed upload commits / resumed bytes identical
  PASS docs/report.docx|paper.pdf|bundle.zip upload ok + sha round-trip
  PASS traversal refused outside_workspace / absolute outside refused / no silent overwrite exists
  PASS manifest lists three files 3

tests/matlab_e2e.py
  PASS PNG uploaded over MCP / bytes identical after upload
  PASS MATLAB imread() read the uploaded PNG  READ_OK 64 96 3 | COLORTYPE truecolor
  PASS preview of MATLAB PNG returns image ['text', 'image']
  PASS MATLAB image is the exact vertical flip of the upload
  PASS download_file reports the same sha / download_url returns identical bytes

tests/resilience_e2e.py
  PASS source_url upload succeeds / size and hash verified
  PASS SSRF guard refuses loopback source source_url_denied
  PASS session survives a hard kill next_offset=32000 expected=32000
  PASS resumed upload after restart commits / resumed bytes intact
  PASS expired session reported session_expired / expired staging file removed

tests/devices_e2e.py
  PASS both devices visible ['agent-dev', 'hub-dev']
  PASS agent advertises file tools (upload/download/preview/transfer_directory/…)
  PASS file lands in the agent workspace / hub workspace untouched
  PASS agent file downloads through the hub
  PASS agent refuses paths outside its workspace outside_workspace
```

## 未能验证的部分

* **在 ChatGPT 界面里点一次真实“上传附件 → 工具”**：需要人在 ChatGPT 侧操作，且该客户端目前
  没有把附件字节交给工具的能力；`content_base64` 通道只验证了协议与落盘正确性。
* **ChatGPT 是否渲染 `resource`/`image` 内容块**：本地用标准 MCP 客户端验证了报文结构，
  实际渲染取决于客户端版本；不渲染时会退回 JSON 元数据 + 链接。
* 真正的“跨机器”远程 Agent：本机以两个独立进程（Hub/Agent）验证；跨网络还需考虑代理与 WSS。

## 上游贡献回归与部署记录

精简 PR 全量测试曾出现既有浏览器 30 ms 断连时序断言失败（2210 passed、41 skipped、29 deselected、1 failed）；随后包含该用例、图片能力与浏览器集成的复核为 65 passed、1 skipped，未放宽断言。本机生产 Core 已切换到 0.3.0.post1，实际检查确认 18 个工具、28 项浏览器能力、附件参数、file.preview、本地 Web UI 和隧道健康。云端工具快照还需在 ChatGPT 刷新。
