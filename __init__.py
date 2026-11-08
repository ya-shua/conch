"""海螺插件宿主。"""

try:  # 版本号以 core/version.py 为准
    from core.version import __version__  # noqa: F401
except Exception:  # 被当成包 import（父目录在 sys.path 上）时走这里
    __version__ = "0.1.11"