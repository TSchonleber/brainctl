# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for brainctl-mcp standalone binary.
# Build: `pyinstaller build/pyinstaller/brainctl_mcp.spec`

import sys
from pathlib import Path

block_cipher = None

# Repo root: spec is at build/pyinstaller/, so go up two levels.
project_root = Path(SPECPATH).parent.parent

a = Analysis(
    [str(project_root / "src" / "agentmemory" / "mcp_server.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    # Ship the schema bootstrapper so a fresh brain.db gets initialized on
    # first launch. _ensure_db_initialized() in mcp_server.py resolves both
    # init_schema.sql and the migrations/ directory via sys._MEIPASS at
    # runtime; without these datas entries the lookup falls back to the
    # repo checkout (only correct for dev installs, never for a packaged
    # sidecar binary like the one Mantic ships).
    #
    # Destination layout inside the bundle:
    #   _MEIPASS/agentmemory/db/init_schema.sql
    #   _MEIPASS/db/migrations/*.sql
    # which mirrors the layouts that migrate.py and _ensure_db_initialized
    # already probe for in package-relative and repo-relative variants.
    datas=[
        (str(project_root / "src" / "agentmemory" / "db" / "init_schema.sql"),
         "agentmemory/db"),
        (str(project_root / "src" / "agentmemory" / "db" / "migrations"),
         "db/migrations"),
    ],
    hiddenimports=[
        "sqlite3",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="brainctl-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
