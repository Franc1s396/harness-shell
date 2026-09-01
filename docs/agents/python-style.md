# Python Style Guide

## 何时必须读取

新增或修改仓库内任何 Python 源码、测试、fixture、helper 或脚本时必须读取本文档。适用范围包括：

- `backend/src/**/*.py`
- `backend/tests/**/*.py`
- `backend/scripts/**/*.py`
- `tests/**/*.py`

本文档是 Python 语言级代码规范和 Code Review 清单的唯一真源；Sidecar 架构规则另见 [Python Sidecar Guide](python-sidecar.md)。

## 范围与职责

本规范的目标是让 Reviewer 不依赖作者口头解释即可判断：

- 类、字段、函数和方法分别负责什么；
- 输入、输出、异常和副作用是什么；
- async task、连接、文件、数据库和秘密由谁拥有、何时释放；
- 失败是如何显式传播的；
- 安全、持久化和 Protocol 不变量是否保持。

注释和 docstring 用来解释职责、原因、不变量与风险，不用来逐行复述代码。

## 当前源码真源

- Python 工程配置：[backend/pyproject.toml](../../backend/pyproject.toml)
- 生产代码：[backend/src/harness_shell_sidecar/](../../backend/src/harness_shell_sidecar/)
- Python 测试：[backend/tests/](../../backend/tests/)
- Python 工具脚本：[backend/scripts/](../../backend/scripts/)
- Python Sidecar 模块边界：[Python Sidecar Guide](python-sidecar.md)

仓库当前没有 Ruff、Black、Mypy、Pyright 或统一 Python formatter 配置；不得把未配置工具写成强制门禁。类型与文档要求通过 Review 和现有测试执行。

## 项目结构规范

- 每个模块只承担一个可命名职责；当 transport、validation、领域状态、persistence 或 presentation 混在一起时应拆分。
- 公共契约放在 `models.py` 或明确公共模块；实现细节保持私有，不通过 `__init__.py` 无限制 re-export。
- Handler 保持薄：解析/校验、调用领域对象、映射结构化结果。资源生命周期进入 runtime/manager/store。
- Test 与生产模块按领域对应；跨模块流程进入 `integration/` 或明确命名的契约测试。
- 避免循环依赖、隐式 import side effect、全局可变状态和无归属的 `utils.py`。
- 常量靠近其契约真源；Protocol limit、安全 allowlist 和 error code 不得散落复制。

## 代码规范

### 模块、类与字段注释

- 每个生产模块必须有 module docstring，简述职责和边界。
- 每个生产类必须有 class docstring，说明职责、协作者、关键不变量和资源所有权。
- 每个生产类字段都必须有用途说明，便于 Reviewer 判断其生命周期和敏感性：
  - Pydantic 字段优先使用 `Field(description="...")`；
  - dataclass 或普通 class field 使用紧邻字段的注释；
  - 在 `__init__` 中创建的 instance field，使用类级 annotation 加说明，或在首次赋值旁说明用途；
  - secret、connection、task、lock、database handle 和缓存字段必须说明 owner、是否持久化及清理时机。
- 注释不能只重复字段名，例如“`sessions` 表示 sessions”；应说明它是否拥有 live session、何时删除以及是否可恢复。

推荐形态：

```python
class SessionRegistry:
    """Own live SSH sessions and close each session exactly once."""

    # In-memory live sessions keyed by opaque IDs; never persisted or replayed.
    _sessions: dict[UUID, SshSession]

    def close(self, session_id: UUID) -> None:
        """Close and remove one live session.

        Raises:
            SessionNotFoundError: The identifier is not currently owned.
        """
```

### 函数与方法 docstring

- 每个生产函数和方法都必须有简洁 docstring，包括私有函数、私有方法、`__init__`、classmethod 和 staticmethod。
- Docstring 按实际复杂度说明：职责、重要参数、返回值、明确抛出的异常、副作用、资源所有权和安全约束。
- 一行显然可读的实现不需要冗长模板，但仍需一句话说明其契约；复杂函数使用 `Args`、`Returns`、`Raises` 等分段。
- Override 或 framework callback 仍需说明本项目附加的不变量；若框架签名禁止类型注解，在相邻注释中解释例外。
- 不写与实现脱节的历史说明。行为变化时在同一变更中更新 docstring。

### 测试注释

- `test_*` 用例可以用完整、可读的测试名表达 Given/When/Then，不强制添加重复 docstring。
- Fixture、helper、Fake/Stub class、测试数据 builder 和不明显的内部方法必须有 docstring，说明模拟的契约边界。
- 复杂测试只有在能改善导航时使用 Arrange / Act / Assert 注释；简单测试不添加仪式化噪声。
- 测试注释说明为什么该条件构成回归证据，不解释 `assert` 的字面含义。

### 类型、命名与接口

- 函数和方法的所有参数及返回值使用类型注解；局部变量在类型无法从赋值清楚推断时注解。
- 使用具体领域名称，避免 `data`、`info`、`manager`、`helper` 等无边界命名。
- `Any`、裸 `dict`、裸 `tuple` 和 `# type: ignore` 仅在边界确实无法建模时使用，并在相邻注释说明原因和收敛点。
- Pydantic model 对跨边界 payload 使用明确字段、strict validation 和禁止未知字段；敏感字段的 description 不得包含示例秘密。
- 公共接口保持小而稳定；不为了测试暴露本应私有的可变状态。

### 异常与失败传播

