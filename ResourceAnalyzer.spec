# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH)
sensor_dir = root / 'vendor' / 'sensors'
if not (sensor_dir / 'SensorBridge.exe').is_file():
    raise RuntimeError('Run tools/build_sensor_bridge.py before building the temperature-enabled app.')
a = Analysis(
    [str(root / 'entrypoint.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / 'static'), 'static'), (str(sensor_dir), 'vendor/sensors'),
           (str(root / 'third_party'), 'third_party')],
    hiddenimports=['server'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='ResourceAnalyzer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    version=str(root / 'version_info.txt'),
)
