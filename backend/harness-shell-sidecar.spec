"""打包官方 SDK 运行时，不包含已移除的 LangChain OpenAI 适配器。"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


BACKEND_ROOT = Path(SPECPATH)


hiddenimports = sorted(
    set(
        collect_submodules("tiktoken")
        + collect_submodules("pydantic")
        + collect_submodules("cryptography")
        + collect_submodules("fastapi")
        + collect_submodules("starlette")
        + collect_submodules("uvicorn")
        + collect_submodules("websockets")
        + collect_submodules("langchain_core")
        + collect_submodules("langgraph")
        + collect_submodules("opentelemetry")
    )
)
datas = collect_data_files(
    "harness_shell_sidecar",
    includes=["storage/migrations/*.sql"],
)

datas += [(str(BACKEND_ROOT / "build" / "tokenizer"), "harness_shell_sidecar/agent/tokenizer_data")]

a = Analysis(
    [str(BACKEND_ROOT / "src" / "harness_shell_sidecar" / "__main__.py")],
    pathex=[str(BACKEND_ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 共用的开发虚拟环境可能仍安装了这个额外包。
    # 即使环境中存在该包，也不得将已移除的适配器重新打入可执行文件。
    excludes=["langchain_openai"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="harness-shell-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
