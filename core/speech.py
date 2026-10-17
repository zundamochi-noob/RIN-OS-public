"""
Mate Engine（/completion）の返答を、VOICEVOXで読み上げる。

返答を返したあとに、別タスクで読み上げる（core/record.py と同じ形）。読み上げの失敗は返答に影響しない。
- 返答を句点（。！？）と改行で文に分け、1文ずつ合成して順に再生する。再生中に次の文を合成しておく
- 新しい返答が予約されたら、前の返答の残りの文は捨てる（再生中の1文は最後まで鳴る）
- 再生は直列（同時に2つ鳴らさない）
- 読み上げない: COMPLETION_SPEAK が False のとき、空の返答、API失敗の固定メッセージ
- ログは構造のみ（文数・文字数）。返答の本文は書かない
"""
import asyncio
import logging
import re

from core import llm_anthropic
from core.config import COMPLETION_SPEAK
from core.voicevox import generate, play

logger = logging.getLogger(__name__)

_SENTENCE = re.compile(r"[^。！？\n]*[。！？]+|[^。！？\n]+")

# 新しい返答が予約されるたびに増える番号。古い返答の読み上げは、番号が変わったら残りを捨てる
_generation = 0
_tasks: set = set()
# 再生を直列にするロック。イベントループごとに作り直す
_lock_state: dict = {"loop": None, "lock": None}


def _get_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if _lock_state["loop"] is not loop:
        _lock_state["loop"], _lock_state["lock"] = loop, asyncio.Lock()
    return _lock_state["lock"]


def split_sentences(text: str) -> list:
    """句点（。！？）と改行で文に分ける。句読点は文に残す。空白だけの文は捨てる"""
    return [s.strip() for s in _SENTENCE.findall(text or "") if s.strip()]


def _skip_reason(reply: str):
    if not COMPLETION_SPEAK:
        return "disabled"
    if not reply or not reply.strip():
        return "empty_reply"
    if reply.strip() in (llm_anthropic.BUDGET_MESSAGE, llm_anthropic.ERROR_MESSAGE):
        return "fixed_message"
    return None


async def _speak(sentences: list, gen: int) -> str:
    """文を順に合成・再生する。戻り値は 'spoken' / 'superseded' / 'failed'（テスト用）。例外は外に出さない"""
    loop = asyncio.get_running_loop()
    try:
        async with _get_lock():
            played = 0
            # 次の文の合成を、いまの文の再生中に進めておく
            pending = loop.create_task(generate(sentences[0]))
            try:
                for i in range(len(sentences)):
                    audio = await pending
                    pending = None
                    if gen != _generation:
                        logger.info("[speech] 新しい返答が来たため残りを読み上げません: %d/%d文", played, len(sentences))
                        return "superseded"
                    if audio is None:
                        logger.warning("[speech] 合成できませんでした（VOICEVOX未起動など）: %d/%d文", played, len(sentences))
                        return "failed"
                    if i + 1 < len(sentences):
                        pending = loop.create_task(generate(sentences[i + 1]))
                    await loop.run_in_executor(None, play, audio)
                    played += 1
            finally:
                if pending is not None:
                    pending.cancel()
        logger.info("[speech] 読み上げました: %d文 %d文字", len(sentences), sum(len(s) for s in sentences))
        return "spoken"
    except Exception as e:
        logger.warning("[speech] 読み上げに失敗しました（%s）", type(e).__name__)
        return "failed"


def schedule_speak(reply: str) -> None:
    """返答を返したあとに読み上げるバックグラウンドタスクを予約する。何があっても例外を出さない"""
    global _generation
    try:
        reason = _skip_reason(reply)
        if reason:
            logger.debug("[speech] スキップ: %s", reason)
            return
        sentences = split_sentences(reply)
        if not sentences:
            return
        _generation += 1
        task = asyncio.get_running_loop().create_task(_speak(sentences, _generation))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except Exception as e:
        logger.warning("[speech] 読み上げの予約に失敗しました（%s）", type(e).__name__)
