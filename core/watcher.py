"""
Obsidian Vaultの diary/ フォルダを監視し、変更があったら
ChromaDBへインデックスする。

main.py側の会話保存（index_markdown_file）は「書いた本人」が直接呼べたが、
日記はじゅんがObsidian上で手動編集するため、書き込み元をプログラムが知らない。
そのため watchdog でファイルシステムイベントを監視する方式を使う。

エディタは1文字入力するごとに保存イベントを連発することがあるため、
「最後の変更からDEBOUNCE_SECONDS秒、何も新しい変更がなければ実行する」
という本来のデバウンス（タイマーのリセット方式）を使う。
"""
import logging
import os
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from core.config import DIARY_DIR
from core.rag import index_diary_file

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 7.0


class _DiaryHandler(FileSystemEventHandler):
    """diary/フォルダ内の.mdファイル変更を検知してインデックスするハンドラ"""

    def __init__(self):
        super().__init__()
        self._timers = {}
        self._lock = threading.Lock()

    def _run_index(self, filepath: str):
        count = index_diary_file(filepath)
        logger.info("[watcher] 日記を検知 → %d件をインデックスしました: %s", count, filepath)
        with self._lock:
            self._timers.pop(filepath, None)

    def _handle(self, filepath: str):
        if not filepath.endswith(".md"):
            return

        with self._lock:
            # 既存のタイマーがあればキャンセルして、再度7秒後にセットし直す
            existing = self._timers.get(filepath)
            if existing:
                existing.cancel()

            timer = threading.Timer(DEBOUNCE_SECONDS, self._run_index, args=(filepath,))
            timer.daemon = True
            self._timers[filepath] = timer
            timer.start()

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path)


def start_diary_watcher() -> Observer:
    """
    diary/フォルダの監視を開始する。
    戻り値: Observerインスタンス（停止する場合はstop()を呼ぶ）
    """
    os.makedirs(DIARY_DIR, exist_ok=True)

    event_handler = _DiaryHandler()
    observer = Observer()
    observer.schedule(event_handler, DIARY_DIR, recursive=False)
    observer.start()
    logger.info("[watcher] 日記フォルダの監視を開始しました: %s", DIARY_DIR)
    return observer
