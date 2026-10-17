"""
Anthropic APIの使用量（トークン数と概算コスト）を月ごとに集計して永続化し、月額上限を判定する。

- 保存先: config.USAGE_DIR の anthropic-YYYY-MM.json（月ごとに別ファイル。月が変われば新しいファイルで0から集計）
- 保存するのは呼び出し回数・入力/出力トークン数・概算コスト（USD）だけ。プロンプトや返答の本文は保存しない
- コストは呼び出しごとに、その時点のconfigの単価で換算して積算する（単価が後で変わっても過去分は変わらない）
- 集計を読めない場合（LOGS_DIRが無い＝ストレージ未マウント、ファイル破損）は UsageUnavailable を送出する。
  呼び出し側（llm_anthropic）は、上限を守れないためAPIを呼ばない（安全側に倒す）
"""
import json
import os
import threading
from datetime import datetime

from core.config import (
    LOGS_DIR, USAGE_DIR, ANTHROPIC_MONTHLY_BUDGET_USD,
    ANTHROPIC_PRICE_PER_MTOK, ANTHROPIC_PRICE_PER_MTOK_LONG, ANTHROPIC_LONG_PROMPT_TOKENS,
)

_lock = threading.Lock()


class UsageUnavailable(RuntimeError):
    """月次の集計を読み書きできない。メッセージにパス以外の内容は含めない"""


def _month() -> str:
    return datetime.now().strftime("%Y-%m")


def _path(month: str) -> str:
    return os.path.join(USAGE_DIR, f"anthropic-{month}.json")


def _empty(month: str) -> dict:
    return {"month": month, "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "updated": None}


def call_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """1回の呼び出しの概算コスト（USD）"""
    p = ANTHROPIC_PRICE_PER_MTOK_LONG if input_tokens > ANTHROPIC_LONG_PROMPT_TOKENS else ANTHROPIC_PRICE_PER_MTOK
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000


def load(month: str | None = None) -> dict:
    """指定月（既定は今月）の集計を返す。ファイルが無ければ0の集計。読めなければ UsageUnavailable"""
    month = month or _month()
    if not os.path.isdir(LOGS_DIR):
        raise UsageUnavailable(f"ログフォルダが存在しません（ストレージが未マウント？）: {LOGS_DIR}")
    path = _path(month)
    if not os.path.exists(path):
        return _empty(month)
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        for key in ("calls", "input_tokens", "output_tokens"):
            if not isinstance(state[key], int) or state[key] < 0:
                raise ValueError(key)
        if not isinstance(state["cost_usd"], (int, float)) or state["cost_usd"] < 0:
            raise ValueError("cost_usd")
        state["month"] = month
        return state
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise UsageUnavailable(f"使用量の集計ファイルを読めません: {path} ({type(e).__name__})") from None


def month_cost_usd(month: str | None = None) -> float:
    return float(load(month)["cost_usd"])


def is_over_budget() -> bool:
    """今月の概算コストが上限に達していればTrue。集計を読めなければ UsageUnavailable"""
    return month_cost_usd() >= ANTHROPIC_MONTHLY_BUDGET_USD


def record(input_tokens: int, output_tokens: int) -> dict:
    """1回分の使用量を今月の集計に積算して保存する。保存後の集計を返す"""
    with _lock:
        state = load()
        state["calls"] += 1
        state["input_tokens"] += int(input_tokens)
        state["output_tokens"] += int(output_tokens)
        state["cost_usd"] = round(float(state["cost_usd"]) + call_cost_usd(input_tokens, output_tokens), 8)
        state["updated"] = datetime.now().isoformat(timespec="seconds")
        try:
            os.makedirs(USAGE_DIR, exist_ok=True)
            tmp = _path(state["month"]) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _path(state["month"]))
        except OSError as e:
            raise UsageUnavailable(f"使用量の集計を保存できません: {USAGE_DIR} ({type(e).__name__})") from None
        return state
