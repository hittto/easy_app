# -*- mode: python ; coding: utf-8 -*-
"""
국토정보맵 자동 다운로더 - PyInstaller 빌드 스펙
"""
import sys
from pathlib import Path

# playwright 패키지 위치
_pw_dir = Path(sys.exec_prefix) / "Lib" / "site-packages" / "playwright"
_driver_dir = _pw_dir / "driver"

a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=[],
    datas=[
        # playwright 드라이버 전체 (node.exe + package/)
        (str(_driver_dir), "playwright/driver"),
    ],
    hiddenimports=[
        "playwright",
        "playwright.async_api",
        "playwright._impl._api_types",
        "playwright._impl._browser",
        "playwright._impl._browser_context",
        "playwright._impl._browser_type",
        "playwright._impl._connection",
        "playwright._impl._driver",
        "playwright._impl._element_handle",
        "playwright._impl._errors",
        "playwright._impl._event_context_manager",
        "playwright._impl._file_chooser",
        "playwright._impl._frame",
        "playwright._impl._helper",
        "playwright._impl._js_handle",
        "playwright._impl._locator",
        "playwright._impl._network",
        "playwright._impl._page",
        "playwright._impl._playwright",
        "playwright._impl._transport",
        "playwright._impl._video",
        "playwright._repo_version",
        "tkinter",
        "tkinter.ttk",
        "tkinter.scrolledtext",
        "tkinter.messagebox",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["runtime_hook_playwright.py"],
    excludes=[
        # 불필요한 대형 패키지 제외
        "matplotlib", "numpy", "pandas", "scipy",
        "PIL", "cv2", "sklearn",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ngii_downloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # 사용자가 직접 실행하는 메인 exe는 압축하지 않는다.
                         # (UPX로 압축된 실행 파일은 Windows Defender/SmartScreen이
                         # "알 수 없는 패커"로 오탐하는 경우가 잦은데, 특히 사용자가
                         # 더블클릭하는 파일에서 발생하면 체감 위험이 가장 크다.)
    console=False,      # 콘솔 창 숨김 (GUI 전용)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# _internal 안의 대형 바이너리(특히 playwright 드라이버의 node.exe, 86MB)만 UPX로
# 압축한다. 이 파일들은 사용자가 직접 실행하지 않고 프로그램이 내부적으로 구동하는
# 보조 바이너리라 메인 exe보다 오탐 체감 위험이 낮다. 빌드 시 UPX가 PATH에 있어야
# 동작하며(https://github.com/upx/upx/releases), 없으면 자동으로 압축 없이 진행된다.
# tcl/tk·python 런타임 DLL은 압축 시 로딩 이슈가 보고된 적이 있어 제외한다.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[
        "python313.dll", "tcl86t.dll", "tk86t.dll",
        "_tkinter.pyd", "libcrypto-3.dll", "libssl-3.dll",
    ],
    name="ngii_downloader",
)
