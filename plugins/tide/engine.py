"""心境引擎（自治，无外部依赖）。

三块状态，各自按时间衰减/累积：

  情绪  离散标签（calm/joyful/sad/angry…），半衰期衰减，强度 0~1
  需求  饿/渴/累/无聊（0~100，越高越缺）+ 思念（随无互动非线性上升）
  关系  按会话一套：亲和 / 信任 / 委屈；委屈分档，每档有自己的"地板"

一条消息进来先过 `Appraiser` 翻成增量：
  "你好棒"   → joyful↑ affinity↑（标签 praise）
  "你真笨"   → angry↑ grievance↑（标签 hostile）
  "这软件真笨" → 不针对她，情绪减半、不记委屈

然后 `plan()` 把这些翻译成发言约束：语气、字数上限、禁止事项、能不能主动开口。
"""

from __future__ import annotations

import math
import time

# ── 底层 ──────────────────────────────────────────────────────


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


# ── 情绪 ──────────────────────────────────────────────────────

PAD_ANCHOR = {
    "calm": (0.10, 0.30, 0.50),
    "joyful": (0.70, 0.60, 0.50),
    "excited": (0.60, 0.85, 0.55),
    "playful": (0.60, 0.60, 0.60),
    "affectionate": (0.70, 0.35, 0.45),
    "shy": (0.40, 0.45, 0.30),
    "sad": (-0.60, 0.25, 0.25),
    "disappointed": (-0.50, 0.30, 0.30),
    "angry": (-0.60, 0.80, 0.60),
    "annoyed": (-0.40, 0.55, 0.50),
    "tired": (-0.20, 0.15, 0.35),
    "bored": (-0.30, 0.20, 0.40),
    "worried": (-0.40, 0.55, 0.30),
    "nostalgic": (0.20, 0.30, 0.45),
}

HALF_LIFE_MIN = {
    "angry": 25, "annoyed": 40, "joyful": 60, "excited": 90, "playful": 70,
    "shy": 45, "sad": 240, "disappointed": 360, "affectionate": 720,
    "worried": 120, "nostalgic": 300, "bored": 90, "tired": 120, "calm": 60,
}

BASELINE_PAD = (0.15, 0.35, 0.50)


class Emotion:
    def __init__(self, max_delta: float = 0.4, calm_threshold: float = 0.12):
        self.levels: dict[str, float] = {}
        self.max_delta = float(max_delta)
        self.calm_threshold = float(calm_threshold)

    def decay(self, seconds: float) -> None:
        if seconds <= 0:
            return
        for label, v in list(self.levels.items()):
            hl = max(1.0, float(HALF_LIFE_MIN.get(label, 90))) * 60.0
            nv = v * 0.5 ** (seconds / hl)
            if nv < 0.01:
                self.levels.pop(label, None)
            else:
                self.levels[label] = nv

    def add(self, label: str, delta: float) -> float:
        """单次增量有上限，防一句话把状态打穿。"""
        delta = max(-self.max_delta, min(self.max_delta, float(delta)))
        v = _clamp(self.levels.get(label, 0.0) + delta, 0.0, 1.0)
        if v < 0.01:
            self.levels.pop(label, None)
        else:
            self.levels[label] = v
        return v

    @property
    def label(self) -> str:
        if not self.levels:
            return "calm"
        label, v = max(self.levels.items(), key=lambda kv: kv[1])
        return label if v >= self.calm_threshold else "calm"

    @property
    def peak(self) -> float:
        return max(self.levels.values()) if self.levels else 0.0

    @property
    def intensity(self) -> float:
        return round(self.peak if self.label != "calm" else min(self.peak, self.calm_threshold), 3)

    def pad(self) -> list[float]:
        total = min(1.0, sum(self.levels.values()))
        if total < 1e-6:
            return [round(x, 3) for x in BASELINE_PAD]
        norm = sum(self.levels.values())
        out = []
        for i in range(3):
            avg = sum(PAD_ANCHOR.get(lab, BASELINE_PAD)[i] * v for lab, v in self.levels.items()) / norm
            out.append(round(BASELINE_PAD[i] * (1 - total) + avg * total, 3))
        return out

    def to_state(self) -> dict:
        return {"levels": {k: round(v, 4) for k, v in self.levels.items()}}

    def load(self, data: dict) -> None:
        self.levels = {k: float(v) for k, v in ((data or {}).get("levels") or {}).items()}


