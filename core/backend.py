"""模型后端：M1 先接 OpenAI 兼容端点（DeepSeek 直连）。

DSH 适配器后续再加，接口不变，换实现不影响插件。
"""

from __future__ import annotations

import aiohttp

from . import log
from .config import Config

log_ = log.get("backend")


class BackendError(Exception):
    pass


class OpenAICompatBackend:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _url(self) -> str:
        base = self.cfg.model.base_url.rstrip("/")
        if not base:
            raise BackendError("model.base_url 没配置")
        return f"{base}/chat/completions"

    def _headers(self) -> dict[str, str]:
        if not self.cfg.model.api_key:
            raise BackendError("model.api_key 没配置")
        return {
            "Authorization": f"Bearer {self.cfg.model.api_key}",
            "Content-Type": "application/json",
        }

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

        last_exc: Exception | None = None
        for attempt in range(2):  # 只重试一次，网络抖动够用了
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        self._url(), json=payload, headers=self._headers()
                    ) as resp:
                        body = await resp.text()
                        if resp.status != 200:
                            raise BackendError(f"HTTP {resp.status}: {body[:300]}")
                        data = __import__("json").loads(body)
                        return self._extract(data)
            except (aiohttp.ClientError, BackendError) as exc:
                last_exc = exc
                if attempt == 0:
                    log_.warning("调用失败，重试一次：%s", exc)
        raise BackendError(f"模型调用失败：{last_exc}")

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