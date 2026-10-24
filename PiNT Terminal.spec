# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from pathlib import Path
import tomllib

release_version = tomllib.loads((Path(SPECPATH) / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']

hiddenimports = []
hiddenimports += collect_submodules('pyte')


a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[(str(Path(SPECPATH) / 'third_party'), 'third_party')],
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
    [],
    exclude_binaries=True,
    name='PiNT Terminal',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PiNT Terminal',
)
app = BUNDLE(
    coll,
    name='PiNT Terminal.app',
    icon=None,
    bundle_identifier='com.pinetworktools.terminal',
    version=release_version,
    info_plist={'CFBundleVersion': release_version},
)
