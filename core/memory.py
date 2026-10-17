import json
import logging
import os

from core.config import HISTORY_PATH, HISTORY_LIMIT

logger = logging.getLogger(__name__)

DATA_DIR = os.path.dirname(HISTORY_PATH)


def load_history() -> list:
    """
    前回保存した会話履歴を読み込む。
    ファイルが存在しない・壊れている場合は空リストを返す。
    """
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def save_history(history: list) -> None:
    """
    会話履歴をJSONに保存する。直近20件だけを保存対象とする。
    data/フォルダが無ければ作成する。
    tmpファイルに書いてからos.replaceでアトミックに差し替えることで、
    書き込み中のクラッシュ（強制終了・電源断等）で history.json が
    壊れた状態のまま残ることを防ぐ。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    trimmed = history[-HISTORY_LIMIT:]
    tmp_path = HISTORY_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(trimmed, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, HISTORY_PATH)
    except OSError as e:
        logger.warning("[memory] 履歴の保存に失敗しました: %s", e)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
