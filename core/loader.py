"""插件加载器：扫目录、读元数据、管生命周期。

一个插件就是一个文件夹，里面有 metadata.json 和 main.py。
坏了一个插件不能影响宿主和其他插件。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import log

log_ = log.get("loader")

STATUS_ENABLED = "enabled"
STATUS_DISABLED = "disabled"
STATUS_ERROR = "error"


@dataclass
class PluginMeta:
    id: str
    name: str
    version: str = "0.0.0"
    author: str = ""
    description: str = ""
    builtin: bool = False
    path: Path = Path(".")
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "builtin": self.builtin,
            "path": str(self.path),
        }


def read_meta(folder: Path) -> PluginMeta:
    meta_path = folder / "metadata.json"
    if not meta_path.exists():
        raise ValueError(f"{folder} 缺少 metadata.json")
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{meta_path} 顶层必须是对象")
    pid = str(data.get("id") or folder.name)
    return PluginMeta(
        id=pid,
        name=str(data.get("name") or pid),
        version=str(data.get("version") or "0.0.0"),
        author=str(data.get("author") or ""),
        description=str(data.get("description") or ""),
        builtin=bool(data.get("builtin", False)),
        path=folder,
    )


class Loader:
    def __init__(self, root: Path, make_ctx: Callable[[PluginMeta], Any]):
        self.root = Path(root)
        self._make_ctx = make_ctx
        self.metas: dict[str, PluginMeta] = {}
        self._modules: dict[str, Any] = {}
        self._ctxs: dict[str, Any] = {}
        self.status: dict[str, str] = {}

    def scan(self) -> list[PluginMeta]:
        self.metas.clear()
        if not self.root.exists():
            return []
        for folder in sorted(p for p in self.root.iterdir() if p.is_dir()):
            try:
                meta = read_meta(folder)
            except Exception as exc:
                log_.warning("跳过 %s：%s", folder.name, exc)
                continue
            cfg_path = folder / "config.json"
            if cfg_path.exists():
                try:
                    meta.config = json.loads(cfg_path.read_text(encoding="utf-8")) or {}
                except Exception as exc:
                    log_.warning("%s 的 config.json 读不了（用空配置）：%s", meta.id, exc)
                    meta.config = {}
            self.metas[meta.id] = meta
            self.status.setdefault(meta.id, STATUS_DISABLED)
        return list(self.metas.values())

    async def enable(self, pid: str) -> None:
        meta = self.metas.get(pid)
        if meta is None:
            raise KeyError(f"没有这个插件：{pid}")

        entry = meta.path / "main.py"
        if not entry.exists():
            raise FileNotFoundError(f"{meta.path} 缺少 main.py")

        module_name = f"qqbridge_plugin_{pid}"
        spec = importlib.util.spec_from_file_location(module_name, entry)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载 {entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            ctx = self._make_ctx(meta)
            setup = getattr(module, "setup", None)
            if setup is None:
                raise RuntimeError("main.py 没有导出 setup(ctx)")
            result = setup(ctx)
            if hasattr(result, "__await__"):
                await result
        except Exception:
            sys.modules.pop(module_name, None)
            self.status[pid] = STATUS_ERROR
            raise

        self._modules[pid] = module
        self._ctxs[pid] = ctx
        self.status[pid] = STATUS_ENABLED
        log_.info("插件已启用：%s v%s", meta.name, meta.version)

    async def disable(self, pid: str) -> None:
        meta = self.metas.get(pid)
        if meta is None:
            return
        module = self._modules.pop(pid, None)
        ctx = self._ctxs.pop(pid, None)
        if module is not None:
            teardown = getattr(module, "teardown", None)
            try:
                if teardown is not None:
                    result = teardown(ctx)
                    if hasattr(result, "__await__"):
                        await result
            except Exception as exc:
                log_.warning("插件 %s 拆卸异常：%s", pid, exc)
        if ctx is not None and hasattr(ctx, "bus"):
            ctx.bus.off_owner(pid)
        sys.modules.pop(f"qqbridge_plugin_{pid}", None)
        self.status[pid] = STATUS_DISABLED

    async def enable_all(self) -> None:
        for pid, meta in self.metas.items():
            if not meta.builtin:
                continue
            try:
                await self.enable(pid)
            except Exception as exc:
                log_.error("插件 %s 启用失败：%s", pid, exc)

    def list(self) -> list[dict[str, Any]]:
        out = []
        for pid, meta in self.metas.items():
            item = meta.to_dict()
            item["status"] = self.status.get(pid, STATUS_DISABLED)
            out.append(item)
        return out