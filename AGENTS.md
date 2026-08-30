# Harness Shell Repository Guide

## 适用范围与阅读顺序

本文件适用于整个仓库，是所有任务必须先读取的入口。

1. 先读取本文件，确认全局红线、业务边界和任务路由。
2. 根据“任务路由”读取匹配的 `docs/agents/*.md`；跨层任务必须读取所有涉及领域的文档。
3. 修改某个目录前，再读取该目录向上最近的局部 `AGENTS.md`。
4. 路径更具体的规则可以细化上层规则，但不得放宽本文件的安全边界或把未实现能力描述成已完成。
5. 一条详细规则只保留一个真源。路由文件只说明读取条件和目标，不复制领域规则全文。

## 业务简述与当前边界

Harness Shell 是面向 Windows 的本地 AI SSH Agent 桌面应用。当前架构为 React/TypeScript WebView、Tauri 2 Rust Core 和 Python Sidecar；Rust Core 通过私有 stdio Protocol v1 独占管理 Sidecar，并作为凭据、进程和 WebView 暴露面的安全边界。

当前 M2 已实现连接管理、显式 Host Key 信任、直连与单层 ProxyJump 和多标签人工 PTY。连接配置数据库为 schema v3，并用 JS-safe 单调 `version` 拒绝凭据解析后的陈旧目标或跳板配置；`updated_at` 仅用于展示。旧的 Sidecar 内部 Agent exec/SFTP/Artifact 运行时已删除；用户手动 SFTP 文件管理器已按“仅用户显式操作”完成实现，进入 Activity 后固定绑定当时选中的 connected terminal tab，Agent、M3 Workflow 与 approval window 均无 SFTP/exec 路由。`verify-manual-sftp.ps1` 自动门禁已通过；Tauri Desktop 人工验收仍须由用户单独确认，M3 Agent Workflow、真实审批、自动恢复、生产部署和迁移验收尚未实现。构建通过、自动测试、容器 SSH Lab 或本地桌面验收均不得表述为生产主机验收。

详细架构与能力边界见 [Architecture Guide](docs/agents/architecture.md)。

## 全局强制规则

- 默认使用简体中文说明；代码、命令、配置键、协议名、错误码和远程输出保持原语言。
- Windows PowerShell 读取可能包含中文、Emoji 或智能标点的文本时，使用 `Get-Content -Encoding UTF8`。
- 遵循 Let it crash：尽早暴露真实问题，禁止用降级、兜底、启发式补丁、猜测解析或后处理伪装解决。
- WebView 不得直接接触凭据、原始协议帧、Sidecar stderr、运行时密钥或任意 shell 能力。
- 不得提交密钥、私钥、运行时数据库、Sidecar 二进制、SSH Lab `.runtime`、依赖目录、缓存和构建产物。
- 协议、安全边界、持久化格式或跨层契约变更，必须同步更新两侧实现、测试和对应领域文档。
- 保留无关和未跟踪的用户工作。未经明确授权，不创建 worktree、分支、commit、push，不暂存或丢弃文件。
- 未经用户明确同意，不开始业务代码实现；诊断和设计阶段保持只读。
- 不得自主对 `docs/superpowers/` 生成的规格或计划执行 Git 操作。
- 复杂流程代码必须注释关键决策、失败路径和资源所有权；Python 面向对象代码还必须遵循独立 Python 规范。
- 验证结论必须陈述证据边界，不能把测试、构建或静态检查等同于真实运行、部署或迁移验收。

## 任务路由

| 任务条件 | 必读文档 |
| --- | --- |
| 跨进程架构、模块归属、状态权威、跨层调用链 | [Architecture Guide](docs/agents/architecture.md) |
| React、UI、组件、状态管理、i18n、前端 API 封装 | [Frontend Guide](docs/agents/frontend.md) |
| Tauri command、capability、permission、Vault、Sidecar Broker/Supervisor | [Rust Core Guide](docs/agents/rust-core.md) 和 [Protocol & Security Guide](docs/agents/protocol-security.md) |
| Python Sidecar、SSH、PTY、存储、Telemetry、远程 I/O | [Python Sidecar Guide](docs/agents/python-sidecar.md) 和 [Python Style Guide](docs/agents/python-style.md) |
| 任意 Python 源码、测试或脚本修改 | [Python Style Guide](docs/agents/python-style.md) |
| IPC、Protocol v1、事件、跨进程错误、凭据或安全边界 | [Architecture Guide](docs/agents/architecture.md) 和 [Protocol & Security Guide](docs/agents/protocol-security.md) |
| 单元测试、集成测试、Sidecar 打包、SSH Lab、M1/M2 验收 | [Testing Guide](docs/agents/testing.md) |
| 跨层功能 | 上述所有涉及层的领域文档 |

