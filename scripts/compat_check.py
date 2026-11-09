"""检查每个插件「接得对不对」——契约六项，一条命令看完。

    python scripts\\compat_check.py          只查磁盘上的契约（不需要服务在跑）
    python scripts\\compat_check.py --live   连正在跑的服务，验 /api/health 和 /api/manifest

六列：
    metadata   metadata.json 能解析、必填字段齐
    docs       docs/对接.md 在
    alone      metadata.standalone 有 works_alone / needs / how
    health     /api/health 回了 ok:true          （--live 才查）
    manifest   /api/manifest 回了、且 metadata 对得上（--live 才查）
    version    metadata 版本 vs /api/health 版本一致（--live 才查）

约定见 docs/插件对接契约.md。退出码：有 ❌ 就是 1。
"""
from __future__ import annotations

import json
import socket
import sys
import unicodedata
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 海螺/
BASE = ROOT.parent                                     # 插件/
REQUIRED = ("id", "name", "version", "launch")         # port 允许只写在 launch 里


def _w(text: str) -> int:
    """显示宽度：中文按 2 算，免得表格错位。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))


def _pad(text: str, width: int) -> str:
    text = str(text)
    return text + " " * max(0, width - _w(text))


# ───────────────────────── 清单 ─────────────────────────

def _items() -> list[dict]:
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from start_all import _launch_items

        return _launch_items()
    except Exception:
        return []


def _plugin_dirs() -> list[Path]:
    """有 metadata.json 的插件目录（不管在不在启动清单里）。"""
    out: list[Path] = []
    for meta in sorted(BASE.glob("*/metadata.json")):
        out.append(meta.parent)
    # 宿主自己（海螺）也查
    if ROOT not in out:
        out.append(ROOT)
    return out


# ───────────────────────── 单项检查 ─────────────────────────

def _read_meta(root: Path) -> dict | None:
    for rel in ("metadata.json", "plugins/conch/metadata.json"):   # 海螺自己的在 plugins/conch/
        path = root / rel
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception:
                return None
    return None


def _port(root: Path, meta: dict | None) -> int:
    if meta:
        port = meta.get("port") or (meta.get("launch") or {}).get("port")
        if port:
            return int(port)
    return 0


def _open(port: int, timeout: float = 0.3) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


SECRET_HINTS = ("password", "consolepassword", "api_key", "apikey", "sk-", "secret", "token")


def _get(port: int, path: str, timeout: float = 3.0) -> dict | None:
    try:
        url = f"http://127.0.0.1:{port}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        data = json.loads(raw)
        if isinstance(data, dict):
            data["_raw"] = raw
        return data
    except urllib.error.HTTPError as exc:
        # 401/403 是很常见的错法：端点被鉴权挡着，探活脚本进不去
        return {"_status": exc.code}
    except Exception:
        return None


def _leaks(payload: dict | None) -> str:
    """免 token 的只读端点不该出现密码/令牌 —— 本地随便一个网页都能 fetch。"""
    raw = (payload or {}).get("_raw") or ""
    low = raw.lower()
    hit = [h for h in SECRET_HINTS if h in low]
    return hit[0] if hit else ""


def check(root: Path, live: bool) -> tuple[str, dict[str, str]]:
    name = root.name
    meta = _read_meta(root)
    cols: dict[str, str] = {}

    if meta is None:
        cols["metadata"] = "❌"
    else:
        missing = [k for k in REQUIRED if not meta.get(k)]
        cols["metadata"] = "✅" if not missing else f"❌缺{','.join(missing)}"
        # 版本常量对不上也算 metadata 问题的一半，留给 version 列

    cols["docs"] = "✅" if (root / "docs" / "对接.md").exists() else "❌"
    alone = (meta or {}).get("standalone") or {}
    ok_alone = bool(alone.get("works_alone") is not None and alone.get("needs") is not None and alone.get("how"))
    cols["alone"] = "✅" if ok_alone else "❌"

    port = _port(root, meta)
    if not live:
        cols["health"] = cols["manifest"] = cols["version"] = "·"
        return name, cols

    if not _open(port):
        cols["health"] = cols["manifest"] = cols["version"] = "·"
        cols["health"] = "·没在跑"
        return name, cols

    health = _get(port, "/api/health")
    leak = _leaks(health)
    if not health or not health.get("ok"):
        code = (health or {}).get("_status")
        cols["health"] = f"❌{code}" if code else "❌"
    elif leak:
        cols["health"] = f"❌泄露{leak}"
    else:
        cols["health"] = "✅"

    manifest = _get(port, "/api/manifest")
    if not manifest or not manifest.get("ok") or not isinstance(manifest.get("metadata"), dict):
        cols["manifest"] = "❌"
    else:
        mm = manifest["metadata"]
        same = all(mm.get(k) == (meta or {}).get(k) for k in ("id", "version"))
        mleak = _leaks({"manifest": None, "_raw": json.dumps(manifest.get("runtime") or {}, ensure_ascii=False)})
        cols["manifest"] = "❌泄露" if mleak else ("✅" if same else "❌不一致")

    hv = (health or {}).get("version")
    mv = (meta or {}).get("version")
    if not hv or not mv:
        cols["version"] = "❌"
    else:
        cols["version"] = "✅" if str(hv) == str(mv) else f"❌{hv}≠{mv}"
    return name, cols


# ───────────────────────── 输出 ─────────────────────────

def main() -> int:
    # Windows 控制台默认 GBK，一遇到 ❌ 就 UnicodeEncodeError（正好在最该用它的时候崩）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    live = "--live" in sys.argv
    plugins = _plugin_dirs()
    if not plugins:
        print("没找到任何插件（metadata.json）")
        return 1

    cols = ["metadata", "docs", "alone", "health", "manifest", "version"]
    widths = [14, 6, 8, 18, 14, 14]
    head = _pad("插件", 12) + "".join(_pad(c, w) for c, w in zip(cols, widths))
    print(head)
    print("-" * _w(head))

    bad = 0
    for root in plugins:
        name, res = check(root, live)
        cells = "".join(_pad(res.get(c, "·"), w) for c, w in zip(cols, widths))
        mark = "" if not any(v.startswith("❌") for v in res.values()) else "  ❗"
        print(_pad(name, 12) + cells + mark)
        if any(v.startswith("❌") for v in res.values()):
            bad += 1

    print("-" * _w(head))
    if bad:
        print(f"{bad} 个插件还没接完整（❌）。规则见 docs/插件对接契约.md")
        return 1
    print("全部合规。" + ("" if live else "（没加 --live，health/manifest 未验证）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())