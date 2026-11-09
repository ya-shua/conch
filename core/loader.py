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

# 海螺本体的目录（core/loader.py 往上两级）。插件目录里如果出现它，要跳过。
_HOST_DIR = Path(__file__).resolve().parents[1]

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
    source: object = ""  # "owner/repo" 或 {"repo": .., "check": "release"|"tag"}
    kind: str = "plugin"  # plugin = 有 main.py；app = 独立应用（只有 metadata，靠启动器起）
    path: Path = Path(".")
    config: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "builtin": self.builtin,
            "source": self.source,
            "kind": self.kind,
            "path": str(self.path),
        }


def read_meta(folder: Path) -> PluginMeta:
    meta_path = folder / "metadata.json"
    if not meta_path.exists():
        raise ValueError(f"{folder} 缺少 metadata.json")
    data = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{meta_path} 顶层必须是对象")
    pid = str(data.get("id") or folder.name)
    kind = str(data.get("kind") or "").strip() or (
        "plugin" if (folder / "main.py").exists() else "app"
    )
    return PluginMeta(
        id=pid,
        name=str(data.get("name") or pid),
        version=str(data.get("version") or "0.0.0"),
        author=str(data.get("author") or ""),
        description=str(data.get("description") or ""),
        builtin=bool(data.get("builtin", False)),
        source=data.get("source") or "",
        kind=kind,
        path=folder,
        raw=data,
    )


class Loader:
    def __init__(
        self,
        root: Path,
        make_ctx: Callable[[PluginMeta], Any],
        extra_roots: list[Path] | None = None,
    ):
        self.root = Path(root)
        # 外部插件目录：插件也可以归它自己的项目管，不必搬进 status 的 plugins/
        self.extra_roots = [Path(p) for p in (extra_roots or [])]
        self._make_ctx = make_ctx
        self.metas: dict[str, PluginMeta] = {}
        self._modules: dict[str, Any] = {}
        self._ctxs: dict[str, Any] = {}
        self.status: dict[str, str] = {}

    def module(self, pid: str):
        """这个插件加载出来的 Python 模块（没启就是 None）。"""
        return self._modules.get(pid)

    def context(self, pid: str):
        """这个插件的 ctx（没启就是 None）。"""
        return self._ctxs.get(pid)

    def scan(self) -> list[PluginMeta]:
        self.metas.clear()
        folders: list[Path] = []
        for base in [self.root, *self.extra_roots]:
            if not base.exists():
                if base != self.root:
                    log_.warning("外部插件目录不存在：%s", base)
                continue
            folders.extend(sorted(p for p in base.iterdir() if p.is_dir()))
        if not folders:
            return []
        for folder in folders:
            # 宿主自己的目录也可能躺在插件目录里（插件都挪到「桌面\插件」之后就是）。
            # 只认「就是海螺本体的那个文件夹」，别用 core/version.py 这种特征 ——
            # 别的插件也可能有 core/version.py（微光域就是），会被误伤。
            try:
                is_host = folder.resolve() == _HOST_DIR or (
                    folder / "core" / "panel" / "server.py"
                ).exists()
                if is_host:
                    log_.debug("跳过 %s：这是宿主自己的目录", folder.name)
                    continue
            except OSError:
                pass
            try:
                meta = read_meta(folder)
            except Exception as exc:
                # 有 main.py 却没写元数据才是配错了；纯数据目录（备份、素材）静静跳过
                if (folder / "main.py").exists():
                    log_.warning("跳过 %s：%s", folder.name, exc)
                else:
                    log_.debug("跳过 %s：%s", folder.name, exc)
                continue
            cfg_path = folder / "config.json"
            if cfg_path.exists():
                try:
                    meta.config = json.loads(cfg_path.read_text(encoding="utf-8-sig")) or {}
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

        if meta.kind == "app":
            # 独立应用不是宿主内插件，起停在启动器那边，这里只登记
            raise RuntimeError(f"{meta.name} 是外部应用，请用「启动」按钮（launchers）起它")

        entry = meta.path / "main.py"
        if not entry.exists():
            raise FileNotFoundError(f"{meta.path} 缺少 main.py")

        module_name = f"conch_plugin_{pid}"
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
        sys.modules.pop(f"conch_plugin_{pid}", None)
        self.status[pid] = STATUS_DISABLED

    async def reload(self, pid: str) -> None:
        """重载：还在跑就先拆掉，再按当前代码装回来。"""
        was_enabled = self.status.get(pid) == STATUS_ENABLED
        if was_enabled:
            await self.disable(pid)
        # 模块缓存里那份也要丢掉，否则改过的代码不会生效
        sys.modules.pop(f"conch_plugin_{pid}", None)
        await self.enable(pid)

    async def reload_all(self) -> dict[str, list[str] | dict[str, str]]:
        done: list[str] = []
        failed: dict[str, str] = {}
        for pid in list(self.metas):
            if self.status.get(pid) != STATUS_ENABLED:
                continue
            try:
                await self.reload(pid)
                done.append(pid)
            except Exception as exc:
                failed[pid] = str(exc)[:200]
        return {"reloaded": done, "failed": failed}

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