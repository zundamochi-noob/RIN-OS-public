"""
llm.py のOllama実装（一時的）。今のollama.py/api.pyの挙動をそのまま維持する。
Bedrockへ切り替えたあとは、このファイルごと削除する。
"""
import json
import logging
from typing import AsyncIterator

import httpx

from core.config import OLLAMA_URL, MODEL

logger = logging.getLogger(__name__)


def _payload(system, messages, max_tokens, temperature, stream: bool) -> dict:
    # Ollamaはsystemもmessagesの先頭に入れる形式。systemが空なら入れない（従来のwarmup/route_toolと同じ）
    msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
    options = {}
    if max_tokens is not None:
        options["num_predict"] = max_tokens
    if temperature is not None:
        options["temperature"] = temperature
    payload = {"model": MODEL, "messages": msgs, "stream": stream}
    if options:
        payload["options"] = options
    return payload


async def complete(system, messages, max_tokens, temperature, timeout) -> str:
    async with httpx.AsyncClient(timeout=timeout) as client:
        res = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json=_payload(system, messages, max_tokens, temperature, stream=False),
        )
    logger.debug("[llm:ollama] HTTP %d (応答 %d バイト)", res.status_code, len(res.content))
    res.raise_for_status()
    return res.json()["message"]["content"]


async def stream(system, messages, max_tokens, temperature, timeout) -> AsyncIterator[str]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{OLLAMA_URL}/api/chat",
            json=_payload(system, messages, max_tokens, temperature, stream=True),
        ) as response:
            logger.debug("[stream] 接続確立: HTTP %d", response.status_code)
            async for line in response.aiter_lines():
                if not line:
                    continue
                data = json.loads(line)
                if data.get("done"):
                    continue
                chunk = data.get("message", {}).get("content", "")
                if chunk:
                    yield chunk
