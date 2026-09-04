# -*- mode: python ; coding: utf-8 -*-
"""VideoRecDiag 诊断工具 spec：onefile console，收集 cv2/numpy 全套。"""
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
for _pkg in ("cv2", "numpy"):
    _t = collect_all(_pkg)
    datas += _t[0]
    binaries += _t[1]
    hiddenimports += _t[2]

a = Analysis(
    ["_diag_win7.py"],
    pathex=[],
    binaries=binaries,
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
    name="VideoRecDiag",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