- 预期领域失败使用具体 exception type 和稳定 error code；调用边界统一映射安全 message。
- 禁止 `except Exception: pass`、空返回、默认成功对象或 broad catch 后继续执行。
- 未知失败保留失败语义并在完整日志中留下 code、message 与 traceback；不得通过 fallback 数据掩盖根因。
- Cleanup 失败不得覆盖更早的业务失败；需要汇总时明确保存并重新抛出首个失败。
- 异常 message、`repr` 和 traceback 会被 Logger 完整记录；产生异常的调用点负责避免主动拼入 credential、runtime key 或 raw secret frame。

### Async、取消与资源

- 创建 async task 时立即记录 owner；task 必须被 await、cancel 后 await，或由明确 registry 管理到结束。
- 取消作为控制流显式传播，不得被 broad exception 捕获后转换为普通失败或成功。
- 网络、进程和远程操作使用明确 timeout policy；禁止隐藏的无限等待。
- connection、channel、PTY、file、database、exporter、lock 和 secret buffer 使用 context manager、`try/finally` 或唯一 manager 确定性释放。
- 复杂关闭顺序写注释说明“为何按此顺序”，并测试正常、失败和取消路径。

### 敏感数据、日志与持久化

- password、private key、passphrase、runtime key 和 secret frame 不得由调用点主动写入日志、异常、Trace attribute 或普通数据库列；Logger 不提供自动过滤。
- 日志写 stderr，完整保留 message、结构化字段、exception traceback 和 HTTP response body；stdout 只用于 Protocol frame。
- 可变 secret buffer 用完后主动覆盖；避免不必要的 `bytes`/`str` 拷贝和长生命周期闭包捕获。
- 新增持久化字段前明确分类、加密、关联数据、migration、删除和自检策略。
- Audit 与 Trace 只保存允许的结构化元数据，不把 payload 当作“调试信息”落盘。

### 复杂度与可读性

- 函数保持单一决策目标；验证、I/O、状态变更和错误映射同时增长时拆分 focused 函数。
- 优先显式控制流，不使用难以 Review 的 metaprogramming、动态 attribute 注入或隐式全局注册。
- 注释描述不明显的不变量、协议原因、并发竞态和安全取舍，不为每个分支添加表面解释。
- 修改复杂旧代码时，只在当前任务触达范围内改善结构和注释，不进行无关全库重构。

## 长期约束

- 新增 Python 文件必须完整遵循本文档。
- 修改已有文件时，新增或实质修改的 module、class、class field、function 和 method 必须符合本文档。
- 本次建立规范不要求批量补齐全部历史代码；若旧代码直接阻碍当前 Review、资源判断或安全理解，应在当前任务范围内补齐。
- 注释语言保持与所在模块既有风格一致；代码标识、Protocol、command、error code 和第三方 API 名保持原文。
- Let it crash 适用于 Python：失败必须显式、可定位、可测试，不以默认值、静默重试或结果后处理代替根因修复。

## 项目命令

从 `backend/` 运行当前 Python 测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

运行单个文件或用例：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\path\test_file.py -v
.\.venv\Scripts\python.exe -m pytest tests\path\test_file.py::test_name -v
```

仓库当前没有已配置的 Python lint、format 或静态类型命令；Review 不得声称这些检查已经运行。

## 验证要求

- 行为变更必须有最小相关 Pytest，先验证失败路径，再验证成功路径和资源回收。
- Docstring/field 注释应与签名、raise path 和实际 owner 对照，不以“存在注释”代替准确性。
- 类型边界检查 unknown field、`None`、空值、越界、canonical encoding 和序列化 round-trip。
- async 测试检查 cancel、timeout、task 泄漏、double close 和 cleanup failure。
- 安全测试检查业务调用点不主动把 secret marker 交给 Logger，且 marker 不进入 SQLite、Trace 或 response；日志格式器测试反向确认传入内容不会被删除。历史 `artifact_metadata` schema 不代表仍存在 Artifact runtime。
- 仅文档变更至少执行链接、标题、占位标记和 `git diff --check`；不声称 Python 行为已被重新验收。

## 何时需要更新本文档

以下变化必须同步更新本文档：

- 团队决定新的 docstring、field comment、typing 或 Review 标准；
- 引入 Ruff、Black、Mypy、Pyright、formatter、lint 或类型检查门禁；
- Python 版本、Pydantic 使用模式、异常模型或 async/resource policy 改变；
- 敏感数据、日志或持久化 Review 标准改变；
- Python 源码或测试的目录范围改变。

普通业务实现只需遵循本文档，无需每次修改本文档；任务结束仍要完成 AGENTS 影响检查。

## Python Code Review 检查清单

- [ ] 模块、类、类字段、函数和方法的 docstring/注释符合触达范围要求，且内容与实现一致。
- [ ] 参数、返回值、关键局部变量和公共模型有准确类型；不存在无解释的 `Any` 或裸容器。
- [ ] 模块和接口职责单一，未新增循环依赖、全局可变状态或 convenience dumping ground。
- [ ] 失败使用具体 exception/error code，未知失败未被吞掉、降级或转换为成功形态。
- [ ] async task、取消、timeout、connection/channel/process/database 和 cleanup owner 清楚且有测试。
- [ ] 业务调用点未主动把 secret 交给日志、异常、Trace、response 或持久化；Logger 对已传入内容不做过滤。
- [ ] migration、Audit、Trace 和加密约束在涉及存储时已核对。
- [ ] 测试覆盖正常、失败、边界、取消/清理路径，Fake/fixture 清楚说明模拟契约。
- [ ] 已读取并检查相关根、领域和局部 `AGENTS.md`；长期事实变化已同步更新唯一真源。
