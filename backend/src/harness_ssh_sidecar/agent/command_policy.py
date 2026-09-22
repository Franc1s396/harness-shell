"""只识别有限字面量语法的只读命令；未知形式交给人工审核。"""

from typing import Literal

from .tools import DANGEROUS_COMMAND_PATTERN

CommandDisposition = Literal["AUTO_READ_ONLY", "REQUIRE_APPROVAL", "BLOCKED"]
_PUNCTUATION = frozenset("_./:@%+=,-")
_SIMPLE_OPTIONS = {
    "pwd": {(), ("-L",), ("-P",)},
    "whoami": {()},
    "id": {(), ("-u",), ("-g",), ("-G",), ("-un",), ("-gn",)},
    "uname": {(), ("-a",), ("-s",), ("-r",), ("-m",), ("-n",)},
    "uptime": {()},
}


def classify_command(command: str) -> CommandDisposition:
    """返回策略类别，绝不修改原始命令或执行远端探测。"""
    if DANGEROUS_COMMAND_PATTERN.search(command) is not None:
        return "BLOCKED"
    tokens = _scan_literals(command)
    if tokens is None or not _matches_read_only_form(tokens):
        return "REQUIRE_APPROVAL"
    return "AUTO_READ_ONLY"


def _literal_character(char: str, *, quoted: bool) -> bool:
    """引用只增加空格和 Unicode 字母数字，不接受控制符或 Shell 展开。"""
    return char in _PUNCTUATION or (
        char.isalnum() and (quoted or char.isascii())
    ) or (quoted and char == " ")


def _scan_literals(command: str) -> tuple[str, ...] | None:
    """完整消费有限语法；不属于该语法的输入明确要求审核。"""
    tokens: list[str] = []
    index = 0
    while index < len(command):
        if command[index] == " ":
            index += 1
            continue
        quoted = command[index] == "'"
        if quoted and not tokens:
            return None
        if quoted:
            index += 1
        start = index
        while index < len(command):
            char = command[index]
            if (quoted and char == "'") or (not quoted and char == " "):
                break
            if not _literal_character(char, quoted=quoted):
                return None
            index += 1
        token = command[start:index]
        if not token:
            return None
        if quoted:
            if index == len(command):
                return None
            index += 1
            if index < len(command) and command[index] != " ":
                return None
        tokens.append(token)
    return tuple(tokens) if tokens else None


def _is_path(value: str) -> bool:
    """路径不可解释为选项或 stdin；字面量已由扫描器校验。"""
    return bool(value) and not value.startswith("-")


def _matches_read_only_form(tokens: tuple[str, ...]) -> bool:
    """逐命令匹配完整参数，未知选项和多余参数不会放行。"""
    name, *values = tokens
    args = tuple(values)
    if name in _SIMPLE_OPTIONS:
        return args in _SIMPLE_OPTIONS[name]
    if name == "ls":
        if args and args[0].startswith("-"):
            flags = args[0][1:]
            if not flags or not set(flags) <= set("aAlhnd") or len(set(flags)) != len(flags):
                return False
            args = args[1:]
        return not args or (len(args) == 1 and _is_path(args[0]))
    if name == "cat":
        if args and args[0] == "--":
            args = args[1:]
        return len(args) == 1 and _is_path(args[0])
    if name in {"head", "tail"}:
        if len(args) == 1:
            return _is_path(args[0])
        return (
            len(args) == 3 and args[0] == "-n"
            and args[1].isascii() and args[1].isdecimal()
            and 1 <= int(args[1]) <= 1000 and _is_path(args[2])
        )
    return False
