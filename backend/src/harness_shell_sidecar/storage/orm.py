"""SQLite STRICT 业务表的显式 ORM 映射；领域对象在仓库边界转换。"""

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, LargeBinary, REAL, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """共享业务 metadata，不负责生产建库或资源生命周期。"""


class RuntimeRecordRow(Base):
    """映射 runtime_records 持久化记录，仅由当前操作 Session 持有。"""
    __tablename__ = 'runtime_records'
    __table_args__ = (
        CheckConstraint('schema_version > 0'),
        {"sqlite_strict": True},
    )
    # 通用记录命名空间，隔离凭据、消息与恢复记录。
    record_type: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    # 同一记录类型内的正文标识，与类型组成复合键。
    record_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    # 正文序列化版本，独立于 Alembic 数据库版本。
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 可能含明文凭据或消息的原始字节，不进入日志。
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # 首次创建的 UTC 时间，更新时保留。
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    # 最近一次成功 mutation 的 UTC 时间。
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

class ConnectionProfileRow(Base):
    """映射 connection_profiles 持久化记录，仅由当前操作 Session 持有。"""
    __tablename__ = 'connection_profiles'
    __table_args__ = (
        CheckConstraint('length(display_name) BETWEEN 1 AND 80'),
        CheckConstraint('group_name IS NULL OR length(group_name) BETWEEN 1 AND 80'),
        CheckConstraint('length(host) BETWEEN 1 AND 255'),
        CheckConstraint('port BETWEEN 1 AND 65535'),
        CheckConstraint('length(username) BETWEEN 1 AND 128'),
        CheckConstraint("auth_kind IN ('password', 'private_key')"),
        CheckConstraint('favorite IN (0, 1)'),
        CheckConstraint('version BETWEEN 1 AND 9007199254740991'),
        CheckConstraint('proxy_jump_id IS NULL OR proxy_jump_id <> connection_id'),
        CheckConstraint("auth_kind = 'private_key' OR passphrase_credential_id IS NULL"),
        {"sqlite_strict": True},
    )
    # 连接配置标识，作为 Host Key 与跳板引用的关联键。
    connection_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    # 用户可编辑的非秘密显示名称。
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    # 可空的连接列表分组名称。
    group_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 用户声明的 SSH 目标地址。
    host: Mapped[str] = mapped_column(Text, nullable=False)
    # 由 CHECK 限定范围的 SSH 目标端口。
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    # SSH 远端登录用户名。
    username: Mapped[str] = mapped_column(Text, nullable=False)
    # SSH 认证方式，约束可用的凭据组合。
    auth_kind: Mapped[str] = mapped_column(Text, nullable=False)
    # 认证秘密的不透明引用，随所属配置 mutation 原子维护。
    credential_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 可空的私钥口令引用，不能用于密码认证。
    passphrase_credential_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 可空的单层跳板引用，数据库禁止悬空与自引用。
    proxy_jump_id: Mapped[str | None] = mapped_column(Text, ForeignKey('connection_profiles.connection_id', ondelete='RESTRICT'), nullable=True)
    # 以受 CHECK 限制的 0/1 整数保存收藏状态。
    favorite: Mapped[int] = mapped_column(Integer, nullable=False)
    # 首次创建的 UTC 时间，更新时保留。
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    # 最近一次成功 mutation 的 UTC 时间。
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    # JS-safe 单调配置版本，拒绝凭据解析后的陈旧配置。
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text('1'))

class HostKeyRow(Base):
    """映射 host_keys 持久化记录，仅由当前操作 Session 持有。"""
    __tablename__ = 'host_keys'
    __table_args__ = (
        CheckConstraint("status IN ('active', 'replaced')"),
        UniqueConstraint('connection_id', 'fingerprint_sha256'),
        {"sqlite_strict": True},
    )
    # 单条 Host Key 信任历史的唯一标识。
    host_key_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    # 连接配置标识，作为 Host Key 与跳板引用的关联键。
    connection_id: Mapped[str] = mapped_column(Text, ForeignKey('connection_profiles.connection_id', ondelete='CASCADE'), nullable=False)
    # 已确认 SSH 公钥的算法名称。
    key_algorithm: Mapped[str] = mapped_column(Text, nullable=False)
    # 已确认公钥指纹，同一连接内唯一。
    fingerprint_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    # 已确认的公开密钥原始字节，不是用户私钥。
    public_key_openssh: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # 受 CHECK 限制的持久化生命周期状态。
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # 用户显式确认 Host Key 的 UTC 时间。
    confirmed_at: Mapped[str] = mapped_column(Text, nullable=False)
    # 旧 Host Key 被替换的时间，活动键为空。
    replaced_at: Mapped[str | None] = mapped_column(Text, nullable=True)

