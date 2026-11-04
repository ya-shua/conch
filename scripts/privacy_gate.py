#!/usr/bin/env python3
"""隐私门禁 —— 上传前查一遍，别把不该出去的东西带出去。

用法
----
    python scripts/privacy_gate.py --staged    只查暂存区（pre-commit 钩子用）
    python scripts/privacy_gate.py --all       查全部已跟踪文件（手动 / CI 用）
    python scripts/privacy_gate.py --files a b 查指定文件

退出码：0 = 通过；1 = 有拦截项；2 = 参数错。

为什么分级
----------
「5-11 位数字」同时匹配 QQ 号、端口、毫秒常量、字节数、测试假号。
裸扫的结果是满屏误报，人就会养成「反正都是误报」直接跳过的习惯，
那这道门禁就废了。所以分三层：

    拦截 block —— 证据足够强，几乎不可能是正常代码（本机路径、key、QQ 号、手机号）
    警告 warn  —— 像，但可能只是示例；只提示，不算失败
    放过 skip  —— 明确的正常写法（版本号、端口、毫秒、白名单数字）

误报就在那一行写 `privacy-ok`（注释里也行），这一行立刻放过。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 永远不该入库的路径（就算被 git add 了也拦下）
BLOCKED_PATHS = (
    "data/",
    "logs/",
    ".venv/",
    "__pycache__/",
    ".pytest_cache/",
    "roles/",  # 人设卡是私人的
    ".git/",
)

BLOCKED_FILES = re.compile(
    r"(^|/)(config\.json|.*\.db|.*\.sqlite3?|token\.txt|panel_auth\.json|.*\.pem|.*\.key)$"
)

# 明确正常的数字：毫秒、字节、常见端口、测试假号
BENIGN_NUMBERS = {
    "10000", "20000", "30000", "50000", "60000", "90000", "100000",
    "120000", "180000", "300000", "600000", "900000",
    "86400", "86400000", "1048576", "3600000",
    "3000", "3001", "5099", "5173", "7801", "8080", "9090", "5188", "8412",
    "123456789", "1234567890", "9876543210", "111222333", "12345678901",
}

VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")
PORT_CONTEXT_RE = re.compile(
    r"(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\]|端口|port)[^\d]{0,4}(\d{2,5})\b", re.I
)


def _hits(regex: re.Pattern[str], text: str, group: int = 0) -> list[tuple[int, int, str]]:
    out = []
    for match in regex.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        col = match.start() - (text.rfind("\n", 0, match.start()) + 1) + 1
        out.append((line, col, match.group(group) if group else match.group(0)))
    return out


def rule_paths(text: str) -> list[tuple[int, int, str]]:
    win = re.compile(r"[A-Za-z]:\\[^\s\"'`<>|]*?(?:Users|我的电脑|桌面|Desktop|AppData)[^\s\"'`<>|]*")
    nix = re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+/[^\s\"'`<>|]*")
    return _hits(win, text) + _hits(nix, text)


def rule_keys(text: str) -> list[tuple[int, int, str]]:
    out = []
    known = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
    out += _hits(known, text)
    # 形如 "access_token": "xxxx" / api_key='xxxx' 且值不是空串
    assign = re.compile(
        r"(?:access_token|ws_access_token|api_key|apikey|password|secret|token)\s*[\"']?\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']",
        re.I,
    )
    for line, col, value in _hits(assign, text, group=1):
        if value.lower() in {"your-token", "your_api_key", "changeme", "replace-me", "example"}:
            continue
        out.append((line, col, f"疑似凭据 {value[:6]}…"))
    bearer = re.compile(r"Bearer\s+([A-Za-z0-9._-]{20,})")
    out += _hits(bearer, text, group=1)
    return out


def rule_qq(text: str) -> list[tuple[int, int, str]]:
    out = []
    ctx = re.compile(r"(?:qq|uin|账号|号|群号)[^\d\n]{0,8}(\d{5,11})(?!\d)", re.I)
    for line, col, value in _hits(ctx, text, group=1):
        if value in BENIGN_NUMBERS or VERSION_RE.match(value):
            continue
        out.append((line, col, value))
    return out


def rule_phone(text: str) -> list[tuple[int, int, str]]:
    out = []
    phone = re.compile(r"(?<![\d])1[3-9]\d{9}(?![\d])")
    for line, col, value in _hits(phone, text):
        window = text[max(0, _offset(text, line, col) - 25) : _offset(text, line, col) + 25]
        if any(port in window for port in ("127.0.0.1", "localhost", "port", "端口")):
            continue
        out.append((line, col, value))
    return out


def _offset(text: str, line: int, col: int) -> int:
    parts = text.split("\n")
    return sum(len(p) + 1 for p in parts[: line - 1]) + col - 1


def rule_numbers_warn(text: str) -> list[tuple[int, int, str]]:
    out = []
    port_spans = {(m.start(1), m.group(1)) for m in PORT_CONTEXT_RE.finditer(text)}
    quoted = re.compile(r"([\"'`])(\d{5,11})\1")
    for line, col, value in _hits(quoted, text, group=2):
        if value in BENIGN_NUMBERS or VERSION_RE.match(value):
            continue
        if any(v == value for _, v in port_spans):
            continue
        out.append((line, col, value))
    return out


def rule_email_warn(text: str) -> list[tuple[int, int, str]]:
    mail = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
    return [
        (line, col, value)
        for line, col, value in _hits(mail, text)
        if "example.com" not in value and "users.noreply.github.com" not in value
    ]


RULES = [
    ("路径", "block", rule_paths),
    ("凭据", "block", rule_keys),
    ("QQ号", "block", rule_qq),
    ("手机号", "block", rule_phone),
    ("裸数字", "warn", rule_numbers_warn),
    ("邮箱", "warn", rule_email_warn),
]


def git(*args: str) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{proc.stderr.strip()[:300]}")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def pick_files(mode: str, extra: list[str]) -> list[str]:
    if mode == "staged":
        return git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    if mode == "all":
        return git("ls-files")
    return extra


def read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None
    return raw.decode("utf-8", "replace")


def main(argv: list[str]) -> int:
    try:  # Windows 控制台默认 GBK，符号会炸
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 2
    mode = argv[0].lstrip("-")
    if mode not in {"staged", "all", "files"}:
        print(f"不认识的参数：{argv[0]}")
        return 2
    files = pick_files(mode, argv[1:])
    if not files:
        print("隐私门禁：没有要查的文件")
        return 0

    blocked: list[str] = []
    warned: list[str] = []
    checked = 0

    for rel in files:
        if BLOCKED_FILES.search(rel) or any(rel.startswith(p) or f"/{p}" in rel for p in BLOCKED_PATHS):
            blocked.append(f"路径  {rel}  ← 这类文件不许入库")
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        text = read_text(path)
        if text is None:
            continue
        checked += 1
        lines = text.split("\n")
        for name, level, rule in RULES:
            for line_no, col, value in rule(text):
                source = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
                if "privacy-ok" in source:
                    continue
                item = f"{name}  {rel}:{line_no}:{col}  {value[:80]}"
                (blocked if level == "block" else warned).append(item)

    print(f"隐私门禁：查了 {checked} 个文件")
    if warned:
        print(f"\n警告（不拦，自己看一眼）：{len(warned)} 条")
        for item in warned[:40]:
            print("  [!] " + item)
    if blocked:
        print(f"\n拦截：{len(blocked)} 条")
        for item in blocked[:60]:
            print("  [x] " + item)
        print("\n处理完再提交；误报就在那一行加 privacy-ok。")
        return 1
    print("\n未发现问题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))