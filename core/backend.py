"""模型后端：M1 先接 OpenAI 兼容端点（DeepSeek 直连）。

DSH 适配器后续再加，接口不变，换实现不影响插件。
"""

from __future__ import annotations

import aiohttp

from . import log
from .config import Config

log_ = log.get("backend")


def explain_http_error(status: int, body: str) -> str:
    """把上游的报错翻成一句人话。

    这一步很重要：欠费、Key 错、模型名错，在上游都是 4xx，
    但用户看到一大段英文 JSON 只会以为是「Key 无效」，白折腾半天。
    """
    low = (body or "").lower()
    if "arrearage" in low or "overdue" in low or "in good standing" in low:
        return "账号欠费了（不是 Key 的问题）：去阿里云百炼充值，回来重试就行"
    if "insufficient" in low or "balance" in low or "quota" in low:
        return "这个账号额度用完了或余额不足"
    if status == 401 or "invalid_api_key" in low or "incorrect api key" in low:
        return "Key 不对或没权限（401）"
    if status == 403:
        return "Key 没有这个模型的权限（403）"
    if status == 404 or "model_not_found" in low or "not exist" in low:
        return "这家没有这个模型名（404），换个模型名或点「拉模型」挑一个"
    if status == 429 or "rate limit" in low:
        return "太频繁被限流了，等一会儿再试"
    if status >= 500:
        return f"上游自己出错了（HTTP {status}），过一会儿再试"
    return f"HTTP {status}：{body[:180]}"


PERM_HINT = (
    "这个账号在中转上没有开通该模型（或套餐/余额到期），不是模型名写错。"
    "去服务商控制台确认开通，或者换一把 Key / 换一个接口地址。"
)


def _friendly(status: int, message: str, raw: str = "") -> str:
    """把上游那种含糊的英文报错翻成能照着做的话。"""
    text = str(message or "")
    low = (text + " " + str(raw or "")).lower()
    if status == 403 and (
        "access to model" in low
        or "unpurchased" in low
        or "denied" in low
        or "没有这个模型的权限" in text
    ):
        return f"{text}｜{PERM_HINT}"
    if status == 401:
        return f"{text}｜Key 本身无效或已过期，换一把。"
    if status in (402, 429) and ("quota" in low or "balance" in low or "arrear" in low):
        return f"{text}｜余额/额度用完了，充值或换 Key。"
    return text


class BackendError(Exception):
    """调用失败。带上游 HTTP 码，好判断该换 Key 还是该认栽。"""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# 这些码说明「这把 Key 不行了」，换下一把还有救
_ROTATE_STATUS = {401, 402, 403, 429}


def _worth_rotating(status: int, text: str) -> bool:
    if status in _ROTATE_STATUS:
        return True
    low = (text or "").lower()
    return any(w in low for w in ("arrearage", "overdue", "insufficient", "quota", "balance"))


class OpenAICompatBackend:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._i = 0  # 现在用第几把（运行中会往前挪）

    # ---------- Key 列表 ----------

    def keys(self) -> list[dict]:
        """按摆放顺序的 Key；只有一把也返回一条。"""
        got = [k for k in (self.cfg.model.keys or []) if str(k.get("key") or "").strip()]
        if not got:
            single = str(self.cfg.model.api_key or "").strip()
            if single:
                got = [{"key": single, "label": "默认"}]
        return got

    def current_index(self) -> int:
        n = len(self.keys())
        if n == 0:
            return -1
        return min(self._i, n - 1)

    def current_label(self) -> str:
        keys = self.keys()
        idx = self.current_index()
        return str(keys[idx].get("label") or "") if idx >= 0 else ""

    def _url(self) -> str:
        base = self.cfg.model.base_url.rstrip("/")
        if not base:
            raise BackendError("model.base_url 没配置")
        return f"{base}/chat/completions"

    @staticmethod
    def _headers(key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 1.0,
    ) -> tuple[str, dict[str, int]]:
        message, usage = await self.chat_raw(messages, model=model, temperature=temperature)
        return str(message.get("content") or "").strip(), usage

    async def chat_raw(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        temperature: float = 1.0,
        tools: list[dict] | None = None,
    ) -> tuple[dict, dict[str, int]]:
        """返回整条 assistant 消息（可能带 tool_calls），给工具循环用。"""
        payload: dict = {
            "model": model or self.cfg.model.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        timeout = aiohttp.ClientTimeout(total=self.cfg.model.timeout_s)

        keys = self.keys()
        if not keys:
            raise BackendError("model.api_key 没配置（面板「模型接入」里填一把）")

        start = self.current_index() if self.current_index() >= 0 else 0
        # 从上次用过的那把开始；一把网络抖了就重试一次，
        # 欠费 / Key 失效 / 被限流就换下一把，全都试过才认栽。
        order = [keys[(start + i) % len(keys)] for i in range(len(keys))]
        last_exc: Exception | None = None

        for slot, item in enumerate(order):
            key = str(item.get("key") or "")
            label = str(item.get("label") or "") or f"第 {keys.index(item) + 1} 把"
            for attempt in range(2):
                kind, got = await self._once(payload, key, timeout)
                if kind == "ok":
                    self._i = keys.index(item)
                    if slot > 0:
                        log_.info("换用 Key「%s」成功", label)
                    return self._extract(got)
                if kind == "rotate":
                    log_.warning("Key「%s」用不了：%s；换下一把", label, got)
                    last_exc = got
                    break
                last_exc = got
                if attempt == 0:
                    log_.warning("调用失败，重试一次：%s", got)

        raise BackendError(f"模型调用失败：{last_exc}")

    async def _once(
        self, payload: dict, key: str, timeout: aiohttp.ClientTimeout
    ) -> tuple[str, object]:
        """用一把 Key 打一次，返回 (ok|rotate|net, 数据或错误)。"""
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self._url(), json=payload, headers=self._headers(key)
                ) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        err = BackendError(
                            _friendly(
                                resp.status,
                                f"{explain_http_error(resp.status, body)}（HTTP {resp.status}）",
                                body,
                            ),
                            status=resp.status,
                        )
                        return ("rotate" if _worth_rotating(resp.status, body) else "net"), err
                    return "ok", __import__("json").loads(body)
        except aiohttp.ClientError as exc:
            return "net", exc

    @staticmethod
    def _extract(data: dict) -> tuple[dict, dict[str, int]]:
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise BackendError(f"返回结构不认识：{str(data)[:300]}") from exc

        raw = data.get("usage") or {}
        details = raw.get("prompt_tokens_details") or {}
        usage = {
            "prompt_tokens": int(raw.get("prompt_tokens") or 0),
            "cached_tokens": int(details.get("cached_tokens") or 0),
            "completion_tokens": int(raw.get("completion_tokens") or 0),
        }
        return message, usage