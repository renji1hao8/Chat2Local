# 0.3.0.post1 发布说明

这是 Chat2Local 的非官方 Plus 分支，基于上游 v0.3.0 和 master 852be7d。

本版同步浏览器能力、跨平台本地 Web UI、Handoff 可视化、进程输出清理和并发文件写入修复；保留附件导入、图片及 PDF/Office 预览、通用文件传输、目录打包和续传。

2026-11-01，使用者在刷新 thm-e 工具列表后确认真实聊天附件导入成功。独立测试也验证了原始字节、摘要、越界拒绝、覆盖保护、Hub/Agent 路由及四种文档渲染。扫描 PDF 不提供 OCR；LibreOffice 可能产生与 Microsoft Office 不同的排版。

安装使用独立 Python 3.12+ 环境，避免与原版同名的 chat2local 模块冲突。文档预览需自行安装 LibreOffice 和 Poppler；发行包不捆绑这两者，也不提供新版桌面 EXE。安装、文件参数和下载链接配置详见 README 及 FILE_TRANSFER.md。

升级现有连接时：停止旧 Core，安装新版后按原配置启动，确认健康检查正常，再在 ChatGPT 刷新工具列表。新列表包含 capability_discover / capability_execute 和六个兼容文件工具。保留工作区数据与 ~/.chat2local 配置；旧版源码或 wheel 可用于回退，但不要让两个版本同时操作同一运行时。

下载 URL 中的 token 是有效期内的访问凭证。跨设备下载需将 download_base_url 路由到生成 token 的设备；Hub 不自动代理 Agent 的 HTTP 下载。文件路径检查和转换器都不构成 OS 沙箱。

本版作为预发布候选分发。GitHub Actions、Linux/macOS 和不同 ChatGPT 客户端的结果以各自验收记录为准，不将本机 Windows 结果当作跨平台证明。

上游贡献：[PR #1：通过现有能力接口预览工作区图片](https://github.com/mmc-cloud/Chat2Local/pull/1)。该草稿 PR 只包含有界图片预览，完整附件和传输功能保留在 Plus 分支。
