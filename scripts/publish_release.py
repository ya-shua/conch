#!/usr/bin/env python3
"""发版：把当前代码推上 GitHub，并打 tag + 建 release。

为什么不用 git push
------------------
有些网络环境只有 api.github.com 通、github.com:443 被挡，
`git push` 会直接失败。这个脚本走 GitHub 的 Git Data API，
做的事和 push 等价：blobs → tree → commit → refs → tag → release。

用法
----
    set GITHUB_TOKEN=...        （或把 40 位 PAT 放在 %USERPROFILE%\\Desktop\\token.txt）
    python scripts/publish_release.py

版本号取自 qqbridge/__init__.py 的 __version__，tag 是 v<版本>。
发版前请先跑隐私门禁：python scripts/privacy_gate.py --all
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
REPO = os.environ.get("GITHUB_REPO", "ya-shua/conch-workbench")
DESC = "海螺工作台：一个 QQ 上的她，插件化、自带控制台"


def token() -> str:
    env = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env:
        return env.strip()
    fallback = Path.home() / "Desktop" / "token.txt"
    if fallback.exists():
        return fallback.read_text(encoding="utf-8").strip()
    raise SystemExit("没有 token：设 GITHUB_TOKEN 环境变量，或放 %USERPROFILE%\\Desktop\\token.txt")


TOKEN = token()


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"token {TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "conch-release",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode() or "{}"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() or "{}"
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"message": raw[:200]}


def version() -> str:
    text = (ROOT / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("读不到 __version__")
    return match.group(1)


def tracked() -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, encoding="utf-8"
    )
    if proc.returncode != 0:
        raise SystemExit("git ls-files 失败（先在仓库根跑一次 git init）")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def section(ver: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    parts = text.split(f"## v{ver}")
    if len(parts) < 2:
        return ""
    body = parts[1]
    cut = body.find("\n## v")
    return (body[:cut] if cut != -1 else body).strip()


def gate() -> int:
    """专属门禁：每次上传前把要传的文件全扫一遍隐私字符，有问题直接不发。"""
    script = ROOT / "scripts" / "privacy_gate.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--all"], capture_output=True, text=True, encoding="utf-8"
    )
    out = (proc.stdout or "").strip()
    if out:
        print(out)
    if proc.returncode != 0:
        print("\n门禁没让过：先处理上面的隐私字符再发版。")
    return proc.returncode


def ensure_repo() -> int:
    """仓库不存在就当场建一个公开的 MIT 项目。"""
    status, info = call("GET", f"/repos/{REPO}")
    if status == 200:
        if info.get("private"):
            print("仓库是私有的，改成公开 …")
            call("PATCH", f"/repos/{REPO}", {"private": False})
        print("仓库已存在:", info.get("html_url"))
        return 0
    owner, _, name = REPO.partition("/")
    status, info = call("POST", "/user/repos", {
        "name": name,
        "description": DESC,
        "private": False,
        "has_issues": True,
        "has_wiki": False,
        "license_template": "mit",
        "auto_init": False,
    })
    if status not in (200, 201):
        print("建仓库失败:", status, info.get("message"))
        return 1
    print("建好仓库:", info.get("html_url"), "（公开）")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ver = version()
    tag = f"v{ver}"
    base = f"/repos/{REPO}"
    print(f"发版 {tag}  →  {REPO}")

    if gate() != 0:
        return 1
    if ensure_repo() != 0:
        return 1

    status, head = call("GET", f"{base}/git/ref/heads/main")
    parent = head["object"]["sha"] if status == 200 else ""
    status, base_tree = call("GET", f"{base}/git/commits/{parent}") if parent else (404, {})
    base_tree_sha = base_tree.get("tree", {}).get("sha", "")

    files = tracked()
    print("文件数:", len(files))
    tree_items = []
    for rel in files:
        raw = (ROOT / rel).read_bytes()
        status, blob = call(
            "POST", f"{base}/git/blobs",
            {"content": base64.b64encode(raw).decode(), "encoding": "base64"},
        )
        if status != 201:
            print("blob 失败:", rel, status, blob.get("message"))
            return 1
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})  # privacy-ok 文件模式不是号

    payload = {"tree": tree_items}
    if base_tree_sha:
        payload["base_tree"] = base_tree_sha
    status, tree = call("POST", f"{base}/git/trees", payload)
    if status != 201:
        print("tree 失败:", status, tree.get("message"))
        return 1

    status, commit = call(
        "POST", f"{base}/git/commits",
        {"message": f"release: {tag}", "tree": tree["sha"], "parents": [parent] if parent else []},
    )
    if status != 201:
        print("commit 失败:", status, commit.get("message"))
        return 1
    sha = commit["sha"]

    status, ref = call("PATCH", f"{base}/git/refs/heads/main", {"sha": sha, "force": True})
    print("main:", status, sha)

    status, tagobj = call(
        "POST", f"{base}/git/tags",
        {"tag": tag, "message": f"海螺 {tag}", "object": sha, "type": "commit"},
    )
    if status == 201:
        st, _ = call("POST", f"{base}/git/refs", {"ref": f"refs/tags/{tag}", "sha": tagobj["sha"]})
        print("tag 已打:", tag, st)
    elif status == 422:
        print("tag 已存在，跳过:", tag)
    else:
        print("tag:", status, tagobj.get("message"))

    status, rel = call(
        "POST", f"{base}/releases",
        {"tag_name": tag, "name": f"海螺 {tag}", "body": section(ver), "draft": False},
    )
    print("release:", status, rel.get("html_url") or rel.get("message"))
    print("完成:", f"https://github.com/{REPO}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())