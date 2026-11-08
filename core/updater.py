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
import subprocess
import sys
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
    """仓库根就是海螺自己（core/ 的上一级）。"""
    return Path(__file__).resolve().parent.parent


def current_version() -> str:
    try:
        from core.version import __version__

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


# 更新时绝不允许被覆盖的东西：本机配置与她那边的私人数据
def _protected(rel: str) -> bool:
    if rel in {"config.json", "token.txt", "panel_auth.json"}:
        return True
    if any(rel.startswith(p) for p in ("data/", "logs/", "roles/", ".venv/", ".git/")):
        return True
    parts = rel.split("/")
    return len(parts) == 3 and parts[0] == "plugins" and parts[2] == "config.json"


async def _download_tarball(tag: str, timeout_s: float = 60.0) -> bytes:
    """走 api.github.com 拿发行包（它会 302 到 codeload，这条路通常通）。"""
    url = f"https://api.github.com/repos/{REPO}/tarball/{tag}"
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            url, headers={"User-Agent": "conch-updater", "Accept": "application/vnd.github+json"}
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"下载失败 HTTP {resp.status}")
            return await resp.read()


def _extract_tarball(blob: bytes, root: Path) -> tuple[int, int]:
    """解到项目根。返回 (写入数, 跳过数)。"""
    import io as _io
    import tarfile

    written = skipped = 0
    with tarfile.open(fileobj=_io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            rel = member.name.split("/", 1)[-1]  # 去掉 github 加的那层前缀
            if not rel or rel.startswith("/") or ".." in rel.split("/"):
                continue
            if _protected(rel):
                skipped += 1
                continue
            data = tar.extractfile(member)
            if data is None:
                continue
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data.read())
            written += 1
    return written, skipped


async def apply_update() -> dict:
    """拉最新代码。只做快进合并，失败原样返回。"""
    root = project_root()
    info = await check()
    latest = str(info.get("latest") or "")

    git = _git()
    if git and (root / ".git").exists():
        pulled = await _git_pull(git, root)
        if pulled["ok"]:
            return pulled
        log_.warning("git pull 没成，改走发行包：%s", str(pulled.get("error"))[:160])

    if latest:
        try:
            blob = await _download_tarball(latest)
            written, skipped = _extract_tarball(blob, root)
            return {
                "ok": True,
                "via": "tarball",
                "latest": latest,
                "written": written,
                "skipped": skipped,
                "output": f"下载 {latest}：写入 {written} 个文件，跳过 {skipped} 个本机配置",
                "restart_hint": "代码已更新，重启海螺后生效",
            }
        except Exception as exc:
            return {"ok": False, "error": f"更新失败：{exc}"}

    return {"ok": False, "error": "没查到可用版本，稍后再试"}


async def restart_self() -> dict:
    """后台重启：调用 scripts/restart_conch.py，它负责杀自己 + 重新拉起。"""
    root = project_root()
    script = root / "scripts" / "restart_conch.py"
    if not script.exists():
        return {"ok": False, "error": "找不到 scripts/restart_conch.py"}

    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    launcher = pyw if pyw.exists() else py

    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen(
            [str(launcher), str(script)],
            cwd=str(root),
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as exc:
        log_.warning("重启没起来：%s", exc)
        return {"ok": False, "error": f"重启失败：{exc}"}
    log_.info("正在重启海螺")
    return {"ok": True, "restarting": True}


async def _git_pull(git: str, root: Path) -> dict:
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