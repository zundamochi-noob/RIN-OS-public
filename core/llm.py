"""
LLM呼び出しの共通窓口。
バックエンド（Ollama / Bedrock）の違いはこのモジュールの裏側に閉じ込め、
呼び出し側（ollama.py, api.py）は complete() と stream() だけを使う。

入力形式はBedrock Converse APIに合わせて統一する:
  - system: システムプロンプトの文字列（空文字なら送らない）
  - messages: [{"role": "user" | "assistant", "content": str}, ...]（systemロールは含めない）

使うバックエンドは core/config.py の LLM_BACKEND で指定する。
"""
from typing import AsyncIterator

from core.config import LLM_BACKEND


def _backend():
    # 使わない側のモジュール（boto3など）を読み込まないよう、遅延importにしている
    if LLM_BACKEND == "ollama":
        from core import llm_ollama
        return llm_ollama
    if LLM_BACKEND == "bedrock":
        from core import llm_bedrock
        return llm_bedrock
    if LLM_BACKEND == "anthropic":
        from core import llm_anthropic
        return llm_anthropic
    raise ValueError(f"LLM_BACKEND が不正です: {LLM_BACKEND!r}（'ollama' / 'bedrock' / 'anthropic'）")


async def complete(
    system: str,
    messages: list,
    max_tokens: int | None = None,
    temperature: float | None = None,
    timeout: float = 120,
) -> str:
    """非ストリーミングで生成し、全文を返す。max_tokens/temperatureがNoneならバックエンド既定。"""
    return await _backend().complete(system, messages, max_tokens, temperature, timeout)


async def stream(
    system: str,
    messages: list,
    max_tokens: int | None = None,
    temperature: float | None = None,
    timeout: float = 120,
) -> AsyncIterator[str]:
    """ストリーミングで生成し、テキストの断片（空文字を除く）を順にyieldする。"""
    async for chunk in _backend().stream(system, messages, max_tokens, temperature, timeout):
        yield chunk
