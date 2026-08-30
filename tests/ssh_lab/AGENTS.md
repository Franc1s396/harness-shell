# SSH Lab Local Rules

## 必读路由

修改或运行本目录内容前，先读取仓库根 `AGENTS.md`，并读取：

- [Protocol & Security Guide](../../docs/agents/protocol-security.md)
- [Testing Guide](../../docs/agents/testing.md)

## 本目录即时规则

- 保持双节点隔离拓扑：`jump` 同时连接 host-facing `ssh_ingress` 和 internal `ssh_lab`；`target` 只能连接 internal `ssh_lab`。
- 只允许 `jump` 的 SSH 端口绑定到 `127.0.0.1:2222`；不得把 `target` 直接暴露到 host 来让 ProxyJump 测试“通过”。
- password、private key、passphrase、Host Key manifest 和 evidence 只生成在 `tests/ssh_lab/.runtime/`，不得提交或输出真实秘密。
- Secret marker 扫描、runtime database evidence 检查和 cleanup 都是 M2 gate 的组成部分；不得跳过失败阶段后报告总门禁成功。
- 启停脚本必须显式检查 Docker/Compose、容器端口、真实 TCP 可达性和资源清理，不能只依据 Compose config 或 health 声称可用。
- PowerShell 调用 `ssh-keygen.exe` 时保留经过测试的空 passphrase 参数语义，不恢复会在 Windows PowerShell 5.1 丢失空参数的写法。
- Linux 容器运行的 `*.sh` 必须保持 LF 行尾；根 `.gitattributes` 固定 checkout 规则，字节级契约测试阻止 CRLF shebang 进入 SSH Lab。
- 测试完成或失败后清理 container、network、临时环境变量和本地进程；不要删除 `.runtime` 之外的用户文件。
- SSH Lab 通过只证明当前 checkout 对选定 containerized OpenSSH 行为，不是 production host、Provider、Agent Workflow、审批、sudo 或远程写验收。
- 任务结束前检查 Protocol、Testing 和本局部规则是否因拓扑、证据或命令变化需要同步更新，并报告结果。
