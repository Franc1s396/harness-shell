"""连接及 Provider 与其凭据在真实短事务中共同提交或回滚。"""

from uuid import uuid4
import pytest
from sqlalchemy.exc import IntegrityError
from harness_shell_sidecar.storage import RuntimeDatabase, PlaintextRecordStore
from harness_shell_sidecar.credentials import CredentialRepository
from harness_shell_sidecar.connections import ConnectionRepository, ConnectionRepositoryError
from harness_shell_sidecar.agent.api_configs import ApiConfigRepository, ApiConfigRepositoryError
from harness_shell_sidecar.agent.conversations import ConversationRepository
from ..agent.conftest import valid_api_config_input
from .test_repository import profile_input, candidate


def test_constraint_failure_rolls_back_new_credential(tmp_path):
    """凭据已经写入后，真实 CHECK 失败不得留下孤立记录。"""
    database = RuntimeDatabase.open(tmp_path / "runtime.sqlite3")
    try:
        with pytest.raises(IntegrityError):
            with database.write_session() as session:
                store = PlaintextRecordStore(session)
                identity = CredentialRepository(store).create("ssh_password", "test-secret")
                invalid = profile_input("prod", credential_id=identity).model_copy(update={"port": 0})
                ConnectionRepository(session).create(invalid)
        with database.read_session() as session:
            assert PlaintextRecordStore(session).list_ids("credential") == ()
            assert ConnectionRepository(session).list() == []
    finally:
        database.close()


def test_failed_host_key_replacement_preserves_active_key(tmp_path):
    """旧键已停用后插入冲突，整个替换事务回滚为原活动键。"""
    database = RuntimeDatabase.open(tmp_path / "runtime.sqlite3")
    try:
        with database.write_session() as session:
            repository = ConnectionRepository(session)
            profile = repository.create(profile_input("prod"))
            first = repository.trust_first_host_key(candidate(profile.connection_id, b"first"))
        with pytest.raises(ConnectionRepositoryError, match="could not be persisted"):
            with database.write_session() as session:
                ConnectionRepository(session).replace_host_key(
                    candidate(profile.connection_id, b"first"), first.fingerprint_sha256)
        with database.read_session() as session:
            assert ConnectionRepository(session).active_host_key(profile.connection_id) == first
    finally:
        database.close()


def test_run_reference_rejects_provider_delete_and_restores_credential(tmp_path):
    """先删除凭据再触发 Run 外键限制，原 Provider 与凭据都保留。"""
    database = RuntimeDatabase.open(tmp_path / "runtime.sqlite3")
    try:
        with database.write_session() as session:
            store = PlaintextRecordStore(session)
            identity = CredentialRepository(store).create("api_key", "test-key")
            config = ApiConfigRepository(session).create(
                valid_api_config_input().model_copy(update={"api_key_credential_id": identity}))
            history = ConversationRepository(session, store)
            history.start_run(history.create_conversation(), uuid4(), config.api_config_id)
        with pytest.raises(ApiConfigRepositoryError):
            with database.write_session() as session:
                CredentialRepository(PlaintextRecordStore(session)).delete(identity)
                ApiConfigRepository(session).delete(config.api_config_id)
        with database.read_session() as session:
            assert ApiConfigRepository(session).get(config.api_config_id) == config
            secret = CredentialRepository(PlaintextRecordStore(session)).resolve(identity, "api_key")
            assert secret == b"test-key"
            secret[:] = b"\x00" * len(secret)
    finally:
        database.close()



def test_failed_credential_replacement_keeps_original_profile(tmp_path):
    """替换凭据后配置 CHECK 失败，原凭据与配置保持，替换凭据不残留。"""
    database = RuntimeDatabase.open(tmp_path / "runtime.sqlite3")
    try:
        with database.write_session() as session:
            identity = CredentialRepository(PlaintextRecordStore(session)).create("ssh_password", "old-secret")
            profile = ConnectionRepository(session).create(profile_input("prod", credential_id=identity))
        with pytest.raises(IntegrityError):
            with database.write_session() as session:
                credentials = CredentialRepository(PlaintextRecordStore(session))
                replacement = credentials.create("ssh_password", "new-secret")
                credentials.delete(identity)
                invalid = profile_input("prod", credential_id=replacement).model_copy(update={"port": 0})
                ConnectionRepository(session).update(profile.connection_id, invalid)
        with database.read_session() as session:
            assert ConnectionRepository(session).get(profile.connection_id) == profile
            assert PlaintextRecordStore(session).list_ids("credential") == (str(identity),)
    finally:
        database.close()
