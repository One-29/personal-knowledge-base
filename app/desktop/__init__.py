"""KnowBase 原生桌面窗口原型。

包初始化保持无副作用，使 ``python -m app.desktop --project-root ...`` 能先切换
工作目录，再由 launcher 加载会读取 ``.env`` 的应用配置。
"""
