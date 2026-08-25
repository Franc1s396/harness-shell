from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


BACKEND_ROOT = Path(SPECPATH)
hiddenimports = sorted(
    set(
        collect_submodules("pydantic")
        + collect_submodules("cryptography")
        + collect_submodules("opentelemetry")
    )
)
datas = collect_data_files(
    "harness_shell_sidecar",
    includes=["storage/migrations/*.sql"],
)

a = Analysis(
    [str(BACKEND_ROOT / "src" / "harness_shell_sidecar" / "__main__.py")],
    pathex=[str(BACKEND_ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
