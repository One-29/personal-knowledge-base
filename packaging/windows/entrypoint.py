"""PyInstaller 的稳定入口；业务入口保持可由 ``python -m`` 复用。"""

from multiprocessing import freeze_support

from app.desktop.__main__ import main


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
