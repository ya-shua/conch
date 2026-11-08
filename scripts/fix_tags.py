"""把 v0.1.6 / v0.1.7 的远端提交修正到正确的版本号，并把 tag 移过去。

发现的问题：v0.1.6 那颗提交里的 __init__.py 还是 "0.1.5"（历史里从来没有提交过 0.1.6
这个版本号），所以补出来的 v0.1.6 名不副实。这里重建：

    v0.1.5(3f50b74) → v0.1.6(树=3049a31，但版本号改成 0.1.6) → v0.1.7(树=411bc62)

v0.1.8 不动（它在 main 上）。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "ya-shua/conch"
TOKEN = Path(os.path.expanduser("~/Desktop/token.txt")).read_text(encoding="utf-8").strip()
API = "https://api.github.com"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={
            "Authorization": f"token {TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "conch-fix",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() or "{}"
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"message": raw[:200]}


def git(*args: str) -> bytes:
    out = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{out.stderr[:300]!r}")
    return out.stdout


def build(commit: str, parent: str, message: str, patch_version: str | None) -> str:
    files = [p for p in git("ls-tree", "-r", "--name-only", commit).decode().splitlines() if p.strip()]
    items = []
    for rel in files:
        raw = git("show", f"{commit}:{rel}")
        if patch_version and rel == "__init__.py":
            text = raw.decode("utf-8")
            import re
            text = re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{patch_version}"', text, count=1)
            raw = text.encode("utf-8")
        st, blob = call("POST", f"/repos/{REPO}/git/blobs",
                        {"content": base64.b64encode(raw).decode(), "encoding": "base64"})
        if st != 201:
            raise SystemExit(f"blob 失败 {rel}: {st} {blob.get('message')}")
        items.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    st, tree = call("POST", f"/repos/{REPO}/git/trees", {"tree": items})
    if st != 201:
        raise SystemExit(f"tree 失败: {st} {tree.get('message')}")
    st, cm = call("POST", f"/repos/{REPO}/git/commits",
                  {"message": message, "tree": tree["sha"], "parents": [parent]})
    if st != 201:
        raise SystemExit(f"commit 失败: {st} {cm.get('message')}")
    print(f"  {message[:36]}… → {cm['sha'][:10]}（{len(items)} 文件）")
    return cm["sha"]


def move_tag(tag: str, sha: str) -> None:
    st, obj = call("POST", f"/repos/{REPO}/git/tags",
                   {"tag": tag, "message": f"海螺 {tag}", "object": sha, "type": "commit"})
    if st != 201:
        raise SystemExit(f"tag {tag} 失败: {st} {obj.get('message')}")
    st, r = call("PATCH", f"/repos/{REPO}/git/refs/tags/{tag}", {"sha": obj["sha"], "force": True})
    if st not in (200, 201):
        st, r = call("POST", f"/repos/{REPO}/git/refs", {"ref": f"refs/tags/{tag}", "sha": obj["sha"]})
    print(f"  tag {tag} → {sha[:10]}：{st}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    st, head = call("GET", f"/repos/{REPO}/git/ref/heads/main")
    st, hc = call("GET", f"/repos/{REPO}/git/commits/{head['object']['sha']}")
    base = (hc.get("parents") or [{"sha": ""}])[0]["sha"]  # v0.1.8 的父提交 = v0.1.5

    print("重建 v0.1.6 …")
    c6 = build("3049a31", base, "release: v0.1.6（工作台、插件外部化、词库联网解释）", "0.1.6")
    move_tag("v0.1.6", c6)

    print("重建 v0.1.7 …")
    c7 = build("411bc62", c6, "release: v0.1.7（面板大改、更新提示、发版门禁）", None)
    move_tag("v0.1.7", c7)

    st, tag = call("GET", f"/repos/{REPO}/git/ref/tags/v0.1.6")
    st2, obj = call("GET", f"/repos/{REPO}/git/tags/{tag['object']['sha']}")
    st3, cm = call("GET", f"/repos/{REPO}/git/commits/{obj['object']['sha']}")
    st4, blob = call("GET", f"/repos/{REPO}/git/blobs/{[t for t in call('GET', f'/repos/{REPO}/git/trees/{cm['tree']['sha']}')[1]['tree'] if t['path'] == '__init__.py'][0]['sha']}")
    print("v0.1.6 里现在写的版本:", base64.b64decode(blob["content"]).decode().strip().splitlines()[-1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())