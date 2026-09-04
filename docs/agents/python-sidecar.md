# Python Backend Guide

## 入口与生命周期

Python Backend 有两个显式入口，均只监听 `127.0.0.1`：

```powershell
python -m harness_shell_sidecar serve --port 8765 --data-dir E:\absolute\dev-data
harness-shell-sidecar.exe desktop --port 0 --data-dir E:\absolute\data --control-read-handle <n> --ready-write-handle <n>
```

`serve` 只用于源码、Python-only 和 SSH Lab；port 必须为 1..65535。`desktop` 只由 Launcher 使用，要求 port 0、绝对 data directory 与两个 inherited Windows handles。不得增加 host override、默认 data dir、自动端口 fallback 或第二 transport。

FastAPI ASGI lifespan 从 `RuntimeSettings` 创建唯一 `RuntimeResources`，完成 database/repository/SSH/PTY/Manual SFTP/Agent/dispatcher 初始化后才接受请求。失败不发布部分 owner。shutdown 只执行一次并继续清理所有 owner，同时保留首个错误。

## 目录职责

- `web/`：Uvicorn、lifespan、typed HTTP/WebSocket gateway、Agent SSE encoder/session/startup barrier、Problem Details 与 OpenAPI export。
- `runtime/`：settings、resources、dispatcher、request context 与 Desktop control pipe。
- `storage/`：schema-v6-only plaintext database 与 generic plaintext records。
- `credentials/`：request envelope 解封、kind-checked plaintext credential repository、temporary secret cleanup；不提供独立 credential mutation route。
- `ssh/`、`terminal/`：SSH/ProxyJump/Host Key/PTY owner。
- `manual_sftp/`：remote-only listing、mutation、temporary/commit/abort/recovery；不得读取或写入本地用户文件。
- `agent/`：Provider metadata、Python credential lookup/zeroize、conversation/run/message、durable stream lifecycle、只发布最终可见文本的 model gateway 与 strict `execute_command` loop。

## 存储

`RuntimeDatabase.open_plaintext` 只接受不存在的新数据库或 exact schema v6。旧 schema、未知对象、约束不匹配或 self-check 失败必须在修改现有文件前退出。没有旧版本 sequential migration、兼容读取或自动备份。

schema v6 的 `runtime_records.payload` 与 credential records 是 plaintext。不要通过命名、注释或文档暗示 at-rest encryption。新增持久化内容时必须明确字段、nullability、删除、敏感性、schema self-check 和测试；没有业务读取或导出闭环的诊断数据不得新增 SQLite 表。

## Protocol 约束

- route 在进入 dispatcher 前完成 strict Pydantic/header/media-type/body-size 校验。
- Agent turn route 要求 `Accept: text/event-stream`。worker 在 shared dispatcher 内取得 conversation lock、创建 durable RUNNING Run、建立 capacity 64 queue 并入队 started 后才允许 HTTP 200；consumer 发送 terminal frame 后才释放 dispatcher request ID/capacity，断连与 shutdown 都必须取消并 await worker。
- Agent SSE 固定 frame 65,536 bytes、body 4,194,304 bytes、terminal reserve 65,536 bytes；producer awaited put，不 drop/merge/truncate。完整 Agent result 的 1,048,576-byte 逻辑预算继续生效。
- Manual SFTP chunk 固定最大 262,144 bytes，使用 raw `application/octet-stream` 和 exact offset/operation identity。
- WebSocket inbound/outbound queue capacity 固定且不 drop/merge；只有 strict ping 刷新 heartbeat。
- credential、command、model response body/text、stdout/stderr、SFTP bytes 和 HTTP body 不得主动进入日志或 Problem detail；Provider failure 日志只允许 stable metadata。
- Connection 与 Provider handler 必须在同一 `RuntimeDatabase` 事务中维护业务记录及其拥有的 credential；更新省略 envelope 时保留现有引用，删除业务记录时同步删除 credential。
- 未知字段、stale profile/session、duplicate owner、取消和 cleanup failure 都显式失败，不重放远程 mutation。

## 验证

```powershell
backend\.venv\Scripts\python.exe -m pytest backend -q
powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\build_sidecar.ps1
```

Python tests证明源码行为；packaged smoke证明本次 `.exe` 的局部 loopback 行为；SSH Lab、Desktop、真实 Provider 与生产主机必须分别验收。
