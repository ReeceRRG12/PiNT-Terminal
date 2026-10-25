# -*- mode: python ; coding: utf-8 -*-
"""Standalone Windows executable with its runtime and notices embedded."""
from pathlib import Path
import tomllib

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable,
    VarFileInfo, VarStruct, VSVersionInfo,
)

release_version = tomllib.loads((Path(SPECPATH) / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
version_tuple = tuple(int(part) for part in release_version.split('.')) + (0,)
executable_name = f'PiNT-Terminal-{release_version}-Windows-x64'
version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=version_tuple, prodvers=version_tuple, mask=0x3f,
        flags=0, OS=0x40004, fileType=0x1, subtype=0, date=(0, 0),
    ),
    kids=[
        StringFileInfo([StringTable('080904b0', [
            StringStruct('CompanyName', 'PiNT'),
            StringStruct('FileDescription', 'PiNT Terminal'),
            StringStruct('FileVersion', release_version),
            StringStruct('InternalName', 'PiNT Terminal'),
            StringStruct('OriginalFilename', executable_name + '.exe'),
            StringStruct('ProductName', 'PiNT Terminal'),
            StringStruct('ProductVersion', release_version),
        ])]),
        VarFileInfo([VarStruct('Translation', [2057, 1200])]),
    ],
)

a = Analysis(
    ['run.py'],
    pathex=[], binaries=[],
    datas=[(str(Path(SPECPATH) / 'third_party'), 'third_party')],
    hiddenimports=collect_submodules('pyte'),
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=executable_name,
    debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False,
    disable_windowed_traceback=False,
    version=version_info,
)