# ── 需求 ──────────────────────────────────────────────────────

AUTO_CARE = {
    "thirst": {"at": 78.0, "to": 22.0, "notes": ["倒了杯水喝"]},
    "hunger": {"at": 80.0, "to": 20.0, "notes": ["吃了点东西"]},
    "energy": {"at": 84.0, "to": 26.0, "notes": ["打了个盹"]},
    "boredom": {
        "at": 78.0, "to": 28.0,
        "notes": ["刷了会儿视频", "听了会儿歌", "出去走了走", "翻了翻以前的聊天记录"],
    },
}

STEP_SEC = 300.0  # 5 分钟一段推进，跨很久也不会一步跳过去


class Needs:
    def __init__(self):
        self.hunger = 10.0
        self.thirst = 12.0
        self.energy = 20.0
        self.boredom = 20.0
        self.last_seen = 0.0
        self.care_log: list[str] = []
        self._care_count = 0

    def advance(self, seconds: float, night: bool, asleep: bool = False) -> None:
        if seconds <= 0:
            return
        if asleep:
            hours = seconds / 3600.0
            self.energy = _clamp(self.energy - 16.0 * hours)
            self.hunger = _clamp(self.hunger + 18.0 * 0.25 * hours)
            self.thirst = _clamp(self.thirst + 24.0 * 0.25 * hours)
            return
        remaining = float(seconds)
        while remaining > 1e-9:
            step = min(STEP_SEC, remaining)
            remaining -= step
            hours = step / 3600.0
            self.hunger = _clamp(self.hunger + 18.0 * hours)
            self.thirst = _clamp(self.thirst + 24.0 * hours)
            self.energy = _clamp(self.energy + 12.0 * hours * (1.6 if night else 1.0))
            self.boredom = _clamp(self.boredom + 20.0 * hours)
            self._self_care()

    def _self_care(self) -> None:
        for key, rule in AUTO_CARE.items():
            if getattr(self, key) >= rule["at"]:
                setattr(self, key, float(rule["to"]))
                notes = rule["notes"]
                self.care_log.append(notes[self._care_count % len(notes)])
                self._care_count += 1
                self.care_log = self.care_log[-12:]

    def longing(self, now: float, affinity: float) -> float:
        if not self.last_seen:
            return 0.0
        idle_h = max(0.0, (now - self.last_seen) / 3600.0)
        # 越亲近，等待越难熬
        tau = 6.0 * max(0.3, 1.4 - 0.8 * (affinity / 100.0))
        return round(_clamp(100.0 * (1 - math.exp(-idle_h / tau))), 1)

    def touched(self, now: float, boredom_drop: float = 25.0) -> None:
        self.last_seen = now
        self.boredom = _clamp(self.boredom - boredom_drop)

    def apply(self, deltas: dict) -> None:
        for key, val in (deltas or {}).items():
            if key in ("hunger", "thirst", "energy", "boredom"):
                setattr(self, key, _clamp(getattr(self, key) + float(val)))

    def to_dict(self, now: float, affinity: float) -> dict:
        return {
            "hunger": round(self.hunger, 1),
            "thirst": round(self.thirst, 1),
            "energy": round(self.energy, 1),
            "longing": self.longing(now, affinity),
            "boredom": round(self.boredom, 1),
        }

    def to_state(self) -> dict:
        return {
            "hunger": self.hunger, "thirst": self.thirst,
            "energy": self.energy, "boredom": self.boredom,
            "last_seen": self.last_seen,
        }

    def load(self, data: dict) -> None:
        data = data or {}
        for key in ("hunger", "thirst", "energy", "boredom", "last_seen"):
            if key in data:
                setattr(self, key, float(data[key]))


# ── 关系 ──────────────────────────────────────────────────────

