"""
会話に足す文脈（RAGの記憶検索）を作る共通処理。/input経路（main.py）と/completion経路（api.py）で共有する。

外部（ローカル外）のLLMバックエンドを使う場合は、日記(diary)を検索対象から外す。
日記は個人的な内容で、検索結果はプロンプトに入ってAPIへ送信されるため。
会話ログ(conversation)は含める。ローカルのOllamaでは従来どおり全て検索する。
"""
import asyncio

from core.config import LLM_BACKEND
from core.rag import query as rag_query

RAG_TOP_N = 3


def excluded_source_types() -> set:
    """外部バックエンドのときだけ日記を除外する"""
    return set() if LLM_BACKEND == "ollama" else {"diary"}


async def search_memory(text: str, n: int = RAG_TOP_N) -> list:
    """textに近い過去の記憶を検索する。埋め込み計算で止まらないよう別スレッドで実行する"""
    loop = asyncio.get_running_loop()
    excluded = excluded_source_types()
    return await loop.run_in_executor(None, lambda: rag_query(text, n, excluded))


def format_memory_context(hits: list) -> str:
    """検索結果を、プロンプトに入れる1つの文字列にする"""
    return "\n".join(h["text"] for h in hits)
