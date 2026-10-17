import os
import sys
import asyncio
import logging
import time
import traceback
import httpx
from datetime import datetime

# ログファイルへのリダイレクト時に文字化けしないよう標準出力をUTF-8に固定する
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from core.config import API_PORT, OVERLAY_PORT, HISTORY_LIMIT, DIARY_DIR, LLM_BACKEND, ANTHROPIC_API_KEY_ENV, USAGE_DIR
from core.voicevox import generate, play, check_speaker
from core.ollama import route_tool, chat, chat_with_context, warmup
from core.weather import get_weather
from core.timer import timer_manager, format_duration
from core.api import input_queue
import core.api as _api
from core.memory import load_history, save_history
from core.vault import append_conversation
from core.rag import index_markdown_file, index_diary_file, get_collection
from core.context import search_memory, format_memory_context
from core.watcher import start_diary_watcher

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("rin")

# コンテキストツール: データを取得してLLMに渡す（戻り値=文字列コンテキスト）
# timer はサイドエフェクトツール（タスクスケジュールのみ）なので _handle_turn で直接処理する
TOOL_FUNCTIONS = {
    "weather": get_weather,
}

# 発話の同時再生を防ぐロック（タイマー発火とターン応答の衝突を避ける）
_speaking_lock = asyncio.Lock()

_OVERLAY_URL = f"http://127.0.0.1:{OVERLAY_PORT}/response"


def _post_overlay(text: str):
    """overlayへの同期POST（別スレッドで実行する前提）"""
    try:
        httpx.post(_OVERLAY_URL, json={"text": text})
    except Exception:
        pass


