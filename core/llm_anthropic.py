"""
llm.py のAnthropic API（直接）実装。anthropic SDKを使う。

認証: 環境変数 RIN_ANTHROPIC_API_KEY から読む（ANTHROPIC_API_KEY は使わない）。キーはコードに書かない。
モデル: config.ANTHROPIC_MODEL（claude-haiku-5-5）。

Haiku 5.5 の仕様（公式ドキュメントで確認、実機での挙動はscripts/anthropic_probe.pyで確認）:
  - 思考(adaptive thinking)は既定で有効。返答用途では thinking={"type": "disabled"} で無効にする。
    思考トークンは max_tokens に含まれるため、有効のままだと route_tool(15トークン)などが
    思考だけで尽きる恐れがある。
  - temperature / top_p / top_k を送るとエラーになりうるため、送らない（モデル既定）。
    llm.complete() の temperature 引数は受け取るだけで無視する。
    ※ route_tool は temperature=0 を渡しているが、このバックエンドでは効かない。
  - assistantの末尾プリフィルはエラーになる。messages は必ず user で終えること。
  - 応答は thinking ブロックで始まりうるため、text ブロックだけを type で選んで取り出す。

安全策（課金と暴走の抑制）:
  - max_tokens は config.ANTHROPIC_MAX_TOKENS_CAP で丸める（呼び出し側が大きな値を渡しても超えない）
  - タイムアウトとリトライ回数は config で明示（リトライは最小限）
  - 月額上限（core/usage.py）に達していたらAPIを呼ばず、固定メッセージを返す。Ollamaへの自動フォールバックはしない
  - 使用量はトークン数だけを月ごとに永続化する。集計を読めないときも、上限を守れないためAPIを呼ばない
  - API失敗時は本文を含まないログを出し、固定メッセージを返す
"""
import logging
import os
from typing import AsyncIterator

from core import usage
from core.config import (
    ANTHROPIC_MODEL, ANTHROPIC_API_KEY_ENV, REPLY_MAX_TOKENS,
    ANTHROPIC_MAX_TOKENS_CAP, ANTHROPIC_TIMEOUT_SEC, ANTHROPIC_MAX_RETRIES,
)

logger = logging.getLogger(__name__)

# 上限到達・エラー時に返す固定メッセージ（句点で終えるので、読み上げの文区切りにもそのまま乗る）
BUDGET_MESSAGE = "今月の利用上限に達したため、いまは返答できません。"
ERROR_MESSAGE = "うまく返答できませんでした。少し時間をおいて、もう一度お試しください。"

# SDKの自動リトライ回数。probeなど課金を厳密に抑えたい場面では、最初の呼び出し前に0へ上書きする
MAX_RETRIES = ANTHROPIC_MAX_RETRIES

_client = None

# 文の区切りとして扱う句読点。core/ollama.py の _stream_chat の `句読点 = "。！？"` と同じ集合
# （tests/test_llm_anthropic_stream.py が、ollama.py側と一致していることを検証する）。
# _stream_chat は「チャンクが句読点1文字と一致するとき」だけバッファをyieldする（`chunk in 句読点`）ため、
# Claudeのチャンク（複数文字をまとめて返す）では区切りが働かない。そこでstream()側で、
# 句読点が必ず単独のチャンクになるようにテキストを分割して出し直す。
SENTENCE_END = "。！？"

# 直近の呼び出しの構造情報（stop_reason, トークン数, ブロック種別）。診断用で、本文は含まない
last_call_info: dict = {}


def _get_client():
    """AsyncAnthropicを遅延生成する（Ollama運用中はanthropic未インストールでも動くように）"""
    global _client
    if _client is None:
        import anthropic
        key = os.environ.get(ANTHROPIC_API_KEY_ENV)
        if not key:
            raise RuntimeError(f"環境変数 {ANTHROPIC_API_KEY_ENV} が設定されていません")
        _client = anthropic.AsyncAnthropic(api_key=key, max_retries=MAX_RETRIES, timeout=ANTHROPIC_TIMEOUT_SEC)
    return _client


def _request(system, messages, max_tokens) -> dict:
    """messages.create / messages.stream 共通のリクエスト引数を組み立てる"""
    # max_tokensは必須。未指定なら/input経路と同じ上限を使い、上限(CAP)を超える値はCAPに丸める
    requested = max_tokens if max_tokens is not None else REPLY_MAX_TOKENS
    req = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max(1, min(requested, ANTHROPIC_MAX_TOKENS_CAP)),
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        "thinking": {"type": "disabled"},
    }
    if system:
        req["system"] = system
    return req


def _timeout(requested) -> float:
    """呼び出し側のタイムアウトが長くても、configの値を超えないようにする"""
    return min(requested, ANTHROPIC_TIMEOUT_SEC) if requested else ANTHROPIC_TIMEOUT_SEC