MOOD_OF_TIER = {"none": "normal", "mild": "slight", "mid": "sulky", "big": "cold"}


class Bond:
    """委屈分档，每档有自己的地板：时间只能磨到地板，再往下要走对应的解法。"""

    def __init__(self, mild: float = 15, mid: float = 35, big: float = 65):
        self.mild, self.mid, self.big = float(mild), float(mid), float(big)
        self.affinity = 50.0
        self.trust = 60.0
        self.grievance = 0.0
        self.apology_seen = False
        self.comfort_seen = False
        self.calm_until = 0.0
        self.last_msg_at = 0.0
        self.her_last_asked = False
        self.last_proactive_at = 0.0
        self.unanswered = 0

    @property
    def tier(self) -> str:
        if self.grievance >= self.big:
            return "big"
        if self.grievance >= self.mid:
            return "mid"
        if self.grievance >= self.mild:
            return "mild"
        return "none"

    @property
    def mood(self) -> str:
        return MOOD_OF_TIER[self.tier]

    @property
    def tone(self) -> str:
        if self.tier in ("mid", "big"):
            return "cool"
        return "warm" if self.affinity >= 70 else "neutral"

    def floor(self, now: float) -> float:
        if self.tier == "big":
            return 0.0 if now >= self.calm_until > 0 else self.big + 1
        if self.tier == "mid":
            return 0.0 if (self.apology_seen and self.comfort_seen) else self.mid + 1
        return 0.0

    def decay(self, seconds: float, now: float) -> None:
        if seconds <= 0 or self.grievance <= 0:
            return
        fl = self.floor(now)
        g = self.grievance * 0.5 ** (seconds / (90.0 * 60.0))
        self.grievance = max(g, fl)
        if fl <= 0 and self.grievance <= 0.5:
            self.grievance = 0.0
            self.apology_seen = self.comfort_seen = False
            self.calm_until = 0.0

    def apply(self, deltas: dict, now: float) -> None:
        if not deltas:
            return
        if "affinity" in deltas:
            self.affinity = _clamp(self.affinity + float(deltas["affinity"]))
        if "trust" in deltas:
            self.trust = _clamp(self.trust + float(deltas["trust"]))
        if "grievance" in deltas:
            before = self.tier
            self.grievance = _clamp(self.grievance + float(deltas["grievance"]))
            if self.tier == "big" and before != "big":
                # 刚跨进大生气：开始冷静倒计时
                self.calm_until = now + 6 * 3600.0
                self.apology_seen = self.comfort_seen = False

    def note(self, tags: list[str], now: float) -> None:
        if "apology" in tags:
            self.apology_seen = True
        if "care" in tags:
            self.comfort_seen = True

    def hint(self, now: float) -> str:
        if self.tier == "none":
            return ""
        if self.tier == "mild":
            return "哄一哄就好"
        if self.tier == "mid":
            need = []
            if not self.apology_seen:
                need.append("道歉")
            if not self.comfort_seen:
                need.append("哄")
            return "还差：" + " + ".join(need) if need else "哄好了"
        left = round(max(0.0, self.calm_until - now) / 3600.0, 1)
        return f"还要冷静 {int(round(left))}h" if left > 0 else "冷静完了"

    def to_state(self) -> dict:
        return {
            "affinity": self.affinity, "trust": self.trust, "grievance": self.grievance,
            "apology_seen": self.apology_seen, "comfort_seen": self.comfort_seen,
            "calm_until": self.calm_until, "last_msg_at": self.last_msg_at,
            "her_last_asked": self.her_last_asked,
            "last_proactive_at": self.last_proactive_at, "unanswered": self.unanswered,
        }

    def load(self, data: dict) -> None:
        data = data or {}
        for key in ("affinity", "trust", "grievance", "calm_until", "last_msg_at", "last_proactive_at"):
            if key in data:
                setattr(self, key, float(data[key]))
        self.apology_seen = bool(data.get("apology_seen", False))
        self.comfort_seen = bool(data.get("comfort_seen", False))
        self.her_last_asked = bool(data.get("her_last_asked", False))
        self.unanswered = int(data.get("unanswered", 0))


