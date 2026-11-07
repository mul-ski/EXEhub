# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['exehub.py'],
    pathex=[],
    binaries=[],
    datas=[('exehub.ico', '.')],
    hiddenimports=['icoextract', 'PIL', 'winrt.system', 'winrt.windows.foundation', 'winrt.windows.foundation.collections', 'winrt.windows.media.control', 'winrt.windows.storage.streams'],
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
    name='EXEHUB',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['exehub.ico'],
)
