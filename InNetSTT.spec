# -*- mode: python ; coding: utf-8 -*-
# ──────────────────────────────────────────────────────────────────────────────
# TTS (IN NET CREATIONS) — PyInstaller Spec
# Produces a single-file Windows .exe with no console window.
#
# BUILD:
#   pip install pyinstaller
#   pyinstaller InNetSTT.spec
#
# OUTPUT: dist\InNetSTT.exe
# ──────────────────────────────────────────────────────────────────────────────

from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None

wv_datas, wv_bins, wv_hidden = collect_all('webview')
pn_datas, pn_bins, pn_hidden = collect_all('pythonnet')
cl_datas, cl_bins, cl_hidden = collect_all('clr_loader')

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=wv_bins + pn_bins + cl_bins,
    # Bundle templates, Assets, certifi CA certs, and pywebview/pythonnet runtime
    datas=[
        ('templates', 'templates'),
        ('Assets', 'Assets'),
    ] + collect_data_files('certifi') + wv_datas + pn_datas + cl_datas,
    # edge-tts and pywebview use dynamic submodules missed on auto-scan
    hiddenimports=[
        'edge_tts',
        'edge_tts.communicate',
        'edge_tts.constants',
        'edge_tts.data_classes',
        'edge_tts.drm',
        'edge_tts.exceptions',
        'edge_tts.submaker',
        'edge_tts.typing',
        'edge_tts.util',
        'certifi',
        'bottle',
        'proxy_tools',
        'clr',
        'aiohttp',
        'aiohttp.connector',
        'aiohttp.client',
        'aiohttp.streams',
        'asyncio',
        'flask',
        'flask.templating',
        'jinja2',
        'jinja2.ext',
        'werkzeug',
        'werkzeug.serving',
        'werkzeug.debug',
        'click',
    ] + wv_hidden + pn_hidden + cl_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Strip heavy unused packages to keep exe size down
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'PIL',
        'cv2',
        'test',
        'unittest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='InNetSTT',
    # Icon: PyInstaller expects a .ico file.
    # Run convert_icon.py first (see build.bat) to produce Assets\logo.ico
    icon='Assets\\logo.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # False = no black console window — app opens straight to browser
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Windows-only version info shown in Explorer "Details" tab
    version='file_version_info.txt',
)
