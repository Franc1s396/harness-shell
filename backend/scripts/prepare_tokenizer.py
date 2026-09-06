"""在构建阶段显式准备确定性的 tokenizer 资源。"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import sys
from importlib.metadata import version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> None:
    """生成固定版本的编码数据，或在不联网的情况下校验资源。"""
    # 1. 显式选择资源目录和生成/检查模式，并确认构建依赖版本固定。
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if version("tiktoken") != "0.12.0":
        raise RuntimeError("tokenizer build requires tiktoken 0.12.0")
    # 2. 仅生成模式允许获取编码源数据；按 rank 排序生成确定性的资源和校验样本。
    if not args.check:
        # 这是唯一允许联网准备编码资源的路径。
        from tiktoken_ext.openai_public import o200k_base
        from tiktoken import Encoding
        descriptor = o200k_base()
        encoding = Encoding(**descriptor)
        raw = b"".join(base64.b64encode(token) + b" " + str(rank).encode() + b"\n"
            for token, rank in sorted(descriptor["mergeable_ranks"].items(), key=lambda pair: pair[1]))
        metadata = {key: descriptor[key] for key in ("name", "pat_str", "special_tokens")}
        metadata.update(schema_version=1, ranks_sha256=hashlib.sha256(raw).hexdigest(),
            samples=[{"text": text, "ids": encoding.encode(text)}
                     for text in ("hello world", "中文上下文🙂")])
        # 3. 将词表和元数据写入构建目录，供 PyInstaller 纳入 packaged Backend。
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "o200k_base.tiktoken").write_bytes(raw)
        (args.output_dir / "o200k_base.json").write_text(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    # 4. 两种模式都使用生产离线 loader 验证结果，确保资源可以实际加载。
    from harness_shell_sidecar.agent.tokenizer import load_local_encoding
    load_local_encoding(args.output_dir, "o200k_base")


if __name__ == "__main__":
    main()