# ── 把一句话翻成增量 ──────────────────────────────────────────

THIRD_PARTY = ["这软件", "这项目", "这东西", "这个程序", "这个插件", "公司", "同事", "老板", "领导"]

NEGATIONS = ("别", "不要", "甭", "不用", "无需", "没那么")

# 内置刺激表：没有配置也能动。想改就改插件目录里的 config.json 的 stimuli。
DEFAULT_STIMULI = [
    {"id": "praise", "keywords": ["厉害", "真棒", "好棒", "牛", "优秀", "喜欢", "可爱", "聪明"],
     "emotion": {"joyful": 0.28}, "bond": {"affinity": 3}},
    {"id": "care", "keywords": ["辛苦了", "休息", "早点睡", "多喝水", "注意身体", "别太累"],
     "emotion": {"affectionate": 0.25}, "bond": {"affinity": 4, "grievance": -6}},
    {"id": "tease", "keywords": ["笨", "傻", "憨", "呆"],
     "emotion": {"playful": 0.2}, "bond": {"affinity": 1}},
    {"id": "apology", "keywords": ["对不起", "抱歉", "我错了", "不好意思"],
     "emotion": {"sad": 0.1}, "bond": {"grievance": -10}},
    {"id": "hostile", "keywords": ["滚", "闭嘴", "烦死了", "讨厌你", "废物", "蠢"],
     "emotion": {"angry": 0.35, "sad": 0.15}, "bond": {"grievance": 20, "affinity": -6},
     "only_if_directed": True},
    {"id": "dismissive", "keywords": ["随便你", "无所谓", "懒得", "别烦我"],
     "emotion": {"disappointed": 0.25}, "bond": {"grievance": 8, "affinity": -3},
     "only_if_directed": True},
    {"id": "feeding", "keywords": ["吃了吗", "吃饭", "请你", "给你", "奶茶"],
     "emotion": {"joyful": 0.18}, "needs": {"hunger": -20}, "bond": {"affinity": 2}},
]

MOOD_WORDS = {
    "sad": ["难过", "难过", "伤心", "emo", "抑郁", "哭"],
    "angry": ["生气", "气死", "烦", "火大"],
    "anxious": ["焦虑", "紧张", "慌", "压力"],
    "tired": ["累", "困", "没力气"],
    "happy": ["开心", "高兴", "哈哈", "爽"],
}

MOOD_EFFECTS = {
    "sad": ({"worried": 0.25}, {"affinity": 1}),
    "angry": ({"worried": 0.15}, {"grievance": 3}),
    "anxious": ({"worried": 0.20}, {}),
    "tired": ({"worried": 0.10}, {}),
    "happy": ({"joyful": 0.15}, {"affinity": 1}),
}


def is_directed(text: str) -> bool:
    if "你" in text:
        return True
    return not any(m in text for m in THIRD_PARTY)


def detect_mood(text: str) -> str:
    best, best_hits = "", 0
    for label, words in MOOD_WORDS.items():
        hits = 0
        for word in words:
            idx = text.find(word)
            while idx != -1:
                pre = text[max(0, idx - 3):idx]
                if not any(n in pre for n in NEGATIONS):  # "别难过"是哄人，不是她在难过
                    hits += 1
                idx = text.find(word, idx + 1)
        if hits > best_hits:
            best, best_hits = label, hits
    return best


