import asyncio
import logging

logger = logging.getLogger(__name__)


def format_duration(minutes: float) -> str:
    """float（分）を日本語文字列に変換。例: 5.0→5分, 90.0→1時間30分, 0.5→30秒"""
    total_sec = round(minutes * 60)
    if total_sec < 60:
        return f"{total_sec}秒"
    total_min = round(minutes)
    if total_min < 60:
        return f"{total_min}分"
    h = total_min // 60
    m = total_min % 60
    return f"{h}時間{m}分" if m > 0 else f"{h}時間"


class TimerManager:
    """asyncioベースのタイマー管理。複数タイマーを同時に保持できる。"""

    def __init__(self):
        self._tasks: list[asyncio.Task] = []

    def schedule(self, minutes: float, callback) -> asyncio.Task:
        """minutes後にcallback()（コルーチン関数）を呼び出す。"""
        async def _run():
            logger.info("[timer] %s のタイマーをスタート", format_duration(minutes))
            await asyncio.sleep(minutes * 60)
            logger.info("[timer] %s のタイマーが完了", format_duration(minutes))
            await callback()

        task = asyncio.create_task(_run())
        self._tasks.append(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task):
        try:
            self._tasks.remove(task)
        except ValueError:
            pass
        if not task.cancelled() and task.exception():
            logger.error("[timer] タスクエラー: %s", task.exception())

    def cancel_all(self):
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    @property
    def count(self) -> int:
        return len(self._tasks)


timer_manager = TimerManager()
