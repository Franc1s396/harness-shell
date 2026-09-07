"""迁移依赖包内绝对资源路径，不借用工作目录。"""

from pathlib import Path
import shutil
import pytest
from harness_shell_sidecar.storage import StorageSelfCheckFailed
from harness_shell_sidecar.storage import migration_runner as runner


def test_resource_manifest_and_unrelated_cwd(tmp_path: Path, monkeypatch) -> None:
    """源码资源完整，切换 cwd 后仍能迁移。"""
    directory = runner.migration_resource_dir()
    for name in ["env.py", "script.py.mako", "versions/0001_initial.py"]:
        assert (directory / name).is_file()
    monkeypatch.chdir(tmp_path)
    runner.upgrade_database(tmp_path / "runtime.sqlite3")


def test_missing_revision_fails_before_database_creation(tmp_path: Path, monkeypatch) -> None:
    """丢失版本资源必须直接失败，不新建无版本业务库。"""
    directory = tmp_path / "alembic"
    shutil.copytree(runner.migration_resource_dir(), directory)
    (directory / "versions/0001_initial.py").unlink()
    monkeypatch.setattr(runner, "migration_resource_dir", lambda: directory)
    path = tmp_path / "runtime.sqlite3"
    with pytest.raises(StorageSelfCheckFailed, match="one head"):
        runner.upgrade_database(path)
    assert not path.exists()



def test_multiple_heads_fail_before_database_creation(tmp_path, monkeypatch):
    """迁移图分叉未合并时拒绝启动，不猜测要执行的 head。"""
    from .test_migration_transactions import add_revision
    directory = tmp_path / "alembic"
    shutil.copytree(runner.migration_resource_dir(), directory)
    add_revision(directory, "branch_a", "0001_initial", "pass")
    add_revision(directory, "branch_b", "0001_initial", "pass")
    monkeypatch.setattr(runner, "migration_resource_dir", lambda: directory)
    path = tmp_path / "runtime.sqlite3"
    with pytest.raises(StorageSelfCheckFailed, match="one head"):
        runner.upgrade_database(path)
    assert not path.exists()
