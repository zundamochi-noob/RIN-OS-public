"""
Mate Engine（/completion）の会話を、凛の記憶に記録する。

返答を返したあとに、バックグラウンドのタスクで、最新のuser発言と返答の1組だけを
既存の仕組み（core/vault.py の append_conversation と core/rag.py の index_markdown_file）で
conversations\\YYYY-MM-DD.md に追記してインデックスする。書式は /input 経路と同じ。

- 記録するのは最新のuser発言とその返答の1組だけ。Unityが毎回送る履歴は記録しない
- 記録しないもの: Warmup・ChatMLでないpromptのフォールバック（呼び出し側api.pyで除外）、
  API失敗の固定メッセージ、空の返答、直近 COMPLETION_DEDUPE_SEC 以内に記録済みの同じ最新発言（再送）、
  COMPLETION_RECORD が False のとき
- 記録の失敗は /completion の返答に影響しない（例外を外に出さない）。失敗は本文なしのログに1行だけ書く
- ログは構造のみ（文字数・件数）。会話本文は書かない
"""
import asyncio
import hashlib
import logging
import time
from datetime import datetime

from core import llm_anthropic
from core.config import COMPLETION_RECORD, COMPLETION_DEDUPE_SEC
from core.rag import index_markdown_file
from core.vault import append_conversation

logger = logging.getLogger(__name__)

# 最新のuser発言のハッシュ → 記録した時刻（再送の判定用）。古いものは判定のたびに掃除する
_recent: dict = {}
# 実行中のバックグラウンドタスクへの参照（完了前にガベージコレクションされないように保持する）
_tasks: set = set()
# インデックス（ChromaDBへの書き込み）を直列にするロック。イベントループごとに作り直す
_lock_state: dict = {"loop": None, "lock": None}


def _clock() -> float:
    return time.monotonic()


def _hhmm() -> str:
    return datetime.now().strftime("%H:%M")


def _get_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if _lock_state["loop"] is not loop:
        _lock_state["loop"], _lock_state["lock"] = loop, asyncio.Lock()
    return _lock_state["lock"]


def _key(user_text: str) -> str:
    return hashlib.sha256(user_text.strip().encode("utf-8")).hexdigest()


def _skip_reason(user_text: str, reply: str):
    """記録しない理由（構造のみの短い識別子）を返す。記録してよければNone。再送判定の印は付けない"""
    if not COMPLETION_RECORD:
        return "disabled"
    if not user_text or not user_text.strip():
        return "empty_user"
    if not reply or not reply.strip():
        return "empty_reply"
    if reply.strip() in (llm_anthropic.BUDGET_MESSAGE, llm_anthropic.ERROR_MESSAGE):
        return "fixed_message"
    now = _clock()
    for k in [k for k, t in _recent.items() if now - t >= COMPLETION_DEDUPE_SEC]:
        del _recent[k]
    if _key(user_text) in _recent:
        return "duplicate"
    return None


async def _run(user_text: str, reply: str, key: str, hhmm: str) -> str:
    """実際の記録。戻り値は 'recorded' または 'failed:<段階>'（テスト用）。例外は外に出さない"""
    stage = "append"
    appended = False
    try:
        # ファイルへの追記は短い同期処理。イベントループ上で行い、/input経路の追記と同時に書き込まれないようにする
        path = append_conversation(hhmm, user_text, reply)
        if path is None:
            raise OSError("append_conversation が失敗した")
        appended = True
        stage = "index"
        async with _get_lock():
            added = await asyncio.get_running_loop().run_in_executor(None, index_markdown_file, path)
        logger.info("[record] 記録しました: user=%d文字 reply=%d文字 新規チャンク=%d", len(user_text), len(reply), added)
        return "recorded"
    except Exception as e:
        # 追記できなかった場合だけ再送判定の印を外す（追記済みなら、同じ組を再送で二重に追記しないよう印を残す）
        if not appended:
            _recent.pop(key, None)
        logger.warning("[record] 記録に失敗しました（段階=%s, %s）", stage, type(e).__name__)
        return f"failed:{stage}"


def schedule_record(user_text: str, reply: str) -> None:
    """返答を返したあとに記録するバックグラウンドタスクを予約する。何があっても例外を出さない"""
    try:
        reason = _skip_reason(user_text, reply)
        if reason:
            logger.debug("[record] スキップ: %s", reason)
            return
        key = _key(user_text)
        _recent[key] = _clock()   # 同時に届いた同一リクエストの二重記録を防ぐため、予約の時点で印を付ける
        task = asyncio.get_running_loop().create_task(_run(user_text, reply, key, _hhmm()))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except Exception as e:
        logger.warning("[record] 記録の予約に失敗しました（%s）", type(e).__name__)


async def record_turn(user_text: str, reply: str) -> str:
    """予約せずに、その場で記録して結果を返す（テスト用）。戻り値: 'recorded' / 'failed:<段階>' / 'skipped:<理由>'"""
    reason = _skip_reason(user_text, reply)
    if reason:
        return f"skipped:{reason}"
    key = _key(user_text)
    _recent[key] = _clock()
    return await _run(user_text, reply, key, _hhmm())
