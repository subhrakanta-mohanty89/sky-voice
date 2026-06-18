# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — produces a single double-clickable Sky Voice AI bundle.

Run via::

    pyinstaller --noconfirm SkyVoiceAI.spec

Output:
  * macOS    →  dist/Sky Voice AI.app
  * Windows  →  dist/SkyVoiceAI/SkyVoiceAI.exe
  * Linux    →  dist/SkyVoiceAI/SkyVoiceAI
"""
from pathlib import Path
import sys

ROOT = Path(SPECPATH).resolve()
APP_NAME = "Sky Voice AI"
EXE_NAME = "SkyVoiceAI"
BUNDLE_ID = "com.millenniuminfotech.skyvoiceai"
VERSION = "1.0.0"

# The React build (web/) is shipped as a data folder — at runtime
# app.py reads it back from sys._MEIPASS / "web".
datas = [
    (str(ROOT / "web"), "web"),
]

# pywebview imports its platform backend lazily; help PyInstaller find it.
hiddenimports = []
if sys.platform == "darwin":
    hiddenimports += [
        "webview.platforms.cocoa",
        "Foundation",
        "AppKit",
        "WebKit",
    ]
elif sys.platform.startswith("win"):
    hiddenimports += [
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "clr",
    ]
elif sys.platform.startswith("linux"):
    hiddenimports += [
        "webview.platforms.gtk",
        "webview.platforms.qt",
    ]

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

# Platform-specific icon (optional — falls back to PyInstaller default).
icon_mac = ROOT / "assets" / "icon.icns"
icon_win = ROOT / "assets" / "icon.ico"
icon = None
if sys.platform == "darwin" and icon_mac.exists():
    icon = str(icon_mac)
elif sys.platform.startswith("win") and icon_win.exists():
    icon = str(icon_win)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # GUI app — no terminal window on Windows
    disable_windowed_traceback=False,
    icon=icon,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=EXE_NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier=BUNDLE_ID,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            # Twilio Voice SDK getUserMedia → mic permission prompt text.
            "NSMicrophoneUsageDescription":
                "Sky Voice AI uses your microphone to place and receive calls.",
            "NSCameraUsageDescription":
                "Sky Voice AI may use your camera for future video features.",
            # WebRTC needs the network entitlement on hardened runtimes,
            # but Info.plist alone is enough for un-notarized .app on
            # the developer's own machine. Add an entitlements_file= on
            # the EXE() above when you're ready to ship via the App
            # Store / notarized distribution.
        },
    )
