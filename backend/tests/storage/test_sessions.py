"""短 Session 的提交、回滚与关闭语义。"""

from pathlib import Path

import pytest

from harness_shell_sidecar.storage import RuntimeDatabase, PlaintextRecord, PlaintextRecordStore


def test_write_failure_rolls_back_record(tmp_path: Path) -> None:
    assert hasattr(RuntimeDatabase, "open"), "ORM Session API is missing"
    database = RuntimeDatabase.open(tmp_path / "runtime.sqlite3")
    try:
        with pytest.raises(RuntimeError, match="injected"):
            with database.write_session() as session:
                PlaintextRecordStore(session).put(PlaintextRecord("test", "one", 1, b"payload"))
                session.flush()
                raise RuntimeError("injected")
        with database.read_session() as session:
            assert PlaintextRecordStore(session).get("test", "one") is None
    finally:
        database.close()


def test_successful_write_survives_reopen(tmp_path: Path) -> None:
    assert hasattr(RuntimeDatabase, "open"), "ORM Session API is missing"
    path = tmp_path / "runtime.sqlite3"
    database = RuntimeDatabase.open(path)
    source = PlaintextRecord("test", "one", 1, b"payload")
    with database.write_session() as session:
        PlaintextRecordStore(session).put(source)
    database.close()
    database = RuntimeDatabase.open(path)
    try:
        with database.read_session() as session:
            assert PlaintextRecordStore(session).get("test", "one") == source
    finally:
        database.close()



def test_read_rolls_back_and_sessions_are_not_reused(tmp_path: Path) -> None:
    """读取作用域误写不得落盘，结束后的 Session 不可重新激活。"""
    from sqlalchemy.exc import InvalidRequestError
    from harness_shell_sidecar.storage import StorageSelfCheckFailed
    database = RuntimeDatabase.open(tmp_path / "runtime.sqlite3")
    try:
        with database.read_session() as first:
            PlaintextRecordStore(first).put(PlaintextRecord("test", "one", 1, b"discard"))
            with pytest.raises(StorageSelfCheckFailed, match="active sessions"):
                database.close()
        with database.read_session() as second:
            assert second is not first
            assert PlaintextRecordStore(second).get("test", "one") is None
        with pytest.raises(InvalidRequestError):
            first.begin()
    finally:
        database.close()
    with pytest.raises(StorageSelfCheckFailed, match="closed"):
        with database.read_session():
            pass


def test_base_exception_rolls_back_and_releases_session(tmp_path: Path) -> None:
    """取消类异常也必须回滚并释放借用资源。"""
    database = RuntimeDatabase.open(tmp_path / "runtime.sqlite3")
    try:
        with pytest.raises(KeyboardInterrupt):
            with database.write_session() as session:
                PlaintextRecordStore(session).put(PlaintextRecord("test", "one", 1, b"discard"))
                raise KeyboardInterrupt()
        with database.read_session() as session:
            assert PlaintextRecordStore(session).get("test", "one") is None
    finally:
        database.close()


def test_constraint_error_hides_secret_parameters(tmp_path: Path, caplog) -> None:
    """约束错误与 SQLAlchemy 日志不暴露业务绑定参数。"""
    from sqlalchemy.exc import IntegrityError
    from harness_shell_sidecar.storage.orm import RuntimeRecordRow
    database = RuntimeDatabase.open(tmp_path / "runtime.sqlite3")
    marker = "secret-parameter-sentinel"
    try:
        with pytest.raises(IntegrityError) as caught:
            with database.write_session() as session:
                session.add(RuntimeRecordRow(record_type=marker, record_id="one", schema_version=0,
                                             payload=marker.encode(), created_at="now", updated_at="now"))
        assert marker not in str(caught.value)
        assert marker not in caplog.text
    finally:
        database.close()



def test_session_cleanup_error_does_not_replace_business_failure(tmp_path, monkeypatch):
    """关闭出错保留业务首错与清理备注，事务仍回滚。"""
    database = RuntimeDatabase.open(tmp_path / "runtime.sqlite3")
    try:
        with pytest.raises(ValueError, match="business failure") as caught:
            with database.write_session() as session:
                original = session.close
                def failed_close():
                    """真实关闭之后注入清理异常。"""
                    original()
                    raise OSError("cleanup failure")
                monkeypatch.setattr(session, "close", failed_close)
                PlaintextRecordStore(session).put(PlaintextRecord("test", "one", 1, b"discard"))
                raise ValueError("business failure")
        assert any("OSError" in note for note in caught.value.__notes__)
        with database.read_session() as session:
            assert PlaintextRecordStore(session).get("test", "one") is None
    finally:
        database.close()
