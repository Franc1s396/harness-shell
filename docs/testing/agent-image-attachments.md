# Agent 图片附件验收记录

日期：2026-09-15。范围：图片输入、文字输出；选择/粘贴上传，不支持拖入和普通文件。

## 已执行的自动验证

- Backend 全量 Pytest：797 passed、15 skipped（160.39s）。覆盖既有 Agent、Runtime、存储、HTTP、SSH 单元与其余 Backend 测试；环境依赖测试的 skip 不视为通过。
- 图片准入：四种真实格式、三种动画拒绝、损坏/截断、显示名、10 MiB 字节边界、4000 万像素边界；原始 bytes 保持不变。
- SQLite：元数据/BLOB 原子写入、错误草稿绑定回滚、已绑定图禁止单独删除、缺失 BLOB、级联回收；0001 与 0003 历史库升级保持旧记录，现有迁移故障/DDL 回滚与资源自检继续执行。
- HTTP：真实 multipart→DB→原图读取→删除，关联头、no-store/nosniff、非法额外字段、无 Content-Length 的实际累计限制与路径变体限制；OpenAPI/固定操作及 limits fixture 一致。
- 实际 ModelGateway + fake SDK：Chat Completions/Responses 的图片首轮、纯图、重试、首帧丢失后恢复、后续追问；canonical 只存 ID，实际 SDK 输入有图片。摘要请求实际含图，摘要覆盖后主模型投影不重复含旧图，原 BLOB 与 canonical 保留。固定 1000/token 的预算不编码图片 bytes。
- 清理：活动会话拒绝删除、第二条消息删除故障使整个事务回滚；真实 Runtime 重启只清未绑定图，已绑定图仍可读取。
- Frontend：64 个测试文件、468 项 Vitest 全通过，TypeScript/Vite 生产 build 通过（存在既有大 chunk 提示）；multipart 与图片响应安全头/流限制，元数据严格校验，串行上传、清理失败保留草稿、删除失败重试不重复上传、晚到结果和 object URL 回收、图片单独发送、混合粘贴、菜单关闭、重试与新草稿隔离。
- Windows Sidecar：锁定依赖、离线 tokenizer 资源、PyInstaller、真实 exe smoke。新增四种格式的真实 HTTP 上传与原图逐字节读取；保留全新迁移、同库重启、未知/旧库拒绝的检查。开发 venv 的本项目 metadata 曾为 0.2.2，按当前源码 0.2.1 重新 editable 安装后通过锁检查，未改项目版本。

## 浏览器观察

通过临时页面渲染真实 AgentWorkspace 与全局 CSS，检查 320px 侧栏、长 Provider 名称截断及下拉换行、两张卡片和真实 CSS spinner/遮罩。检查加号菜单及外侧点击、预览弹窗、Esc 关闭与原控件焦点恢复。没有导入用户真实图片；临时页面和测试服务器已移除/关闭。

## 实现时的调整

- 附件操作为固定 dispatcher handlers；同步 decoder 与短事务中无 await，因此不额外增加草稿锁和新的长期 service owner。
- 正式调用前把 canonical ID 解析成局部 HumanMessage 图片输入，Gateway 不接收数据库/Session。预算通过显式 `for_budget=True` 生成空 URL 标记，再结构化移除图片块并加算 1000，保持同一协议 mapper；不把空 URL 用于正式请求。
- 旧 schema 1 的 AI Responses 内容块保持原有回放能力，严格新规则针对用户图片内容。图片 schema 2 保存受校验的引用。
- 计划的测试职责分布在 `test_image_service.py`、`test_migrations.py`、`test_autonomous_lifespan.py`、现有 controller/workspace/http-client 测试等文件，未为每个规划名创建重复文件。
- 原有授权提示测试的两条中文断言与 HEAD 的英文提示词不一致，已改为断言当前英文的 UI 审核/不重复索权语义；未更改生产提示词。

## 尚未验收

- [ ] 真实支持图片的 Provider，以及不支持图片的真实 Provider 错误。
- [ ] 安装版 Tauri/WebView 的系统文件选择器、截图粘贴、真实大图、跨标签与应用退出 matrix。
- [ ] 用户真实旧库迁移与生产 SSH。

以上自动测试、浏览器组件观察与 packaged Backend smoke 不等于这些环境的验收。新对话是后端历史及图片的逻辑删除，不是安全擦除。

## 最终检查

`export_http_contract.py --check` 与 `git diff --check` 通过。同步更新根 `AGENTS.md`、Backend 局部 `AGENTS.md`，以及 architecture、frontend、python-sidecar、protocol-security、testing 五份领域指南。Python Style Guide 与 Rust Core Guide 的长期规则无需更改。没有创建分支/worktree，没有暂存、commit 或 push；原有 Cargo.toml 工作区状态保留。
