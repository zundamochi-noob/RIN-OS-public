"""
llm.py のBedrock実装（骨組みのみ）。

TODO: 未検証。boto3のConverse / ConverseStreamを呼ぶ構造だけを書いてあり、
      実際のAWSへの呼び出しはまだ一度も行っていない。切り替え前に必ず実機で確認すること。
      特に以下は「不明」のまま:
      - 思考(adaptive thinking)がデフォルトONらしい。オフにする設定キーが不明
        （additionalModelRequestFields に何を渡すか）。オフにしないとmax_tokens=15の
        route_toolが思考トークンで尽きる恐れがある
      - temperature に制約があるか
      - messages の user/assistant 交互ルール、先頭がuserである必要があるか
        （history.json の中身が違反しないか）
      - 応答の content に reasoningContent 等が混ざる場合の扱い（textブロックだけ拾っている）
      - AWS_BEARER_TOKEN_BEDROCK を読めるboto3の最低バージョン
認証情報はコードに書かない。環境変数 AWS_BEARER_TOKEN_BEDROCK をboto3が読む前提。
"""
import asyncio
import logging
from typing import AsyncIterator

from core.config import BEDROCK_REGION, BEDROCK_MODEL_ID

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    """boto3クライアントを遅延生成する（Ollama運用中はboto3未インストールでも動くように）"""
    global _client
    if _client is None:
        import boto3
        _client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    return _client


def _request(system, messages, max_tokens, temperature) -> dict:
    """converse / converse_stream 共通のリクエスト引数を組み立てる"""
    req = {
        "modelId": BEDROCK_MODEL_ID,
        "messages": [
            {"role": m["role"], "content": [{"text": m["content"]}]}
            for m in messages
        ],
    }
    if system:
        req["system"] = [{"text": system}]
    inference_config = {}
    if max_tokens is not None:
        inference_config["maxTokens"] = max_tokens
    if temperature is not None:
        inference_config["temperature"] = temperature
    if inference_config:
        req["inferenceConfig"] = inference_config
    return req


async def complete(system, messages, max_tokens, temperature, timeout) -> str:
    # TODO: timeout は未反映（botocoreのConfig(read_timeout=...)で渡す必要がある）
    req = _request(system, messages, max_tokens, temperature)
    response = await asyncio.to_thread(_get_client().converse, **req)
    blocks = response["output"]["message"]["content"]
    return "".join(b["text"] for b in blocks if "text" in b)


async def stream(system, messages, max_tokens, temperature, timeout) -> AsyncIterator[str]:
    # boto3は同期APIなので、別スレッドでイベントを読み、asyncioのキュー経由で受け渡す。
    # TODO: 呼び出し側が途中で読むのをやめた場合、スレッド側は最後まで読み続ける（未対処）
    req = _request(system, messages, max_tokens, temperature)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    done = object()

    def _worker():
        try:
            response = _get_client().converse_stream(**req)
            for event in response["stream"]:
                text = event.get("contentBlockDelta", {}).get("delta", {}).get("text")
                if text:
                    loop.call_soon_threadsafe(queue.put_nowait, text)
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, e)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, done)

    loop.run_in_executor(None, _worker)
    while True:
        item = await queue.get()
        if item is done:
            break
        if isinstance(item, Exception):
            raise item
        yield item
