import os
from datetime import datetime

from core.config import CONVERSATIONS_DIR, DIARY_DIR


def _today_log_path() -> str:
    """今日の日付のログファイルパスを返す"""
    today = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(CONVERSATIONS_DIR, f"{today}.md")


def append_conversation(time_str: str, user_input: str, response: str) -> str | None:
    """
    今日のMarkdownログにユーザー発言と凛の返答を追記する。
    ファイルが無ければ見出し付きで新規作成する。
    conversations/フォルダが無ければ作成する。
    戻り値: 成功時は書き込んだファイルパス、失敗時はNone
    """
    os.makedirs(CONVERSATIONS_DIR, exist_ok=True)
    path = _today_log_path()
    is_new = not os.path.exists(path)

    try:
        with open(path, "a", encoding="utf-8") as f:
            if is_new:
                today = datetime.now().strftime("%Y-%m-%d")
                f.write(f"# {today}\n\n")
            f.write(f"## [{time_str}] User\n{user_input}\n\n")
            f.write(f"## [{time_str}] 凛\n{response}\n\n")
        return path
    except OSError as e:
        print(f"[vault] ログの書き込みに失敗しました: {e}")
        return None
