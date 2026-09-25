"""KnowBase 原生桌面宿主。

包初始化保持无副作用，使 ``python -m app.desktop --project-root ...`` 能先切换
工作目录或准备冻结包的用户配置，再由 launcher 加载全局应用配置。
"""