class ModelApiConfigRow(Base):
    """映射 model_api_configs 持久化记录，仅由当前操作 Session 持有。"""
    __tablename__ = 'model_api_configs'
    __table_args__ = (
        CheckConstraint('context_window_size > 0 AND context_window_size <= 9007199254740991'),
        CheckConstraint('context_compaction_threshold_ratio > 0 AND context_compaction_threshold_ratio < 1'),
        CheckConstraint('max_output_tokens > 0 AND max_output_tokens < context_window_size'),
        CheckConstraint('length(display_name) BETWEEN 1 AND 80'),
        CheckConstraint("api_type IN ('CHAT_COMPLETIONS', 'RESPONSES')"),
        CheckConstraint('length(base_url) BETWEEN 1 AND 2048'),
        CheckConstraint('length(model) BETWEEN 1 AND 255'),
        CheckConstraint('enabled IN (0, 1)'),
        {"sqlite_strict": True},
    )
    # 该配置声明的上下文 token 上限。
    context_window_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text('128000'))
    # 触发上下文压缩的预算占用比例。
    context_compaction_threshold_ratio: Mapped[float] = mapped_column(REAL, nullable=False, server_default=text('0.75'))
    # 为模型输出预留的 token 预算。
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text('8192'))
    # Provider 配置标识，Run 引用存在时禁止删除。
    api_config_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    # 用户可编辑的非秘密显示名称。
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    # 已校验的 Provider 协议类型。
    api_type: Mapped[str] = mapped_column(Text, nullable=False)
    # Provider 请求基地址，不附带 API Key。
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Provider 模型名称，轮次开始时冻结其配置。
    model: Mapped[str] = mapped_column(Text, nullable=False)
    # 所属 API Key 的不透明凭据引用，不包含秘密正文。
    api_key_credential_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 以受 CHECK 限制的 0/1 整数保存启用状态。
    enabled: Mapped[int] = mapped_column(Integer, nullable=False)
    # 首次创建的 UTC 时间，更新时保留。
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    # 最近一次成功 mutation 的 UTC 时间。
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

class AgentConversationRow(Base):
    """映射 agent_conversations 持久化记录，仅由当前操作 Session 持有。"""
    __tablename__ = 'agent_conversations'
    __table_args__ = (
        {"sqlite_strict": True},
    )
    # 持久化会话标识，决定 Run、消息和摘要归属。
    conversation_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    # 首次创建的 UTC 时间，更新时保留。
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    # 最近一次成功 mutation 的 UTC 时间。
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

class AgentRunRow(Base):
    """映射 agent_runs 持久化记录，仅由当前操作 Session 持有。"""
    __tablename__ = 'agent_runs'
    __table_args__ = (
        CheckConstraint("status IN ('RUNNING','COMPLETED','FAILED','LIMIT_REACHED','CANCELLED')"),
        CheckConstraint('react_iteration BETWEEN 0 AND 128'),
        {"sqlite_strict": True},
    )
    # 持久化轮次标识，关联该轮次的状态与消息。
    agent_run_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    # 前端用户消息的稳定标识；同一消息的多次重试拥有不同 Run。
    user_message_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    # 持久化会话标识，决定 Run、消息和摘要归属。
    conversation_id: Mapped[str] = mapped_column(Text, ForeignKey('agent_conversations.conversation_id', ondelete='NO ACTION'), nullable=False)
    # 本轮冻结的 SSH 会话标识，不用于自动恢复连接。
    ssh_session_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Provider 配置标识，Run 引用存在时禁止删除。
    api_config_id: Mapped[str] = mapped_column(Text, ForeignKey('model_api_configs.api_config_id', ondelete='NO ACTION'), nullable=False)
    # 受 CHECK 限制的持久化生命周期状态。
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # 当前轮次已完成的工具调用循环数。
    react_iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    # 允许公开的稳定失败码，不保存异常正文。
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Run 进入运行态的 UTC 时间。
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    # 终态提交时间，运行中保持为空。
    ended_at: Mapped[str | None] = mapped_column(Text, nullable=True)

class AgentMessageRow(Base):
    """映射 agent_messages 持久化记录，仅由当前操作 Session 持有。"""
    __tablename__ = 'agent_messages'
    __table_args__ = (
        CheckConstraint('sequence > 0'),
        CheckConstraint("message_type IN ('SYSTEM','HUMAN','AI','TOOL')"),
        UniqueConstraint('conversation_id', 'sequence'),
        UniqueConstraint('record_id'),
        {"sqlite_strict": True},
    )
    # 消息元数据标识，不替代明文记录复合键。
    message_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    # 持久化会话标识，决定 Run、消息和摘要归属。
    conversation_id: Mapped[str] = mapped_column(Text, ForeignKey('agent_conversations.conversation_id', ondelete='NO ACTION'), nullable=False)
    # 会话内单调且唯一的消息顺序号。
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # 严格历史序列化对应的消息角色类型。
    message_type: Mapped[str] = mapped_column(Text, nullable=False)
    # 同一记录类型内的正文标识，与类型组成复合键。
    record_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 可空的工具调用关联标识，用于验证历史边界。
    tool_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 持久化轮次标识，关联该轮次的状态与消息。
    agent_run_id: Mapped[str] = mapped_column(Text, ForeignKey('agent_runs.agent_run_id', ondelete='NO ACTION'), nullable=False)
    # 首次创建的 UTC 时间，更新时保留。
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

class AgentContextSummaryRow(Base):
    """映射 agent_context_summaries 持久化记录，仅由当前操作 Session 持有。"""
    __tablename__ = 'agent_context_summaries'
    __table_args__ = (
        CheckConstraint('revision > 0'),
        CheckConstraint('covered_through_sequence > 0'),
        CheckConstraint('length(trim(summary_text)) > 0'),
        {"sqlite_strict": True},
    )
    # 持久化会话标识，决定 Run、消息和摘要归属。
    conversation_id: Mapped[str] = mapped_column(Text, ForeignKey('agent_conversations.conversation_id', ondelete='CASCADE'), primary_key=True, nullable=False)
    # 摘要乐观并发版本，用于拒绝陈旧压缩结果。
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # 摘要已覆盖的最后一条历史消息序号。
    covered_through_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # 可能包含历史敏感内容的明文摘要，不进入日志。
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 生成摘要的持久化 Run 引用。
    source_run_id: Mapped[str] = mapped_column(Text, ForeignKey('agent_runs.agent_run_id', ondelete='NO ACTION'), nullable=False)
    # 首次创建的 UTC 时间，更新时保留。
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    # 最近一次成功 mutation 的 UTC 时间。
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

Index("one_active_host_key_per_connection", HostKeyRow.connection_id, unique=True,
      sqlite_where=HostKeyRow.status == "active")
