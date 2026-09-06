"""加载显式随包提供的编码资源，不查询缓存或执行网络 I/O。"""
from __future__ import annotations
import base64
import hashlib
import json
import sys
from pathlib import Path
import tiktoken
from .context_models import ContextError


def tokenizer_resource_dir() -> Path:
    """选择唯一的源码构建或打包资源位置。"""
    if getattr(sys, "frozen", False):
        return Path(__file__).parent / "tokenizer_data"
    return Path(__file__).resolve().parents[3] / "build" / "tokenizer"


def load_local_encoding(resource_dir: Path, encoding_name: str) -> tiktoken.Encoding:
    """接受 Agent 请求前校验本地 ranks 和固定样本答案。"""
    try:
        # 1. 只接受约定编码，并从明确的本地目录读取资源，不访问网络或用户缓存。
        if encoding_name != "o200k_base":
            raise ValueError("unsupported configured encoding")
        metadata = json.loads((resource_dir / f"{encoding_name}.json").read_text(encoding="utf-8"))
        raw = (resource_dir / f"{encoding_name}.tiktoken").read_bytes()
        # 2. 核对资源身份和 ranks 哈希，尽早发现漏打包、错版本或损坏。
        if (metadata["schema_version"] != 1 or metadata["name"] != encoding_name
                or hashlib.sha256(raw).hexdigest() != metadata["ranks_sha256"]):
            raise ValueError("encoding resource identity mismatch")
        # 3. 解析词表，要求 token 不重复且 rank 唯一连续，拒绝不完整的数据。
        ranks: dict[bytes, int] = {}
        for line in raw.splitlines():
            token, rank = line.split()
            decoded = base64.b64decode(token, validate=True)
            if decoded in ranks:
                raise ValueError("duplicate encoding token")
            ranks[decoded] = int(rank)
        if set(ranks.values()) != set(range(len(ranks))):
            raise ValueError("encoding ranks are not unique and contiguous")
        # 4. 使用已校验的本地数据构造编码器，再通过固定样本验证实际编码结果。
        encoding = tiktoken.Encoding(name=metadata["name"], pat_str=metadata["pat_str"],
            mergeable_ranks=ranks, special_tokens=metadata["special_tokens"])
        for sample in metadata["samples"]:
            if encoding.encode(sample["text"], disallowed_special=()) != sample["ids"]:
                raise ValueError("encoding known-answer check failed")
        return encoding
    # 5. 加载或校验失败统一报初始化错误，不下载资源或切换其他编码。
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        raise ContextError("CONTEXT_TOKENIZER_UNAVAILABLE",
            "the bundled context tokenizer could not be loaded or verified") from error
