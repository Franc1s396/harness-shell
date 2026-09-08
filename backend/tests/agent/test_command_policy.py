"""有限只读策略的允许形式和 Shell 绕过边界。"""

import pytest

from harness_shell_sidecar.agent.command_policy import classify_command


@pytest.mark.parametrize("command", [
    "pwd", "pwd -L", "pwd -P", "whoami", "id", "id -un", "id -G",
    "uname -a", "uptime", "ls", "ls -lah /var/log", "ls -d",
    "ls '/目录 with spaces'", "cat -- /etc/hosts", "cat /etc/hosts",
    "head -n 100 /var/log/app.log", "tail -n 1000 /var/log/app.log",
    "head /etc/hosts", "  pwd  ",
])
def test_explicit_read_only_forms(command: str) -> None:
    assert classify_command(command) == "AUTO_READ_ONLY"


@pytest.mark.parametrize("command", [
    "pwd; touch /tmp/x", "cat $(touch /tmp/x)", "cat `whoami`", "ls | cat",
    "tail -f /var/log/app.log", "git status", "find /tmp -delete", "ls > /tmp/x",
    "env ls", "/bin/ls", "pwd\n", "pwd\t", "cat", "cat -", "head -n 0 /a",
    "head -n 1001 /a", "head -n +1 /a", "ls -aa", "ls -l -a", "ls /a /b",
    "ls ''", "ls 'unterminated", "ls a'b'", "'pwd'", 'ls "a"',
    "ls *.log", "ls $HOME", "LC_ALL=C ls", "ls /a &", "ls /a #comment",
    "ls /a\\b", "ls\u00a0/a", "ls 'a\nb'", "ls 'a;touch x'", "",
    "sudo pwd", "python script.py", "id someone", "uname --all", "pwd -LP",
])
def test_unknown_or_shell_forms_require_approval(command: str) -> None:
    assert classify_command(command) == "REQUIRE_APPROVAL"


def test_existing_hard_block_takes_precedence() -> None:
    assert classify_command("rm -rf /") == "BLOCKED"