async def notify_overlay(text: str):
    """
    overlayの会話ログにシステムメッセージを送る。
    同期httpxをイベントループ内で直接叩くとループが一瞬止まるため、
    別スレッド（executor）に逃がして非ブロッキングにする。
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _post_overlay, text)


def _index_diary_on_startup():
    """
    起動時に未インデックスの日記ファイルを差分インデックスする。
    ChromaDBは永続化されているため、既存データはPC再起動後も保持される。
    ここでの失敗が会話ループ開始を妨げないよう、呼び出し側でtry/exceptする。
    戻り値: 新たにインデックスした件数
    """
    if not os.path.exists(DIARY_DIR):
        return 0

    today_str = datetime.now().strftime("%Y-%m-%d")
    collection = get_collection()
    existing = collection.get(where={"source_type": "diary"})
    indexed_dates = {m["date"] for m in existing["metadatas"]} if existing["metadatas"] else set()

    total = 0
    for fname in os.listdir(DIARY_DIR):
        if not fname.endswith(".md"):
            continue
        date_str = os.path.splitext(fname)[0]
        # 未インデックス or 当日ファイル（追記される可能性があるため毎回再インデックス）
        if date_str not in indexed_dates or date_str == today_str:
            total += index_diary_file(os.path.join(DIARY_DIR, fname))
    return total


def _model_check_hint() -> str:
    """モデルの読み込みに失敗したとき、LLM_BACKENDに応じて確認すべき点を返す（キーの値は含めない）"""
    if LLM_BACKEND == "anthropic":
        return f"APIキー（環境変数 {ANTHROPIC_API_KEY_ENV}）と月額上限（{USAGE_DIR}）を確認してください。"
    if LLM_BACKEND == "bedrock":
        return "AWSの認証情報とリージョンを確認してください。"
    return "Ollamaが起動しているか確認してください。"


async def main():
    logger.info("凛が起動しました")

    # 話者IDが設定した話者と一致するか照合（VOICEVOX未起動ならスキップ。起動は失敗させない）
    await check_speaker()

    # 日記フォルダの監視を開始（じゅんがObsidianで書いた日記を自動でインデックス）
    diary_observer = start_diary_watcher()

    # 起動時の日記インデックス（ここでコケても凛本体は起動を続けられるように保護）
    try:
        diary_total = await asyncio.get_running_loop().run_in_executor(None, _index_diary_on_startup)
        if diary_total > 0:
            logger.info("[rag] 起動時: 日記%d件をインデックスしました", diary_total)
    except Exception:
        logger.warning("[rag] 起動時の日記インデックスに失敗しました:\n%s", traceback.format_exc())

    history = load_history()

    # モデルのウォームアップ
    await notify_overlay("システム: モデルを読み込んでいます...")
    logger.info("モデルを読み込んでいます...")
    success = await warmup()
    if success:
        msg = "システム: 準備完了しました。"
        if history:
            msg += f"（前回の会話履歴{len(history)}件）"
            logger.info("前回の会話履歴（%d件）を読み込みました。", len(history))
        await notify_overlay(msg)
        logger.info("モデルの読み込みが完了しました。")
    else:
        hint = _model_check_hint()
        await notify_overlay(f"システム: モデルの読み込みに失敗しました。{hint}")
        logger.error("モデルの読み込みに失敗しました。（LLM_BACKEND=%s）%s", LLM_BACKEND, hint)

    await _conversation_loop(history)

    # 終了処理：タイマーと日記監視を停止する
    timer_manager.cancel_all()
    diary_observer.stop()
    diary_observer.join()
    logger.info("日記フォルダの監視を停止しました")


async def _producer(queue, gen, container):
    """LLMのストリーミング出力をキューに流しつつ、テキストを蓄積する"""
    try:
        async for chunk in gen:
            container.append(chunk)
            print(chunk, end="", flush=True)
            await queue.put(chunk)
    finally:
        await queue.put(None)


async def _consumer(queue):
    """
    キューから文（句読点区切り）を受け取り、VOICEVOXで音声化して順番に再生する。
    音声OFF時・VOICEVOX未起動時はスキップして会話だけ進める。
    """
    loop = asyncio.get_running_loop()
    play_task = None
    voicevox_error_shown = False
    while True:
        chunk = await queue.get()
        if chunk is None:
            break
        if not _api.voice_enabled:
            continue
        audio_data = await generate(chunk)
        if audio_data is None:
            if not voicevox_error_shown:
                await notify_overlay("システム: VOICEVOXが起動していません")
                voicevox_error_shown = True
            continue
        if play_task:
            await play_task
        play_task = loop.run_in_executor(None, play, audio_data)
    if play_task:
        await play_task


async def _fire_timer_notification(minutes: float, original_request: str, history: list):
    """タイマー発火時の通知メッセージを生成・再生し、履歴に記録する"""
    logger.info("[timer] 発火通知を生成します: %s", format_duration(minutes))
    dur = format_duration(minutes)
    recent = list(history[-4:])
    context = (
        f"「{original_request}」というリクエストで設定した{dur}のタイマーが完了しました。"
        "「時間になりました」「お時間です」のような自然な一言だけ伝えてください。"
        "「何かお手伝いできることがあれば」のような締め文句は不要です。"
    )
    response_gen = chat_with_context("（タイマーが鳴りました）", recent, context, context_type="tool")

    queue = asyncio.Queue()
    container = []
    async with _speaking_lock:
        await asyncio.gather(
            _producer(queue, response_gen, container),
            _consumer(queue),
        )

    full_response = "".join(container)
    _dt = datetime.now()
    now = _dt.strftime("%H:%M")
    history_ts = _dt.strftime("%m/%d %H:%M")
    await notify_overlay(f"[{now}] 凛: {full_response}")

    # タイマー完了を履歴に記録することで、以降の会話でRinが「もう鳴った」と正しく認識できる
    history.append({"role": "user", "content": f"[{history_ts}] [システム: {dur}のタイマーが完了しました]"})
    history.append({"role": "assistant", "content": full_response})
    if len(history) > HISTORY_LIMIT:
        del history[:-HISTORY_LIMIT]
    save_history(history)


async def _handle_turn(user_input: str, history: list, loop):
    """1ターン分の処理：ツール判定 → 返答生成 → 音声再生 → 保存・インデックス"""
    _dt = datetime.now()
    now = _dt.strftime("%H:%M")           # vault・overlay表示用（形式変更するとRAGのパースが壊れる）
    history_ts = _dt.strftime("%m/%d %H:%M")  # history用（日付込みで時系列を明確にする）
    t0 = time.monotonic()
    logger.debug("[turn] 開始: input=%r history=%d件", user_input[:40], len(history))

    # Step1: ツール判定とパラメータ抽出を1推論で同時に行う
    route_result = await route_tool(user_input)
    tool_name, params = route_result if route_result else (None, {})
    logger.debug("[turn] route_tool完了: %.2fs → %s %s", time.monotonic() - t0, tool_name, params)

    if tool_name == "timer":
        minutes = params.get("minutes")
        if minutes is None:
            # 時間が解析できなかった → 凛に自然に聞き返してもらう
            logger.info("[tool] timer: 時間不明のためフォールバック")
            response_gen = chat(user_input, history)
        else:
            async def _fire(m=minutes, req=user_input, hist=history):
                await _fire_timer_notification(m, req, hist)
            timer_manager.schedule(minutes, _fire)
            logger.info("[tool] timer: %s をスケジュール", format_duration(minutes))
            context = f"{format_duration(minutes)}のタイマーを設定しました。タイマーが鳴った後のことは書かず、セットしたことだけを一言で伝えてください。"
            response_gen = chat_with_context(user_input, history, context, context_type="tool")

    elif tool_name:
        # コンテキストツール（weather など）
        logger.info("[tool] %s を呼び出します", tool_name)
        tool_func = TOOL_FUNCTIONS.get(tool_name)
        if tool_func:
            context = await tool_func()
            logger.debug("[turn] ツール実行完了: %.2fs", time.monotonic() - t0)
            response_gen = chat_with_context(user_input, history, context)
        else:
            logger.warning("[tool] %s はTOOL_FUNCTIONSに未登録です。通常チャットにフォールバック", tool_name)
            response_gen = chat(user_input, history)

    else:
        # ツール不要：RAGで関連する過去の会話を検索
        rag_hits = await search_memory(user_input)
        logger.debug("[turn] RAG完了: %.2fs → %d件", time.monotonic() - t0, len(rag_hits))
        if rag_hits:
            rag_context = format_memory_context(rag_hits)
            logger.info("[rag] %d件の関連ログを発見", len(rag_hits))
            response_gen = chat_with_context(user_input, history, rag_context, context_type="memory")
        else:
            response_gen = chat(user_input, history)

    logger.debug("[turn] Ollamaリクエスト送信: %.2fs", time.monotonic() - t0)
    queue = asyncio.Queue()
    container = []
    async with _speaking_lock:
        await asyncio.gather(
            _producer(queue, response_gen, container),
            _consumer(queue),
        )
    logger.debug("[turn] 生成完了: %.2fs tokens=%d", time.monotonic() - t0, len(container))
    full_response = "".join(container)

    # overlayに時刻付きで送信
    await notify_overlay(f"[{now}] 凛: {full_response}")

    # 会話履歴に追加（凛が時系列を把握できるよう、user側にだけ日付+時刻を付ける）
    # ※assistant側に付けるとLLMが自分の発言冒頭に時刻を書く模倣をしてしまう
    # ※日付込み（07/15 14:32）にすることで日またぎの会話でも時系列が明確になる
    history.append({"role": "user", "content": f"[{history_ts}] {user_input}"})
    history.append({"role": "assistant", "content": full_response})
    if len(history) > HISTORY_LIMIT:
        del history[:-HISTORY_LIMIT]

    # 会話履歴をJSONに保存（毎ターン）
    save_history(history)

    # ObsidianVaultにMarkdownで追記（RAG用の素材）
    log_path = append_conversation(now, user_input, full_response)

    # 保存した直後のファイルをそのままインデックス（新規チャンクのみ）
    if log_path:
        indexed_count = await loop.run_in_executor(None, index_markdown_file, log_path)
        logger.info("[rag] %d件をインデックスしました", indexed_count)


async def _conversation_loop(history: list):
    """入力キューを待ち受け、1ターンずつ処理する。'q'で終了。"""
    loop = asyncio.get_running_loop()
    while True:
        user_input = await input_queue.get()
        if user_input == "q":
            break

        logger.info("凛が考えています...")
        try:
            await _handle_turn(user_input, history, loop)
        except Exception as e:
            logger.error("[error] 予期せぬエラー:\n%s", traceback.format_exc())
            await notify_overlay(f"システム: エラーが発生しました → {type(e).__name__}: {e}")


import uvicorn
from core.api import app


async def start():
    # timeout_keep_alive: デフォルト(5秒)のKeep-Alive接続で、.NET系HTTPクライアント
    # （PowerShellのInvoke-WebRequest、UnityのUnityWebRequest等）からのリクエストが
    # 応答を受信できずハングする現象が確認されたため、Keep-Aliveを実質無効化する
    # ごく短い値に設定した。
    config = uvicorn.Config(app, host="127.0.0.1", port=API_PORT, log_level="warning", timeout_keep_alive=1)
    server = uvicorn.Server(config)

    async def run_main():
        await main()
        # main()のループ（qでbreak）が終わったら、uvicornも止めてプロセス自体を終了させる
        server.should_exit = True

    await asyncio.gather(
        server.serve(),
        run_main(),
    )


if __name__ == "__main__":
    asyncio.run(start())