def appraise(text: str, rules: list[dict] | None = None) -> dict:
    """返回 {emotion, bond, needs, tags, mood, directed}。"""
    text = text or ""
    rules = rules if rules is not None else DEFAULT_STIMULI
    directed = is_directed(text)
    short = len(text.strip()) <= 3

    emotion: dict[str, float] = {}
    bond: dict[str, float] = {}
    needs: dict[str, float] = {}
    tags: list[str] = []
    mood = ""

    def merge(e=None, b=None, n=None):
        for k, v in (e or {}).items():
            emotion[k] = emotion.get(k, 0.0) + float(v)
        for k, v in (b or {}).items():
            bond[k] = bond.get(k, 0.0) + float(v)
        for k, v in (n or {}).items():
            needs[k] = needs.get(k, 0.0) + float(v)

    for rule in rules:
        keywords = [str(k) for k in (rule.get("keywords") or [])]
        if not any(k and k in text for k in keywords):
            continue
        if rule.get("requires_short") and not short:
            continue
        if rule.get("only_if_directed") and not directed:
            continue
        tags.append(str(rule.get("id") or "rule"))
        merge(rule.get("emotion"), rule.get("bond"), rule.get("needs"))

    mood = detect_mood(text)
    effects = MOOD_EFFECTS.get(mood)
    if effects:
        tags.append("mood:" + mood)
        merge(effects[0], effects[1])

    if not directed:  # 说的不是她，情绪减半、不记仇
        emotion = {k: v * 0.5 for k, v in emotion.items()}
        bond.pop("grievance", None)

    return {"emotion": emotion, "bond": bond, "needs": needs,
            "tags": tags, "mood": mood, "directed": directed}


# ── 门面 ──────────────────────────────────────────────────────


