"""
PyInstaller 런타임 훅 - playwright 드라이버 경로 패치
frozen 환경에서 sys._MEIPASS 기준으로 node.exe / cli.js 경로를 재설정합니다.
"""
import sys
from pathlib import Path


if getattr(sys, "frozen", False):
    _base = Path(sys._MEIPASS)
    _node = str(_base / "playwright" / "driver" / "node.exe")
    _cli  = str(_base / "playwright" / "driver" / "package" / "cli.js")

    def _patched_compute_driver_executable():
        return (_node, _cli)

    import playwright._impl._driver as _drv
    _drv.compute_driver_executable = _patched_compute_driver_executable
