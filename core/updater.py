"""版本与更新：问 GitHub 有没有新版，并一键拉下来。

两个原则
--------
1. **检查是只读的**：只打一个 GET 到公开的 releases 接口，不带任何凭据，
   也不上传本机信息。连不上就当「检查失败」，不打扰她说话。
2. **更新不偷偷做**：只在人点了按钮之后 `git pull --ff-only`。
   快进合并失败（本地有改动、跑偏了分支）就原样报错，绝不 `--force`，
   也不替人 stash —— 那些是人的东西。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import aiohttp

from . import log

log_ = log.get("update")

REPO = "ya-shua/conch"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
CODE_URL = f"https://github.com/{REPO}"

_GIT_CANDIDATES = (
    r"D:\Git\cmd\git.exe",
    r"C:\Program Files\Git\cmd\git.exe",
)


def project_root() -> Path:
    """仓库根就是 qqbridge 自己（core/ 的上一级）。"""
    return Path(__file__).resolve().parent.parent


def current_version() -> str:
    try:
        from qqbridge import __version__

        return str(__version__)
    except Exception:
        return "0.0.0"


def _git() -> str:
    found = shutil.which("git")
    if found:
        return found
    for path in _GIT_CANDIDATES:
        if Path(path).exists():
            return path
    return ""


def _tuple(version: str) -> tuple[int, ...]:
    raw = str(version or "").strip().lstrip("vV")
    parts: list[int] = []
    for chunk in raw.split(".")[:4]:
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def compare(current: str, latest: str) -> bool:
    """latest 是否比 current 新。"""
    return _tuple(latest) > _tuple(current)


async def check(timeout_s: float = 8.0) -> dict:
    """查最新 release。任何异常都折成 ok=False，别让面板因此空白。"""
    current = current_version()
    result = {
        "ok": False,
        "current": current,
        "latest": "",
        "has_update": False,
        "url": CODE_URL,
        "notes": "",
        "error": "",
    }
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                RELEASES_API,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "conch-updater",
                },
            ) as resp:
                if resp.status == 404:
                    result["error"] = "仓库还没有发布过版本"
                    return result
                if resp.status != 200:
                    result["error"] = f"GitHub 返回 {resp.status}"
                    return result
                data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        result["error"] = f"连不上 GitHub（{type(exc).__name__}）"
        return result

    latest = str(data.get("tag_name") or "").strip()
    result.update(
        {
            "ok": True,
            "latest": latest,
            "has_update": bool(latest) and compare(current, latest),
            "url": str(data.get("html_url") or CODE_URL),
            "notes": str(data.get("body") or "")[:1500],
            "published": str(data.get("published_at") or ""),
        }
    )
    return result


async def apply_update() -> dict:
    """拉最新代码。只做快进合并，失败原样返回。"""
    git = _git()
    if not git:
        return {"ok": False, "error": "找不到 git，手动更新：git pull"}
    root = project_root()
    if not (root / ".git").exists():
        return {"ok": False, "error": f"{root} 不是 git 仓库，手动拉一次代码"}

    proc = await asyncio.create_subprocess_exec(
        git,
        "-C",
        str(root),
        "pull",
        "--ff-only",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode("utf-8", "replace").strip()
    ok = proc.returncode == 0
    if not ok:
        log_.warning("更新失败：%s", text[:300])
    return {
        "ok": ok,
        "output": text[-800:],
        "error": "" if ok else (text[-300:] or "git pull 失败"),
        "restart_hint": "代码已更新，重启海螺后生效",
    }