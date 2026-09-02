"""Check or explicitly regenerate deterministic HTTP/WebSocket artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SOURCE = REPOSITORY_ROOT / "backend" / "src"
if str(BACKEND_SOURCE) not in sys.path:
    sys.path.insert(0, str(BACKEND_SOURCE))

from harness_shell_sidecar.web.contracts import (  # noqa: E402
    build_openapi_document,
    build_websocket_schema,
    canonical_json_bytes,
)


ARTIFACTS = {
    REPOSITORY_ROOT / "docs" / "protocol" / "http" / "openapi-v1.json": (
        build_openapi_document
    ),
    REPOSITORY_ROOT
    / "docs"
    / "protocol"
    / "http"
    / "runtime-websocket-v1.schema.json": build_websocket_schema,
}


def build_parser() -> argparse.ArgumentParser:
    """Build an explicit non-ambiguous contract export CLI."""

    parser = argparse.ArgumentParser(allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--stdout", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Check reviewed bytes, write explicitly, or print deterministic combined JSON."""

    args = build_parser().parse_args(argv)
    generated = {path: canonical_json_bytes(builder()) for path, builder in ARTIFACTS.items()}
    if args.stdout:
        combined = {
            path.name: json.loads(content)
            for path, content in generated.items()
        }
        sys.stdout.buffer.write(canonical_json_bytes(combined))
        return 0
    if args.write:
        for path, content in generated.items():
            path.write_bytes(content)
        return 0

    mismatches = [
        path
        for path, content in generated.items()
        if not path.exists() or path.read_bytes() != content
    ]
    if mismatches:
        for path in mismatches:
            print(f"contract artifact differs: {path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
