"""SQLite 存储：账本、会话、插件私有 KV。

只用一个连接、一把锁。M1 是单进程单事件循环，够用；
真到并发瓶颈再换连接池，不要提前设计。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 6

_MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        plugin_id TEXT NOT NULL,
        feature TEXT NOT NULL DEFAULT 'chat',
        conv_id TEXT NOT NULL DEFAULT '',
        turn_id TEXT NOT NULL DEFAULT '',
        step INTEGER NOT NULL DEFAULT 0,
        retry INTEGER NOT NULL DEFAULT 0,
        model TEXT NOT NULL DEFAULT '',
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        cost_yuan REAL NOT NULL DEFAULT 0,
        peak INTEGER NOT NULL DEFAULT 0,
        UNIQUE (conv_id, turn_id, step, retry)
    );
    CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
    CREATE INDEX IF NOT EXISTS idx_usage_plugin ON usage(plugin_id);
    CREATE TABLE IF NOT EXISTS sessions (
        conv_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL DEFAULT 'private',
        ai_session_id TEXT,
        role TEXT NOT NULL DEFAULT 'default',
        policy_json TEXT NOT NULL DEFAULT '{}',
        first_wake INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS kv (
        plugin_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value_json TEXT NOT NULL,
        PRIMARY KEY (plugin_id, key)
    );
    """,
    # v2：聊天流水、词库、审批
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        conv_id TEXT NOT NULL,
        direction TEXT NOT NULL,
        sender_id TEXT NOT NULL DEFAULT '',
        sender_name TEXT NOT NULL DEFAULT '',
        text TEXT NOT NULL DEFAULT '',
        message_id TEXT NOT NULL DEFAULT '',
        plugin_id TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, id DESC);
    CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts);

    CREATE TABLE IF NOT EXISTS lexicon (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        term TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL DEFAULT 'slang',
        note TEXT NOT NULL DEFAULT '',
        ts INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        conv_id TEXT NOT NULL,
        turn_id TEXT NOT NULL DEFAULT '',
        question TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending',
        decided_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_appr_status ON approvals(status, id DESC);
    """,
    # v3：QQ 表情包收藏
    """
    CREATE TABLE IF NOT EXISTS stickers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hash TEXT NOT NULL UNIQUE,
        url TEXT NOT NULL DEFAULT '',
        file TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        sender_id TEXT NOT NULL DEFAULT '',
        sender_name TEXT NOT NULL DEFAULT '',
        conv_id TEXT NOT NULL DEFAULT '',
        uses INTEGER NOT NULL DEFAULT 0,
        ts INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_sticker_ts ON stickers(id DESC);
    """,
    # v4：表情包来源（qq_favorite / collected）、备注、最后一次使用
    """
    ALTER TABLE stickers ADD COLUMN source TEXT NOT NULL DEFAULT 'collected';
    ALTER TABLE stickers ADD COLUMN remark TEXT NOT NULL DEFAULT '';
    ALTER TABLE stickers ADD COLUMN last_used INTEGER NOT NULL DEFAULT 0;
    """,
    # v5：词库记一下这条解释是哪来的（人工 / 大模型 / 联网）
    """
    ALTER TABLE lexicon ADD COLUMN source TEXT NOT NULL DEFAULT '';
    """,
    # v6：解释记录 —— 每次解释都留档，可以搜、可以改，挑一条认定为默认
    """
    CREATE TABLE IF NOT EXISTS lexicon_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        term TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        evidence TEXT NOT NULL DEFAULT '',
        picked INTEGER NOT NULL DEFAULT 0,
        ts INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_lexhist_term ON lexicon_history(term);
    CREATE INDEX IF NOT EXISTS idx_lexhist_ts ON lexicon_history(id DESC);
    """,
    # v7：表情包她得看得懂 —— 视觉模型给的一句意思 + 标签，
    # 另记一张「看过没」的表：同一张图永远只调一次模型
    """
    ALTER TABLE stickers ADD COLUMN meaning TEXT NOT NULL DEFAULT '';
    ALTER TABLE stickers ADD COLUMN tags TEXT NOT NULL DEFAULT '';
    CREATE TABLE IF NOT EXISTS sticker_vision (
        hash TEXT PRIMARY KEY,
        meaning TEXT NOT NULL DEFAULT '',
        tags TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        ts INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'ok'
    );
    CREATE INDEX IF NOT EXISTS idx_sticker_vision_ts ON sticker_vision(ts DESC);
    """,
    # v8：两个区 + 重试
    #   · 未收录的图在「缓存区」里等她要，命中缓存就不用再下载、再理解一次
    #   · 模型调用失败的进「缓冲区」，记下第几次、什么时候再试
    """
    ALTER TABLE stickers ADD COLUMN last_seen INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE sticker_vision ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE sticker_vision ADD COLUMN next_at INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX IF NOT EXISTS idx_sticker_vision_next ON sticker_vision(next_at);
    """,
    # v9：按天计数（聊天看图每天几次这类），放在海螺自己的库里，不动记忆库
    """
    CREATE TABLE IF NOT EXISTS counters (
        name TEXT NOT NULL,
        day TEXT NOT NULL,
        n INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (name, day)
    );
    """,
    # v10：同一张图当天只认一次（按图链里的 fileid 认人），省钱也省时间
    """
    CREATE TABLE IF NOT EXISTS look_cache (
        key TEXT PRIMARY KEY,
        day TEXT NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        ts INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_look_cache_ts ON look_cache(ts);
    """,
]


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()

    # ---------- schema ----------

    def ensure_schema(self) -> None:
        with self._lock:
            cur = self.conn.cursor()
            for i, sql in enumerate(_MIGRATIONS, start=1):
                if i > self._current_version(cur):
                    cur.executescript(sql)
                    cur.execute(
                        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(i),),
                    )
            self.conn.commit()

    def _current_version(self, cur: sqlite3.Cursor) -> int:
        try:
            row = cur.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row["value"]) if row else 0

    # ---------- kv ----------

    def kv_get(self, plugin_id: str, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute(
                "SELECT value_json FROM kv WHERE plugin_id=? AND key=?", (plugin_id, key)
            ).fetchone()
        return json.loads(row["value_json"]) if row else default

    def kv_put(self, plugin_id: str, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        with self._lock:
            self.conn.execute(
                "INSERT INTO kv(plugin_id, key, value_json) VALUES(?,?,?) "
                "ON CONFLICT(plugin_id, key) DO UPDATE SET value_json=excluded.value_json",
                (plugin_id, key, payload),
            )
            self.conn.commit()

    # ---------- sessions ----------

    def session_get(self, conv_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM sessions WHERE conv_id=?", (conv_id,)).fetchone()
        return dict(row) if row else None

    def session_upsert(self, conv_id: str, **fields: Any) -> None:
        existing = self.session_get(conv_id) or {}
        merged = {**existing, **fields, "updated_at": int(time.time() * 1000)}
        cols = ["conv_id", "kind", "ai_session_id", "role", "policy_json", "first_wake", "updated_at"]
        values = [
            conv_id,
            merged.get("kind", "private"),
            merged.get("ai_session_id"),
            merged.get("role", "default"),
            json.dumps(merged.get("policy", {}), ensure_ascii=False)
            if isinstance(merged.get("policy"), (dict, list))
            else merged.get("policy_json", "{}"),
            int(merged.get("first_wake", 0)),
            merged["updated_at"],
        ]
        with self._lock:
            self.conn.execute(
                f"INSERT INTO sessions({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
                f"ON CONFLICT(conv_id) DO UPDATE SET "
                + ",".join(f"{c}=excluded.{c}" for c in cols[1:]),
                values,
            )
            self.conn.commit()

    # ---------- usage ----------

    def usage_add(self, row: dict[str, Any]) -> None:
        """写一条用量。同一 (conv_id, turn_id, step, retry) 重放只保留一条。"""
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO usage(ts, plugin_id, feature, conv_id, turn_id, step, retry,
                                  model, prompt_tokens, cached_tokens, completion_tokens,
                                  cost_yuan, peak)
                VALUES(:ts,:plugin_id,:feature,:conv_id,:turn_id,:step,:retry,
                       :model,:prompt_tokens,:cached_tokens,:completion_tokens,
                       :cost_yuan,:peak)
                ON CONFLICT(conv_id, turn_id, step, retry) DO UPDATE SET
                    ts=excluded.ts,
                    plugin_id=excluded.plugin_id,
                    feature=excluded.feature,
                    model=excluded.model,
                    prompt_tokens=excluded.prompt_tokens,
                    cached_tokens=excluded.cached_tokens,
                    completion_tokens=excluded.completion_tokens,
                    cost_yuan=excluded.cost_yuan,
                    peak=excluded.peak
                """,
                {
                    "ts": int(row.get("ts") or time.time() * 1000),
                    "plugin_id": row.get("plugin_id") or "unknown",
                    "feature": row.get("feature") or "chat",
                    "conv_id": row.get("conv_id") or "",
                    "turn_id": row.get("turn_id") or "",
                    "step": int(row.get("step") or 0),
                    "retry": int(row.get("retry") or 0),
                    "model": row.get("model") or "",
                    "prompt_tokens": int(row.get("prompt_tokens") or 0),
                    "cached_tokens": int(row.get("cached_tokens") or 0),
                    "completion_tokens": int(row.get("completion_tokens") or 0),
                    "cost_yuan": float(row.get("cost_yuan") or 0),
                    "peak": 1 if row.get("peak") else 0,
                },
            )
            self.conn.commit()

    def usage_summary(
        self,
        since_ms: int,
        *,
        plugin_id: str | None = None,
        conv_id: str | None = None,
        feature: str | None = None,
        group_by: Iterable[str] = ("plugin_id",),
    ) -> list[dict[str, Any]]:
        where = ["ts >= ?"]
        args: list[Any] = [since_ms]
        for col, val in (("plugin_id", plugin_id), ("conv_id", conv_id), ("feature", feature)):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)

        cols = [c for c in group_by if c in {"plugin_id", "feature", "conv_id", "model"}]
        select = ", ".join(cols) if cols else "'all' AS scope"
        group = ", ".join(cols) if cols else "1"

        sql = f"""
            SELECT {select},
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(cached_tokens) AS cached_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(cost_yuan) AS cost_yuan,
                   COUNT(*) AS calls
            FROM usage
            WHERE {' AND '.join(where)}
            GROUP BY {group}
            ORDER BY cost_yuan DESC
        """
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def usage_total(self, since_ms: int) -> dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT COALESCE(SUM(cost_yuan),0) AS cost_yuan,
                       COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
                       COALESCE(SUM(cached_tokens),0) AS cached_tokens,
                       COALESCE(SUM(completion_tokens),0) AS completion_tokens,
                       COUNT(*) AS calls
                FROM usage WHERE ts >= ?
                """,
                (since_ms,),
            ).fetchone()
        return dict(row)

    # ---------- messages ----------

    def message_add(
        self,
        conv_id: str,
        direction: str,
        text: str,
        *,
        sender_id: str = "",
        sender_name: str = "",
        message_id: str = "",
        plugin_id: str = "",
        ts: int | None = None,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO messages(ts, conv_id, direction, sender_id, sender_name, text, message_id, plugin_id)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    int(ts or time.time() * 1000),
                    conv_id,
                    direction,
                    sender_id,
                    sender_name,
                    text[:4000],
                    message_id,
                    plugin_id,
                ),
            )
            self.conn.commit()

    def message_list(
        self, conv_id: str | None = None, limit: int = 80, before_id: int | None = None
    ) -> list[dict[str, Any]]:
        where, args = [], []
        if conv_id:
            where.append("conv_id = ?")
            args.append(conv_id)
        if before_id:
            where.append("id < ?")
            args.append(int(before_id))
        sql = "SELECT * FROM messages"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(500, limit)))
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def message_convs(self, limit: int = 50) -> list[dict[str, Any]]:
        """每个会话：条数、最后一条时间、最后一句。"""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT m.conv_id AS conv_id,
                       COUNT(*) AS total,
                       MAX(m.ts) AS last_ts,
                       (SELECT text FROM messages x WHERE x.conv_id = m.conv_id ORDER BY x.id DESC LIMIT 1) AS last_text
                FROM messages m
                GROUP BY m.conv_id
                ORDER BY last_ts DESC
                LIMIT ?
                """,
                (max(1, min(200, limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def message_clear(self, conv_id: str) -> int:
        with self._lock:
            cur = self.conn.execute("DELETE FROM messages WHERE conv_id = ?", (conv_id,))
            self.conn.commit()
        return cur.rowcount

    # ---------- lexicon ----------

    def lexicon_list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM lexicon ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def lexicon_add(
        self, term: str, kind: str = "slang", note: str = "", source: str = ""
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO lexicon(term, kind, note, source, ts) VALUES(?,?,?,?,?)
                ON CONFLICT(term) DO UPDATE SET kind=excluded.kind, note=excluded.note,
                    source=excluded.source, ts=excluded.ts
                """,
                (term.strip()[:80], kind, note[:500], source[:12], int(time.time() * 1000)),
            )
            self.conn.commit()

    def lexicon_set_note(self, term: str, note: str, source: str = "") -> int:
        """改一条黑话的含义（不动类型）。"""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE lexicon SET note = ?, source = ?, ts = ? WHERE term = ?",
                (note[:500], source[:12], int(time.time() * 1000), term.strip()[:80]),
            )
            self.conn.commit()
        return cur.rowcount

    # ---------- 解释记录 ----------

    def lex_history_add(self, term: str, note: str, source: str, evidence: str = "") -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO lexicon_history(term, note, source, evidence, picked, ts) VALUES(?,?,?,?,0,?)",
                (term, note, source, (evidence or "")[:2000], int(time.time())),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def lex_history_list(self, q: str = "", limit: int = 300) -> list[dict[str, Any]]:
        q = (q or "").strip()
        if q:
            like = f"%{q}%"
            rows = self.conn.execute(
                "SELECT * FROM lexicon_history WHERE term LIKE ? OR note LIKE ? ORDER BY id DESC LIMIT ?",
                (like, like, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM lexicon_history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def lex_history_update(self, hid: int, note: str) -> int:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE lexicon_history SET note = ?, ts = ? WHERE id = ?",
                (note, int(time.time()), hid),
            )
            self.conn.commit()
            return cur.rowcount

    def lex_history_get(self, hid: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM lexicon_history WHERE id = ?", (hid,)).fetchone()
        return dict(row) if row else None

    def lex_history_pick(self, hid: int) -> dict[str, Any] | None:
        """把这条认定为该词的默认解释：词库里写它，记录里只留这一条 picked。"""
        row = self.lex_history_get(hid)
        if not row:
            return None
        term = row["term"]
        with self._lock:
            self.conn.execute("UPDATE lexicon_history SET picked = 0 WHERE term = ?", (term,))
            self.conn.execute("UPDATE lexicon_history SET picked = 1 WHERE id = ?", (hid,))
            self.conn.execute(
                "UPDATE lexicon SET note = ?, source = ?, ts = ? WHERE term = ?",
                (row["note"], "人工", int(time.time()), term),
            )
            self.conn.execute(
                "INSERT INTO lexicon(term, kind, note, source, ts) "
                "SELECT ?, 'slang', ?, '人工', ? WHERE NOT EXISTS (SELECT 1 FROM lexicon WHERE term = ?)",
                (term, row["note"], int(time.time()), term),
            )
            self.conn.commit()
        return row

    def lexicon_remove(self, term: str) -> int:
        with self._lock:
            cur = self.conn.execute("DELETE FROM lexicon WHERE term = ?", (term,))
            self.conn.commit()
        return cur.rowcount

    # ---------- approvals ----------

    def approval_add(self, conv_id: str, question: str, payload: dict[str, Any] | None = None,
                     turn_id: str = "") -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO approvals(ts, conv_id, turn_id, question, payload_json) VALUES(?,?,?,?,?)",
                (
                    int(time.time() * 1000),
                    conv_id,
                    turn_id,
                    question[:2000],
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def approval_list(self, status: str = "pending", limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM approvals WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, max(1, min(200, limit))),
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["payload"] = json.loads(item.pop("payload_json"))
            except Exception:
                item["payload"] = {}
            out.append(item)
        return out

    def approval_decide(self, approval_id: int, status: str) -> bool:
        if status not in ("approved", "rejected"):
            raise ValueError("status must be approved/rejected")
        with self._lock:
            cur = self.conn.execute(
                "UPDATE approvals SET status=?, decided_at=? WHERE id=? AND status='pending'",
                (status, int(time.time() * 1000), int(approval_id)),
            )
            self.conn.commit()
        return cur.rowcount > 0

    # ---------- 表情包 ----------
    def sticker_add(
        self,
        digest: str,
        url: str = "",
        file: str = "",
        summary: str = "",
        sender_id: str = "",
        sender_name: str = "",
        conv_id: str = "",
        ts: int = 0,
        source: str = "collected",
        remark: str = "",
        meaning: str = "",
        tags: str = "",
    ) -> int:
        """收一张表情；已经有了就把次数加一（用来排「最常用」）。"""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("SELECT id, source, meaning FROM stickers WHERE hash = ?", (digest,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE stickers SET uses = uses + 1,"
                    " file = CASE WHEN file = '' THEN ? ELSE file END,"
                    " url = CASE WHEN url = '' THEN ? ELSE url END,"
                    " meaning = CASE WHEN meaning = '' THEN ? ELSE meaning END,"
                    " tags = CASE WHEN tags = '' THEN ? ELSE tags END"
                    " WHERE id = ?",
                    (file, url, meaning, tags, row["id"]),
                )
                self.conn.commit()
                return int(row["id"])
            cur.execute(
                "INSERT INTO stickers (hash, url, file, summary, sender_id, sender_name,"
                " conv_id, uses, ts, source, remark, meaning, tags)"
                " VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?)",
                (
                    digest, url, file, summary, sender_id, sender_name, conv_id, ts,
                    source, remark, meaning, tags,
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def sticker_list(
        self, limit: int = 200, offset: int = 0, source: str = ""
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM stickers"
        args: list[Any] = []
        if source:
            sql += " WHERE source = ?"
            args.append(source)
        sql += " ORDER BY uses DESC, id DESC LIMIT ? OFFSET ?"
        args.extend([int(limit), int(offset)])
        with self._lock:
            cur = self.conn.execute(sql, tuple(args))
            return [dict(r) for r in cur.fetchall()]

    def sticker_count(self, source: str = "") -> int:
        with self._lock:
            if source:
                cur = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM stickers WHERE source = ?", (source,)
                )
            else:
                cur = self.conn.execute("SELECT COUNT(*) AS n FROM stickers")
            return int(cur.fetchone()["n"])

    def sticker_get(self, sticker_id: int) -> dict[str, Any] | None:
        with self._lock:
            cur = self.conn.execute("SELECT * FROM stickers WHERE id = ?", (int(sticker_id),))
            row = cur.fetchone()
            return dict(row) if row else None

    def sticker_find(self, name: str) -> dict[str, Any] | None:
        """按备注 / 意思 / 标签 / 摘要 / 来源人找一张，给「发个 xxx 表情」用。

        先认「正好就是这个标签」的（`无语` 命中 tag=无语 的那张），
        没有再看包含关系；同分时用过的优先。
        """
        text = (name or "").strip()
        if not text:
            return None
        with self._lock:
            cur = self.conn.execute(
                "SELECT *, CASE"
                "  WHEN tags = ? THEN 2"
                "  WHEN remark = ? THEN 1"
                "  ELSE 0 END AS hit"
                " FROM stickers WHERE file != '' AND source != 'candidate' AND"
                " (remark LIKE ? OR meaning LIKE ? OR tags LIKE ? OR summary LIKE ?"
                "  OR sender_name LIKE ?)"
                " ORDER BY hit DESC, uses DESC, id DESC LIMIT 1",
                (text, text, f"%{text}%", f"%{text}%", f"%{text}%", f"%{text}%",
                 f"%{text}%"),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def sticker_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM stickers WHERE source != 'candidate'"
                " ORDER BY uses DESC, id DESC LIMIT ?", (int(limit),)
            )
            return [dict(r) for r in cur.fetchall()]

    def sticker_random(self, exclude_ids: list[int] | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM stickers WHERE file != '' AND source != 'candidate'"
        args: list[Any] = []
        if exclude_ids:
            sql += " AND id NOT IN (" + ",".join("?" * len(exclude_ids)) + ")"
            args.extend(int(i) for i in exclude_ids)
        sql += " ORDER BY RANDOM() LIMIT 1"
        with self._lock:
            cur = self.conn.execute(sql, tuple(args))
            row = cur.fetchone()
            return dict(row) if row else None

    # ---------- 候选池：她还没说要的 ----------

    def sticker_candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            # 又见到同一张时 last_seen 会刷新，它就该排到最上面
            cur = self.conn.execute(
                "SELECT * FROM stickers WHERE source = 'candidate'"
                " ORDER BY COALESCE(NULLIF(last_seen, 0), ts) DESC, id DESC LIMIT ?",
                (int(limit),)
            )
            return [dict(r) for r in cur.fetchall()]

    def sticker_promote(self, sticker_id: int, remark: str = "") -> bool:
        """候选 → 正式收下（她开口要的，或者主人在面板上点的）。"""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE stickers SET source = 'collected',"
                " remark = CASE WHEN ? != '' THEN ? ELSE remark END"
                " WHERE id = ?",
                (remark, remark[:40], int(sticker_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def sticker_expire_candidates(self, days: int, now_ms: int = 0) -> list[str]:
        """候选放太久就清掉，返回要删的文件名。

        时间从「最后一次见到」算：她又被刷到一次，就重新开始计时。
        """
        if days <= 0:
            return []
        cutoff = (now_ms or int(time.time() * 1000)) - int(days) * 86400_000
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT id, file FROM stickers WHERE source = 'candidate'"
                " AND COALESCE(NULLIF(last_seen, 0), ts) < ?",
                (cutoff,),
            )
            rows = [(int(r[0]), str(r[1] or "")) for r in cur.fetchall()]
            if not rows:
                return []
            cur.executemany(
                "DELETE FROM stickers WHERE id = ?", [(sid,) for sid, _ in rows]
            )
            self.conn.commit()
            return [f for _, f in rows if f]

    def sticker_set_meaning(self, sticker_id: int, meaning: str, tags: str = "") -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE stickers SET meaning = ?, tags = ? WHERE id = ?",
                (meaning[:200], tags[:200], int(sticker_id)),
            )
            self.conn.commit()

    # ---------- 视觉模型看过的账（同一张只调一次） ----------

    def sticker_vision_get(self, digest: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM sticker_vision WHERE hash = ?", (str(digest),)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def sticker_vision_put(
        self, digest: str, meaning: str, tags: str, model: str = "", status: str = "ok"
    ) -> None:
        """看得懂就记一条（attempts+1、清掉重试时间）。"""
        with self._lock:
            self.conn.execute(
                "INSERT INTO sticker_vision (hash, meaning, tags, model, ts, status, attempts, next_at)"
                " VALUES (?,?,?,?,?,?,1,0)"
                " ON CONFLICT(hash) DO UPDATE SET meaning=excluded.meaning,"
                " tags=excluded.tags, model=excluded.model, ts=excluded.ts,"
                " status=excluded.status, attempts=attempts + 1, next_at=0",
                (str(digest), meaning[:200], tags[:200], model[:40],
                 int(time.time() * 1000), status),
            )
            self.conn.commit()

    def counter_bump(self, name: str, day: str, step: int = 1) -> int:
        """今天这个计数 +1（跨天自动从 0 开始），返回加完的值。"""
        with self._lock:
            self.conn.execute(
                "INSERT INTO counters (name, day, n) VALUES (?,?,?)"
                " ON CONFLICT(name, day) DO UPDATE SET n = n + excluded.n",
                (str(name), str(day), int(step)),
            )
            self.conn.commit()
            cur = self.conn.execute(
                "SELECT n FROM counters WHERE name = ? AND day = ?", (str(name), str(day))
            )
            row = cur.fetchone()
            return int(row["n"]) if row else int(step)

    def counter_get(self, name: str, day: str) -> int:
        with self._lock:
            cur = self.conn.execute(
                "SELECT n FROM counters WHERE name = ? AND day = ?", (str(name), str(day))
            )
            row = cur.fetchone()
            return int(row["n"]) if row else 0

    def counter_cleanup(self, keep_days: int = 14) -> int:
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - keep_days * 86400))
        with self._lock:
            cur = self.conn.execute("DELETE FROM counters WHERE day < ?", (cutoff,))
            self.conn.commit()
            return max(0, cur.rowcount)

    def look_cache_get(self, key: str, day: str) -> str:
        """同一张图今天认过没有：认过就直接用那句描述，不再调模型、不占额度。"""
        if not key:
            return ""
        with self._lock:
            cur = self.conn.execute(
                "SELECT text FROM look_cache WHERE key = ? AND day = ?", (str(key), str(day))
            )
            row = cur.fetchone()
            return str(row["text"]) if row else ""

    def look_cache_put(self, key: str, day: str, text: str) -> None:
        if not key or not text:
            return
        with self._lock:
            self.conn.execute(
                "INSERT INTO look_cache (key, day, text, ts) VALUES (?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET day = excluded.day, text = excluded.text, ts = excluded.ts",
                (str(key), str(day), str(text), int(time.time())),
            )
            self.conn.commit()

    def look_cache_cleanup(self, keep_days: int = 1) -> int:
        """聊天看图只是「当天别重复花钱」：默认只留今天，跨天就清。"""
        cutoff = time.strftime(
            "%Y-%m-%d", time.localtime(time.time() - max(0, int(keep_days) - 1) * 86400)
        )
        with self._lock:
            cur = self.conn.execute("DELETE FROM look_cache WHERE day < ?", (cutoff,))
            self.conn.commit()
            return max(0, cur.rowcount)

    def sticker_vision_delete(self, digest: str) -> None:
        """图删了，账也删：没图的意思没法核对，留着只是占地方。"""
        if not digest:
            return
        with self._lock:
            self.conn.execute("DELETE FROM sticker_vision WHERE hash = ?", (str(digest),))
            self.conn.commit()

    def sticker_vision_prune_orphans(self) -> int:
        """清掉图已经不在库里的账（历史遗留 / 候选过期删掉的）。"""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM sticker_vision WHERE hash NOT IN (SELECT hash FROM stickers)"
            )
            self.conn.commit()
            return max(0, cur.rowcount)

    def sticker_vision_last_model(self) -> str:
        """最近一次真看成用的哪个模型（面板显示用：look 块没写 model 时靠它）。"""
        with self._lock:
            cur = self.conn.execute(
                "SELECT model FROM sticker_vision WHERE status = 'ok' AND model != ''"
                " ORDER BY ts DESC LIMIT 1"
            )
            row = cur.fetchone()
            return str(row["model"]) if row else ""

    def sticker_vision_fail(self, digest: str, next_at: int = 0) -> int:
        """没看成：记一次失败 + 下次什么时候再试，返回已经试了几次。"""
        now = int(time.time() * 1000)
        with self._lock:
            self.conn.execute(
                "INSERT INTO sticker_vision (hash, meaning, tags, model, ts, status, attempts, next_at)"
                " VALUES (?, '', '', '', ?, 'fail', 1, ?)"
                " ON CONFLICT(hash) DO UPDATE SET ts=excluded.ts, status='fail',"
                " attempts=attempts + 1, next_at=excluded.next_at",
                (str(digest), now, int(next_at)),
            )
            self.conn.commit()
            cur = self.conn.execute(
                "SELECT attempts FROM sticker_vision WHERE hash = ?", (str(digest),)
            )
            row = cur.fetchone()
            return int(row["attempts"]) if row else 1

    def sticker_vision_due(self, limit: int = 20, now_ms: int = 0) -> list[dict[str, Any]]:
        """缓冲区里到点该重试的（按排队先后）。"""
        now = now_ms or int(time.time() * 1000)
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM sticker_vision WHERE status = 'fail' AND next_at <= ?"
                " ORDER BY next_at ASC LIMIT ?",
                (now, int(limit)),
            )
            return [dict(r) for r in cur.fetchall()]

    def sticker_vision_failed_hashes(self) -> set[str]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT hash FROM sticker_vision WHERE status = 'fail'"
            )
            return {str(r["hash"]) for r in cur.fetchall()}

    def sticker_vision_trim_fails(self, max_rows: int) -> int:
        """缓冲区（status=fail）最多排这么多张，超出丢最旧的。"""
        if max_rows <= 0:
            return 0
        with self._lock:
            cur = self.conn.execute(
                "SELECT hash FROM sticker_vision WHERE status = 'fail'"
                " ORDER BY next_at DESC, ts DESC LIMIT -1 OFFSET ?",
                (int(max_rows),),
            )
            extra = [str(r["hash"]) for r in cur.fetchall()]
            if extra:
                self.conn.executemany(
                    "DELETE FROM sticker_vision WHERE hash = ?", [(h,) for h in extra]
                )
                self.conn.commit()
            return len(extra)

    def sticker_vision_prune(self, keep_days: int, max_rows: int, keep_hashes: set[str]) -> int:
        """清理理解记录：已收下的永久留；没收录的过 N 天删；总数超上限按时间淘汰。"""
        removed = 0
        with self._lock:
            if keep_days > 0:
                cutoff = int(time.time() * 1000) - int(keep_days) * 86400_000
                keep = [h for h in keep_hashes]
                sql = "DELETE FROM sticker_vision WHERE ts < ?"
                args: list[Any] = [cutoff]
                if keep:
                    sql += " AND hash NOT IN (" + ",".join("?" * len(keep)) + ")"
                    args.extend(keep)
                cur = self.conn.execute(sql, tuple(args))
                removed += max(0, cur.rowcount)
            if max_rows > 0:
                cur = self.conn.execute(
                    "SELECT hash FROM sticker_vision ORDER BY ts DESC LIMIT -1 OFFSET ?",
                    (int(max_rows),),
                )
                extra = [str(r["hash"]) for r in cur.fetchall() if str(r["hash"]) not in keep_hashes]
                if extra:
                    self.conn.executemany(
                        "DELETE FROM sticker_vision WHERE hash = ?", [(h,) for h in extra]
                    )
                    removed += len(extra)
            self.conn.commit()
        return removed

    def sticker_vision_count_today(self) -> int:
        """今天「真看成了」几张（失败不占每天额度）。"""
        start = int(time.time() // 86400 * 86400 * 1000)
        with self._lock:
            cur = self.conn.execute(
                "SELECT COUNT(*) AS n FROM sticker_vision WHERE ts >= ? AND status = 'ok'",
                (start,),
            )
            return int(cur.fetchone()["n"])

    def sticker_touch(self, digest: str) -> None:
        """缓存命中：更新 last_seen（淘汰时专挑最久没出现的）。"""
        with self._lock:
            self.conn.execute(
                "UPDATE stickers SET last_seen = ? WHERE hash = ?",
                (int(time.time() * 1000), str(digest)),
            )
            self.conn.commit()

    def sticker_oldest_candidates(self, limit: int = 1) -> list[dict[str, Any]]:
        """缓存区里最久没出现的那几张（淘汰用）。"""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM stickers WHERE source = 'candidate'"
                " ORDER BY COALESCE(NULLIF(last_seen, 0), ts) ASC LIMIT ?",
                (int(limit),),
            )
            return [dict(r) for r in cur.fetchall()]

    def sticker_trim_candidates(self, max_items: int) -> list[str]:
        """缓存区超过上限：淘汰最久没被提过的，返回要删的文件。"""
        if max_items <= 0:
            return []
        with self._lock:
            cur = self.conn.execute(
                "SELECT id, file FROM stickers WHERE source = 'candidate'"
                " ORDER BY COALESCE(NULLIF(last_seen, 0), ts) ASC"
            )
            rows = [(int(r["id"]), str(r["file"] or "")) for r in cur.fetchall()]
            extra = rows[: max(0, len(rows) - int(max_items))]
            if extra:
                self.conn.executemany(
                    "DELETE FROM stickers WHERE id = ?", [(sid,) for sid, _ in extra]
                )
                self.conn.commit()
            return [f for _, f in extra if f]

    def sticker_by_hash(self, digest: str) -> dict[str, Any] | None:
        with self._lock:
            cur = self.conn.execute("SELECT * FROM stickers WHERE hash = ?", (str(digest),))
            row = cur.fetchone()
            return dict(row) if row else None

    def sticker_mark_used(self, sticker_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE stickers SET uses = uses + 1, last_used = ? WHERE id = ?",
                (int(time.time() * 1000), int(sticker_id)),
            )
            self.conn.commit()

    def sticker_set_remark(self, sticker_id: int, remark: str) -> bool:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE stickers SET remark = ? WHERE id = ?", (remark[:40], int(sticker_id))
            )
            self.conn.commit()
            return cur.rowcount > 0

    def sticker_files(self) -> list[tuple[int, str]]:
        """所有记了本地文件的表情：[(id, file)]，给路径迁移用。"""
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("SELECT id, file FROM stickers WHERE file != ''")
            return [(int(r[0]), str(r[1])) for r in cur.fetchall()]

    def sticker_set_file(self, sticker_id: int, file: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE stickers SET file = ? WHERE id = ?", (file, int(sticker_id))
            )
            self.conn.commit()

    def sticker_sync_favorites(self, items: list[dict[str, Any]], ts: int = 0) -> int:
        """把 QQ「我的表情」整批写进来（按 url 去重，不动使用次数）。返回新增条数。"""
        added = 0
        with self._lock:
            cur = self.conn.cursor()
            for it in items:
                url = str(it.get("url") or "").strip()
                if not url:
                    continue
                digest = "fav:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
                cur.execute("SELECT id FROM stickers WHERE hash = ?", (digest,))
                if cur.fetchone():
                    continue
                cur.execute(
                    "INSERT INTO stickers (hash, url, file, summary, sender_id, sender_name,"
                    " conv_id, uses, ts, source, remark)"
                    " VALUES (?,?,?,?,?,?,?,0,?,?,?)",
                    (
                        digest,
                        url,
                        str(it.get("file") or ""),
                        str(it.get("summary") or "")[:80],
                        "",
                        "",
                        "",
                        ts,
                        "qq_favorite",
                        str(it.get("remark") or "")[:40],
                    ),
                )
                added += 1
            self.conn.commit()
        return added

    def sticker_remove(self, sticker_id: int) -> bool:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("DELETE FROM stickers WHERE id = ?", (int(sticker_id),))
            self.conn.commit()
            return cur.rowcount > 0

    def checkpoint(self) -> None:
        """把 WAL 合回主库。

        WAL 模式下新记录先进 `-wal` 文件，主库要等 checkpoint 才更新。
        不主动合的话，直接打开（或拷走）`海螺.db` 的人看到的是旧数据 ——
        「记录没进去」多半就是这么来的。
        """
        with self._lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.conn.close()