进入以下高风险目录时，还必须读取局部规则：

- `frontend/src-tauri/` → `frontend/src-tauri/AGENTS.md`
- `backend/src/harness_shell_sidecar/` → `backend/src/harness_shell_sidecar/AGENTS.md`
- `tests/ssh_lab/` → `tests/ssh_lab/AGENTS.md`

## 项目结构

```text
.
├── frontend/                         # React/TypeScript WebView 与 Tauri Rust Core
│   ├── src/                          # UI、功能、typed API、状态与 i18n
│   └── src-tauri/                    # Tauri commands、Protocol、Vault、Sidecar 生命周期
├── backend/                          # Python Sidecar 工程
│   ├── src/harness_shell_sidecar/    # Protocol、runtime、SSH、PTY、存储与遥测
│   ├── tests/                        # Python 单元、集成与 SSH 测试
│   └── scripts/                      # Sidecar 打包脚本
├── scripts/                          # 仓库级 M1/M2 验证与 SSH Lab 生命周期脚本
├── tests/ssh_lab/                    # 隔离的双节点 OpenSSH 容器实验室
├── docs/protocol/                    # Protocol v1 规范与 fixture
├── docs/testing/                     # 自动门禁和人工验收记录
├── docs/agents/                      # 按任务读取的长期领域指导
└── docs/superpowers/                 # 已批准规格与实施计划
```

新增文件按职责归属，不按调用方便或临时复用随意落位。详细模块地图由对应领域文档维护。

## 常用项目命令

从仓库根启动前端开发和检查：

```powershell
cd frontend
npm install
npm run dev
npm run test
npm run build
npm run tauri:dev
```

从 `frontend/` 切换到 Python Sidecar：

```powershell
cd ..\backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

从 `backend/` 返回仓库根运行自动门禁：

```powershell
cd ..
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m1.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-m2.ps1
```

`verify-m2.ps1` 额外要求 Docker Desktop、Docker Compose v2 和 Windows OpenSSH `ssh-keygen.exe`。更细的命令和适用范围见对应领域文档；仓库当前没有统一 lint、format、部署或迁移命令，不得虚构。

## 工作与验证流程

1. 开始前读取根规则、任务路由命中的领域文档和最近的局部规则。
2. 只读确认源码真源、工作区状态和当前能力边界。
3. 获得实现授权后，仅修改任务范围内文件，并保留无关工作。
4. 先运行最小相关验证，再按风险扩大到子系统或 M1/M2 门禁。
5. 明确区分单元测试、构建、打包、容器实验室、桌面验收和生产验收。
6. 结束前执行 AGENTS 文档影响检查和 `git diff --check`；未获授权不得执行 Git 写操作。

## AGENTS.md 维护规则

代码更新一旦改变长期事实，必须在同一变更中同步更新对应 `AGENTS.md` 或 `docs/agents/*.md` 的唯一真源。需要更新的长期事实包括：

- 目录、模块、类或跨层组件的职责与所有权；
- 架构、进程边界、调用链、状态权威或生命周期；
- 项目命令、工具链、关键依赖或版本要求；
- API、IPC、Protocol、事件、错误码或数据结构；
- 安全、权限、凭据、日志、持久化或敏感数据边界；
- 测试入口、验收标准、证据范围或生成物规则。

纯实现细节或局部重构未改变长期约束时，不为制造文档改动而更新。维护时遵守以下规则：

1. 更新承载该规则的唯一真源，不在多个文件复制详细内容。
2. 文件路径或职责变化时，同步修正根路由、局部链接和“当前源码真源”。
3. 命令变化时先核对 manifest 或脚本，再更新文档。
4. 删除模块或能力时同步删除过时描述，不保留误导性的历史现状。
5. 任务结束报告必须二选一：列出已同步更新的 AGENTS 文档；或明确说明“已检查相关 AGENTS.md，无需更新”。