def _blocked_message() -> str | None:
    """APIを呼んではいけないときは返す固定メッセージ、呼んでよいときはNone"""
    try:
        if usage.is_over_budget():
            logger.warning("[llm:anthropic] 月額上限に達したためAPIを呼びません（今月の概算 $%.4f / 上限 $%.2f）",
                           usage.month_cost_usd(), usage.ANTHROPIC_MONTHLY_BUDGET_USD)
            return BUDGET_MESSAGE
    except usage.UsageUnavailable as e:
        logger.error("[llm:anthropic] 使用量の集計を読めないため、上限を守れずAPIを呼びません: %s", e)
        return ERROR_MESSAGE
    return None


def _log_api_error(e: Exception) -> None:
    """本文を含まないログ。例外クラス名・HTTPステータス・request_idだけを出す"""
    detail = str(e) if isinstance(e, RuntimeError) else ""  # 自前のRuntimeError（キー未設定など）の文言のみ
    logger.error("[llm:anthropic] API呼び出しに失敗しました: %s status=%s request_id=%s %s",
                 type(e).__name__, getattr(e, "status_code", None), getattr(e, "request_id", None), detail)


def _store_usage(input_tokens: int, output_tokens: int) -> None:
    try:
        state = usage.record(input_tokens, output_tokens)
        logger.info("[llm:anthropic] 今月: calls=%d in=%d out=%d 概算$%.4f / 上限$%.2f",
                    state["calls"], state["input_tokens"], state["output_tokens"],
                    state["cost_usd"], usage.ANTHROPIC_MONTHLY_BUDGET_USD)
    except usage.UsageUnavailable as e:
        logger.warning("[llm:anthropic] 使用量を保存できませんでした（次回の呼び出しは集計を読めず停止します）: %s", e)


def _record(message) -> None:
    """応答の構造情報を記録・ログ出力し、使用量を月次集計に積算する（本文は扱わない）"""
    global last_call_info
    last_call_info = {
        "stop_reason": message.stop_reason,
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "block_types": [b.type for b in message.content],
    }
    logger.info("[llm:anthropic] stop=%s in=%d out=%d blocks=%s",
                last_call_info["stop_reason"], last_call_info["input_tokens"],
                last_call_info["output_tokens"], last_call_info["block_types"])
    if message.stop_reason == "refusal":
        logger.warning("[llm:anthropic] 安全分類器により応答が拒否されました（stop_reason=refusal）")
    _store_usage(message.usage.input_tokens, message.usage.output_tokens)


def _record_snapshot(stream_obj) -> None:
    """ストリームが最後まで読まれなかった場合に、その時点までの使用量を積算する"""
    try:
        snap = stream_obj.current_message_snapshot
        _store_usage(snap.usage.input_tokens or 0, snap.usage.output_tokens or 0)
    except Exception:  # スナップショットがまだ無い（接続前の失敗など）
        pass


async def complete(system, messages, max_tokens, temperature, timeout) -> str:
    blocked = _blocked_message()
    if blocked:
        return blocked
    try:
        message = await _get_client().messages.create(
            **_request(system, messages, max_tokens), timeout=_timeout(timeout))
    except Exception as e:
        _log_api_error(e)
        return ERROR_MESSAGE
    _record(message)
    return "".join(b.text for b in message.content if b.type == "text")


def _split_sentence_end(text: str) -> list:
    """textを句読点の位置で分割する。句読点は1文字ずつ単独の要素になる。
    例: "こんにちは。今日は！" -> ["こんにちは", "。", "今日は", "！"]（空文字は含めない）"""
    pieces = []
    start = 0
    for i, ch in enumerate(text):
        if ch in SENTENCE_END:
            if i > start:
                pieces.append(text[start:i])
            pieces.append(ch)
            start = i + 1
    if start < len(text):
        pieces.append(text[start:])
    return pieces


async def stream(system, messages, max_tokens, temperature, timeout) -> AsyncIterator[str]:
    blocked = _blocked_message()
    if blocked:
        yield blocked
        return
    yielded = False
    try:
        async with _get_client().messages.stream(
                **_request(system, messages, max_tokens), timeout=_timeout(timeout)) as s:
            recorded = False
            try:
                async for text in s.text_stream:
                    # 句読点を単独チャンクにして出し直す（_stream_chat が文単位で区切れるように）
                    for piece in _split_sentence_end(text):
                        yielded = True
                        yield piece
                _record(await s.get_final_message())
                recorded = True
            finally:
                if not recorded:
                    _record_snapshot(s)
    except Exception as e:
        _log_api_error(e)
        # 途中まで返答を出した後なら、固定メッセージは足さない（読み上げが不自然になるため）
        if not yielded:
            yield ERROR_MESSAGE
