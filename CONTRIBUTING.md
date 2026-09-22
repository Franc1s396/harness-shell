# 贡献指南

简体中文 | [English](CONTRIBUTING.en.md)

欢迎通过问题反馈、文档、翻译、测试或代码改进参与 Harness SSH。

## 问题反馈与功能建议

- 提交前搜索 [现有 Issues](https://github.com/Franc1s396/harness-shell/issues)，避免重复；可以在相关问题下补充复现信息。
- [新建 Issue](https://github.com/Franc1s396/harness-shell/issues/new/choose) 时选择 Bug 或功能建议模板，中文和英文均可。
- Bug 请提供应用版本或 commit、Windows 版本、安装或源码运行方式、复现步骤、预期与实际结果。Provider、模型和 SSH 环境信息仅在相关时补充。
- 功能建议请说明具体使用场景、当前困难和期望行为。较大的功能、架构或协议调整，建议先通过 Issue 讨论范围。
- 日志与截图请先脱敏，不要上传 API Key、密码、私钥、运行时数据库或敏感服务器信息。可利用的安全漏洞细节不要直接发布到公开 Issue。

## 本地开发

环境准备与启动步骤见 [README](README.md#本地调试-quick-start)。修改前阅读 [AGENTS.md](AGENTS.md)，并按其中的任务路由阅读相关领域指南和目录规则。

外部贡献者可 Fork 仓库，在自己的分支上完成改动后向上游提交 Pull Request。保持每个 PR 的目标集中，避免混入无关格式调整、生成文件或依赖升级。

## 验证改动

先运行与改动直接相关的测试，再根据影响范围扩大验证。完整命令、环境要求与验收边界以 [Testing Guide](docs/agents/testing.md) 为准。

- 前端改动：相关测试，以及 `npm.cmd --prefix frontend run test` 和 `npm.cmd --prefix frontend run build`。
- Python 改动：通过 `backend\.venv\Scripts\python.exe -m pytest` 运行相关测试；需要完整后端回归时运行 `backend\.venv\Scripts\python.exe -m pytest backend -q`。
- Launcher、Tauri、协议、打包或 SSH/SFTP 改动：按 Testing Guide 选择对应测试和门禁。
- 文档或模板改动：检查链接、命令、格式及中英文内容一致性。
- 提交前运行 `git diff --check`。

行为变更应补充有意义的测试；契约或长期架构事实变化应同步对应实现、测试与领域文档。无法执行的验证请说明原因，不要将自动测试或构建通过写成真实 Provider、安装版 Desktop 或生产 SSH 已验收。

## 提交 Pull Request

按 PR 模板说明：解决的问题、最终行为、关联 Issue、验证命令与结果，以及未验证项。UI 改动请附脱敏截图；涉及持久化或兼容性时，请说明升级影响。

不要提交凭据、运行时数据库、`.runtime/`、依赖目录、缓存或构建产物。修改 README 时同步中英文版本。

维护者会根据项目范围和验证结果评审；提交 PR 不代表一定会合并。小而完整、易于复现和验证的改动更便于评审。
