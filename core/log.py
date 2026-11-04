"""日志。统一格式，插件前缀由 plugin 名带出，同时留一份环形缓冲给面板看。"""

from __future__ import annotations

import collections
import logging
import sys

_CONFIGURED = False

# 面板「运维」页用的最近日志（不落盘，5 分钟滚动）
BUFFER: collections.deque[str] = collections.deque(maxlen=300)


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            BUFFER.append(self.format(record))
        except Exception:
            pass


def setup(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    # Windows 控制台默认 GBK，中文日志会变乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s", "%H:%M:%S")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    buffered = _BufferHandler()
    buffered.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)
    root.addHandler(buffered)
    _CONFIGURED = True


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)