class MoodEngine:
    """一套状态：情绪（全局）+ 需求（全局）+ 关系（按会话）。"""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.quiet_start = str(cfg.get("quiet_start") or "00:30")
        self.quiet_end = str(cfg.get("quiet_end") or "07:30")
        self.threshold = float(cfg.get("urgency_threshold") or 60)
        self.cooldown_min = float(cfg.get("proactive_cooldown_min") or 90)
        self.max_unanswered = int(cfg.get("proactive_max_unanswered") or 2)
        self.rules = cfg.get("stimuli") or DEFAULT_STIMULI
        self.emotion = Emotion()
        self.needs = Needs()
        self.bonds: dict[str, Bond] = {}
        self._last = time.time()
        self.last_said_at = 0.0

    # 时间
    def is_quiet(self, now: float | None = None) -> bool:
        now = now or time.time()
        lt = time.localtime(now)
        cur = lt.tm_hour * 60 + lt.tm_min

        def parse(s: str) -> int:
            try:
                h, m = str(s).split(":")
                return int(h) * 60 + int(m)
            except Exception:
                return 0

        a, b = parse(self.quiet_start), parse(self.quiet_end)
        return a <= cur < b if a <= b else (cur >= a or cur < b)

    def bond(self, person: str) -> Bond:
        return self.bonds.setdefault(str(person), Bond())

    def _advance(self, now: float) -> None:
        dt = max(0.0, now - self._last)
        self._last = now
        if dt <= 0:
            return
        self.emotion.decay(dt)
        self.needs.advance(dt, night=self.is_quiet(now), asleep=self.is_quiet(now))
        for bond in self.bonds.values():
            bond.decay(dt, now)

    # 观察
    def observe(self, text: str, person: str, speaker: str = "other", now: float | None = None) -> dict:
        now = now or time.time()
        self._advance(now)
        bond = self.bond(person)
        bond.last_msg_at = now

        if speaker != "self":
            result = appraise(text, self.rules)
            for label, delta in result["emotion"].items():
                self.emotion.add(label, delta)
            bond.apply(result["bond"], now)
            bond.note(result["tags"], now)
            self.needs.apply(result["needs"])
            self.needs.touched(now)
            bond.unanswered = 0
        return self.snapshot(person, now)

    # 读
    def snapshot(self, person: str, now: float | None = None) -> dict:
        now = now or time.time()
        self._advance(now)
        bond = self.bond(person)
        idle_min = round((now - bond.last_msg_at) / 60.0, 1) if bond.last_msg_at else None
        return {
            "quiet": self.is_quiet(now),
            "emotion": {"label": self.emotion.label, "intensity": self.emotion.intensity,
                        "pad": self.emotion.pad()},
            "needs": self.needs.to_dict(now, bond.affinity),
            "bond": {"affinity": round(bond.affinity, 1), "trust": round(bond.trust, 1),
                     "grievance": round(bond.grievance, 1), "mood": bond.mood,
                     "tone": bond.tone, "tier": bond.tier,
                     "tier_name": {"none": "没事", "mild": "有点小情绪",
                                   "mid": "还在赌气", "big": "气还没消"}[bond.tier],
                     "hint": bond.hint(now)},
            "idle_min": idle_min,
        }

    def plan(self, person: str, now: float | None = None) -> dict:
        now = now or time.time()
        snap = self.snapshot(person, now)
        bond = self.bond(person)
        label = self.emotion.label
        needs = snap["needs"]

        # 心情决定这条回复能有多长
        if bond.mood == "cold":
            max_chars = 6
        elif bond.mood == "sulky":
            max_chars = 25
        elif bond.mood == "slight" or label == "tired" or needs["energy"] > 70:
            max_chars = 40
        else:
            max_chars = 60

        banned = []
        if bond.tier == "big":
            banned.append("讨好式道歉")
        if bond.mood in ("cold", "sulky"):
            banned.append("连续追问")

        block = (
            f"当前情绪：{label}（强度 {self.emotion.intensity:.2f}）\n"
            f"需求：饿 {needs['hunger']:.0f}｜累 {needs['energy']:.0f}"
            f"｜想你 {needs['longing']:.0f}｜无聊 {needs['boredom']:.0f}\n"
            f"关系：亲和 {bond.affinity:.0f}｜委屈 {bond.grievance:.0f}"
            f"（{bond.mood}·{bond.hint(now) or '平常'}）\n"
            f"→ 回复长度上限：{max_chars} 字\n"
            f"→ 语气：{bond.tone}\n"
            f"→ 禁止：{'；'.join(banned) if banned else '无'}"
        )

        return {
            "tone": bond.tone,
            "max_chars": max_chars,
            "banned": banned,
            "prompt": block,
            "proactive": self.decide_proactive(person, now),
        }

    def decide_proactive(self, person: str, now: float | None = None) -> dict:
        """她想不想自己开口。规则和阈值都在配置里，改动立刻生效。"""
        now = now or time.time()
        bond = self.bond(person)
        needs = self.needs.to_dict(now, bond.affinity)

        urgency = round(max(0.0,
            0.50 * needs["longing"]
            + 0.45 * needs["boredom"]
            + 0.28 * needs["energy"]
            - 0.40 * bond.grievance), 1)

        reasons = []
        if self.is_quiet(now):
            reasons.append("勿扰时段")
        if bond.unanswered >= self.max_unanswered:
            reasons.append(f"已有 {bond.unanswered} 条主动消息没回音，停发")
        if bond.last_proactive_at and now - bond.last_proactive_at < self.cooldown_min * 60:
            reasons.append("冷却中")
        if bond.grievance > 0 and urgency < 80:
            reasons.append("还在委屈，不想先开口")
        if urgency < self.threshold:
            reasons.append(f"想念不够（{urgency} < {self.threshold}）")

        return {
            "should_send": not reasons,
            "urgency": urgency,
            "threshold": self.threshold,
            "reason": "；".join(reasons) if reasons else "想你了，可以开口",
        }

    def mark_replied(self, person: str, now: float | None = None) -> None:
        """回了一句话：结算掉思念，但不动主动冒泡的冷却。"""
        now = now or time.time()
        bond = self.bond(person)
        bond.unanswered = 0
        self.last_said_at = now

    def mark_proactive(self, person: str, now: float | None = None) -> None:
        """自己主动冒泡：开始冷却，并记一笔"这条还没人回"。"""
        now = now or time.time()
        bond = self.bond(person)
        bond.last_proactive_at = now
        bond.unanswered += 1
        self.last_said_at = now

    # 存取
    def to_state(self) -> dict:
        return {
            "version": 1,
            "emotion": self.emotion.to_state(),
            "needs": self.needs.to_state(),
            "people": {k: v.to_state() for k, v in self.bonds.items()},
            "last": self._last,
        }

    def load_state(self, data: dict) -> None:
        data = data or {}
        self.emotion.load(data.get("emotion"))
        self.needs.load(data.get("needs"))
        for pid, item in (data.get("people") or {}).items():
            self.bond(pid).load(item)
        if data.get("last"):
            self._last = float(data["last"])